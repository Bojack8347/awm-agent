"""CompanionService — companion context assembly and result sanitization.

Extracted from app.py. Contains no Flask dependencies.

Handles:
- Context assembly (parallelised DB fetches)
- Same-turn auto-confirmation of companion-created pending confirmations
- Result sanitization (action-type contract enforcement)
- Fact-change grounding against the current user turn

Injected callables:
  commit_confirmed_pending_fn   — runs CONFIRMATION_PIPELINE for one confirmation
"""
from __future__ import annotations

import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from advisor.runtime.run_context import AgentRunContext  # noqa: E402

try:
    from api.persistence import (
        get_companion_messages as db_get_companion_messages,
        get_active_journey_runs as db_get_active_journey_runs,
        get_advisory_events as db_get_advisory_events,
        get_journey_state as db_get_journey_state,
        get_recent_journey_events as db_get_recent_journey_events,
        get_knowledge_facts as db_get_knowledge_facts,
        resolve_pending_confirmation as db_resolve_pending_confirmation,
        get_pending_confirmation as db_get_pending_confirmation,
    )
    from api.services.client_state_view import build_client_state_view
except ImportError:
    db_get_companion_messages = lambda *a, **kw: []  # noqa: E731
    db_get_active_journey_runs = lambda *a, **kw: []  # noqa: E731
    db_get_advisory_events = lambda *a, **kw: []  # noqa: E731
    db_get_journey_state = lambda *a, **kw: None  # noqa: E731
    db_get_recent_journey_events = lambda *a, **kw: []  # noqa: E731
    db_get_knowledge_facts = lambda *a, **kw: []  # noqa: E731
    db_resolve_pending_confirmation = lambda *a, **kw: None  # noqa: E731
    db_get_pending_confirmation = lambda *a, **kw: None  # noqa: E731
    build_client_state_view = lambda *a, **kw: None  # noqa: E731


# ---------------------------------------------------------------------------
# Pure logic helpers — no class, no injection needed
# ---------------------------------------------------------------------------

def _normalize_turn_text(text: str) -> str:
    """Normalize free text for lightweight grounding checks."""
    normalized = re.sub(r"[^a-z0-9$ ]+", " ", str(text or "").lower())
    return " ".join(normalized.split())


def _fact_change_signal_phrases(change: Dict[str, Any]) -> Set[str]:
    """Build a small set of lexical signals for a proposed fact change."""
    label = str(change.get("label") or "").strip().lower()
    category = str(change.get("category") or "").strip().lower()
    domain = str(change.get("domain") or "").strip().lower()

    signals: Set[str] = set()
    alias_map = {
        "annual bonus": {"bonus", "retention bonus", "cash bonus"},
        "bonus": {"bonus", "retention bonus", "cash bonus"},
        "client annual bonus": {"bonus", "retention bonus", "cash bonus"},
        "annual salary": {"salary", "pay", "compensation", "income", "raise"},
        "client base salary": {"salary", "pay", "compensation", "income", "raise"},
        "mortgage balance": {"mortgage", "home loan"},
        "mortgage interest rate": {"mortgage rate", "interest rate", "mortgage"},
        "car loan balance": {"car loan", "auto loan"},
        "number of dependents": {"dependent", "dependents", "baby", "child", "children"},
    }
    for alias_label, aliases in alias_map.items():
        if alias_label in label:
            signals.update(aliases)

    generic_stopwords = {
        "annual", "monthly", "current", "client", "spouse", "household",
        "balance", "amount", "value", "expected", "future",
    }
    tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", label)
        if token not in generic_stopwords
    ]
    signals.update(tokens)

    if domain == "wealth" and category == "income":
        signals.update({"income"})
    if domain == "wealth" and category == "liabilities":
        signals.update({"debt", "loan"})

    return {phrase for phrase in signals if phrase}


def _is_fact_change_grounded(change: Dict[str, Any], user_message: str) -> bool:
    """Return True when the proposed fact change is supported by the current turn."""
    normalized_message = _normalize_turn_text(user_message)
    if not normalized_message:
        return False
    for phrase in _fact_change_signal_phrases(change):
        if phrase in normalized_message:
            return True
    return False


