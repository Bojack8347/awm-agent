from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence

from pydantic import ValidationError

from advisor.agents.quant_contracts._shared import _numeric_leaves
from advisor.agents.quant_contracts.constants import QUANT_TOOL_NAMES
from advisor.agents.quant_contracts.evidence_allocation import (
    _allocation_evidence,
    _asset_location_evidence,
    _portfolio_risk_evidence,
)
from advisor.agents.quant_contracts.evidence_calculator import (
    _calculator_evidence,
    _quant_comparison_evidence,
    _wolfram_alpha_evidence,
)
from advisor.agents.quant_contracts.evidence_cashflow import (
    _cashflow_audit_evidence,
    _cashflow_evidence,
    _contribution_solver_evidence,
)
from advisor.agents.quant_contracts.evidence_public_fact import (
    _public_fact_evidence,
    _public_fact_reuse_receipt_evidence,
)
from advisor.agents.quant_contracts.models import QuantEvidenceClaim, QuantEvidenceEnvelope


def build_quant_evidence(tool_name: str, result: Mapping[str, Any]) -> QuantEvidenceEnvelope:
    """Derive recommendation evidence from a normalized deterministic result."""

    if tool_name in {"run_cashflow_projection", "get_cashflow_analysis"}:
        return _cashflow_evidence(result)
    if tool_name == "audit_cashflow_analysis":
        return _cashflow_audit_evidence(result)
    if tool_name in {"run_asset_allocation", "get_asset_allocation_analysis"}:
        return _allocation_evidence(result)
    if tool_name == "solve_cashflow_contribution":
        return _contribution_solver_evidence(result)
    if tool_name == "compare_quant_analyses":
        return _quant_comparison_evidence(result)
    if tool_name in {"calculate_cashflow_metrics", "calculate_financial_math"}:
        return _calculator_evidence(tool_name, result)
    if tool_name == "query_wolfram_alpha":
        return _wolfram_alpha_evidence(result)
    if tool_name == "research_public_financial_fact":
        return _public_fact_evidence(result)
    if tool_name == "review_public_fact_reuse":
        return _public_fact_reuse_receipt_evidence(result)
    if tool_name == "analyze_portfolio_risk":
        return _portfolio_risk_evidence(result)
    if tool_name == "analyze_asset_location":
        return _asset_location_evidence(result)
    return _generic_quant_evidence(tool_name, result)


def _generic_quant_evidence(tool_name: str, result: Mapping[str, Any]) -> QuantEvidenceEnvelope:
    """Expose lookup/estimation values for reporting, never for recommendations.

    These convenience tools do not carry the signed-assessment, full constraint,
    or approved conclusion contracts required by the recommendation-grade tools.
    A successful lookup may therefore support an exact labeled fact, but cannot
    become recommendation evidence merely because the global policy flag is on.
    """

    full_result = result.get("full_result") if isinstance(result.get("full_result"), dict) else {}
    execution_ok = result.get("ok") is True
    valid = execution_ok and bool(full_result)
    claims = [
        QuantEvidenceClaim(
            metric_key=path.removeprefix("$."),
            value=value,
            unit="unspecified",
            source_path=path,
        )
        for path, value in _numeric_leaves(full_result)
    ]
    return QuantEvidenceEnvelope(
        tool=tool_name,
        status="complete" if valid else "invalid",
        execution_ok=execution_ok,
        valid_for_reporting=valid,
        valid_for_conclusion=False,
        valid_for_recommendation=False,
        claims=claims,
        errors=[] if valid else ["quant_tool_result_invalid"],
    )


def _evidence_from_tool_results(tool_results: Sequence[Dict[str, Any]]) -> List[QuantEvidenceEnvelope]:
    evidence: List[QuantEvidenceEnvelope] = []
    for result in tool_results:
        if not isinstance(result, dict):
            continue
        tool_name = str(result.get("tool") or "")
        if tool_name not in QUANT_TOOL_NAMES:
            continue
        raw = result.get("recommendation_evidence")
        try:
            envelope = (
                QuantEvidenceEnvelope.model_validate(raw)
                if isinstance(raw, dict)
                else build_quant_evidence(tool_name, result)
            )
        except ValidationError:
            envelope = QuantEvidenceEnvelope(
                tool=tool_name,
                status="invalid",
                valid_for_reporting=False,
                valid_for_recommendation=False,
                errors=["malformed_recommendation_evidence"],
            )
        evidence.append(envelope)
    return [
        envelope
        for index, envelope in enumerate(evidence)
        if not (
            not envelope.valid_for_reporting
            and not envelope.claims
            and any(
                later.tool == envelope.tool and later.valid_for_reporting
                for later in evidence[index + 1 :]
            )
        )
    ]
