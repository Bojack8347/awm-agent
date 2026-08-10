from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from client_file.lifecycle import has_pending_draft_facts

from advisor.agents.context import AwmAgentContext
from advisor.agents.quant_contracts import QuantConclusionValidation
from advisor.agents.runtime.assessment_artifacts import (
    _investment_assessment_artifacts_from_tool_results,
    _pending_unsigned_investment_assessments,
)
from advisor.agents.runtime.client_file_state import (
    _client_file_has_signed_assessment,
    _client_file_ready_for_assessment_presentation,
)


def _is_fact_writeback_only_turn(tool_results: List[Dict[str, Any]]) -> bool:
    successful_tools = {
        str(result.get("tool") or "").strip()
        for result in tool_results
        if isinstance(result, dict)
        and result.get("ok") is not False
        and str(result.get("tool") or "").strip()
    }
    fact_write_tools = {
        "draft_fact",
        "save_fact",
        "commit_facts",
        "present_fact_confirmation",
        "resolve_fact_confirmation",
    }
    allowed_tools = {
        "activate_skill",
        "draft_fact",
        "save_fact",
        "commit_facts",
        "present_fact_confirmation",
        "resolve_fact_confirmation",
        "save_consultation_checkpoint",
    }
    return bool(successful_tools.intersection(fact_write_tools)) and successful_tools.issubset(
        allowed_tools
    )


def _pending_assessment_created_this_turn(
    tool_results: List[Dict[str, Any]],
) -> bool:
    return any(
        isinstance(result, dict)
        and result.get("tool") == "create_investment_assessment"
        and result.get("ok") is True
        for result in tool_results
    ) and not any(
        isinstance(result, dict)
        and result.get("tool") == "record_assessment_signoff"
        and result.get("ok") is True
        for result in tool_results
    )


def _should_repair_proposal_claim(
    claim_errors: List[Dict[str, str]],
    context: AwmAgentContext,
) -> bool:
    if context.proposal_claim_repair_attempted:
        return False
    required = {str(item.get("required_tool") or "") for item in claim_errors}
    return "run_asset_allocation" in required


def _should_repair_assessment_creation(
    guard_errors: List[Dict[str, Any]],
    context: AwmAgentContext,
    *,
    conclusion_validation: Optional[QuantConclusionValidation] = None,
) -> bool:
    if context.assessment_claim_repair_attempted:
        return False
    # Allow create-or-replay so a pending assessment can still surface the Agree card.
    if not _client_file_ready_for_assessment_presentation(context.client_file or {}):
        return False
    # If a pending unsigned assessment is already durable, emit the card from
    # Client File instead of asking the model to recreate it.
    if _pending_unsigned_investment_assessments(context.client_file or {}):
        return False
    successful_tools = {
        str(result.get("tool") or "")
        for result in context.tool_results
        if isinstance(result, dict) and result.get("ok") is not False
    }
    if "create_investment_assessment" in successful_tools:
        return False
    for error in guard_errors:
        if not isinstance(error, dict):
            continue
        if str(error.get("required_tool") or "") == "create_investment_assessment":
            return True
        if str(error.get("claim") or "") in {
            "assessment_card_not_structured",
            "assessment_signoff_recorded",
        }:
            return True
        if str(error.get("type") or "") == "missing_quant_evidence":
            return True
    if conclusion_validation is not None:
        for issue in list(conclusion_validation.errors or []) + list(
            conclusion_validation.sanitized_errors or []
        ):
            issue_type = getattr(issue, "type", None)
            if issue_type is None and isinstance(issue, dict):
                issue_type = issue.get("type")
            if str(issue_type or "") == "missing_quant_evidence":
                return True
            detail = f"{issue_type} {getattr(issue, 'detail', '')}".lower()
            if "assessment" in detail or "create_investment_assessment" in detail:
                return True
    return False