def _build_fact_update_acknowledgement(changes: List[Dict[str, Any]]) -> str:
    """Create a concise acknowledgement for grounded fact updates."""
    fragments: List[str] = []
    for change in changes[:2]:
        label = str(change.get("label") or "that fact").strip().lower()
        value = change.get("value")
        if "bonus" in label and isinstance(value, (int, float)):
            fragments.append(f"your annual bonus to ${value:,.0f}")
        elif "mortgage" in label and isinstance(value, (int, float)):
            fragments.append(f"your mortgage balance to ${value:,.0f}")
        elif isinstance(value, (int, float)):
            fragments.append(f"{label} to ${value:,.0f}")
        else:
            fragments.append(f"{label}")

    if not fragments:
        return "Got it, I'll update that."
    if len(fragments) == 1:
        return f"Got it, I'll update {fragments[0]}."
    return f"Got it, I'll update {fragments[0]} and {fragments[1]}."


def sanitize_result(result: Dict[str, Any], user_message: str) -> Dict[str, Any]:
    """Enforce the companion action contract before stateful side effects run.

    The orchestrator may occasionally emit `confirm_fact` conversationally without
    providing a structured fact payload. In that case we must not imply that a write
    occurred, because downstream truth persistence only works from structured deltas.
    """
    action_type = str(result.get("action_type") or "chat")
    proposed_changes = result.get("proposed_fact_changes")
    pending_ids = result.get("pending_confirmation_ids")
    has_proposed_changes = isinstance(proposed_changes, list) and len(proposed_changes) > 0
    has_pending_ids = isinstance(pending_ids, list) and len(pending_ids) > 0

    if action_type == "confirm_fact" and not has_proposed_changes and not has_pending_ids:
        print(
            "[companion] Invalid confirm_fact without structured payload; "
            "downgrading to chat clarification.",
            flush=True,
        )
        result["action_type"] = "chat"
        result["assistant_message"] = (
            "I want to make sure I record that correctly. "
            "Can you confirm the exact updated amount or detail?"
        )
        result["proposed_fact_changes"] = None
        result["pending_confirmation_ids"] = None
    return result


def ground_result_to_current_turn(
    result: Dict[str, Any],
    user_message: str,
) -> Dict[str, Any]:
    """Keep only fact changes explicitly grounded in the latest user turn."""
    if result.get("action_type") != "confirm_fact":
        return result

    proposed_changes = result.get("proposed_fact_changes")
    if not isinstance(proposed_changes, list) or not proposed_changes:
        return result

    grounded_changes = [
        change for change in proposed_changes
        if isinstance(change, dict) and _is_fact_change_grounded(change, user_message)
    ]

    if len(grounded_changes) == len(proposed_changes):
        return result

    if grounded_changes:
        print(
            "[companion] Filtered ungrounded fact changes from confirm_fact payload.",
            flush=True,
        )
        result["proposed_fact_changes"] = grounded_changes
        result["assistant_message"] = _build_fact_update_acknowledgement(grounded_changes)
        return result

    print(
        "[companion] No proposed fact changes were grounded in the current user turn; "
        "downgrading to chat clarification.",
        flush=True,
    )
    result["action_type"] = "chat"
    result["assistant_message"] = (
        "I want to make sure I only update what you meant in this message. "
        "Can you restate the specific fact you'd like me to record?"
    )
    result["proposed_fact_changes"] = None
    result["pending_confirmation_ids"] = None
    return result


# ---------------------------------------------------------------------------
# CompanionService — stateful operations that need injected dependencies
# ---------------------------------------------------------------------------

