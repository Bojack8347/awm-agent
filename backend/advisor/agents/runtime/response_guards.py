from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from advisor.agents.context import AwmAgentContext
from advisor.agents.quant_contracts import (
    QuantConclusionValidation,
    QuantValidationIssue,
    ensure_required_allocation_proposal_metrics,
    format_quant_response_for_client,
    propagate_quant_warnings,
    render_asset_allocation_failure_fallback,
    render_quantitative_missing_data_fallback,
    render_quantitative_reporting_fallback,
    validate_quantitative_response,
)
from advisor.agents.runtime.claim_checks import (
    _is_fact_writeback_only_turn,
    _pending_assessment_created_this_turn,
    _writeback_claim_errors,
)
from advisor.agents.runtime.guard_messages import (
    _MISSING_QUANT_EVIDENCE_RESPONSE,
    _QUANT_CLAIM_BLOCKED_RESPONSE,
    _blocked_response_for_claim_errors,
    _calculation_capability_gap_response,
    _structured_clarification,
)
from advisor.agents.runtime.quant_intent import (
    _is_explicit_read_only_quant_request,
    _remove_applied_assumptions_appendix,
    _stored_followup_requests_assumptions,
)


def _apply_final_response_guards(
    response_text: str,
    *,
    context: AwmAgentContext,
    user_message: str,
    response_rewriter: Optional[
        Callable[[str, QuantConclusionValidation], Optional[str]]
    ] = None,
) -> tuple[str, List[Dict[str, Any]], QuantConclusionValidation]:
    """Apply writeback and quantitative guards identically to run and resume."""

    calculation_gap_response = _calculation_capability_gap_response(
        context.tool_results
    )
    if calculation_gap_response is not None:
        validation = validate_quantitative_response(
            calculation_gap_response,
            context.tool_results,
            client_file=context.client_file,
            user_message=user_message,
        )
        return (
            format_quant_response_for_client(calculation_gap_response),
            [],
            validation,
        )

    clarification = _structured_clarification(context.tool_results)
    if clarification is not None:
        question = str(clarification.get("question") or "").strip()
        validation = validate_quantitative_response(
            question,
            context.tool_results,
            client_file=context.client_file,
            user_message=user_message,
        )
        return format_quant_response_for_client(question), [], validation

    if _pending_assessment_created_this_turn(context.tool_results):
        return (
            (
                "I’ve prepared your Investment Consultation Summary. "
                "Please review the details below and choose Agree when you’re "
                "comfortable signing off, or Cancel if anything needs to change."
            ),
            [],
            QuantConclusionValidation(
                status="not_applicable",
                valid_for_recommendation=False,
                recommendation_policy_enabled=False,
            ),
        )

    include_assumptions = _stored_followup_requests_assumptions(user_message)
    visible_response = ensure_required_allocation_proposal_metrics(
        response_text,
        context.tool_results,
    )
    visible_response = propagate_quant_warnings(
        visible_response,
        context.tool_results,
        include_assumptions=include_assumptions,
    )
    writeback_errors = [
        {**error, "source": "writeback_claim_validator"}
        for error in _writeback_claim_errors(
            visible_response,
            context.tool_results,
            user_message=user_message,
            client_file=context.client_file,
        )
    ]
    conclusion_validation = validate_quantitative_response(
        visible_response,
        context.tool_results,
        client_file=context.client_file,
        user_message=user_message,
    )
    if _is_fact_writeback_only_turn(context.tool_results) and not writeback_errors:
        # Percentages and dollar amounts can be facts being staged for
        # confirmation; they are not automatically quantitative conclusions.
        # The successful tool path is the deterministic discriminator here, so
        # natural-language fact extraction remains entirely model-owned.
        return (
            format_quant_response_for_client(visible_response),
            [],
            QuantConclusionValidation(
                status="not_applicable",
                valid_for_recommendation=False,
                recommendation_policy_enabled=False,
            ),
        )
    missing_data_fallback = render_quantitative_missing_data_fallback(context.tool_results)
    allocation_failure_fallback = render_asset_allocation_failure_fallback(
        context.tool_results
    )
    if (
        response_rewriter is not None
        and not writeback_errors
        and not missing_data_fallback
        and not allocation_failure_fallback
        and conclusion_validation.status == "blocked"
        and conclusion_validation.quant_tools_seen
        and conclusion_validation.evidence
        and all(
            item.valid_for_reporting
            for item in conclusion_validation.evidence
        )
    ):
        revised_response = response_rewriter(
            visible_response,
            conclusion_validation,
        )
        if str(revised_response or "").strip():
            visible_response = ensure_required_allocation_proposal_metrics(
                str(revised_response),
                context.tool_results,
            )
            visible_response = propagate_quant_warnings(
                visible_response,
                context.tool_results,
                include_assumptions=include_assumptions,
            )
            writeback_errors = [
                {**error, "source": "writeback_claim_validator"}
                for error in _writeback_claim_errors(
                    visible_response,
                    context.tool_results,
                    user_message=user_message,
                    client_file=context.client_file,
                )
            ]
            conclusion_validation = validate_quantitative_response(
                visible_response,
                context.tool_results,
                client_file=context.client_file,
                user_message=user_message,
            )
    if allocation_failure_fallback:
        visible_response = propagate_quant_warnings(
            allocation_failure_fallback,
            context.tool_results,
            include_assumptions=include_assumptions,
        )
        fallback_validation = validate_quantitative_response(
            visible_response,
            context.tool_results,
            client_file=context.client_file,
            user_message=user_message,
        )
        conclusion_validation = fallback_validation.model_copy(
            update={"sanitized_errors": conclusion_validation.errors}
        )
    elif writeback_errors and not missing_data_fallback:
        reporting_fallback = (
            render_quantitative_reporting_fallback(
                context.tool_results,
                user_message=user_message,
            )
            if _is_explicit_read_only_quant_request(user_message)
            else None
        )
        if reporting_fallback:
            visible_response = propagate_quant_warnings(
                reporting_fallback,
                context.tool_results,
                include_assumptions=include_assumptions,
            )
            fallback_validation = validate_quantitative_response(
                visible_response,
                context.tool_results,
                client_file=context.client_file,
                user_message=user_message,
            )
            if fallback_validation.status == "passed":
                sanitized = [
                    *conclusion_validation.errors,
                    *[
                        QuantValidationIssue(
                            type="unsupported_writeback_claim",
                            detail=(
                                "Model-authored narration implied a proposal or policy "
                                "writeback during an explicitly read-only quantitative request."
                            ),
                        )
                        for _error in writeback_errors
                    ],
                ]
                conclusion_validation = fallback_validation.model_copy(
                    update={"sanitized_errors": sanitized}
                )
                writeback_errors = []
            else:
                visible_response = _blocked_response_for_claim_errors(
                    writeback_errors,
                    user_message=user_message,
                )
                conclusion_validation = validate_quantitative_response(
                    visible_response,
                    context.tool_results,
                    client_file=context.client_file,
                    user_message=user_message,
                ).model_copy(
                    update={
                        "sanitized_errors": [
                            *conclusion_validation.errors,
                            *[
                                QuantValidationIssue(
                                    type=str(error.get("type") or "missing_writeback_tool"),
                                    detail=str(
                                        error.get("claim")
                                        or error.get("required_tool")
                                        or "unsupported_writeback_claim"
                                    ),
                                )
                                for error in writeback_errors
                            ],
                        ]
                    }
                )
                writeback_errors = []
        else:
            visible_response = _blocked_response_for_claim_errors(
                writeback_errors,
                user_message=user_message,
            )
            fallback_validation = validate_quantitative_response(
                visible_response,
                context.tool_results,
                client_file=context.client_file,
                user_message=user_message,
            )
            conclusion_validation = fallback_validation.model_copy(
                update={
                    "sanitized_errors": [
                        *conclusion_validation.errors,
                        *[
                            QuantValidationIssue(
                                type=str(error.get("type") or "missing_writeback_tool"),
                                detail=str(
                                    error.get("claim")
                                    or error.get("required_tool")
                                    or "unsupported_writeback_claim"
                                ),
                            )
                            for error in writeback_errors
                        ],
                    ]
                }
            )
            # Safe canned reply was delivered; do not fail the companion turn.
            writeback_errors = []
    elif missing_data_fallback:
        missing_data_fallback = propagate_quant_warnings(
            missing_data_fallback,
            context.tool_results,
            include_assumptions=include_assumptions,
        )
        fallback_validation = validate_quantitative_response(
            missing_data_fallback,
            context.tool_results,
            client_file=context.client_file,
            user_message=user_message,
        )
        if fallback_validation.status in {"abstained", "not_applicable"}:
            visible_response = missing_data_fallback
            conclusion_validation = fallback_validation.model_copy(
                update={"sanitized_errors": conclusion_validation.errors}
            )
        else:
            visible_response = _QUANT_CLAIM_BLOCKED_RESPONSE
    elif conclusion_validation.status == "blocked":
        reporting_fallback = render_quantitative_reporting_fallback(
            context.tool_results,
            user_message=user_message,
        )
        if reporting_fallback:
            reporting_fallback = propagate_quant_warnings(
                reporting_fallback,
                context.tool_results,
                include_assumptions=include_assumptions,
            )
            fallback_validation = validate_quantitative_response(
                reporting_fallback,
                context.tool_results,
                client_file=context.client_file,
                user_message=user_message,
            )
            if fallback_validation.status == "passed":
                visible_response = reporting_fallback
                conclusion_validation = fallback_validation.model_copy(
                    update={"sanitized_errors": conclusion_validation.errors}
                )
            else:
                visible_response = _QUANT_CLAIM_BLOCKED_RESPONSE
        elif not conclusion_validation.quant_tools_seen and any(
            issue.type == "missing_quant_evidence"
            for issue in conclusion_validation.errors
        ):
            visible_response = _MISSING_QUANT_EVIDENCE_RESPONSE
            conclusion_validation = validate_quantitative_response(
                visible_response,
                context.tool_results,
                client_file=context.client_file,
                user_message=user_message,
            ).model_copy(update={"sanitized_errors": conclusion_validation.errors})
        else:
            visible_response = _QUANT_CLAIM_BLOCKED_RESPONSE
    if (
        visible_response == _QUANT_CLAIM_BLOCKED_RESPONSE
        and conclusion_validation.status == "blocked"
    ):
        blocked_errors = conclusion_validation.errors
        conclusion_validation = validate_quantitative_response(
            visible_response,
            context.tool_results,
            client_file=context.client_file,
            user_message=user_message,
        ).model_copy(update={"sanitized_errors": blocked_errors})
    visible_response = propagate_quant_warnings(
        visible_response,
        context.tool_results,
        include_assumptions=include_assumptions,
    )
    if not include_assumptions:
        visible_response = _remove_applied_assumptions_appendix(visible_response)
    # Validation already ran on tagged/disclosure text; strip internal markup for chat.
    visible_response = format_quant_response_for_client(visible_response)
    quant_errors = [
        {**issue.model_dump(mode="json"), "source": "quant_conclusion_validator"}
        for issue in conclusion_validation.errors
    ]
    return visible_response, [*writeback_errors, *quant_errors], conclusion_validation
