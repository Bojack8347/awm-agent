from __future__ import annotations

from typing import Any, List, Mapping

from advisor.agents.quant_contracts._shared import _finite_numeric, _string_list
from advisor.agents.quant_contracts.models import (
    QuantEvidenceClaim,
    QuantEvidenceEnvelope,
    _cashflow_metric_claim,
    _typed_metric_claim,
)


def _contribution_solver_evidence(
    result: Mapping[str, Any],
) -> QuantEvidenceEnvelope:
    full_result = (
        result.get("full_result")
        if isinstance(result.get("full_result"), dict)
        else {}
    )
    status = str(full_result.get("status") or "")
    valid_statuses = {
        "bounded_solution",
        "baseline_infeasible",
        "search_ceiling_feasible",
        "baseline_satisfies_target",
        "target_not_reached_within_search_ceiling",
    }
    execution_ok = result.get("ok") is True
    valid = bool(
        execution_ok
        and full_result.get("schema_version")
        == "awm.cashflow_contribution_solver.v1"
        and status in valid_statuses
    )
    claims: List[QuantEvidenceClaim] = []

    def add_claim(
        metric_key: str,
        value: Any,
        unit: str,
        source_path: str,
    ) -> None:
        if _finite_numeric(value) is None:
            return
        claims.append(
            QuantEvidenceClaim(
                metric_key=metric_key,
                value=value,
                unit=unit,
                source_path=source_path,
            )
        )

    selected = (
        full_result.get("selected")
        if isinstance(full_result.get("selected"), dict)
        else {}
    )
    boundary = (
        full_result.get("monthly_boundary")
        if isinstance(full_result.get("monthly_boundary"), dict)
        else {}
    )
    boundary_interpretation = (
        full_result.get("boundary_interpretation")
        if isinstance(full_result.get("boundary_interpretation"), dict)
        else {}
    )
    add_claim(
        "selected_monthly_contribution",
        selected.get("monthly_contribution"),
        "USD_per_month",
        "$.full_result.selected.monthly_contribution",
    )
    add_claim(
        "selected_annual_contribution",
        selected.get("annual_contribution"),
        "USD_per_year",
        "$.full_result.selected.annual_contribution",
    )
    for key in (
        "lower_tested_boundary",
        "upper_tested_boundary",
        "search_tolerance",
    ):
        add_claim(
            key,
            boundary.get(key),
            "USD_per_month",
            f"$.full_result.monthly_boundary.{key}",
        )
    known_feasible_interval = (
        boundary_interpretation.get("known_feasible_monthly_interval")
        if isinstance(
            boundary_interpretation.get("known_feasible_monthly_interval"),
            dict,
        )
        else {}
    )
    for key in ("minimum", "maximum"):
        add_claim(
            f"known_feasible_interval.{key}",
            known_feasible_interval.get(key),
            "USD_per_month",
            (
                "$.full_result.boundary_interpretation."
                f"known_feasible_monthly_interval.{key}"
            ),
        )
    transition_interval = (
        boundary_interpretation.get("transition_interval")
        if isinstance(boundary_interpretation.get("transition_interval"), dict)
        else {}
    )
    for key in ("lower", "upper"):
        add_claim(
            f"transition_interval.{key}",
            transition_interval.get(key),
            "USD_per_month",
            (
                "$.full_result.boundary_interpretation."
                f"transition_interval.{key}"
            ),
        )
    add_claim(
        "target_terminal_value",
        full_result.get("target_terminal_value"),
        "USD",
        "$.full_result.target_terminal_value",
    )
    selected_metrics = (
        selected.get("metrics")
        if isinstance(selected.get("metrics"), dict)
        else {}
    )
    metric_units = {
        "success_probability": "probability_0_to_1",
        "median_shortfall": "USD",
        "p10_minimum_liquidity": "USD",
        "median_terminal_value": "USD",
    }
    for key, unit in metric_units.items():
        add_claim(
            f"selected.{key}",
            selected_metrics.get(key),
            unit,
            f"$.full_result.selected.metrics.{key}",
        )
    arguments = result.get("arguments") if isinstance(result.get("arguments"), dict) else {}
    argument_units = {
        "minimum_success_probability": "probability_0_to_1",
        "minimum_p10_liquidity": "USD",
        "maximum_monthly_contribution": "USD_per_month",
        "monthly_tolerance": "USD_per_month",
        "start_horizon_years": "count",
        "duration_years": "count",
        "monte_carlo_paths": "count",
        "target_terminal_value": "USD",
    }
    for key, unit in argument_units.items():
        add_claim(
            f"input.{key}",
            arguments.get(key),
            unit,
            f"$.arguments.{key}",
        )

    limitations = [
        str(item)
        for item in full_result.get("limitations") or []
        if str(item).strip()
    ]
    constraint_checks = {
        str(item.get("constraint_key") or ""): {
            "passed": False,
            "meaning": str(item.get("label") or ""),
            "source": "binding_tested_point",
        }
        for item in boundary_interpretation.get("binding_failed_constraints")
        or []
        if isinstance(item, dict) and str(item.get("constraint_key") or "").strip()
    }
    return QuantEvidenceEnvelope(
        tool="solve_cashflow_contribution",
        status="complete" if valid else "invalid",
        execution_ok=execution_ok,
        valid_for_reporting=bool(valid and claims),
        valid_for_conclusion=bool(valid and claims),
        valid_for_recommendation=False,
        conclusion_code=status or None,
        claims=claims,
        constraint_checks=constraint_checks,
        warnings=limitations,
        assumptions=[
            "Contribution solver uses the same cash-flow model configuration, "
            "path count, random seed, contribution window, and explicit constraints "
            "for every tested candidate."
        ]
        if valid
        else [],
        errors=[] if valid else ["cashflow_contribution_solver_result_invalid"],
    )


