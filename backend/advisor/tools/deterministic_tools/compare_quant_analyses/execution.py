"""Pure comparison logic for immutable quantitative analyses."""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, Mapping, Optional


_DEFAULT_CASHFLOW_METRICS = {
    "success_probability",
    "projected_terminal_value",
    "terminal_value_percentiles",
    "shortfall",
    "shortfall_percentiles",
    "minimum_liquidity",
}
_DEFAULT_ALLOCATION_METRICS = {
    "portfolio_expected_return_annual_decimal",
    "portfolio_expected_volatility_annual_decimal",
    "target_volatility_annual_decimal",
    "target_volatility_difference_bps",
    "target_volatility_tolerance_bps",
    "total_investment",
    "active_sleeve_weight",
    "passive_sleeve_weight",
}


def compare_quant_analysis_results(
    *,
    domain: str,
    base: Mapping[str, Any],
    comparison: Mapping[str, Any],
    metric_keys: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Return unit-safe arithmetic deltas without causal interpretation."""

    requested = {
        str(key).strip()
        for key in (metric_keys or [])
        if str(key).strip()
    }
    base_claims = _claims_by_metric(base.get("recommendation_evidence"))
    comparison_claims = _claims_by_metric(comparison.get("recommendation_evidence"))
    common = sorted(set(base_claims).intersection(comparison_claims))
    if requested:
        common = [key for key in common if key in requested]
        selection_policy = "explicit_metric_keys"
    else:
        defaults = (
            _DEFAULT_CASHFLOW_METRICS
            if domain == "cashflow"
            else _DEFAULT_ALLOCATION_METRICS
        )
        common = [
            key
            for key in common
            if key in defaults
            or (
                domain == "asset_allocation"
                and (
                    key.startswith("category_weight.")
                    or key.startswith("asset_weight.")
                )
            )
        ]
        selection_policy = "bounded_decision_useful_defaults"

    deltas = []
    skipped = []
    for metric_key in common:
        left = base_claims[metric_key]
        right = comparison_claims[metric_key]
        if left["unit"] != right["unit"]:
            skipped.append(
                {
                    "metric_key": metric_key,
                    "reason": "unit_mismatch",
                    "base_unit": left["unit"],
                    "comparison_unit": right["unit"],
                }
            )
            continue
        left_values = dict(_numeric_leaves(left["value"]))
        right_values = dict(_numeric_leaves(right["value"]))
        shared_paths = sorted(set(left_values).intersection(right_values))
        if not shared_paths:
            skipped.append(
                {"metric_key": metric_key, "reason": "no_common_numeric_values"}
            )
            continue
        for value_path in shared_paths:
            base_value = left_values[value_path]
            comparison_value = right_values[value_path]
            delta = comparison_value - base_value
            relative = (
                delta / abs(base_value)
                if base_value != 0
                else None
            )
            suffix = value_path.removeprefix("$.")
            delta_key = (
                f"delta.{metric_key}.{suffix}"
                if suffix
                else f"delta.{metric_key}"
            )
            deltas.append(
                {
                    "metric_key": metric_key,
                    "value_path": value_path,
                    "delta_metric_key": delta_key,
                    "base_value": base_value,
                    "comparison_value": comparison_value,
                    "delta": delta,
                    "relative_delta": relative,
                    "unit": left["unit"],
                    "direction": (
                        "higher" if delta > 0 else "lower" if delta < 0 else "unchanged"
                    ),
                    "base_claim_id": left["claim_id"],
                    "comparison_claim_id": right["claim_id"],
                }
            )

    missing_requested = sorted(requested - set(common))
    if not deltas:
        return {
            "success": False,
            "error": "no_comparable_metrics",
            "missing_requested_metric_keys": missing_requested,
            "skipped_metrics": skipped,
        }
    return {
        "success": True,
        "full_result": {
            "schema_version": "awm.quant_analysis_comparison.v1",
            "domain": domain,
            "base_analysis_id": base.get("analysis_id"),
            "comparison_analysis_id": comparison.get("analysis_id"),
            "base_input_fingerprint": base.get("input_fingerprint"),
            "comparison_input_fingerprint": comparison.get("input_fingerprint"),
            "inputs_differ": (
                base.get("input_fingerprint")
                != comparison.get("input_fingerprint")
            ),
            "selection_policy": selection_policy,
            "compared_metric_keys": sorted(
                {item["metric_key"] for item in deltas}
            ),
            "deltas": deltas,
            "missing_requested_metric_keys": missing_requested,
            "skipped_metrics": skipped,
            "interpretation_policy": {
                "allowed": "Report exact arithmetic differences with units.",
                "not_allowed": (
                    "Do not claim causation unless the two analyses differ by exactly one "
                    "validated input and a separate policy explicitly permits that conclusion."
                ),
            },
        },
    }


def _claims_by_metric(raw: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return {}
    output: Dict[str, Dict[str, Any]] = {}
    for claim in raw.get("claims") or []:
        if not isinstance(claim, Mapping):
            continue
        metric_key = str(claim.get("metric_key") or "").strip()
        unit = str(claim.get("unit") or "").strip()
        if not metric_key or not unit:
            continue
        output[metric_key] = {
            "value": claim.get("value"),
            "unit": unit,
            "claim_id": str(claim.get("claim_id") or metric_key),
        }
    return output


def _numeric_leaves(value: Any, path: str = "$"):
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isfinite(number):
            yield path, number
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield from _numeric_leaves(child, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            yield from _numeric_leaves(child, f"{path}[{index}]")
