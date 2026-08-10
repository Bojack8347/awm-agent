"""Knowledge Updater Agent — the single writer of long-term client knowledge.

Validates, merges, and versions knowledge facts. No tools.
Input: current facts + candidate updates + evidence refs.
Output: committed facts, new snapshot version, pending confirmations.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from advisor.llm.adapter import (
    LLMClientFactory,
    LLMGenerateRequest,
    LLMMessage,
)
from advisor.llm.prompt_logging import append_prompt_log, serialize_messages
from advisor.llm.schemas import CASHFLOW_STATE_SCHEMA, KNOWLEDGE_UPDATE_SCHEMA
from advisor.instructions import task_prompt_dir
from advisor.runtime.task_definition import get_task_profile


class KnowledgeUpdaterAgent:
    """Single writer of long-term client knowledge facts.

    Validates candidate facts against current facts, detects conflicts,
    decides which updates to auto-commit vs require confirmation,
    and produces a merged fact set with a new snapshot version.

    Also owns cashflow state mapping: after building a snapshot, the agent
    automatically derives the cashflow_state payload that downstream agents
    (Diagnosis, FinancialPlanning) consume.  This consolidation ensures
    that every snapshot stored in the DB already contains cashflow_state —
    callers can no longer forget the enrichment step.

    Does NOT extend ToolLoopRunner — no tools, no ReAct loop.
    """

    def __init__(
        self,
        llm_api_key: str = "",
        model_chain: Optional[List[Tuple[str, str]]] = None,
        prompts_dir: Optional[Path] = None,
        llm_timeout_ms: Optional[int] = None,
        cashflow_prompts_dir: Optional[Path] = None,
        cashflow_timeout_ms: Optional[int] = None,
        cashflow_model_chain: Optional[List[Tuple[str, str]]] = None,
        temperature: Optional[float] = None,
        cashflow_temperature: Optional[float] = None,
        reasoning_effort: Optional[str] = None,
        cashflow_reasoning_effort: Optional[str] = None,
    ):
        profile = get_task_profile("knowledge_updater")
        cashflow_profile = get_task_profile("cashflow_mapper")
        self.llm_api_key = llm_api_key
        self.model_chain = model_chain if model_chain is not None else list(profile.chain)
        self.prompts_dir = prompts_dir or task_prompt_dir("knowledge_updater")
        self.llm_timeout_ms = llm_timeout_ms or profile.llm_timeout_ms
        self.temperature = temperature if temperature is not None else profile.temperature
        self.reasoning_effort = reasoning_effort or profile.reasoning_effort

        # Cashflow mapping config — separate prompt, timeout, and model chain
        self.cashflow_prompts_dir = cashflow_prompts_dir or (
            task_prompt_dir("cashflow_mapper")
        )
        self.cashflow_timeout_ms = cashflow_timeout_ms or cashflow_profile.llm_timeout_ms
        self.cashflow_model_chain = (
            cashflow_model_chain
            if cashflow_model_chain is not None
            else list(cashflow_profile.chain)
        )
        self.cashflow_temperature = (
            cashflow_temperature
            if cashflow_temperature is not None
            else cashflow_profile.temperature
        )
        self.cashflow_reasoning_effort = (
            cashflow_reasoning_effort or cashflow_profile.reasoning_effort
        )

    def update_knowledge(
        self,
        client_id: str,
        current_facts: List[Dict[str, Any]],
        candidate_updates: List[Dict[str, Any]],
        evidence_refs: Optional[List[Dict[str, Any]]] = None,
        source_event_id: Optional[str] = None,
        trigger_event: str = "consultation_complete",
    ) -> Dict[str, Any]:
        """Validate, merge, and version knowledge facts.

        The LLM produces committed_facts, pending_confirmations, and
        section_summaries (narrative summaries per knowledge section)
        in a single call.

        Args:
            client_id: The client identifier.
            current_facts: Existing committed facts from the knowledge store.
            candidate_updates: New candidate facts from extraction.
            evidence_refs: References to source events (session IDs, etc.)
            source_event_id: The ID of the event that triggered this update.
            trigger_event: Type of trigger (consultation_complete, fact_confirm, etc.)

        Returns:
            {
                "committed_facts": [...],       # facts to be committed
                "pending_confirmations": [...],  # material changes needing user consent
                "snapshot_data": {...},          # compact client model with section summaries
            }
        """
        system_prompt = self._read_prompt("system_prompt.txt")

        user_content = self._build_merge_prompt(
            current_facts=current_facts,
            candidate_updates=candidate_updates,
            evidence_refs=evidence_refs or [],
            source_event_id=source_event_id,
        )

        messages = [LLMMessage(role="user", content=user_content)]

        result, model_used, provider_used = self._generate_with_fallback(
            messages=messages,
            system_instruction=system_prompt,
            temperature=self.temperature,
            stage_name="knowledge_updater",
            response_schema=KNOWLEDGE_UPDATE_SCHEMA,
        )

        parsed = self._parse_json_response(result.text)

        committed_facts = parsed.get("committed_facts", [])
        pending_confirmations = parsed.get("pending_confirmations", [])
        llm_section_summaries = parsed.get("section_summaries", {})

        self._align_semantic_fact_identity(
            current_facts=current_facts,
            committed_facts=committed_facts,
            pending_confirmations=pending_confirmations,
        )

        # Attach source event IDs to committed facts
        for fact in committed_facts:
            if source_event_id:
                existing_refs = fact.get("source_event_ids", [])
                if source_event_id not in existing_refs:
                    existing_refs.append(source_event_id)
                fact["source_event_ids"] = existing_refs

        # Build compact snapshot from committed facts + unchanged current facts.
        # build_compact_snapshot auto-enriches with cashflow_state by default.
        all_facts = self._merge_fact_lists(current_facts, committed_facts)
        snapshot_data = self.build_compact_snapshot(
            all_facts, section_summaries=llm_section_summaries or None,
        )

        return {
            "committed_facts": committed_facts,
            "pending_confirmations": pending_confirmations,
            "snapshot_data": snapshot_data,
            "model_used": model_used,
            "provider_used": provider_used,
        }

    def build_compact_snapshot(
        self,
        facts: List[Dict[str, Any]],
        section_summaries: Optional[Dict[str, str]] = None,
        enrich_cashflow: bool = True,
    ) -> Dict[str, Any]:
        """Build a compact JSON client model from facts.

        This is a deterministic operation (no LLM) that groups facts
        by domain/category into the compact snapshot format that
        other agents consume.

        If section_summaries are provided (from the Knowledge Updater's
        LLM output, or carried forward from a previous snapshot), they
        are stored under each domain's "_section_summaries" key.

        If enrich_cashflow is True (default), the cashflow_state is
        automatically derived via LLM and attached to the snapshot.
        """
        snapshot: Dict[str, Any] = {}
        for fact in facts:
            if fact.get("status") == "dismissed" or fact.get("is_deleted"):
                continue
            domain = fact.get("domain", "unknown")
            category = fact.get("category", "unknown")
            if domain not in snapshot:
                snapshot[domain] = {}
            if category not in snapshot[domain]:
                snapshot[domain][category] = {}
            label = fact.get("label", "unknown")
            snapshot[domain][category][label] = {
                "value": fact.get("value"),
                "status": fact.get("status", "inferred"),
                "confidence": fact.get("confidence", 0.7),
                "fact_id": fact.get("id"),
            }

        # Attach section summaries if provided
        if section_summaries:
            # Wealth section summaries go under the wealth domain
            _WEALTH_SECTIONS = {
                "financial_position", "income", "expected_future_expenses",
                "tax", "preferences_and_constraints", "protection_and_resilience",
            }
            wealth_summaries = {
                k: v for k, v in section_summaries.items() if k in _WEALTH_SECTIONS
            }
            if wealth_summaries and "wealth" in snapshot:
                snapshot["wealth"]["_section_summaries"] = wealth_summaries

            # People and health get domain-level summaries
            if "people" in section_summaries and "people" in snapshot:
                snapshot["people"]["_section_summaries"] = {"people": section_summaries["people"]}
            if "health" in section_summaries and "health" in snapshot:
                snapshot["health"]["_section_summaries"] = {"health": section_summaries["health"]}

        if enrich_cashflow:
            snapshot["cashflow_state"] = self.map_cashflow_state(snapshot)

        return snapshot

    # ------------------------------------------------------------------
    # Cashflow state mapping
    # ------------------------------------------------------------------

    def map_cashflow_state(self, snapshot_data: Dict[str, Any]) -> Dict[str, Any]:
        """Map a knowledge snapshot to a cashflow-ready state payload.

        This is a separate LLM call with its own prompt and temperature,
        completely independent of the knowledge-merge call above.

        Args:
            snapshot_data: The compact knowledge snapshot with people/wealth/health domains.

        Returns:
            A cashflow_state dict matching the cashflow model's input schema,
            with every field populated (real values or defaults).
        """
        system_prompt = self._read_prompt_from(
            self.cashflow_prompts_dir, "system_prompt.txt"
        )

        # Strip _section_summaries from snapshot to reduce noise for the mapper
        clean_snapshot = self._strip_section_summaries(snapshot_data)

        user_content = (
            "## Knowledge Snapshot\n"
            f"```json\n{json.dumps(clean_snapshot, indent=2, ensure_ascii=True)}\n```\n\n"
            "## Task\n"
            "Map the above snapshot into a cashflow_state JSON object following "
            "the schema in your system prompt. Return ONLY the JSON object."
        )

        messages = [LLMMessage(role="user", content=user_content)]

        result, _, _ = self._generate_with_fallback(
            messages=messages,
            system_instruction=system_prompt,
            temperature=self.cashflow_temperature,
            stage_name="cashflow_mapper",
            model_chain_override=self.cashflow_model_chain,
            timeout_override=self.cashflow_timeout_ms,
            reasoning_effort=self.cashflow_reasoning_effort,
            response_schema=CASHFLOW_STATE_SCHEMA,
        )

        cashflow_state = self._parse_json_response(result.text)

        # Validate required top-level keys and fill defaults for any missing
        cashflow_state = self._ensure_cashflow_schema(cashflow_state)

        return cashflow_state

    def _strip_section_summaries(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """Remove _section_summaries from snapshot to reduce prompt noise."""
        cleaned: Dict[str, Any] = {}
        for domain, domain_data in snapshot.items():
            if domain == "cashflow_state":
                continue  # Don't feed existing cashflow_state back
            if not isinstance(domain_data, dict):
                cleaned[domain] = domain_data
                continue
            cleaned[domain] = {
                k: v for k, v in domain_data.items() if k != "_section_summaries"
            }
        return cleaned

    def _align_semantic_fact_identity(
        self,
        current_facts: List[Dict[str, Any]],
        committed_facts: List[Dict[str, Any]],
        pending_confirmations: List[Dict[str, Any]],
    ) -> None:
        """Backfill existing fact identity for semantically equivalent updates.

        The LLM remains responsible for deciding whether a fact is new, updated,
        or confirmation-worthy. This post-processing step only stabilizes fact
        identity when the underlying fact is the same but the phrasing changed.
        """
        semantic_index = self._build_semantic_index(current_facts)

        for fact in committed_facts:
            if fact.get("id"):
                continue
            existing = self._find_semantic_match(fact, semantic_index)
            if not existing:
                continue
            fact["id"] = existing.get("id")
            fact["label"] = existing.get("label", fact.get("label"))
            if existing.get("status") and fact.get("status") == "inferred":
                fact["status"] = existing["status"]

        for pending in pending_confirmations:
            if pending.get("fact_id"):
                continue
            existing = self._find_semantic_match(pending, semantic_index)
            if not existing:
                continue
            pending["fact_id"] = existing.get("id")
            pending["previous_value"] = existing.get("value")
            pending["label"] = existing.get("label", pending.get("label"))

    def _build_semantic_index(
        self,
        facts: List[Dict[str, Any]],
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Index active facts by semantic key for identity backfilling."""
        index: Dict[str, List[Dict[str, Any]]] = {}
        for fact in facts:
            if fact.get("status") == "dismissed" or fact.get("is_deleted"):
                continue
            key = self._semantic_fact_key(fact)
            if not key:
                continue
            index.setdefault(key, []).append(fact)
        return index

    def _find_semantic_match(
        self,
        fact: Dict[str, Any],
        semantic_index: Dict[str, List[Dict[str, Any]]],
    ) -> Optional[Dict[str, Any]]:
        """Return a unique semantic match for the given fact, if one exists."""
        key = self._semantic_fact_key(fact)
        if not key:
            return None
        matches = semantic_index.get(key, [])
        if len(matches) == 1:
            return matches[0]
        return None

    def _semantic_fact_key(self, fact: Dict[str, Any]) -> Optional[str]:
        """Build a category-aware semantic identity key for a fact label."""
        domain = str(fact.get("domain") or "").strip().lower()
        category = str(fact.get("category") or "").strip().lower()
        label = str(fact.get("label") or "").strip().lower()
        if not domain or not category or not label:
            return None

        label = label.replace("emergency fund", "emergency reserve")
        label = label.replace("per month", "monthly")
        label = label.replace("per year", "annual")

        synonyms = {
            "salary": "income",
            "earnings": "income",
            "wages": "income",
            "compensation": "income",
            "spousal": "spouse",
            "wife": "spouse",
            "husband": "spouse",
            "fiancee": "spouse",
            "fiance": "spouse",
        }
        generic_tokens = {
            "amount",
            "level",
            "estimated",
            "current",
            "total",
        }
        category_stopwords = {
            "accounts": {"account", "accounts", "balance", "balances", "holding", "holdings"},
            "recurring_expenses": {"monthly", "annual", "expense", "expenses", "cost", "costs", "payment", "payments"},
            "non_recurring_expenses": {"expense", "expenses", "cost", "costs"},
            "income": {"monthly", "annual"},
            "preferences": {"preference", "preferences", "target", "goal", "desired"},
            "liabilities": {"balance", "balances"},
        }

        tokens: List[str] = []
        for raw_token in re.findall(r"[a-z0-9]+", label):
            token = synonyms.get(raw_token, raw_token)
            if token in generic_tokens:
                continue
            if token in category_stopwords.get(category, set()):
                continue
            tokens.append(token)

        if not tokens:
            return None
        return f"{domain}:{category}:{' '.join(sorted(tokens))}"

    def _ensure_cashflow_schema(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Ensure all required top-level sections exist with defaults."""
        # --- client_profile ---
        if "client_profile" not in state:
            state["client_profile"] = {}
        cp = state["client_profile"]
        cp.setdefault("age", 35)
        cp.setdefault("retirement_age", 65)
        cp.setdefault("life_expectancy", 90)
        cp.setdefault("dependents_detail", [])

        # --- income ---
        if "income" not in state:
            state["income"] = {}
        inc = state["income"]
        for k, d in [("salary", 0.0), ("bonus", 0.0), ("spouse_income", 0.0),
                      ("yearly_increase", 3.0), ("net_monthly_take_home_min", 0.0),
                      ("net_monthly_take_home_max", 0.0)]:
            inc.setdefault(k, d)

        # --- expenses ---
        if "expenses" not in state:
            state["expenses"] = {}
        exp = state["expenses"]
        exp.setdefault("base_spending", 0.0)
        exp.setdefault("yearly_increase", 3.0)
        if "housing" not in exp:
            exp["housing"] = {}
        for k, d in [("mortgage_balance", 0.0), ("monthly_principal_interest", 0.0),
                      ("monthly_property_tax_and_homeowners_insurance", 0.0)]:
            exp["housing"].setdefault(k, d)

        # --- accounts (array-based pools) ---
        if "accounts" not in state:
            state["accounts"] = {}
        accts = state["accounts"]
        for pool in ("bank", "brokerage", "retirement", "education"):
            if pool not in accts or not isinstance(accts[pool], list):
                accts[pool] = []

        # --- liabilities ---
        if "liabilities" not in state:
            state["liabilities"] = {}
        state["liabilities"].setdefault("mortgage_balance", 0.0)

        # --- preferences ---
        if "preferences" not in state:
            state["preferences"] = {}
        state["preferences"].setdefault("maintain_emergency_reserve_months", 6.0)

        # --- goals & one_off_expenses ---
        state.setdefault("goals", [])
        state.setdefault("one_off_expenses", [])

        return state

    def _read_prompt_from(self, prompts_dir: Path, filename: str) -> str:
        """Load a prompt file from an arbitrary prompts directory."""
        path = prompts_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Prompt file not found: {path}")
        return path.read_text(encoding="utf-8")

    def regenerate_section_summary(
        self,
        section_key: str,
        section_title: str,
        section_facts: List[Dict[str, Any]],
    ) -> Optional[str]:
        """Regenerate a single section's narrative summary via a lightweight LLM call.

        Used when a fact is confirmed, corrected, or dismissed — only the
        affected section needs a fresh summary, not the full merge pipeline.

        Returns the new summary string, or None if generation fails.
        """
        if not section_facts:
            return None

        compact_facts = [
            {
                "label": f.get("label"),
                "value": f.get("value"),
                "category": f.get("category"),
                "status": f.get("status"),
            }
            for f in section_facts
            if f.get("status") != "dismissed" and not f.get("is_deleted")
        ]

        if not compact_facts:
            return None

        user_content = (
            f"## Section: {section_title}\n\n"
            f"Facts:\n```json\n{json.dumps(compact_facts, indent=2)}\n```\n\n"
            "## Task\n"
            "Write a single narrative paragraph (2-3 sentences) summarizing this section. "
            "Embed 2-4 key numbers from the facts. Write natural prose, not a list. "
            "Never mention risk profile as a general household attribute. "
            "Return ONLY the summary text, no JSON, no quotes, no explanation."
        )

        try:
            result, _, _ = self._generate_with_fallback(
                messages=[LLMMessage(role="user", content=user_content)],
                system_instruction="You produce concise narrative summaries of client financial facts for a wealth advisory app.",
                temperature=self.temperature,
                stage_name="knowledge_updater_summary",
            )
            summary = result.text.strip().strip('"').strip("'")
            return summary if summary else None
        except Exception as exc:
            print(f"[KnowledgeUpdaterAgent] Failed to regenerate summary for {section_key}: {exc}", flush=True)
            return None

    def _build_merge_prompt(
        self,
        current_facts: List[Dict[str, Any]],
        candidate_updates: List[Dict[str, Any]],
        evidence_refs: List[Dict[str, Any]],
        source_event_id: Optional[str] = None,
    ) -> str:
        """Build the user prompt for the merge/validation task."""
        parts = []

        if current_facts:
            # Compact representation of existing facts
            compact_current = [
                {
                    "id": f.get("id"),
                    "domain": f.get("domain"),
                    "category": f.get("category"),
                    "label": f.get("label"),
                    "value": f.get("value"),
                    "status": f.get("status"),
                    "confidence": f.get("confidence"),
                }
                for f in current_facts
                if f.get("status") != "dismissed" and not f.get("is_deleted")
            ]
            parts.append(f"## Current Committed Facts\n```json\n{json.dumps(compact_current, indent=2)}\n```")
        else:
            parts.append("## Current Committed Facts\nNo existing facts. This is a fresh client profile.")

        parts.append(f"## Candidate Updates\n```json\n{json.dumps(candidate_updates, indent=2)}\n```")

        if evidence_refs:
            parts.append(f"## Evidence References\n```json\n{json.dumps(evidence_refs, indent=2)}\n```")

        parts.append(
            "## Task\n"
            "Merge the candidate updates into the current facts following the rules in your system prompt. "
            "Return a JSON object with `committed_facts`, `pending_confirmations`, and `section_summaries`."
        )

        return "\n\n".join(parts)

    def _merge_fact_lists(
        self,
        current_facts: List[Dict[str, Any]],
        committed_updates: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Merge committed updates into current facts for snapshot building.

        Updates with matching IDs replace existing facts.
        New facts (no matching ID) are appended.
        """
        facts_by_id = {f["id"]: f for f in current_facts if f.get("id")}
        semantic_index = self._build_semantic_index(current_facts)
        for update in committed_updates:
            uid = update.get("id")
            if uid:
                facts_by_id[uid] = update
            else:
                existing = self._find_semantic_match(update, semantic_index)
                if existing and existing.get("id"):
                    fid = existing["id"]
                    facts_by_id[fid] = {**existing, **update, "id": fid, "label": existing.get("label", update.get("label"))}
                else:
                    key = f"{update.get('domain')}:{update.get('category')}:{update.get('label')}"
                    facts_by_id[key] = update
        return list(facts_by_id.values())

    def _generate_with_fallback(
        self,
        messages: List[LLMMessage],
        system_instruction: str,
        temperature: Optional[float] = None,
        stage_name: str = "knowledge_updater",
        model_chain_override: Optional[List[Tuple[str, str]]] = None,
        timeout_override: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        response_schema: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Any, str, str]:
        """Call LLM with provider fallback chain."""
        chain = model_chain_override or self.model_chain
        timeout = timeout_override or self.llm_timeout_ms
        last_error = None
        for provider_name, model_name in chain:
            started = time.perf_counter()
            try:
                adapter = LLMClientFactory.create(
                    provider=provider_name,
                    api_key=self.llm_api_key,
                    timeout_ms=timeout,
                )
                request = LLMGenerateRequest(
                    messages=messages,
                    system_instruction=system_instruction,
                    temperature=temperature,
                    response_schema=response_schema,
                    reasoning_effort=reasoning_effort or self.reasoning_effort,
                )
                response = adapter.generate(request=request, model=model_name)
                append_prompt_log(
                    stage=stage_name,
                    provider=provider_name,
                    model=model_name,
                    system_instruction=system_instruction,
                    temperature=temperature,
                    contents=serialize_messages(messages),
                    use_tools=False,
                    elapsed_seconds=time.perf_counter() - started,
                    success=True,
                )
                return response, model_name, provider_name
            except Exception as exc:
                append_prompt_log(
                    stage=stage_name,
                    provider=provider_name,
                    model=model_name,
                    system_instruction=system_instruction,
                    temperature=temperature,
                    contents=serialize_messages(messages),
                    use_tools=False,
                    elapsed_seconds=time.perf_counter() - started,
                    success=False,
                    error=str(exc),
                )
                last_error = exc
                message = str(exc)
                if "429" in message or "RESOURCE_EXHAUSTED" in message:
                    time.sleep(4)
                continue
        raise RuntimeError(
            f"KnowledgeUpdaterAgent LLM generation failed after all providers: {last_error}"
        )

    def _read_prompt(self, filename: str) -> str:
        """Load a prompt file from the prompts directory."""
        path = self.prompts_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Prompt file not found: {path}")
        return path.read_text(encoding="utf-8")

    def _parse_json_response(self, raw_text: str) -> Dict[str, Any]:
        """Parse JSON from LLM response, with fallback extraction."""
        text = raw_text.strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(text[start : end + 1])
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        print(f"[KnowledgeUpdaterAgent] Failed to parse JSON from response", flush=True)
        return {"committed_facts": [], "pending_confirmations": []}
