from __future__ import annotations

from typing import Any, Dict, List, Optional


_WRITEBACK_CLAIM_BLOCKED_RESPONSE = (
    "I want to verify that this was actually recorded before I say it’s saved. "
    "Give me a moment to double-check, then we’ll continue."
)


_FACT_DUMP_CLAIM_BLOCKED_RESPONSE = (
    "I need to record those details carefully before I treat them as saved. "
    "Let me capture them correctly."
)


_FACT_DUMP_CLAIM_BLOCKED_RESPONSE = (
    "I need to record those details into your Client File with the proper fact tools "
    "before I treat them as saved. Let me capture them correctly."
)


_QUANT_CLAIM_BLOCKED_RESPONSE = (
    "I cannot conclude from this result because the required quantitative evidence is "
    "invalid or incomplete. I have not inferred a planning conclusion."
)


_MISSING_QUANT_EVIDENCE_RESPONSE = (
    "I don’t have a completed analysis to rely on yet. I can run it once we have the "
    "remaining details, and I’ll ask only for what is needed."
)


def _calculation_capability_gap_response(
    tool_results: List[Dict[str, Any]],
) -> Optional[str]:
    """Return the server-authored response for a terminal calculation gap."""

    for result in reversed(tool_results):
        if (
            not isinstance(result, dict)
            or result.get("tool") != "report_calculation_capability_gap"
            or result.get("ok") is not True
        ):
            continue
        full_result = (
            result.get("full_result")
            if isinstance(result.get("full_result"), dict)
            else {}
        )
        if result.get("terminal") is not True and full_result.get("terminal") is not True:
            continue
        client_message = str(
            result.get("client_message")
            or full_result.get("client_message")
            or ""
        ).strip()
        if client_message:
            return client_message
    return None


def _writeback_claim_blocked_response(user_message: str) -> str:
    text = " ".join(str(user_message or "").lower().split())
    if "assessment" in text and any(
        marker in text
        for marker in ("pending", "unsigned", "not signed", "without sign")
    ):
        return (
            "We still need your sign-off on the investment assessment before I can prepare "
            "an allocation or say that anything was saved."
        )
    if any(marker in text for marker in ("proposal", "policy")) and any(
        marker in text for marker in ("saved", "active", "activated", "recorded")
    ):
        return (
            "Running the allocation analysis does not by itself save or activate a proposal. "
            "I don’t have a successful save yet, so nothing has been locked in."
        )
    return _WRITEBACK_CLAIM_BLOCKED_RESPONSE


def _structured_clarification(
    tool_results: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Return the latest validated model-selected clarification pause."""

    for result in reversed(tool_results):
        if (
            isinstance(result, dict)
            and result.get("tool") == "request_clarification"
            and result.get("ok") is True
            and isinstance(result.get("pending_intent"), dict)
        ):
            pending = result["pending_intent"]
            if (
                pending.get("schema_version") == "awm.pending_clarification.v1"
                and str(pending.get("operation") or "").strip()
                and str(pending.get("question") or "").strip()
                and isinstance(pending.get("missing_fields"), list)
                and pending.get("missing_fields")
            ):
                return pending
    return None


_PENDING_DRAFT_COMMIT_BLOCKED_RESPONSE = (
    "Those details still need a quick confirmation before I treat them as final. "
    "Let me confirm and save them before we continue."
)


_ASSESSMENT_CARD_REQUIRED_RESPONSE = (
    "I want to put the investment assessment in front of you properly before we talk about sign-off. "
    "Let me pull that together first."
)


_PROPOSAL_PROMISE_BLOCKED_RESPONSE = (
    "I still need the investment analysis before I can show the next step. "
    "Give me a moment to finish that."
)


_PROPOSAL_STALLED_BLOCKED_RESPONSE = (
    "We’ve defined the money set aside, but the investment recommendation isn’t ready yet. "
    "Give me a moment to finish that next step."
)


_PROJECTION_RUN_BLOCKED_RESPONSE = (
    "I couldn’t complete the projection just now. Your saved details are still intact. "
    "Please try again."
)


def _blocked_response_for_claim_errors(
    claim_errors: List[Dict[str, str]],
    *,
    user_message: str = "",
) -> str:
    claims = {str(item.get("claim") or "") for item in claim_errors}
    if "material_facts_not_drafted" in claims:
        return _FACT_DUMP_CLAIM_BLOCKED_RESPONSE
    if "pending_drafts_not_committed" in claims:
        return _PENDING_DRAFT_COMMIT_BLOCKED_RESPONSE
    if "proposal_construction_incomplete" in claims:
        return _PROPOSAL_STALLED_BLOCKED_RESPONSE
    if "projection_run_incomplete" in claims:
        return _PROJECTION_RUN_BLOCKED_RESPONSE
    if "assessment_card_not_structured" in claims:
        return _ASSESSMENT_CARD_REQUIRED_RESPONSE
    if "proposal_or_policy_created" in claims:
        return _writeback_claim_blocked_response(user_message)
    return _writeback_claim_blocked_response(user_message)
