from __future__ import annotations

import re
from typing import Any, Dict

from pydantic import ValidationError

from advisor.agents.quant_contracts.tool_requests import (
    AssetAllocationAgentToolRequest,
    CashflowAgentToolRequest,
    CashflowContributionSolverRequest,
)
from advisor.tools.deterministic_tools.run_cashflow_projection.scenarios import cashflow_required_metrics


class QuantToolArgumentError(ValueError):
    """Raised when model-authored quantitative tool arguments fail the SDK contract."""

    def __init__(self, tool_name: str, validation_error: ValidationError) -> None:
        self.tool_name = tool_name
        self.validation_errors = [
            {
                "path": ".".join(str(part) for part in item.get("loc", ())),
                "type": str(item.get("type") or "value_error"),
                "message": str(item.get("msg") or "Invalid value."),
            }
            for item in validation_error.errors(include_input=False, include_url=False)
        ]
        super().__init__(f"{tool_name} arguments failed validation")


def normalize_quant_tool_arguments(
    tool_name: str,
    arguments: Dict[str, Any],
    *,
    user_message: str = "",
) -> Dict[str, Any]:
    """Validate model-authored arguments and inject server-owned routing fields."""

    if tool_name == "run_cashflow_projection":
        try:
            request = CashflowAgentToolRequest.model_validate(arguments)
        except ValidationError as exc:
            raise QuantToolArgumentError(tool_name, exc) from exc
        public_scenario = request.scenario.model_dump(mode="json", exclude_none=True)
        return {
            "question": request.question,
            # Server-owned: derived only from the original user turn, never
            # from model-authored question text.
            "mortgage_defaults_authorized": (
                mortgage_default_fallback_authorized(user_message)
            ),
            "allocation_analysis_id": request.allocation_analysis_id,
            "allocation_analysis_ids": request.allocation_analysis_ids,
            "monte_carlo_paths": request.monte_carlo_paths,
            "detail_report_groups": request.detail_report_groups,
            "calendar_years": request.calendar_years,
            "detail_columns": request.detail_columns,
            "public_fact_refs": (
                [item.model_dump(mode="json") for item in request.public_fact_refs]
                if request.public_fact_refs
                else None
            ),
            "scenario": {
                "requested": True,
                "action": "run_cashflow_model",
                "confidence": 1.0,
                "source": "sdk_validated_tool_call",
                "reason": "The specialist selected the strict cash-flow simulation boundary.",
                "evidence": [],
                "requested_metrics": cashflow_required_metrics(),
                "scenario_summary": public_scenario.get("scenario_summary"),
                "scenario_rationale": public_scenario.get("scenario_rationale"),
                "scenario_changes": public_scenario.get("scenario_changes", []),
                "negated": False,
            },
        }
    if tool_name == "run_asset_allocation":
        try:
            request = AssetAllocationAgentToolRequest.model_validate(arguments)
        except ValidationError as exc:
            raise QuantToolArgumentError(tool_name, exc) from exc
        return request.model_dump(mode="json")
    if tool_name == "solve_cashflow_contribution":
        try:
            request = CashflowContributionSolverRequest.model_validate(arguments)
        except ValidationError as exc:
            raise QuantToolArgumentError(tool_name, exc) from exc
        return {
            **request.model_dump(mode="json"),
            "mortgage_defaults_authorized": (
                mortgage_default_fallback_authorized(user_message)
            ),
        }
    return arguments


_MORTGAGE_DEFAULT_NEGATION_RE = re.compile(
    r"\b(?:do not|don't|dont|never|without)\b.{0,30}"
    r"\b(?:mortgage\s+)?(?:defaults?|fallback assumptions?)\b",
    re.IGNORECASE,
)


_MORTGAGE_DEFAULT_REQUEST_RE = re.compile(
    r"\b(?:use|apply|accept|proceed with|go with|run with)\b.{0,50}"
    r"\b(?:the\s+)?(?:mortgage\s+)?(?:defaults?|configured assumptions?|"
    r"fallback assumptions?)\b",
    re.IGNORECASE,
)


_MORTGAGE_VALUE_CONTEXT_RE = re.compile(
    r"\b(?:mortgage|home value|property value|appreciation|interest rate|"
    r"remaining term|loan term|principal and interest|p&i|housing cost|"
    r"those (?:mortgage )?(?:values|details|terms|inputs)|"
    r"the requested (?:values|details|terms|inputs|information)|"
    r"that information|them|those)\b",
    re.IGNORECASE,
)


_CANNOT_PROVIDE_VALUE_RE = re.compile(
    r"\b(?:cannot|can't|cant|unable to|do not|don't|dont)\s+"
    r"(?:provide|supply|confirm|find|get|locate|know|have)\b|"
    r"\b(?:not sure|unknown|unavailable)\b",
    re.IGNORECASE,
)


def mortgage_default_fallback_authorized(user_message: str) -> bool:
    """Allow mortgage defaults only from explicit language in the user turn."""

    message = " ".join(str(user_message or "").split())
    if not message or _MORTGAGE_DEFAULT_NEGATION_RE.search(message):
        return False
    if _MORTGAGE_DEFAULT_REQUEST_RE.search(message):
        return True
    return bool(
        _CANNOT_PROVIDE_VALUE_RE.search(message)
        and _MORTGAGE_VALUE_CONTEXT_RE.search(message)
    )