def _cashflow_evidence(result: Mapping[str, Any]) -> QuantEvidenceEnvelope:
    analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else {}
    status = analysis.get("status") if isinstance(analysis.get("status"), dict) else {}
    metrics = analysis.get("metrics") if isinstance(analysis.get("metrics"), dict) else {}
    request = analysis.get("request") if isinstance(analysis.get("request"), dict) else {}
    requested_input = (
        request.get("requested_input") if isinstance(request.get("requested_input"), dict) else {}
    )
    raw_requested_metrics = requested_input.get("requested_metrics")
    requested_metrics = (
        [str(metric) for metric in raw_requested_metrics if isinstance(metric, str) and metric]
        if isinstance(raw_requested_metrics, list)
        else []
    )
    missing_metrics = [str(key) for key in requested_metrics if str(key) not in metrics]
    warnings = _string_list(status.get("warnings"))
    errors: List[str] = []
    if analysis.get("schema_version") != "awm.cashflow_result.v2":
        errors.append("cashflow_result_schema_not_v2")
    requested_metric_count = (
        len(raw_requested_metrics) if isinstance(raw_requested_metrics, list) else -1
    )
    if not requested_metrics or len(requested_metrics) != requested_metric_count:
        errors.append("cashflow_requested_metrics_missing_or_invalid")
    if status.get("execution") != "succeeded":
        errors.append(str(status.get("error") or "cashflow_execution_not_succeeded"))
    if status.get("validation") == "failed":
        errors.append("cashflow_validation_failed")
    if not metrics:
        errors.append("cashflow_metrics_missing")
    if missing_metrics:
        errors.append("requested_metrics_missing:" + ",".join(missing_metrics))
    raw_declared_missing_metrics = status.get("missing_required_metrics")
    if not isinstance(raw_declared_missing_metrics, list):
        errors.append("cashflow_missing_metric_status_invalid")
    declared_missing_metrics = _string_list(raw_declared_missing_metrics)
    if declared_missing_metrics:
        errors.append("requested_metrics_missing:" + ",".join(declared_missing_metrics))
    raw_normalization_errors = status.get("normalization_errors")
    if not isinstance(raw_normalization_errors, list):
        errors.append("cashflow_normalization_status_invalid")
    normalization_errors = _string_list(raw_normalization_errors)
    if normalization_errors:
        errors.append("cashflow_normalization_errors:" + ",".join(normalization_errors))
    claims: List[QuantEvidenceClaim] = []
    for metric_key, payload in metrics.items():
        claim, metric_errors = _cashflow_metric_claim(str(metric_key), payload)
        errors.extend(metric_errors)
        if claim is None:
            continue
        claims.append(claim)
    reporting_blockers = [
        error
        for error in errors
        if not error.startswith("cashflow_recommendation_")
    ]
    execution_ok = status.get("execution") == "succeeded"
    valid_for_reporting = bool(
        execution_ok
        and status.get("validation") not in {"failed", "invalid_request", "missing_data"}
        and claims
        and not reporting_blockers
    )
    explicit_valid = status.get("valid_for_recommendation") is True
    valid_for_conclusion = bool(
        valid_for_reporting
        and (status.get("valid_for_conclusion") is True or explicit_valid)
    )
    valid_for_recommendation = bool(explicit_valid and valid_for_reporting)
    headline = analysis.get("headline") if isinstance(analysis.get("headline"), dict) else {}
    return QuantEvidenceEnvelope(
        tool="run_cashflow_projection",
        status="complete" if valid_for_reporting else ("blocked" if analysis else "invalid"),
        execution_ok=execution_ok,
        valid_for_reporting=valid_for_reporting,
        valid_for_conclusion=valid_for_conclusion,
        valid_for_recommendation=valid_for_recommendation,
        conclusion_code=(
            str(headline.get("conclusion"))
            if valid_for_recommendation and headline.get("conclusion")
            else None
        ),
        claims=claims,
        warnings=warnings,
        assumptions=_string_list(analysis.get("assumptions")),
        errors=errors,
    )