def _writeback_claim_errors(
    response_text: str,
    tool_results: List[Dict[str, Any]],
    *,
    user_message: str = "",
    client_file: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, str]]:
    """Reject high-risk save/proposal claims that lack matching tool evidence."""

    text = " ".join(str(response_text or "").lower().split())
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    if not text:
        return []

    successful_tools = {
        str(result.get("tool") or "")
        for result in tool_results
        if isinstance(result, dict) and result.get("ok") is not False
    }
    errors: List[Dict[str, str]] = []

    projection_progress_claim = bool(
        re.search(r"\b(projection|cashflow|cash flow|financial outlook)\b", text)
        and (
            re.search(
                r"\b(i'?m|i am|we'?re|we are)\b.{0,28}"
                r"\b(running|processing|calculating|building|preparing)\b",
                text,
            )
            or re.search(
                r"\b(i'?ll|i will|we'?ll|we will)\b.{0,32}"
                r"\b(share|send|show|provide)\b.{0,24}\b(results?|projection)\b",
                text,
            )
        )
    )
    if projection_progress_claim and "run_cashflow_projection" not in successful_tools:
        errors.append(
            {
                "type": "missing_writeback_tool",
                "claim": "projection_run_incomplete",
                "required_tool": "run_cashflow_projection",
            }
        )

    assessment_presentation_claim = re.search(
        r"\b(draft\s+)?investment consultation assessment\b|\binvestment assessment\b|\bassessment for (?:your )?sign[- ]?off\b",
        text,
    )
    has_assessment_artifact = bool(_investment_assessment_artifacts_from_tool_results(tool_results, response_text))
    if (
        assessment_presentation_claim
        and "create_investment_assessment" not in successful_tools
        and "record_assessment_signoff" not in successful_tools
        and not has_assessment_artifact
    ):
        errors.append(
            {
                "type": "missing_writeback_tool",
                "claim": "assessment_card_not_structured",
                "required_tool": "create_investment_assessment",
            }
        )

    fact_save_claim = bool(
        re.search(r"\b(saved|recorded|updated|committed|confirmed|noted)\b", text)
    )
    if has_pending_draft_facts(client_file or {}) and fact_save_claim:
        if "commit_facts" not in successful_tools:
            errors.append(
                {
                    "type": "missing_writeback_tool",
                    "claim": "pending_drafts_not_committed",
                    "required_tool": "commit_facts",
                }
            )

    signoff_claim = re.search(r"\b(sign[- ]?off|assessment approval)\b", text) and re.search(
        r"\b(recorded|saved|confirm(?:ed|ing)?|captured|noted|ready to move on)\b",
        text,
    )
    signoff_denial = bool(
        re.search(
            r"\b(?:sign[- ]?off|assessment approval).{0,32}"
            r"(?:was|is|has|have|were)?\s*(?:not|never)\s+"
            r"(?:recorded|saved|confirmed|captured|noted)\b",
            text,
        )
        or re.search(
            r"\b(?:no|without) (?:recorded |saved )?(?:sign[- ]?off|assessment approval)\b",
            text,
        )
    )
    if (
        signoff_claim
        and not signoff_denial
        and "record_assessment_signoff" not in successful_tools
    ):
        errors.append(
            {
                "type": "missing_writeback_tool",
                "claim": "assessment_signoff_recorded",
                "required_tool": "record_assessment_signoff",
            }
        )

    if text:
        proposal_noun = re.search(r"\b(proposal|proposed policy|asset allocation|allocation)\b", text)
        proposal_presented = re.search(
            r"\b(here'?s|here is)\b.{0,80}\b(full\s+)?(?:investment\s+)?proposal\b",
            text,
        ) or re.search(
            r"\b(portfolio includes|model targets|detailed allocation|funds and allocations)\b",
            text,
        )
        proposal_promised = re.search(
            r"\b(i'?ll|i will|let me|going to)\b.{0,40}\b(generate|prepare|build|draft|create)\b",
            text,
        ) or re.search(
            r"\b(i'?m|i am)\s+(?:now\s+)?(?:generating|preparing|building|drafting|creating)\b",
            text,
        ) or re.search(
            r"\b(?:generating|preparing|building|drafting)\b.{0,40}\b(proposal|allocation)\b",
            text,
        )
        if (
            proposal_noun
            and (proposal_promised or proposal_presented)
            and "run_asset_allocation" not in successful_tools
        ):
            errors.append(
                {
                    "type": "missing_writeback_tool",
                    "claim": "proposal_or_policy_created",
                    "required_tool": "run_asset_allocation",
                }
            )

    proposal_claim = re.search(r"\b(proposal|proposed policy)\b", text) and re.search(
        r"\b(created|generated|built|prepared|drafted|saved)\b",
        text,
    )
    proposal_denial = bool(
        re.search(
            r"\b(?:no|not|never|without)\b.{0,28}\b(?:proposal|proposed policy)\b",
            text,
        )
        or re.search(
            r"\b(?:proposal|proposed policy)\b.{0,32}"
            r"\b(?:was|is|has|have|were|does|do)?\s*(?:not|never)\s+"
            r"(?:been\s+)?"
            r"(?:created|generated|built|prepared|drafted|saved)\b",
            text,
        )
        or re.search(
            r"\b(?:does not|do not|did not|will not|would not|cannot|can't)\s+"
            r"(?:create|generate|build|prepare|draft|save)\b.{0,28}"
            r"\b(?:proposal|proposed policy)\b",
            text,
        )
    )
    proposal_writeback_succeeded = any(
        _proposal_writeback_succeeded(result)
        for result in tool_results
        if isinstance(result, dict)
    )
    if proposal_claim and not proposal_denial and not proposal_writeback_succeeded:
        errors.append(
            {
                "type": "missing_writeback_tool",
                "claim": "proposal_or_policy_created",
                "required_tool": "create_asset_allocation_proposal",
            }
        )

    # Structural stall: money-pool / signoff progressed without allocation when a signed assessment exists.
    if (
        "run_asset_allocation" not in successful_tools
        and "upsert_money_pool" in successful_tools
        and (
            "record_assessment_signoff" in successful_tools
            or _client_file_has_signed_assessment(client_file or {})
        )
    ):
        errors.append(
            {
                "type": "missing_writeback_tool",
                "claim": "proposal_construction_incomplete",
                "required_tool": "run_asset_allocation",
            }
        )

    return errors


def _proposal_writeback_succeeded(result: Dict[str, Any]) -> bool:
    """Require durable evidence for a proposal/policy creation claim.

    ``run_asset_allocation`` is intentionally read-only.  Its success proves a
    calculation completed, not that a proposal or policy was created.  This
    helper accepts only an explicit persistence result so narration cannot
    turn an in-memory allocation into a false writeback claim.
    """

    if result.get("ok") is False:
        return False
    proposal_writeback = result.get("proposal_writeback")
    if isinstance(proposal_writeback, dict) and proposal_writeback.get("ok") is True:
        return True
    if result.get("tool") != "create_asset_allocation_proposal":
        return False
    write_result = result.get("write_result")
    return isinstance(write_result, dict) and write_result.get("ok") is True
