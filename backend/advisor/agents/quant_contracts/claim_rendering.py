from __future__ import annotations

from typing import Any, Dict, List, Optional

from advisor.agents.quant_contracts._shared import _finite_numeric
from advisor.agents.quant_contracts.constants import STRICT_QUANT_TOOL_NAMES
from advisor.agents.quant_contracts.models import QuantEvidenceClaim, QuantEvidenceEnvelope


def _render_evidence_claims(
    envelope: QuantEvidenceEnvelope,
    *,
    deterministic_single_path: bool = False,
) -> List[str]:
    preferred: Dict[str, tuple[str, ...]] = {
        "run_cashflow_projection": (
            "success_probability",
            "projected_terminal_value",
            "terminal_value_percentiles",
            "shortfall",
            "shortfall_percentiles",
            "reserve_breach_probability",
            "first_depletion_year_distribution",
            "first_shortfall_year_distribution",
            "minimum_liquidity",
        ),
        "run_asset_allocation": (
            "portfolio_expected_return_annual_decimal",
            "portfolio_expected_volatility_annual_decimal",
            "target_volatility_annual_decimal",
            "target_volatility_difference_bps",
            "target_volatility_tolerance_bps",
            "target_volatility_passed",
            "total_investment",
            "category_weight.equity",
            "category_weight.defensive_fixed_income",
            "category_weight.growth_fixed_income",
            "category_weight.real_assets_and_alternatives",
            "category_weight.cash",
            "active_sleeve_weight",
            "passive_sleeve_weight",
            "security_count",
            "security_dollar_sum",
        ),
        "solve_cashflow_contribution": (
            "selected_monthly_contribution",
            "selected_annual_contribution",
            "known_feasible_interval.minimum",
            "known_feasible_interval.maximum",
            "transition_interval.lower",
            "transition_interval.upper",
            "lower_tested_boundary",
            "upper_tested_boundary",
            "search_tolerance",
            "selected.success_probability",
            "selected.median_shortfall",
            "selected.p10_minimum_liquidity",
            "selected.median_terminal_value",
            "target_terminal_value",
        ),
        "analyze_portfolio_risk": (
            "drawdown.maximum_drawdown_percentiles.p10",
            "drawdown.maximum_drawdown_percentiles.p50",
            "drawdown.maximum_drawdown_percentiles.p90",
            "drawdown.probability_exceeds.10_percent",
            "drawdown.probability_exceeds.20_percent",
            "drawdown.probability_exceeds.30_percent",
            "drawdown.configuration.horizon_years",
            "drawdown.configuration.num_simulations",
            "drawdown.configuration.seed",
            "fee_drag.annual_fee_bps",
            "fee_drag.horizon_years",
            "fee_drag.initial_investment",
            "fee_drag.gross_expected_return_annual_decimal",
            "fee_drag.net_expected_return_annual_decimal",
            "fee_drag.gross_terminal_value",
            "fee_drag.net_terminal_value_after_fee",
            "fee_drag.cumulative_fee_drag",
            "fee_drag.cumulative_fee_drag_as_share_of_gross_terminal",
        ),
        "audit_cashflow_analysis": (
            "checks_passed",
            "checks_failed",
            "checks_not_tested",
            "path_count",
            "series_row_count",
            "series_start_year",
            "series_end_year",
            "stored_terminal_net_worth",
            "recomputed_terminal_net_worth",
            "terminal_net_worth_difference",
            "stored_terminal_shortfall",
            "recomputed_terminal_shortfall",
            "terminal_shortfall_difference",
            "stored_success_value",
            "recomputed_success_value",
            "stored_first_net_worth_depletion_year",
            "recomputed_first_net_worth_depletion_year",
            "stored_first_cashflow_shortfall_year",
            "recomputed_first_cashflow_shortfall_year",
        ),
        "calculate_cashflow_metrics": (
            "comparison.signed_difference",
            "comparison.absolute_difference",
            "calculation_result",
            "primary_operand",
            "secondary_operand",
        ),
        "calculate_financial_math": (
            "comparison.signed_difference",
            "comparison.absolute_difference",
            "calculation_result",
            "input.primary_value",
            "input.secondary_value",
            "input.annual_rate_decimal",
            "input.periods",
            "input.payments_per_year",
        ),
    }
    order = preferred.get(envelope.tool, ())
    priority = {key: index for index, key in enumerate(order)}
    claims = sorted(
        envelope.claims,
        key=lambda claim: (
            priority.get(claim.metric_key, len(priority)),
            claim.metric_key,
        ),
    )
    rendered: List[str] = []
    metric_keys = {claim.metric_key for claim in claims}
    for claim in claims:
        if claim.metric_key == "ending_balance" and "projected_terminal_value" in metric_keys:
            continue
        if (
            not deterministic_single_path
            and claim.metric_key == "projected_terminal_value"
            and "terminal_value_percentiles" in metric_keys
        ):
            continue
        if (
            not deterministic_single_path
            and claim.metric_key == "shortfall"
            and "shortfall_percentiles" in metric_keys
        ):
            continue
        if deterministic_single_path and claim.metric_key in {
            "terminal_value_percentiles",
            "shortfall_percentiles",
            "reserve_breach_probability",
        }:
            continue
        item = _render_evidence_claim(
            envelope.tool,
            claim,
            deterministic_single_path=deterministic_single_path,
        )
        if item:
            rendered.append(item)
        claim_limit = (
            18
            if envelope.tool == "analyze_portfolio_risk"
            else 16
            if envelope.tool == "run_asset_allocation"
            else 10
        )
        if len(rendered) >= claim_limit:
            break
    return rendered


