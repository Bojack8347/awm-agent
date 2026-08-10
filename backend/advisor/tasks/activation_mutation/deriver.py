"""AI Fact Deriver — uses LLM to derive household model mutations from policy activation."""

from __future__ import annotations

import json
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
from advisor.llm.schemas import ACTIVATION_MUTATIONS_SCHEMA
from advisor.instructions import task_prompt_dir
from advisor.runtime.task_definition import get_task_profile


# ---------------------------------------------------------------------------
# Journey-specific guidance — add a string here to improve LLM output for
# a new journey type.  No guidance registered?  The LLM still works.
# ---------------------------------------------------------------------------

JOURNEY_GUIDANCE: Dict[str, str] = {
    "investment": (
        "This is an investment policy activation. Typical mutations:\n"
        "1. DEBIT the funding source account — find the matching account fact by ID "
        "and reduce its balance by the total_transfer amount. The funding source could "
        "be a bank account, brokerage account, or any other account.\n"
        "2. CREATE a new investment account fact under wealth/accounts with the "
        "deployed amount, policy name, securities count, and status 'active'.\n"
        "3. CREATE an asset_allocation fact listing each security with its "
        "allocation_pct, allocation_amount, asset_class, and management_style.\n"
        "4. If multiple funding sources are involved, debit each one separately.\n"
        "5. For in-kind transfers (securities moving directly), remove the holding "
        "from the source and add it to the destination — no cash mutation needed."
    ),
    "insurance": (
        "This is an insurance policy activation. Typical mutations:\n"
        "1. CREATE a coverage fact under the appropriate domain (wealth/insurance "
        "or healthcare/coverage) with policy details.\n"
        "2. CREATE a recurring_expenses fact for the premium payments.\n"
        "3. DEBIT the funding source for any upfront payment or first premium.\n"
        "4. If replacing existing coverage, note the replacement in evidence."
    ),
}


class ActivationFactDeriver:
    """Derives household model mutations from a policy activation using LLM."""

    def __init__(
        self,
        llm_api_key: str = "",
        model_chain: Optional[List[Tuple[str, str]]] = None,
        prompts_dir: Optional[Path] = None,
        llm_timeout_ms: Optional[int] = None,
        temperature: Optional[float] = None,
        reasoning_effort: Optional[str] = None,
    ):
        profile = get_task_profile("activation_mutation")
        self.llm_api_key = llm_api_key
        self.model_chain = model_chain if model_chain is not None else list(profile.chain)
        self.prompts_dir = prompts_dir or task_prompt_dir("activation_mutation")
        self.llm_timeout_ms = llm_timeout_ms or profile.llm_timeout_ms
        self.temperature = temperature if temperature is not None else profile.temperature
        self.reasoning_effort = reasoning_effort or profile.reasoning_effort

    def derive(
        self,
        journey_id: str,
        journey_type: str,
        solution: Dict[str, Any],
        current_facts: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Derive fact mutations from an activated policy.

        Returns:
            {
                "mutations": [...],       # list of fact mutation dicts
                "model_used": str,
                "provider_used": str,
            }
        """
        if not solution:
            return {"mutations": [], "model_used": "", "provider_used": ""}

        system_prompt = self._read_prompt("system_prompt.txt")
        user_content = self._build_prompt(
            journey_id, journey_type, solution, current_facts,
        )

        messages = [LLMMessage(role="user", content=user_content)]

        result, model_used, provider_used = self._generate_with_fallback(
            messages=messages,
            system_instruction=system_prompt,
            temperature=self.temperature,
            response_schema=ACTIVATION_MUTATIONS_SCHEMA,
        )

        parsed = self._parse_json_response(result.text)
        mutations = parsed.get("mutations", [])

        # Stamp evidence on all mutations
        for m in mutations:
            if not m.get("evidence"):
                m["evidence"] = f"Policy activated from journey {journey_id}"

        return {
            "mutations": mutations,
            "model_used": model_used,
            "provider_used": provider_used,
        }

    def _build_prompt(
        self,
        journey_id: str,
        journey_type: str,
        solution: Dict[str, Any],
        current_facts: List[Dict[str, Any]],
    ) -> str:
        """Build the user prompt for the derivation task."""
        parts = []

        parts.append(
            f"## Activation Context\n"
            f"- Journey ID: {journey_id}\n"
            f"- Journey type: {journey_type}\n"
        )

        # Journey-specific guidance
        guidance = JOURNEY_GUIDANCE.get(journey_type)
        if guidance:
            parts.append(f"## Journey-Specific Guidance\n{guidance}")

        # Solution output
        parts.append(
            f"## Solution Output (the policy being activated)\n"
            f"```json\n{json.dumps(solution, indent=2, default=str)}\n```"
        )

        # Current facts — only send relevant ones to keep prompt focused
        relevant_facts = self._filter_relevant_facts(current_facts, journey_type)
        if relevant_facts:
            compact = [
                {
                    "id": f.get("id"),
                    "domain": f.get("domain"),
                    "category": f.get("category"),
                    "label": f.get("label"),
                    "value": f.get("value"),
                    "status": f.get("status"),
                    "confidence": f.get("confidence"),
                }
                for f in relevant_facts
                if f.get("status") != "dismissed" and not f.get("is_deleted")
            ]
            parts.append(
                f"## Current Household Facts\n"
                f"```json\n{json.dumps(compact, indent=2, default=str)}\n```"
            )
        else:
            parts.append("## Current Household Facts\nNo existing facts.")

        parts.append(
            "## Task\n"
            "Determine what concrete mutations to the client's household model "
            "this policy activation implies. Return a JSON object with a "
            "`mutations` array following the rules in your system prompt."
        )

        return "\n\n".join(parts)

    def _filter_relevant_facts(
        self, facts: List[Dict[str, Any]], journey_type: str,
    ) -> List[Dict[str, Any]]:
        """Filter facts to only those relevant to this journey type."""
        # For now, wealth-domain facts are relevant to all journey types.
        # Healthcare facts are relevant for insurance journeys.
        relevant_domains = {"wealth"}
        if journey_type == "insurance":
            relevant_domains.add("healthcare")
        return [f for f in facts if f.get("domain") in relevant_domains]

    def _generate_with_fallback(
        self,
        messages: List[LLMMessage],
        system_instruction: str,
        temperature: Optional[float] = None,
        response_schema: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Any, str, str]:
        """Call LLM with provider fallback chain."""
        last_error = None
        for provider_name, model_name in self.model_chain:
            started = time.perf_counter()
            try:
                adapter = LLMClientFactory.create(
                    provider=provider_name,
                    api_key=self.llm_api_key,
                    timeout_ms=self.llm_timeout_ms,
                )
                request = LLMGenerateRequest(
                    messages=messages,
                    system_instruction=system_instruction,
                    temperature=temperature,
                    response_schema=response_schema,
                    reasoning_effort=self.reasoning_effort,
                )
                response = adapter.generate(request=request, model=model_name)
                append_prompt_log(
                    stage="activation_mutation",
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
                    stage="activation_mutation",
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
            f"ActivationFactDeriver LLM generation failed after all providers: {last_error}"
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
        print(
            f"[ActivationFactDeriver] Failed to parse JSON from response",
            flush=True,
        )
        return {"mutations": []}
