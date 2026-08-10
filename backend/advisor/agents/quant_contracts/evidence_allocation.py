from __future__ import annotations

import math
from typing import Any, List, Mapping, Optional, Sequence

from advisor.agents.quant_contracts._shared import _finite_numeric, _string_list
from advisor.agents.quant_contracts.constants import REQUIRED_ALLOCATION_CONSTRAINT_CHECKS
from advisor.agents.quant_contracts.models import (
    QuantEvidenceClaim,
    QuantEvidenceEnvelope,
    _typed_metric_claim,
)


def _portfolio_risk_evidence(
    result: Mapping[str, Any],
) -> QuantEvidenceEnvelope:
    """Validate component-risk and one-period stress output as typed evidence."""

    full_result = (
        result.get("full_result")
        if isinstance(result.get("full_result"), dict)
        else {}
    )
    execution_ok = result.get("ok") is True
    errors: List[str] = []
    claims: List[QuantEvidenceClaim] = []
    if full_result.get("schema_version") != "awm.portfolio_risk_analysis.v1":
        errors.append("portfolio_risk_schema_invalid")

    def add_claim(
        metric_key: str,
        value: Any,
        unit: str,
        source_path: str,
        *,
        semantic_metric_keys: Sequence[str] = (),
    ) -> bool:
        numeric = _finite_numeric(value)
        if numeric is None:
            return False
        claims.append(
            QuantEvidenceClaim(
                metric_key=metric_key,
                value=numeric,
                unit=unit,
                source_path=source_path,
                semantic_metric_keys=list(semantic_metric_keys),
            )
        )
        return True

    for key in (
        "expected_return_annual_decimal",
        "expected_volatility_annual_decimal",
    ):
        if not add_claim(
            key,
            full_result.get(key),
            "annual_decimal_0_to_1",
            f"$.full_result.{key}",
        ):
            errors.append(f"{key}_invalid")

    contributions = full_result.get("risk_contributions")
    contribution_percentages: List[float] = []
    if not isinstance(contributions, list) or not contributions:
        errors.append("risk_contributions_missing")
        contributions = []
    for index, row in enumerate(contributions):
        if not isinstance(row, dict) or not str(row.get("asset_class") or "").strip():
            errors.append(f"risk_contribution_{index}_invalid")
            continue
        prefix = f"risk_contribution.{index}"
        path = f"$.full_result.risk_contributions[{index}]"
        fields = {
            "weight": "weight_0_to_1",
            "marginal_volatility": "annual_decimal",
            "component_volatility": "annual_decimal",
            "percentage_of_total_variance": "share_of_total_variance",
        }
        for field, unit in fields.items():
            if not add_claim(
                f"{prefix}.{field}",
                row.get(field),
                unit,
                f"{path}.{field}",
                semantic_metric_keys=(
                    (
                        "portfolio_risk",
                        "portfolio_variance",
                        "portfolio_volatility",
                        "risk_contribution",
                    )
                    if field == "percentage_of_total_variance"
                    else ()
                ),
            ):
                errors.append(f"risk_contribution_{index}_{field}_invalid")
        percentage = _finite_numeric(row.get("percentage_of_total_variance"))
        if percentage is not None:
            contribution_percentages.append(percentage)
    if contribution_percentages and not math.isclose(
        sum(contribution_percentages),
        1.0,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        errors.append("risk_contribution_reconciliation_failed")

    stresses = full_result.get("stress_scenarios")
    if not isinstance(stresses, list) or not stresses:
        errors.append("stress_scenarios_missing")
        stresses = []
    for index, row in enumerate(stresses):
        if not isinstance(row, dict) or not str(row.get("name") or "").strip():
            errors.append(f"stress_scenario_{index}_invalid")
            continue
        if not add_claim(
            f"stress_scenario.{index}.portfolio_return",
            row.get("portfolio_return"),
            "one_period_return_decimal",
            f"$.full_result.stress_scenarios[{index}].portfolio_return",
        ):
            errors.append(f"stress_scenario_{index}_portfolio_return_invalid")

    drawdown = full_result.get("drawdown_analysis")
    if drawdown is not None:
        if not isinstance(drawdown, dict) or drawdown.get("type") != (
            "synthetic_maximum_drawdown_distribution"
        ):
            errors.append("drawdown_analysis_invalid")
        else:
            percentiles = drawdown.get("maximum_drawdown_percentiles")
            probabilities = drawdown.get("probability_maximum_drawdown_exceeds")
            configuration = drawdown.get("configuration")
            if not isinstance(percentiles, dict):
                errors.append("drawdown_percentiles_missing")
                percentiles = {}
            if not isinstance(probabilities, dict):
                errors.append("drawdown_probabilities_missing")
                probabilities = {}
            if not isinstance(configuration, dict):
                errors.append("drawdown_configuration_missing")
                configuration = {}
            prior = -1.0
            for percentile in ("p10", "p50", "p90"):
                value = _finite_numeric(percentiles.get(percentile))
                if value is None or not 0.0 <= value <= 1.0 or value < prior:
                    errors.append(f"drawdown_percentile_{percentile}_invalid")
                    continue
                prior = value
                add_claim(
                    f"drawdown.maximum_drawdown_percentiles.{percentile}",
                    value,
                    "drawdown_decimal_0_to_1",
                    (
                        "$.full_result.drawdown_analysis."
                        f"maximum_drawdown_percentiles.{percentile}"
                    ),
                )
            previous_probability = 1.0
            for threshold in ("10_percent", "20_percent", "30_percent"):
                value = _finite_numeric(probabilities.get(threshold))
                if (
                    value is None
                    or not 0.0 <= value <= 1.0
                    or value > previous_probability
                ):
                    errors.append(f"drawdown_probability_{threshold}_invalid")
                    continue
                previous_probability = value
                add_claim(
                    f"drawdown.probability_exceeds.{threshold}",
                    value,
                    "probability_0_to_1",
                    (
                        "$.full_result.drawdown_analysis."
                        f"probability_maximum_drawdown_exceeds.{threshold}"
                    ),
                )
                threshold_value = float(threshold.split("_", 1)[0]) / 100.0
                add_claim(
                    f"drawdown.threshold_magnitude.{threshold}",
                    threshold_value,
                    "drawdown_threshold_decimal_0_to_1",
                    (
                        "$.full_result.drawdown_analysis."
                        f"probability_maximum_drawdown_exceeds.{threshold}#key"
                    ),
                )
                add_claim(
                    f"drawdown.threshold_loss_return.{threshold}",
                    -threshold_value,
                    "one_period_return_decimal",
                    (
                        "$.full_result.drawdown_analysis."
                        f"probability_maximum_drawdown_exceeds.{threshold}#key"
                    ),
                )
            for field in ("horizon_years", "num_simulations", "seed"):
                if not add_claim(
                    f"drawdown.configuration.{field}",
                    configuration.get(field),
                    "count",
                    f"$.full_result.drawdown_analysis.configuration.{field}",
                ):
                    errors.append(f"drawdown_configuration_{field}_invalid")

    fee_drag = full_result.get("fee_drag_analysis")
    if fee_drag is not None:
        if not isinstance(fee_drag, dict) or fee_drag.get("type") != (
            "blended_annual_fee_scenario"
        ):
            errors.append("fee_drag_analysis_invalid")
        else:
            fee_fields = {
                "annual_fee_bps": "basis_points_per_year",
                "horizon_years": "years",
                "initial_investment": "USD",
                "gross_expected_return_annual_decimal": "annual_decimal",
                "net_expected_return_annual_decimal": "annual_decimal",
                "gross_terminal_value": "USD",
                "net_terminal_value_after_fee": "USD",
                "cumulative_fee_drag": "USD",
                "cumulative_fee_drag_as_share_of_gross_terminal": "decimal_0_to_1",
            }
            for field, unit in fee_fields.items():
                if not add_claim(
                    f"fee_drag.{field}",
                    fee_drag.get(field),
                    unit,
                    f"$.full_result.fee_drag_analysis.{field}",
                ):
                    errors.append(f"fee_drag_{field}_invalid")

    valid = bool(execution_ok and claims and not errors)
    return QuantEvidenceEnvelope(
        tool="analyze_portfolio_risk",
        status="complete" if valid else "invalid",
        execution_ok=execution_ok,
        valid_for_reporting=valid,
        valid_for_conclusion=valid,
        valid_for_recommendation=False,
        conclusion_code="portfolio_risk_analysis_complete" if valid else None,
        claims=claims,
        warnings=_string_list(full_result.get("limitations")),
        assumptions=[
            str(value)
            for value in (
                (full_result.get("methodology") or {}).get("risk_contribution"),
                (full_result.get("methodology") or {}).get("stress"),
                (full_result.get("methodology") or {}).get("drawdown"),
                (full_result.get("methodology") or {}).get("fee_drag"),
            )
            if value
        ],
        errors=errors,
    )


def _asset_location_evidence(
    result: Mapping[str, Any],
) -> QuantEvidenceEnvelope:
    """Validate capacity-constrained asset-location output as typed evidence."""

    full_result = (
        result.get("full_result")
        if isinstance(result.get("full_result"), dict)
        else {}
    )
    execution_ok = result.get("ok") is True
    errors: List[str] = []
    claims: List[QuantEvidenceClaim] = []
    if full_result.get("schema_version") != "awm.asset_location_analysis.v1":
        errors.append("asset_location_schema_invalid")

    def add_claim(
        metric_key: str,
        value: Any,
        unit: str,
        source_path: str,
    ) -> bool:
        numeric = _finite_numeric(value)
        if numeric is None:
            return False
        claims.append(
            QuantEvidenceClaim(
                metric_key=metric_key,
                value=numeric,
                unit=unit,
                source_path=source_path,
            )
        )
        return True

    capacity = (
        full_result.get("account_capacity")
        if isinstance(full_result.get("account_capacity"), dict)
        else {}
    )
    for field in ("taxable_brokerage", "retirement"):
        if not add_claim(
            f"account_capacity.{field}",
            capacity.get(field),
            "USD",
            f"$.full_result.account_capacity.{field}",
        ):
            errors.append(f"asset_location_{field}_invalid")

    placements = full_result.get("placements")
    if not isinstance(placements, list) or not placements:
        errors.append("asset_location_placements_missing")
        placements = []
    for index, row in enumerate(placements):
        if not isinstance(row, dict) or not str(row.get("asset_class") or "").strip():
            errors.append(f"asset_location_placement_{index}_invalid")
            continue
        path = f"$.full_result.placements[{index}]"
        fields = {
            "portfolio_weight": "weight_0_to_1",
            "target_amount": "USD",
            "retirement_amount": "USD",
            "taxable_amount": "USD",
            "tax_inefficiency_score": "ordinal_score",
        }
        for field, unit in fields.items():
            if not add_claim(
                f"placement.{index}.{field}",
                row.get(field),
                unit,
                f"{path}.{field}",
            ):
                errors.append(f"asset_location_placement_{index}_{field}_invalid")

    reconciliation = (
        full_result.get("reconciliation")
        if isinstance(full_result.get("reconciliation"), dict)
        else {}
    )
    for field in (
        "target_total",
        "taxable_total",
        "retirement_total",
        "difference",
    ):
        if not add_claim(
            f"reconciliation.{field}",
            reconciliation.get(field),
            "USD",
            f"$.full_result.reconciliation.{field}",
        ):
            errors.append(f"asset_location_reconciliation_{field}_invalid")
    difference = _finite_numeric(reconciliation.get("difference"))
    if difference is None or abs(difference) > 0.01:
        errors.append("asset_location_reconciliation_failed")

    valid = bool(execution_ok and claims and not errors)
    methodology_version = str(
        full_result.get("methodology_version") or ""
    ).strip()
    return QuantEvidenceEnvelope(
        tool="analyze_asset_location",
        status="complete" if valid else "invalid",
        execution_ok=execution_ok,
        valid_for_reporting=valid,
        valid_for_conclusion=valid,
        valid_for_recommendation=False,
        conclusion_code="asset_location_analysis_complete" if valid else None,
        claims=claims,
        warnings=_string_list(full_result.get("limitations")),
        assumptions=(
            [f"Methodology version: {methodology_version}"]
            if methodology_version
            else []
        ),
        errors=errors,
    )


def _allocation_evidence(result: Mapping[str, Any]) -> QuantEvidenceEnvelope:
    full_result = result.get("full_result") if isinstance(result.get("full_result"), dict) else {}
    status = result.get("status") if isinstance(result.get("status"), dict) else {}
    if not status and isinstance(full_result.get("status"), dict):
        status = full_result["status"]
    checks = (
        result.get("constraint_checks")
        if isinstance(result.get("constraint_checks"), dict)
        else full_result.get("constraint_checks")
    )
    checks = checks if isinstance(checks, dict) else {}
    normalized_checks = {
        str(name): dict(payload)
        for name, payload in checks.items()
        if isinstance(payload, dict)
    }
    warnings = _string_list(result.get("warnings")) or _string_list(full_result.get("warnings"))
    errors: List[str] = []
    missing_checks = sorted(REQUIRED_ALLOCATION_CONSTRAINT_CHECKS - set(normalized_checks))
    failed_checks = sorted(
        name for name, payload in normalized_checks.items() if payload.get("passed") is not True
    )
    if status.get("execution") != "succeeded":
        errors.append(str(status.get("error") or "allocation_execution_not_succeeded"))
    if missing_checks:
        errors.append("allocation_constraint_checks_missing:" + ",".join(missing_checks))
    if failed_checks:
        errors.append("allocation_constraint_checks_failed:" + ",".join(failed_checks))
    explicit_valid = (
        result.get("valid_for_recommendation") is True
        or status.get("valid_for_recommendation") is True
    )
    claims: List[QuantEvidenceClaim] = []
    for canonical_key, legacy_key in (
        ("portfolio_expected_return_annual_decimal", "portfolio_expected_return_pct"),
        ("portfolio_expected_volatility_annual_decimal", "portfolio_expected_volatility_pct"),
    ):
        value, source_key = _allocation_annual_decimal(full_result, canonical_key, legacy_key)
        if value is None or source_key is None:
            errors.append(f"allocation_portfolio_metric_invalid:{canonical_key}")
            continue
        claims.append(
            QuantEvidenceClaim(
                metric_key=canonical_key,
                value=value,
                unit="annual_decimal_0_to_1",
                source_path=f"$.full_result.{source_key}",
            )
        )
    total_investment = _finite_numeric(full_result.get("total_investment"))
    if total_investment is not None and total_investment > 0:
        claims.append(
            QuantEvidenceClaim(
                metric_key="total_investment",
                value=total_investment,
                unit="USD",
                source_path="$.full_result.total_investment",
            )
        )
    else:
        errors.append("allocation_total_investment_invalid")
    layers = full_result.get("layers") if isinstance(full_result.get("layers"), dict) else {}
    layer1 = layers.get("layer1") if isinstance(layers.get("layer1"), dict) else {}
    weights = layer1.get("selected_weights") if isinstance(layer1.get("selected_weights"), dict) else {}
    for asset_class, weight in weights.items():
        claims.append(
            QuantEvidenceClaim(
                metric_key=f"asset_weight.{asset_class}",
                value=weight,
                unit="weight_0_to_1",
                source_path=f"$.full_result.layers.layer1.selected_weights.{asset_class}",
            )
        )
    securities = full_result.get("securities") if isinstance(full_result.get("securities"), list) else []
    for index, security in enumerate(securities):
        if not isinstance(security, dict):
            continue
        label = str(security.get("ticker") or security.get("isin") or index)
        for key, unit in (("weight", "weight_0_to_1"), ("amount", "USD")):
            if security.get(key) is not None:
                claims.append(
                    QuantEvidenceClaim(
                        metric_key=f"security.{label}.{key}",
                        value=security[key],
                        unit=unit,
                        source_path=f"$.full_result.securities[{index}].{key}",
                    )
                )
    agent_view = (
        result.get("asset_allocation_agent_view")
        if isinstance(result.get("asset_allocation_agent_view"), dict)
        else {}
    )
    typed_metrics = (
        agent_view.get("typed_metrics")
        if isinstance(agent_view.get("typed_metrics"), dict)
        else {}
    )
    for metric_key, payload in typed_metrics.items():
        claim, metric_errors = _typed_metric_claim(
            str(metric_key),
            payload,
            namespace="asset_allocation_agent_view",
        )
        errors.extend(metric_errors)
        if claim is not None and all(
            existing.metric_key != claim.metric_key for existing in claims
        ):
            claims.append(claim)
    exclusions = full_result.get("excluded_asset_classes")
    if isinstance(exclusions, list):
        claims.append(
            QuantEvidenceClaim(
                metric_key="excluded_asset_classes",
                value=exclusions,
                unit="asset_class_list",
                source_path="$.full_result.excluded_asset_classes",
            )
        )
    execution_ok = status.get("execution") == "succeeded"
    valid_for_reporting = bool(execution_ok and claims and not errors)
    valid_for_conclusion = bool(
        valid_for_reporting
        and (status.get("valid_for_conclusion") is True or explicit_valid)
    )
    valid_for_recommendation = bool(valid_for_reporting and explicit_valid)
    return QuantEvidenceEnvelope(
        tool="run_asset_allocation",
        status=(
            "complete"
            if valid_for_reporting
            else ("blocked" if full_result or status else "invalid")
        ),
        execution_ok=execution_ok,
        valid_for_reporting=valid_for_reporting,
        valid_for_conclusion=valid_for_conclusion,
        valid_for_recommendation=valid_for_recommendation,
        conclusion_code="allocation_constraints_verified" if valid_for_conclusion else None,
        claims=claims,
        constraint_checks=normalized_checks,
        warnings=warnings,
        assumptions=_string_list(result.get("assumptions")) or _string_list(full_result.get("assumptions")),
        errors=errors,
    )


def _allocation_annual_decimal(
    result: Mapping[str, Any],
    canonical_key: str,
    legacy_key: str,
) -> tuple[Optional[float], Optional[str]]:
    if canonical_key in result:
        canonical = _finite_numeric(result.get(canonical_key))
        if canonical is None or not 0.0 <= canonical <= 1.0:
            return None, None
        return canonical, canonical_key
    legacy = _finite_numeric(result.get(legacy_key))
    if legacy is None or legacy < 0:
        return None, None
    normalized = legacy / 100.0 if legacy > 1.0 else legacy
    if normalized > 1.0:
        return None, None
    return normalized, legacy_key
