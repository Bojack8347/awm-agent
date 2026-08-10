from __future__ import annotations

import json
import re
from collections import deque
from typing import Any, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

from advisor.agents.context import AwmAgentContext
from advisor.agents.quant_contracts import (
    QuantConclusionValidation,
    build_quant_response_annotations,
    format_quant_response_for_client,
    propagate_quant_warnings,
    quant_recommendation_policy,
    validate_quantitative_response,
)
from advisor.agents.runtime.artifact_store import (
    _combined_subagent_artifacts,
    _dedupe_subagent_artifacts,
)
from advisor.agents.runtime.assessment_artifacts import (
    _investment_assessment_artifacts_from_client_file,
    _investment_assessment_artifacts_from_tool_results,
)
from advisor.agents.runtime.guard_messages import _structured_clarification
from advisor.agents.runtime.quant_intent import (
    _remove_applied_assumptions_appendix,
    _stored_followup_requests_assumptions,
)
from advisor.agents.runtime.token_budget import (
    chat_record_token_budget,
    count_chat_record_tokens,
    count_message_tokens,
)
from advisor.tracing.tracing import format_trace_timeline


def _normalized_conversation_turn(turn: Any) -> Optional[Dict[str, str]]:
    if not isinstance(turn, dict):
        return None
    role = str(turn.get("role") or "").strip().lower()
    content = str(turn.get("content") or "").strip()
    if role not in {"user", "assistant"} or not content:
        return None
    return {"role": role, "content": content}