class CompanionService:
    """Companion context assembly and same-turn confirmation logic.

    Usage in app.py::

        _COMPANION_SERVICE: Optional[CompanionService] = None

        def get_companion_service() -> CompanionService:
            global _COMPANION_SERVICE
            if _COMPANION_SERVICE is None:
                _COMPANION_SERVICE = CompanionService(
                    _commit_confirmed_pending,
                )
            return _COMPANION_SERVICE
    """

    def __init__(
        self,
        commit_confirmed_pending_fn: Callable,
    ) -> None:
        self._commit_confirmed = commit_confirmed_pending_fn

    def build_context(
        self,
        *,
        client_id: str,
        session_id: str,
        user_message: str,
        knowledge_snapshot: Optional[Dict[str, Any]],
        diagnosis_snapshot: Optional[Dict[str, Any]],
        pending_confirmations: List[Dict[str, Any]],
        cached_recent_turns: Optional[List[Dict[str, Any]]] = None,
        ctx: Optional[AgentRunContext] = None,
    ) -> Dict[str, Any]:
        """Assemble companion context via parallelised I/O-bound fetches."""

        def _fetch_recent_turns() -> List[Dict[str, Any]]:
            if cached_recent_turns is not None:
                return cached_recent_turns
            return db_get_companion_messages(session_id, limit=5, latest_first_window=True)

        def _fetch_active_journeys() -> List[Dict[str, Any]]:
            rows = db_get_active_journey_runs(client_id)
            enriched: List[Dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                merged = dict(row)
                journey_id = str(row.get("id") or "").strip()
                if journey_id:
                    state_row = db_get_journey_state(journey_id)
                    if isinstance(state_row, dict):
                        merged.update(state_row)
                enriched.append(merged)
            return enriched

        def _fetch_dismissed_facts() -> List[Dict[str, Any]]:
            return [
                {"domain": f["domain"], "category": f["category"], "label": f["label"]}
                for f in db_get_knowledge_facts(client_id, status="dismissed")
            ]

        def _fetch_recent_journey_events() -> List[Dict[str, Any]]:
            return db_get_recent_journey_events(client_id, limit=12)

        def _fetch_advisory_events() -> List[Dict[str, Any]]:
            return db_get_advisory_events(
                client_id=client_id,
                status="open",
                limit=12,
            )

        def _fetch_advisor_state() -> Optional[Dict[str, Any]]:
            enabled = (
                os.getenv("AWM_CLIENT_STATE_CONTEXT")
                or os.getenv("CLIENT_STATE_CONTEXT_ENABLED")
                or "off"
            ).strip().lower()
            if enabled in {"0", "false", "no", "off"}:
                return None
            state = build_client_state_view(client_id)
            return state if isinstance(state, dict) else None

        with ThreadPoolExecutor(max_workers=6) as pool:
            f_turns = pool.submit(_fetch_recent_turns)
            f_journeys = pool.submit(_fetch_active_journeys)
            f_dismissed = pool.submit(_fetch_dismissed_facts)
            f_journey_events = pool.submit(_fetch_recent_journey_events)
            f_advisory_events = pool.submit(_fetch_advisory_events)
            f_advisor_state = pool.submit(_fetch_advisor_state)

            recent_turns = f_turns.result()
            active_journeys = f_journeys.result()
            dismissed_facts = f_dismissed.result()
            recent_journey_events = f_journey_events.result()
            advisory_events = f_advisory_events.result()
            advisor_state = f_advisor_state.result()

        context = {
            "knowledge_summary": (
                knowledge_snapshot.get("snapshot_data", {})
                if knowledge_snapshot else {}
            ),
            "diagnosis_summary": (
                diagnosis_snapshot.get("diagnosis_data", {})
                if diagnosis_snapshot else {}
            ),
            "active_journeys": active_journeys,
            "unresolved_confirmations": pending_confirmations,
            "recent_turns": recent_turns,
            "recent_journey_events": recent_journey_events,
            "advisory_events": advisory_events,
            "dismissed_facts": dismissed_facts,
        }
        if advisor_state:
            context["advisor_state"] = advisor_state
        return context

    def apply_same_turn_confirmations(
        self,
        client_id: str,
        pending_confirmations: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Immediately resolve companion-created confirmations from explicit user updates.

        Companion `confirm_fact` messages already represent the user's direct statement
        of the new value. When the Knowledge Updater conservatively emits a pending
        confirmation for the same turn, we can safely confirm it immediately so the
        persisted facts match the assistant's acknowledgment.
        """
        committed_facts: List[Dict[str, Any]] = []
        for pending in pending_confirmations:
            confirmation_id = pending.get("id")
            if not confirmation_id:
                continue
            db_resolve_pending_confirmation(confirmation_id, "confirmed")
            confirmation = db_get_pending_confirmation(confirmation_id)
            if not confirmation:
                continue
            confirm_ctx = self._commit_confirmed(
                client_id=client_id,
                confirmation_id=confirmation_id,
                confirmation=confirmation,
                caller="companion_message_auto_confirm",
                refresh_diagnosis=False,
                return_context=True,
            )
            committed_fact = confirm_ctx.get("committed_fact")
            if committed_fact:
                committed_facts.append(committed_fact)
        return committed_facts