def _cashflow_audit_evidence(
    result: Mapping[str, Any],
) -> QuantEvidenceEnvelope:
    """Expose a stored-snapshot audit as reporting-only typed evidence."""

    full_result = (
        result.get("full_result")
        if isinstance(result.get("full_result"), dict)
        else {}
    )
    errors: List[str] = []
    if full_result.get("schema_version") != "awm.cashflow_analysis_audit.v1":
        errors.append("cashflow_audit_schema_invalid")
    if full_result.get("audit_status") not in {"passed", "failed", "limited"}:
        errors.append("cashflow_audit_status_invalid")
    metrics = (
        full_result.get("metrics")
        if isinstance(full_result.get("metrics"), dict)
        else {}
    )
    claims: List[QuantEvidenceClaim] = []
    for metric_key, payload in metrics.items():
        claim, metric_errors = _typed_metric_claim(
            str(metric_key),
            payload,
            namespace="cashflow_audit",
        )
        errors.extend(metric_errors)
        if claim is not None:
            claims.append(claim)
    if not claims:
        errors.append("cashflow_audit_metrics_missing")

    checks = (
        full_result.get("checks")
        if isinstance(full_result.get("checks"), list)
        else []
    )
    constraint_checks = {
        str(item.get("check_id")): {
            "passed": item.get("status") == "passed",
            "status": item.get("status"),
            "meaning": item.get("meaning"),
            "evidence_paths": item.get("evidence_paths") or [],
        }
        for item in checks
        if isinstance(item, dict) and str(item.get("check_id") or "").strip()
    }
    execution_ok = result.get("ok") is True
    valid = bool(execution_ok and claims and not errors)
    calculation_policy = str(full_result.get("calculation_policy") or "").strip()
    return QuantEvidenceEnvelope(
        tool="audit_cashflow_analysis",
        status="complete" if valid else "invalid",
        execution_ok=execution_ok,
        valid_for_reporting=valid,
        valid_for_conclusion=False,
        valid_for_recommendation=False,
        claims=claims,
        constraint_checks=constraint_checks,
        warnings=_string_list(full_result.get("limitations")),
        assumptions=[calculation_policy] if calculation_policy else [],
        errors=errors,
    )