def _budget_recent_turns(
    turns: Iterable[Dict[str, Any]],
    *,
    current_user_message: str,
    summary_block: str = "",
    token_budget: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Keep every turn when possible, otherwise retain the newest fitting suffix."""
    raw, normalized, _trimmed = _budget_recent_turns_with_status(
        turns,
        current_user_message=current_user_message,
        summary_block=summary_block,
        token_budget=token_budget,
    )
    return raw, normalized


def _budget_recent_turns_with_status(
    turns: Iterable[Dict[str, Any]],
    *,
    current_user_message: str,
    summary_block: str = "",
    token_budget: Optional[int] = None,
    newest_first: bool = False,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]], bool]:
    """Budget history and report whether any otherwise-valid row was removed."""
    if newest_first:
        return _budget_newest_first_turns_with_status(
            turns,
            current_user_message=current_user_message,
            summary_block=summary_block,
            token_budget=token_budget,
        )
    budget = token_budget or chat_record_token_budget()
    current = str(current_user_message or "").strip()
    base_tokens = count_chat_record_tokens(
        [],
        current_user_message=current,
        summary_block=summary_block,
    )
    selected: Deque[Tuple[Dict[str, Any], Dict[str, str], int]] = deque()
    selected_tokens = base_tokens
    trimmed = False
    pending: Optional[Tuple[Dict[str, Any], Dict[str, str]]] = None

    def _append(raw: Dict[str, Any], normalized: Dict[str, str]) -> None:
        nonlocal selected_tokens, trimmed
        item_tokens = count_message_tokens(normalized)
        selected.append((raw, normalized, item_tokens))
        selected_tokens += item_tokens
        while selected and selected_tokens > budget:
            _, _, removed_tokens = selected.popleft()
            selected_tokens -= removed_tokens
            trimmed = True

    for turn in turns:
        normalized = _normalized_conversation_turn(turn)
        if normalized is None:
            continue
        if pending is not None:
            _append(*pending)
        pending = (turn, normalized)

    if pending is not None:
        raw, normalized = pending
        if normalized != {"role": "user", "content": current}:
            _append(raw, normalized)

    return (
        [raw for raw, _, _ in selected],
        [normalized for _, normalized, _ in selected],
        trimmed,
    )


def _budget_newest_first_turns_with_status(
    turns: Iterable[Dict[str, Any]],
    *,
    current_user_message: str,
    summary_block: str = "",
    token_budget: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]], bool]:
    """Retain a bounded newest suffix from a newest-first history stream."""
    budget = token_budget or chat_record_token_budget()
    current = str(current_user_message or "").strip()
    selected_tokens = count_chat_record_tokens(
        [],
        current_user_message=current,
        summary_block=summary_block,
    )
    selected: List[Tuple[Dict[str, Any], Dict[str, str]]] = []
    first_valid = True
    trimmed = False

    iterator = iter(turns)
    try:
        for turn in iterator:
            normalized = _normalized_conversation_turn(turn)
            if normalized is None:
                continue
            if first_valid:
                first_valid = False
                if normalized == {"role": "user", "content": current}:
                    continue
            item_tokens = count_message_tokens(normalized)
            if selected_tokens + item_tokens > budget:
                trimmed = True
                break
            selected.append((turn, normalized))
            selected_tokens += item_tokens
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()

    selected.reverse()
    return (
        [raw for raw, _ in selected],
        [normalized for _, normalized in selected],
        trimmed,
    )


def _normalize_recent_turns(
    turns: Iterable[Dict[str, Any]],
    *,
    current_user_message: str,
    summary_block: str = "",
    token_budget: Optional[int] = None,
) -> List[Dict[str, str]]:
    return _budget_recent_turns(
        turns,
        current_user_message=current_user_message,
        summary_block=summary_block,
        token_budget=token_budget,
    )[1]


_ANALYSIS_REFERENCE_RE = re.compile(
    r"\b(?P<prefix>cashflow|allocation)[_-][A-Za-z0-9][A-Za-z0-9_-]{2,159}\b",
    re.IGNORECASE,
)


def _artifact_context_from_recent_turns(
    turns: List[Dict[str, Any]],
    *,
    pending_operation: Optional[Dict[str, Any]] = None,
    carried_artifact_references: Optional[Iterable[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Resolve compact immutable analysis references from durable recent history."""

    references: List[Dict[str, str]] = []
    for item in carried_artifact_references or []:
        if isinstance(item, dict):
            _append_artifact_reference(
                references,
                domain=item.get("domain"),
                analysis_id=item.get("analysis_id"),
                source_tool=item.get("source_tool") or "conversation_memory",
            )
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        metadata = turn.get("metadata")
        stored = (
            metadata.get("artifact_context")
            if isinstance(metadata, dict)
            else None
        )
        if (
            isinstance(stored, dict)
            and stored.get("schema_version")
            == "awm.artifact_reference_context.v1"
        ):
            for item in stored.get("references") or []:
                if isinstance(item, dict):
                    _append_artifact_reference(
                        references,
                        domain=item.get("domain"),
                        analysis_id=item.get("analysis_id"),
                        source_tool=item.get("source_tool") or "durable_history",
                    )
        content = str(turn.get("content") or "")
        for match in _ANALYSIS_REFERENCE_RE.finditer(content):
            _append_artifact_reference(
                references,
                domain=(
                    "cashflow"
                    if match.group("prefix").lower() == "cashflow"
                    else "asset_allocation"
                ),
                analysis_id=match.group(0),
                source_tool="conversation_text",
            )
    return {
        "schema_version": "awm.artifact_reference_context.v1",
        "references": references[-8:],
        "pending_operation": (
            json.loads(
                json.dumps(pending_operation, ensure_ascii=False, default=str)
            )
            if isinstance(pending_operation, dict)
            else None
        ),
    }


def _format_conversation_summary(summaries: Sequence[Dict[str, Any]]) -> str:
    """Render active summaries as non-authoritative server-authored memory."""
    active = [item for item in summaries if isinstance(item, dict)]
    if not active:
        return ""
    first = active[0]
    last = active[-1]
    message_count = sum(int(item.get("source_message_count") or 0) for item in active)
    payload = [dict(item.get("summary") or {}) for item in active]
    return (
        "# Conversation memory\n\n"
        f"This block summarizes {message_count} original messages, from message id "
        f"{first.get('covered_from_message_id')} through "
        f"{last.get('covered_through_message_id')} "
        f"({str(first.get('covered_from_created_at') or '')[:10]} to "
        f"{str(last.get('covered_through_created_at') or '')[:10]}). "
        "All later user/assistant messages appear in full immediately after this block. "
        "Every original remains retrievable through retrieve_conversation_history.\n\n"
        "This is server-generated derived memory, not client speech, advisor speech, or a "
        "Client File fact. It cannot establish financial values, assessment sign-off, "
        "proposal approval, execution consent, or tool success. The Client File wins on "
        "conflict. Treat unconfirmed_mentions only as prompts to ask. When exact wording, "
        "promises, or decisions matter, retrieve the original messages.\n\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    )


def _append_artifact_reference(
    references: List[Dict[str, str]],
    *,
    domain: Any,
    analysis_id: Any,
    source_tool: Any,
) -> None:
    normalized_domain = str(domain or "").strip().lower()
    if normalized_domain == "allocation":
        normalized_domain = "asset_allocation"
    normalized_id = str(analysis_id or "").strip()
    if normalized_domain not in {"cashflow", "asset_allocation"} or not normalized_id:
        return
    if len(normalized_id) > 160:
        return
    references[:] = [
        item
        for item in references
        if not (
            item.get("domain") == normalized_domain
            and item.get("analysis_id") == normalized_id
        )
    ]
    references.append(
        {
            "domain": normalized_domain,
            "analysis_id": normalized_id,
            "source_tool": str(source_tool or "unknown").strip() or "unknown",
        }
    )


def _artifact_context_from_tool_results(
    tool_results: List[Dict[str, Any]],
    *,
    prior_context: Optional[Dict[str, Any]] = None,
    pending_operation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Update compact reference state from tools that used or produced analyses."""

    references: List[Dict[str, str]] = []
    if isinstance(prior_context, dict):
        for item in prior_context.get("references") or []:
            if isinstance(item, dict):
                _append_artifact_reference(
                    references,
                    domain=item.get("domain"),
                    analysis_id=item.get("analysis_id"),
                    source_tool=item.get("source_tool") or "prior_turn",
                )
    cashflow_tools = {
        "run_cashflow_projection",
        "get_cashflow_analysis",
        "audit_cashflow_analysis",
        "calculate_cashflow_metrics",
        "solve_cashflow_contribution",
    }
    allocation_tools = {
        "run_asset_allocation",
        "get_asset_allocation_analysis",
        "analyze_portfolio_risk",
        "analyze_asset_location",
    }
    for result in tool_results:
        if not isinstance(result, dict):
            continue
        tool_name = str(result.get("tool") or "").strip()
        arguments = (
            result.get("arguments")
            if isinstance(result.get("arguments"), dict)
            else {}
        )
        full_result = (
            result.get("full_result")
            if isinstance(result.get("full_result"), dict)
            else {}
        )
        if tool_name in cashflow_tools:
            for analysis_id in (
                result.get("analysis_id"),
                result.get("selected_cashflow_analysis_id"),
                arguments.get("analysis_id"),
                full_result.get("analysis_id"),
                full_result.get("selected_cashflow_analysis_id"),
            ):
                _append_artifact_reference(
                    references,
                    domain="cashflow",
                    analysis_id=analysis_id,
                    source_tool=tool_name,
                )
        if tool_name in allocation_tools:
            for analysis_id in (
                result.get("analysis_id"),
                result.get("source_allocation_analysis_id"),
                arguments.get("analysis_id"),
                arguments.get("allocation_analysis_id"),
                full_result.get("analysis_id"),
                full_result.get("source_allocation_analysis_id"),
            ):
                _append_artifact_reference(
                    references,
                    domain="asset_allocation",
                    analysis_id=analysis_id,
                    source_tool=tool_name,
                )
        if tool_name == "compare_quant_analyses":
            domain = str(
                arguments.get("domain") or full_result.get("domain") or ""
            ).strip()
            for analysis_id in (
                arguments.get("base_analysis_id"),
                arguments.get("comparison_analysis_id"),
                full_result.get("base_analysis_id"),
                full_result.get("comparison_analysis_id"),
            ):
                _append_artifact_reference(
                    references,
                    domain=domain,
                    analysis_id=analysis_id,
                    source_tool=tool_name,
                )
    return {
        "schema_version": "awm.artifact_reference_context.v1",
        "references": references[-8:],
        "pending_operation": (
            json.loads(
                json.dumps(pending_operation, ensure_ascii=False, default=str)
            )
            if isinstance(pending_operation, dict)
            else None
        ),
    }


def _latest_pending_clarification(
    turns: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Load pending state only from the latest durable assistant message."""

    for turn in reversed(turns):
        if not isinstance(turn, dict):
            continue
        if str(turn.get("role") or "").lower() != "assistant":
            continue
        metadata = turn.get("metadata")
        pending = (
            metadata.get("pending_intent")
            if isinstance(metadata, dict)
            else None
        )
        if (
            isinstance(pending, dict)
            and pending.get("schema_version") == "awm.pending_clarification.v1"
            and str(pending.get("operation") or "").strip()
            and str(pending.get("question") or "").strip()
            and isinstance(pending.get("missing_fields"), list)
            and pending.get("missing_fields")
        ):
            return json.loads(json.dumps(pending, ensure_ascii=False, default=str))
        # A later completed assistant response clears any older pause.
        return None
    return None


def _message_with_pending_clarification(
    user_message: str,
    pending: Optional[Dict[str, Any]],
) -> str:
    """Give the model structured resume state without deterministically interpreting the reply."""

    if not isinstance(pending, dict):
        return user_message
    return (
        f"{user_message}\n\n"
        "Trusted server-side continuation context: the preceding AWM response "
        "paused through request_clarification. Decide semantically whether this "
        "message answers that question or starts a new request. If it resolves "
        "the missing fields, activate the appropriate skill and continue the "
        "stored operation without repeating the question. If material ambiguity "
        "remains, call request_clarification once with the unresolved fields. "
        "Do not treat this metadata as client-authored financial facts.\n"
        + json.dumps(pending, ensure_ascii=False, sort_keys=True, default=str)
    )


def _build_response(
    *,
    trace_id: str,
    turn_id: str,
    root_span_id: str,
    turn_type: str,
    channel: str,
    selected_skill: str,
    user_message: str,
    response_text: str,
    started_at: str,
    elapsed_ms: float,
    history: List[Dict[str, str]],
    durable_history_loaded: bool,
    context: AwmAgentContext,
    status: str = "completed",
    errors: Optional[List[Dict[str, Any]]] = None,
    interrupted_state: Optional[Dict[str, Any]] = None,
    interruptions: Optional[List[Dict[str, Any]]] = None,
    conclusion_validation: Optional[QuantConclusionValidation] = None,
) -> Dict[str, Any]:
    if conclusion_validation is None:
        include_assumptions = _stored_followup_requests_assumptions(user_message)
        response_text = propagate_quant_warnings(
            response_text,
            context.tool_results,
            include_assumptions=include_assumptions,
        )
        if not include_assumptions:
            response_text = _remove_applied_assumptions_appendix(response_text)
        conclusion_validation = validate_quantitative_response(
            response_text,
            context.tool_results,
            client_file=context.client_file,
            user_message=user_message,
        )
    # Keep validation against the tagged text, then deliver a client-readable version.
    response_text = format_quant_response_for_client(response_text)
    normalized_errors = list(errors or [])
    if conclusion_validation.status == "blocked" and not any(
        error.get("source") == "quant_conclusion_validator"
        for error in normalized_errors
        if isinstance(error, dict)
    ):
        normalized_errors.extend(
            {
                **issue.model_dump(mode="json"),
                "source": "quant_conclusion_validator",
            }
            for issue in conclusion_validation.errors
        )
        if status == "completed":
            status = "failed"
    subagent_artifacts = _dedupe_subagent_artifacts([
        *_combined_subagent_artifacts(context),
        *_investment_assessment_artifacts_from_tool_results(context.tool_results, response_text),
    ])
    # Pending assessments already on Client File must still surface as Agree cards
    # even when the model only activates a skill or invents prose without tools.
    if not any(
        isinstance(artifact, dict)
        and isinstance(artifact.get("payload"), dict)
        and str((artifact.get("payload") or {}).get("artifact_type") or "")
        == "investment_assessment"
        for artifact in subagent_artifacts
    ):
        durable_assessment_cards = _investment_assessment_artifacts_from_client_file(
            context.client_file,
            response_text,
        )
        if durable_assessment_cards:
            subagent_artifacts = _dedupe_subagent_artifacts(
                [*subagent_artifacts, *durable_assessment_cards]
            )
            if not str(response_text or "").strip():
                response_text = (
                    "Here's the investment consultation summary for your review."
                )
    clarification = _structured_clarification(context.tool_results)
    context.artifact_context = _artifact_context_from_tool_results(
        context.tool_results,
        prior_context=context.artifact_context,
        pending_operation=clarification,
    )
    response = {
        "trace_id": trace_id,
        "turn_id": turn_id,
        "root_span_id": root_span_id,
        "response_text": response_text,
        "turn_type": turn_type,
        "channel": channel,
        "status": status,
        "selected_skill": selected_skill,
        "runtime": "openai_agents_sdk",
        "memory_context": {
            "recent_turn_count": len(history),
            "durable_history_loaded": durable_history_loaded,
            "conversation_summary_count": len(context.conversation_summaries),
            "artifact_reference_count": len(
                context.artifact_context.get("references") or []
            ),
        },
        "artifact_context": context.artifact_context,
        "active_skills": dict(context.active_skills),
        "skill_candidates": dict(context.skill_candidates),
        "planned_actions": [],
        "tool_results": context.tool_results,
        "subagent_artifacts": subagent_artifacts,
        "errors": normalized_errors,
        "conclusion_validation": conclusion_validation.model_dump(mode="json"),
        "quantitative_response": build_quant_response_annotations(
            response_text,
            context.tool_results,
        ).model_dump(mode="json"),
        "recommendation_policy": quant_recommendation_policy(),
        "llm_calls": context.llm_calls,
        "timing": _turn_timing_breakdown(
            started_at=started_at,
            total_elapsed_ms=elapsed_ms,
            context=context,
        ),
        "trace_events": context.trace_events,
        "trace_timeline": format_trace_timeline(user_message=user_message, response_text=response_text, trace_events=context.trace_events),
    }
    if clarification is not None:
        response["routing_clarification"] = {
            "operation": clarification["operation"],
            "question": clarification["question"],
            "missing_fields": list(clarification["missing_fields"]),
            "candidate_references": list(
                clarification.get("candidate_references") or []
            ),
        }
        response["pending_intent"] = clarification
    if interrupted_state is not None:
        response["interrupted_state"] = interrupted_state
    if interruptions is not None:
        response["interruptions"] = interruptions
    return response


def _turn_timing_breakdown(
    *,
    started_at: str,
    total_elapsed_ms: float,
    context: AwmAgentContext,
) -> Dict[str, Any]:
    """Separate user-visible, LLM, quantitative-tool, and other elapsed time."""

    llm_elapsed = sum(
        float(call.get("duration_ms") or 0.0)
        for call in context.llm_calls
        if isinstance(call, dict)
    )
    tool_events = [
        event
        for event in context.trace_events
        if isinstance(event, dict)
        and event.get("event") == "sdk_tool_completed"
        and isinstance(event.get("duration_ms"), (int, float))
    ]
    quant_names = {
        "run_cashflow_projection",
        "solve_cashflow_contribution",
        "get_cashflow_analysis",
        "audit_cashflow_analysis",
        "run_asset_allocation",
        "get_asset_allocation_analysis",
        "compare_quant_analyses",
        "calculate_cashflow_metrics",
        "calculate_financial_math",
        "analyze_portfolio_risk",
        "analyze_asset_location",
        "estimateAllocationRiskReturn",
        "lookupRiskReturnFrontier",
    }
    quant_tool_elapsed = sum(
        float(event["duration_ms"])
        for event in tool_events
        if str(event.get("node") or "") in quant_names
    )
    all_tool_elapsed = sum(float(event["duration_ms"]) for event in tool_events)
    other_tool_elapsed = max(0.0, all_tool_elapsed - quant_tool_elapsed)
    accounted = llm_elapsed + all_tool_elapsed
    return {
        "schema_version": "awm.turn_timing.v2",
        "turn_started_at": started_at,
        "total_elapsed_ms": round(float(total_elapsed_ms), 3),
        "llm_elapsed_ms": round(llm_elapsed, 3),
        "quantitative_tool_elapsed_ms": round(quant_tool_elapsed, 3),
        "other_tool_elapsed_ms": round(other_tool_elapsed, 3),
        "orchestration_and_validation_elapsed_ms": round(
            max(0.0, float(total_elapsed_ms) - accounted),
            3,
        ),
        "llm_call_count": len(context.llm_calls),
        "tool_call_count": len(tool_events),
        "measurement_note": (
            "Quantitative-tool time includes adapter and engine execution; "
            "end-to-end time additionally includes LLM orchestration and validation."
        ),
    }