def _render_evidence_claim(
    tool_name: str,
    claim: QuantEvidenceClaim,
    *,
    deterministic_single_path: bool = False,
) -> Optional[str]:
    labels = {
        "success_probability": "Success probability",
        "projected_terminal_value": "Projected terminal value",
        "terminal_value_percentiles": "Terminal net worth percentiles",
        "ending_balance": "Ending balance",
        "shortfall": "Projected shortfall",
        "shortfall_percentiles": "Terminal cash-flow shortfall percentiles",
        "reserve_breach_probability": "Reserve-breach probability",
        "first_depletion_year_distribution": "First depletion event distribution",
        "first_shortfall_year_distribution": "First shortfall event distribution",
        "minimum_liquidity": "Minimum liquidity",
        "portfolio_expected_return_annual_decimal": "Expected annual return",
        "portfolio_expected_volatility_annual_decimal": "Expected annual volatility",
        "target_volatility_annual_decimal": "Signed target volatility",
        "target_volatility_difference_bps": "Distance from signed volatility target",
        "target_volatility_tolerance_bps": "Signed volatility tolerance",
        "target_volatility_passed": "Signed volatility tolerance check passed",
        "active_risk_percentage": "Signed active-risk setting",
        "passive_risk_percentage": "Signed passive-risk setting",
        "active_sleeve_weight": "Active-security sleeve weight",
        "passive_sleeve_weight": "Passive-security sleeve weight",
        "security_count": "Security count",
        "asset_class_count": "Asset-class count",
        "security_weight_sum": "Reconciled security-weight sum",
        "security_dollar_sum": "Reconciled security-dollar sum",
        "total_investment": "Total investment",
        "excluded_asset_classes": "Excluded asset classes",
        "selected_monthly_contribution": "Selected bounded monthly contribution",
        "selected_annual_contribution": "Annualized contribution used by LifeModel",
        "known_feasible_interval.minimum": "Known feasible interval minimum",
        "known_feasible_interval.maximum": "Known feasible interval maximum",
        "transition_interval.lower": "Transition interval lower point",
        "transition_interval.upper": "Transition interval upper point",
        "lower_tested_boundary": "Lower tested monthly boundary",
        "upper_tested_boundary": "Upper tested monthly boundary",
        "search_tolerance": "Monthly search tolerance",
        "target_terminal_value": "Target median terminal value",
        "selected.success_probability": "Selected scenario success probability",
        "selected.median_shortfall": "Selected scenario median shortfall",
        "selected.p10_minimum_liquidity": "Selected scenario p10 minimum liquidity",
        "selected.median_terminal_value": "Selected scenario median terminal value",
        "input.maximum_monthly_contribution": "Monthly search ceiling",
        "input.minimum_success_probability": "Minimum success-probability constraint",
        "input.minimum_p10_liquidity": "Minimum p10-liquidity constraint",
        "input.monthly_tolerance": "Requested monthly search tolerance",
        "input.monte_carlo_paths": "Monte Carlo paths per tested candidate",
        "input.duration_years": "Contribution duration",
        "checks_passed": "Audit checks passed",
        "checks_failed": "Audit checks failed",
        "checks_not_tested": "Audit checks not tested",
        "path_count": "Modeled path count",
        "series_row_count": "Stored annual rows",
        "series_start_year": "Stored series start year",
        "series_end_year": "Stored series end year",
        "stored_terminal_net_worth": "Stored terminal net worth",
        "recomputed_terminal_net_worth": "Recomputed terminal net worth",
        "terminal_net_worth_difference": "Terminal net-worth reconciliation difference",
        "stored_terminal_shortfall": "Stored terminal cash-flow shortfall",
        "recomputed_terminal_shortfall": "Recomputed terminal cash-flow shortfall",
        "terminal_shortfall_difference": "Terminal shortfall reconciliation difference",
        "stored_success_value": "Stored success value",
        "recomputed_success_value": "Recomputed success value",
        "stored_first_net_worth_depletion_year": "Stored first net-worth depletion year",
        "recomputed_first_net_worth_depletion_year": "Recomputed first net-worth depletion year",
        "stored_first_cashflow_shortfall_year": "Stored first cash-flow shortfall year",
        "recomputed_first_cashflow_shortfall_year": "Recomputed first cash-flow shortfall year",
        "calculation_result": "Calculated result",
        "comparison.signed_difference": "Signed difference (comparison minus source)",
        "comparison.absolute_difference": "Absolute difference",
        "primary_operand": "Source value",
        "secondary_operand": "Comparison value",
    }
    semantic_keys = {
        claim.claim_id,
        claim.metric_key,
        *claim.semantic_metric_keys,
    }
    is_concentration = any("concentration" in key for key in semantic_keys)
    label = "Employer-stock concentration" if is_concentration else labels.get(claim.metric_key)
    if claim.metric_key.startswith("asset_weight."):
        label = f"{claim.metric_key.removeprefix('asset_weight.')} weight"
    elif claim.metric_key.startswith("category_weight."):
        category = (
            claim.metric_key.removeprefix("category_weight.")
            .replace("_", " ")
            .title()
        )
        label = f"{category} category weight"
    elif claim.metric_key.startswith("security."):
        label = claim.metric_key.replace("security.", "Security ").replace(".", " ")
    elif claim.metric_key.startswith("drawdown.probability_exceeds."):
        threshold = (
            claim.metric_key.rsplit(".", 1)[-1]
            .replace("_percent", "%")
            .replace("_", " ")
        )
        label = f"Probability maximum drawdown exceeds {threshold}"
    elif claim.metric_key.startswith("drawdown.threshold_magnitude."):
        label = "Configured maximum-drawdown magnitude threshold"
    elif claim.metric_key.startswith("drawdown.threshold_loss_return."):
        label = "Equivalent loss-return threshold"
    elif label is None and tool_name not in STRICT_QUANT_TOOL_NAMES:
        label = claim.metric_key.replace("_", " ").replace(".", " ").strip().capitalize()
    if not label:
        return None

    reference = f"[evidence: {tool_name}/{claim.claim_id}]"
    if deterministic_single_path and claim.metric_key == "success_probability":
        value = _finite_numeric(claim.value)
        if value is None:
            return None
        outcome = "passed" if value >= 1.0 else "did not pass"
        return (
            f"Baseline path result: {outcome} the model's net-worth success rule "
            f"{reference}"
        )
    if (
        deterministic_single_path
        and claim.metric_key
        in {
            "first_depletion_year_distribution",
            "first_shortfall_year_distribution",
        }
        and isinstance(claim.value, dict)
    ):
        probabilities = claim.value.get("probability_by_year")
        nonzero_years = (
            sorted(
                str(year)
                for year, probability in probabilities.items()
                if _finite_numeric(probability) is not None
                and _finite_numeric(probability) > 0
            )
            if isinstance(probabilities, dict)
            else []
        )
        event_label = (
            "First modeled net-worth depletion year"
            if claim.metric_key == "first_depletion_year_distribution"
            else "First technically nonzero modeled cash-flow shortfall year"
        )
        if nonzero_years:
            return f"{event_label}: {nonzero_years[0]} {reference}"
        probability_never = _finite_numeric(claim.value.get("probability_never"))
        if probability_never == 1.0:
            return f"{event_label}: no event within the modeled horizon {reference}"
        return None

    canonical_money_unit = str(claim.unit or "").startswith(
        ("money:USD", "money_per_month:USD", "money_per_year:USD")
    )
    if is_concentration:
        concentration = _finite_numeric(claim.value_decimal or claim.value)
        display = f"{concentration * 100:.2f}%" if concentration is not None else None
    else:
        display = (
            _format_claim_value(claim.value_decimal or claim.value, claim.unit)
            if canonical_money_unit
            else claim.display_value or _format_claim_value(claim.value, claim.unit)
        )
    if display is None:
        return None
    return f"{label}: {display} {reference}"


