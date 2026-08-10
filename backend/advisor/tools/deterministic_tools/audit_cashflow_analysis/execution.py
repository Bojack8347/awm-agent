"""Pure reconciliation logic for immutable cash-flow analysis snapshots."""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, Mapping, Optional


AUDIT_SCHEMA_VERSION = "awm.cashflow_analysis_audit.v1"
_USD_TOLERANCE = 0.01
_PROBABILITY_TOLERANCE = 1e-9


def audit_cashflow_snapshot(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    """Audit stored evidence and annual series without executing LifeModel."""

    analysis = (
        snapshot.get("analysis")
        if isinstance(snapshot.get("analysis"), Mapping)
        else {}
    )
    metrics = (
        analysis.get("metrics")
        if isinstance(analysis.get("metrics"), Mapping)
        else {}
    )
    metadata = (
        analysis.get("native_result_metadata")
        if isinstance(analysis.get("native_result_metadata"), Mapping)
        else {}
    )
    detail_series = (
        analysis.get("detail_series")
        if isinstance(analysis.get("detail_series"), Mapping)
        else {}
    )
    checks: list[Dict[str, Any]] = []

    def record(
        check_id: str,
        status: str,
        meaning: str,
        *,
        evidence_paths: Iterable[str] = (),
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        item: Dict[str, Any] = {
            "check_id": check_id,
            "status": status,
            "meaning": meaning,
            "evidence_paths": list(evidence_paths),
        }
        if isinstance(details, Mapping):
            item["details"] = dict(details)
        checks.append(item)

    if analysis.get("schema_version") == "awm.cashflow_result.v2":
        record(
            "stored_analysis_schema",
            "passed",
            "The stored analysis uses the supported cash-flow result schema.",
            evidence_paths=("$.analysis.schema_version",),
        )
    else:
        record(
            "stored_analysis_schema",
            "failed",
            "The stored analysis schema is missing or unsupported.",
            evidence_paths=("$.analysis.schema_version",),
        )

    simulation_mode = str(metadata.get("simulation_mode") or "unknown").strip().lower()
    path_count = _positive_integer(metadata.get("num_simulations"))
    _audit_event_distribution(
        metrics,
        metric_key="first_depletion_year_distribution",
        expected_path_count=path_count,
        checks=checks,
    )
    _audit_event_distribution(
        metrics,
        metric_key="first_shortfall_year_distribution",
        expected_path_count=path_count,
        checks=checks,
    )

    stored_terminal_net_worth = _metric_number(
        metrics,
        "projected_terminal_value",
    )
    stored_terminal_net_worth_source = (
        "$.analysis.metrics.projected_terminal_value.value"
    )
    if stored_terminal_net_worth is None:
        stored_terminal_net_worth = _metric_number(metrics, "ending_balance")
        stored_terminal_net_worth_source = (
            "$.analysis.metrics.ending_balance.value"
        )
    stored_terminal_shortfall = _metric_number(metrics, "shortfall")
    stored_success = _metric_number(metrics, "success_probability")

    net_worth_series = _percentile_series(detail_series, "Net Worth", "p50")
    shortfall_series = _percentile_series(
        detail_series,
        "Cashflow Shortfall Debt",
        "p50",
    )
    bank_series = _percentile_series(detail_series, "Bank Balance", "p50")
    all_series_years = sorted(
        set(net_worth_series) | set(shortfall_series) | set(bank_series)
    )
    audit_metrics: Dict[str, Dict[str, Any]] = {}

    if path_count is not None:
        audit_metrics["path_count"] = _metric(
            path_count,
            unit="count",
            source_path="$.analysis.native_result_metadata.num_simulations",
            derivation="stored_metadata_identity",
        )
    if all_series_years:
        audit_metrics["series_row_count"] = _metric(
            len(all_series_years),
            unit="count",
            source_path="$.analysis.detail_series",
            derivation="distinct_stored_calendar_year_count",
        )
        audit_metrics["series_start_year"] = _metric(
            all_series_years[0],
            unit="calendar_year",
            source_path="$.analysis.detail_series",
            derivation="minimum_stored_calendar_year",
        )
        audit_metrics["series_end_year"] = _metric(
            all_series_years[-1],
            unit="calendar_year",
            source_path="$.analysis.detail_series",
            derivation="maximum_stored_calendar_year",
        )
        expected_years = list(range(all_series_years[0], all_series_years[-1] + 1))
        if all_series_years == expected_years:
            record(
                "annual_series_contiguous",
                "passed",
                "Stored annual series are contiguous.",
                evidence_paths=("$.analysis.detail_series",),
            )
        else:
            record(
                "annual_series_contiguous",
                "failed",
                "Stored annual series contain one or more calendar-year gaps.",
                evidence_paths=("$.analysis.detail_series",),
            )
    else:
        record(
            "annual_series_available",
            "not_tested",
            (
                "This older snapshot does not contain annual balance series, so "
                "row-by-row terminal and first-event recomputation is unavailable."
            ),
            evidence_paths=("$.analysis.detail_series",),
        )

    _audit_band_ordering(detail_series, checks)
    _audit_nonnegative_shortfall(detail_series, checks)

    if stored_terminal_net_worth is not None:
        audit_metrics["stored_terminal_net_worth"] = _metric(
            stored_terminal_net_worth,
            unit="USD",
            source_path=stored_terminal_net_worth_source,
            derivation="stored_metric_identity",
        )
    _reconcile_terminal(
        stored_value=stored_terminal_net_worth,
        series=net_worth_series,
        metric_prefix="terminal_net_worth",
        stored_metric_source=stored_terminal_net_worth_source,
        series_source="$.analysis.detail_series['Net Worth'][terminal_year].p50",
        audit_metrics=audit_metrics,
        checks=checks,
    )

    if stored_terminal_shortfall is not None:
        audit_metrics["stored_terminal_shortfall"] = _metric(
            stored_terminal_shortfall,
            unit="USD",
            source_path="$.analysis.metrics.shortfall.value",
            derivation="stored_metric_identity",
        )
    _reconcile_terminal(
        stored_value=stored_terminal_shortfall,
        series=shortfall_series,
        metric_prefix="terminal_shortfall",
        stored_metric_source="$.analysis.metrics.shortfall.value",
        series_source=(
            "$.analysis.detail_series['Cashflow Shortfall Debt'][terminal_year].p50"
        ),
        audit_metrics=audit_metrics,
        checks=checks,
    )

    if stored_success is not None:
        audit_metrics["stored_success_value"] = _metric(
            stored_success,
            unit="probability_0_to_1",
            source_path="$.analysis.metrics.success_probability.value",
            derivation="stored_metric_identity",
        )
    if simulation_mode == "deterministic" and net_worth_series:
        success_threshold = _finite_number(metadata.get("success_threshold"))
        success_threshold = 0.0 if success_threshold is None else success_threshold
        recomputed_success = (
            1.0
            if all(value >= success_threshold for value in net_worth_series.values())
            else 0.0
        )
        audit_metrics["recomputed_success_value"] = _metric(
            recomputed_success,
            unit="probability_0_to_1",
            source_path="$.analysis.detail_series['Net Worth'][*].p50",
            derivation="all_deterministic_years_meet_success_threshold",
        )
        matches = (
            stored_success is not None
            and abs(stored_success - recomputed_success) <= _PROBABILITY_TOLERANCE
        )
        record(
            "deterministic_success_reconciliation",
            "passed" if matches else "failed",
            (
                "Stored deterministic success agrees with the annual net-worth path."
                if matches
                else "Stored deterministic success does not agree with the annual net-worth path."
            ),
            evidence_paths=(
                "$.analysis.metrics.success_probability.value",
                "$.analysis.detail_series['Net Worth'][*].p50",
            ),
        )
    elif simulation_mode == "deterministic":
        record(
            "deterministic_success_reconciliation",
            "not_tested",
            "Annual net-worth rows are unavailable for deterministic success recomputation.",
            evidence_paths=("$.analysis.detail_series['Net Worth']",),
        )

    depletion_distribution = _metric_mapping(
        metrics,
        "first_depletion_year_distribution",
    )
    shortfall_distribution = _metric_mapping(
        metrics,
        "first_shortfall_year_distribution",
    )
    _audit_terminal_never_contradiction(
        stored_terminal=stored_terminal_net_worth,
        distribution=depletion_distribution,
        default_threshold=0.0,
        metric_key="net_worth_depletion",
        checks=checks,
    )
    _audit_terminal_never_contradiction(
        stored_terminal=stored_terminal_shortfall,
        distribution=shortfall_distribution,
        default_threshold=1e-6,
        metric_key="cashflow_shortfall",
        checks=checks,
    )

    if simulation_mode == "deterministic":
        _audit_deterministic_first_event(
            series=net_worth_series,
            distribution=depletion_distribution,
            operator="less_than",
            default_threshold=0.0,
            metric_prefix="net_worth_depletion",
            distribution_metric_key="first_depletion_year_distribution",
            audit_metrics=audit_metrics,
            checks=checks,
        )
        _audit_deterministic_first_event(
            series=shortfall_series,
            distribution=shortfall_distribution,
            operator="greater_than",
            default_threshold=1e-6,
            metric_prefix="cashflow_shortfall",
            distribution_metric_key="first_shortfall_year_distribution",
            audit_metrics=audit_metrics,
            checks=checks,
        )
    elif simulation_mode == "monte_carlo":
        record(
            "path_level_event_recomputation",
            "not_tested",
            (
                "Monte Carlo first-event distributions require individual simulated "
                "paths; percentile series cannot reconstruct them."
            ),
            evidence_paths=("$.analysis.metrics",),
        )

    passed = sum(item["status"] == "passed" for item in checks)
    failed = sum(item["status"] == "failed" for item in checks)
    not_tested = sum(item["status"] == "not_tested" for item in checks)
    audit_metrics["checks_passed"] = _metric(
        passed,
        unit="count",
        source_path="$.full_result.checks",
        derivation="count_status_passed",
    )
    audit_metrics["checks_failed"] = _metric(
        failed,
        unit="count",
        source_path="$.full_result.checks",
        derivation="count_status_failed",
    )
    audit_metrics["checks_not_tested"] = _metric(
        not_tested,
        unit="count",
        source_path="$.full_result.checks",
        derivation="count_status_not_tested",
    )
    audit_status = "failed" if failed else "limited" if not_tested else "passed"
    limitations = []
    if not all_series_years:
        limitations.append(
            "The stored snapshot predates annual-series retention; a new source "
            "projection is required for row-by-row reconciliation."
        )
    if simulation_mode == "monte_carlo":
        limitations.append(
            "The immutable snapshot stores percentile series and event distributions, "
            "not every Monte Carlo path; path-level stochastic replay requires a new run."
        )
    return {
        "success": True,
        "full_result": {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "analysis_id": snapshot.get("analysis_id"),
            "source_input_fingerprint": snapshot.get("input_fingerprint"),
            "audit_status": audit_status,
            "scope": "stored_snapshot_only",
            "reran_model": False,
            "simulation": {
                "mode": simulation_mode,
                "path_count": path_count,
            },
            "metrics": audit_metrics,
            "checks": checks,
            "summary": {
                "passed": passed,
                "failed": failed,
                "not_tested": not_tested,
            },
            "limitations": limitations,
            "requires_source_rerun": failed > 0 or not all_series_years,
            "calculation_policy": (
                "The audit recomputes only from the immutable stored metrics, "
                "metadata, and annual series. It does not execute or replace LifeModel."
            ),
        },
    }


def _audit_event_distribution(
    metrics: Mapping[str, Any],
    *,
    metric_key: str,
    expected_path_count: Optional[int],
    checks: list[Dict[str, Any]],
) -> None:
    distribution = _metric_mapping(metrics, metric_key)
    if not distribution:
        checks.append(
            {
                "check_id": f"{metric_key}.available",
                "status": "not_tested",
                "meaning": "The stored event distribution is unavailable.",
                "evidence_paths": [f"$.analysis.metrics.{metric_key}"],
            }
        )
        return
    probabilities = distribution.get("probability_by_year")
    probability_never = _finite_number(distribution.get("probability_never"))
    numeric_probabilities = (
        [
            number
            for value in probabilities.values()
            if (number := _finite_number(value)) is not None
        ]
        if isinstance(probabilities, Mapping)
        else []
    )
    mass = (
        sum(numeric_probabilities) + probability_never
        if probability_never is not None
        else None
    )
    reconciled = (
        isinstance(probabilities, Mapping)
        and len(numeric_probabilities) == len(probabilities)
        and mass is not None
        and abs(mass - 1.0) <= _PROBABILITY_TOLERANCE
        and all(0.0 <= value <= 1.0 for value in numeric_probabilities)
        and 0.0 <= probability_never <= 1.0
    )
    checks.append(
        {
            "check_id": f"{metric_key}.probability_mass",
            "status": "passed" if reconciled else "failed",
            "meaning": (
                "Event probabilities reconcile to one."
                if reconciled
                else "Event probabilities do not reconcile to one."
            ),
            "evidence_paths": [f"$.analysis.metrics.{metric_key}.value"],
            "details": {"probability_mass": mass},
        }
    )
    sample_count = _positive_integer(distribution.get("sample_count"))
    if expected_path_count is None:
        checks.append(
            {
                "check_id": f"{metric_key}.sample_count",
                "status": "not_tested",
                "meaning": "Run metadata does not provide an expected path count.",
                "evidence_paths": [
                    f"$.analysis.metrics.{metric_key}.value.sample_count",
                    "$.analysis.native_result_metadata.num_simulations",
                ],
            }
        )
    else:
        matches = sample_count == expected_path_count
        checks.append(
            {
                "check_id": f"{metric_key}.sample_count",
                "status": "passed" if matches else "failed",
                "meaning": (
                    "Event sample count matches run metadata."
                    if matches
                    else "Event sample count does not match run metadata."
                ),
                "evidence_paths": [
                    f"$.analysis.metrics.{metric_key}.value.sample_count",
                    "$.analysis.native_result_metadata.num_simulations",
                ],
            }
        )


def _reconcile_terminal(
    *,
    stored_value: Optional[float],
    series: Mapping[int, float],
    metric_prefix: str,
    stored_metric_source: str,
    series_source: str,
    audit_metrics: Dict[str, Dict[str, Any]],
    checks: list[Dict[str, Any]],
) -> None:
    if not series:
        checks.append(
            {
                "check_id": f"{metric_prefix}.series_reconciliation",
                "status": "not_tested",
                "meaning": "The annual series required for terminal reconciliation is unavailable.",
                "evidence_paths": [series_source],
            }
        )
        return
    terminal_year = max(series)
    recomputed = series[terminal_year]
    audit_metrics[f"recomputed_{metric_prefix}"] = _metric(
        recomputed,
        unit="USD",
        source_path=series_source,
        derivation="terminal_stored_annual_series_value",
    )
    if stored_value is None:
        checks.append(
            {
                "check_id": f"{metric_prefix}.series_reconciliation",
                "status": "failed",
                "meaning": "The stored headline metric is unavailable for terminal reconciliation.",
                "evidence_paths": [series_source],
            }
        )
        return
    difference = recomputed - stored_value
    audit_metrics[f"{metric_prefix}_difference"] = _metric(
        difference,
        unit="USD",
        source_path=f"$.full_result.metrics.{metric_prefix}_difference",
        derivation="recomputed_minus_stored",
    )
    reconciled = abs(difference) <= _USD_TOLERANCE
    checks.append(
        {
            "check_id": f"{metric_prefix}.series_reconciliation",
            "status": "passed" if reconciled else "failed",
            "meaning": (
                "Stored and recomputed terminal values reconcile within one cent."
                if reconciled
                else "Stored and recomputed terminal values differ by more than one cent."
            ),
            "evidence_paths": [
                series_source,
                stored_metric_source,
            ],
            "details": {
                "terminal_year": terminal_year,
                "difference": difference,
                "tolerance": _USD_TOLERANCE,
            },
        }
    )


def _audit_terminal_never_contradiction(
    *,
    stored_terminal: Optional[float],
    distribution: Mapping[str, Any],
    default_threshold: float,
    metric_key: str,
    checks: list[Dict[str, Any]],
) -> None:
    if stored_terminal is None or not distribution:
        checks.append(
            {
                "check_id": f"{metric_key}.terminal_never_consistency",
                "status": "not_tested",
                "meaning": "Terminal value or event distribution is unavailable.",
                "evidence_paths": ["$.analysis.metrics"],
            }
        )
        return
    threshold = _finite_number(distribution.get("threshold"))
    threshold = default_threshold if threshold is None else threshold
    operator = str(distribution.get("operator") or "")
    terminal_crossed = (
        stored_terminal < threshold
        if operator == "less_than"
        else stored_terminal > threshold
    )
    probability_never = _finite_number(distribution.get("probability_never"))
    contradictory = terminal_crossed and probability_never == 1.0
    checks.append(
        {
            "check_id": f"{metric_key}.terminal_never_consistency",
            "status": "failed" if contradictory else "passed",
            "meaning": (
                "Terminal value contradicts an event distribution that reports the event never occurs."
                if contradictory
                else "Terminal value does not contradict the stored event-never probability."
            ),
            "evidence_paths": [
                f"$.analysis.metrics.{metric_key}",
                "$.analysis.metrics.projected_terminal_value"
                if metric_key == "net_worth_depletion"
                else "$.analysis.metrics.shortfall",
            ],
        }
    )


def _audit_deterministic_first_event(
    *,
    series: Mapping[int, float],
    distribution: Mapping[str, Any],
    operator: str,
    default_threshold: float,
    metric_prefix: str,
    distribution_metric_key: str,
    audit_metrics: Dict[str, Dict[str, Any]],
    checks: list[Dict[str, Any]],
) -> None:
    if not series:
        checks.append(
            {
                "check_id": f"{metric_prefix}.first_event_reconciliation",
                "status": "not_tested",
                "meaning": "Annual deterministic series are unavailable for first-event recomputation.",
                "evidence_paths": ["$.analysis.detail_series"],
            }
        )
        return
    threshold = _finite_number(distribution.get("threshold"))
    threshold = default_threshold if threshold is None else threshold
    ordered_years = sorted(series)
    first_year = None
    for year in ordered_years[1:]:
        value = series[year]
        crossed = value < threshold if operator == "less_than" else value > threshold
        if crossed:
            first_year = year
            break
    stored_year, stored_known = _deterministic_distribution_year(distribution)
    if first_year is not None:
        audit_metrics[f"recomputed_first_{metric_prefix}_year"] = _metric(
            first_year,
            unit="calendar_year",
            source_path="$.analysis.detail_series",
            derivation="first_threshold_crossing_excluding_opening_baseline",
        )
    if stored_known and stored_year is not None:
        audit_metrics[f"stored_first_{metric_prefix}_year"] = _metric(
            stored_year,
            unit="calendar_year",
            source_path=(
                f"$.analysis.metrics.{distribution_metric_key}.value"
            ),
            derivation="stored_deterministic_event_distribution",
        )
    matches = stored_known and stored_year == first_year
    checks.append(
        {
            "check_id": f"{metric_prefix}.first_event_reconciliation",
            "status": "passed" if matches else "failed",
            "meaning": (
                "Stored and recomputed deterministic first-event timing agree."
                if matches
                else "Stored and recomputed deterministic first-event timing do not agree."
            ),
            "evidence_paths": [
                "$.analysis.detail_series",
                f"$.analysis.metrics.{distribution_metric_key}",
            ],
        }
    )


def _audit_band_ordering(
    detail_series: Mapping[str, Any],
    checks: list[Dict[str, Any]],
) -> None:
    rows_tested = 0
    violations = 0
    for annual in detail_series.values():
        if not isinstance(annual, Mapping):
            continue
        for values in annual.values():
            if not isinstance(values, Mapping):
                continue
            p10 = _finite_number(values.get("p10"))
            p50 = _finite_number(values.get("p50"))
            p90 = _finite_number(values.get("p90"))
            if p10 is None or p50 is None or p90 is None:
                continue
            rows_tested += 1
            if not p10 <= p50 <= p90:
                violations += 1
    checks.append(
        {
            "check_id": "percentile_band_ordering",
            "status": (
                "not_tested"
                if rows_tested == 0
                else "passed"
                if violations == 0
                else "failed"
            ),
            "meaning": (
                "No complete percentile rows are stored for ordering checks."
                if rows_tested == 0
                else "Stored percentile bands are ordered p10 ≤ p50 ≤ p90."
                if violations == 0
                else "One or more stored percentile rows violate p10 ≤ p50 ≤ p90."
            ),
            "evidence_paths": ["$.analysis.detail_series"],
            "details": {
                "rows_tested": rows_tested,
                "violations": violations,
            },
        }
    )


def _audit_nonnegative_shortfall(
    detail_series: Mapping[str, Any],
    checks: list[Dict[str, Any]],
) -> None:
    series = detail_series.get("Cashflow Shortfall Debt")
    values = []
    if isinstance(series, Mapping):
        for row in series.values():
            if not isinstance(row, Mapping):
                continue
            values.extend(
                number
                for value in row.values()
                if (number := _finite_number(value)) is not None
            )
    violations = sum(value < -_USD_TOLERANCE for value in values)
    checks.append(
        {
            "check_id": "cashflow_shortfall_nonnegative",
            "status": (
                "not_tested"
                if not values
                else "passed"
                if violations == 0
                else "failed"
            ),
            "meaning": (
                "Stored shortfall series are unavailable."
                if not values
                else "Stored cash-flow shortfall debt is nonnegative."
                if violations == 0
                else "Stored cash-flow shortfall debt contains negative values."
            ),
            "evidence_paths": [
                "$.analysis.detail_series['Cashflow Shortfall Debt']"
            ],
            "details": {"values_tested": len(values), "violations": violations},
        }
    )


def _percentile_series(
    detail_series: Mapping[str, Any],
    column: str,
    percentile: str,
) -> Dict[int, float]:
    annual = detail_series.get(column)
    if not isinstance(annual, Mapping):
        return {}
    output: Dict[int, float] = {}
    for raw_year, values in annual.items():
        if not isinstance(values, Mapping):
            continue
        try:
            year = int(str(raw_year))
        except (TypeError, ValueError):
            continue
        value = _finite_number(values.get(percentile))
        if value is not None:
            output[year] = value
    return output


def _metric_number(metrics: Mapping[str, Any], key: str) -> Optional[float]:
    payload = metrics.get(key)
    return (
        _finite_number(payload.get("value"))
        if isinstance(payload, Mapping)
        else None
    )


def _metric_mapping(metrics: Mapping[str, Any], key: str) -> Dict[str, Any]:
    payload = metrics.get(key)
    value = payload.get("value") if isinstance(payload, Mapping) else None
    return dict(value) if isinstance(value, Mapping) else {}


def _deterministic_distribution_year(
    distribution: Mapping[str, Any],
) -> tuple[Optional[int], bool]:
    if not distribution:
        return None, False
    probabilities = distribution.get("probability_by_year")
    probability_never = _finite_number(distribution.get("probability_never"))
    if not isinstance(probabilities, Mapping) or probability_never is None:
        return None, False
    event_years = []
    for raw_year, raw_probability in probabilities.items():
        probability = _finite_number(raw_probability)
        if probability is None:
            return None, False
        if probability > 0:
            try:
                event_years.append((int(str(raw_year)), probability))
            except (TypeError, ValueError):
                return None, False
    if probability_never == 1.0 and not event_years:
        return None, True
    if (
        probability_never == 0.0
        and len(event_years) == 1
        and event_years[0][1] == 1.0
    ):
        return event_years[0][0], True
    return None, False


def _metric(
    value: float | int,
    *,
    unit: str,
    source_path: str,
    derivation: str,
) -> Dict[str, Any]:
    return {
        "value": value,
        "unit": unit,
        "source_path": source_path,
        "provenance": {
            "source_path": source_path,
            "derivation": derivation,
        },
    }


def _positive_integer(value: Any) -> Optional[int]:
    number = _finite_number(value)
    if number is None or number < 1 or not number.is_integer():
        return None
    return int(number)


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