def _format_claim_value(value: Any, unit: str) -> Optional[str]:
    number = _finite_numeric(value)
    if number is not None:
        if unit in {
            "USD",
            "USD_per_month",
            "USD_per_year",
            "money:USD",
            "money_per_month:USD",
            "money_per_year:USD",
        }:
            sign = "-" if number < 0 else ""
            suffix = (
                "/month"
                if unit in {"USD_per_month", "money_per_month:USD"}
                else "/year"
                if unit in {"USD_per_year", "money_per_year:USD"}
                else ""
            )
            return f"{sign}${abs(number):,.2f}{suffix}"
        if unit in {
            "probability_0_to_1",
            "annual_decimal_0_to_1",
            "annual_decimal",
            "decimal_change",
            "decimal_0_to_1",
            "drawdown_decimal_0_to_1",
            "drawdown_threshold_decimal_0_to_1",
            "one_period_return_decimal",
            "share_of_total_variance",
            "weight_0_to_1",
        }:
            return f"{number * 100:.2f}%"
        if unit in {"basis_points", "basis_points_per_year"}:
            return f"{number:,.1f} bps"
        if unit == "count":
            return f"{number:,.0f}"
        if unit == "calendar_year" and number.is_integer():
            return str(int(number))
        return f"{number:,.6g}"
    if unit == "boolean" and isinstance(value, bool):
        return "yes" if value else "no"
    if unit == "asset_class_list" and isinstance(value, list):
        labels = [str(item).strip() for item in value if str(item).strip()]
        return ", ".join(labels) if labels else None
    if unit == "USD" and isinstance(value, dict):
        percentile_parts = []
        for key in ("p10", "p50", "p90"):
            percentile = _finite_numeric(value.get(key))
            if percentile is None:
                continue
            sign = "-" if percentile < 0 else ""
            percentile_parts.append(f"{key} {sign}${abs(percentile):,.2f}")
        return "; ".join(percentile_parts) if percentile_parts else None
    if unit == "probability_by_calendar_year" and isinstance(value, dict):
        probability_by_year = value.get("probability_by_year")
        if not isinstance(probability_by_year, dict):
            return None
        year_parts: List[str] = []
        ordered_years = sorted(
            (
                (str(year), normalized)
                for year, probability in probability_by_year.items()
                if (normalized := _finite_numeric(probability)) is not None
            ),
            key=lambda item: item[0],
        )
        nonzero_years = [item for item in ordered_years if item[1] > 0.0]
        if len(nonzero_years) == 1:
            year, probability = nonzero_years[0]
            year_parts.append(f"first event: {year} at {probability * 100:.2f}%")
        elif nonzero_years:
            earliest_year = nonzero_years[0][0]
            latest_year = nonzero_years[-1][0]
            peak_year, peak_probability = max(
                nonzero_years,
                key=lambda item: item[1],
            )
            year_parts.append(
                f"nonzero first-event years span {earliest_year} to {latest_year}"
            )
            year_parts.append(
                f"peak first-event year: {peak_year} at {peak_probability * 100:.2f}%"
            )
        else:
            year_parts.append("no first event in the modeled paths")
        probability_never = _finite_numeric(value.get("probability_never"))
        if probability_never is not None:
            year_parts.append(f"never: {probability_never * 100:.2f}%")
        sample_count = _finite_numeric(value.get("sample_count"))
        if sample_count is not None:
            year_parts.append(f"sample count: {sample_count:,.0f}")
        return "; ".join(year_parts) if year_parts else None
    return None
