"""Execution for the run_cashflow_projection deterministic tool."""

from __future__ import annotations

import copy
import math
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from typing import Any, Callable, Dict, List, Optional

import requests

from contracts.tools import build_cashflow_tool_args
from advisor.tools.deterministic_tools.common.cache import compute_tool_cache_key
from advisor.tools.deterministic_tools.execution_control import (
    commit_read_only_tool_state,
    effective_read_only_request_timeout,
    read_only_tool_execution_cancelled,
    wait_for_read_only_tool_cancellation,
)


LogDebug = Callable[[str], None]
ResolveCashflowPayload = Callable[[Dict[str, Any]], Dict[str, Any]]
CASHFLOW_CACHE_NAMESPACE = "life_model-v2:awm.cashflow_engine_response.v2"


def _ensure_cache_coordination(state):
    """Backfill coordination fields for lightweight test adapter states."""

    lock = getattr(state, "_tool_cache_lock", None)
    if lock is None:
        lock = threading.RLock()
        state._tool_cache_lock = lock
    with lock:
        if not hasattr(state, "_tool_inflight"):
            state._tool_inflight = {}
        if not hasattr(state, "_tool_result_cache_max_entries"):
            state._tool_result_cache_max_entries = 64
    return lock


def _cashflow_execution_cancelled_result(
    request: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "success": False,
        "error": "tool_execution_cancelled",
        "cancelled": True,
    }
    if request is not None:
        result["request"] = request
    return result


def build_cashflow_headers(config: Any) -> Dict[str, str]:
    """Build headers for cashflow API requests."""
    headers = {"Content-Type": "application/json"}
    if getattr(config, "cashflow_api_key", ""):
        headers["X-Api-Key"] = config.cashflow_api_key
    return headers


def _cashflow_model_url(config: Any) -> str:
    return str(
        getattr(config, "cashflow_model_url", None)
        or getattr(config, "cashflow_api_url", None)
        or ""
    ).rstrip("/")


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively deep-merge dictionaries without mutating either input."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


_BAND_PERCENTILES = {
    "Top 5%": "p95",
    "Top 10%": "p90",
    "Top 25%": "p75",
    "Median": "p50",
    "Bottom 25%": "p25",
    "Bottom 10%": "p10",
    "Bottom 5%": "p05",
}


def normalize_cashflow_engine_response(full_result: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize current and legacy LifeModel responses into one typed contract."""

    result = full_result.get("result") if isinstance(full_result.get("result"), dict) else {}
    normalization_errors: List[str] = []
    metrics = _legacy_cashflow_metrics(full_result, normalization_errors)
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    input_context = (
        metadata.get("input_context")
        if isinstance(metadata.get("input_context"), dict)
        else {}
    )
    projection_parameters = (
        input_context.get("projection_parameters")
        if isinstance(input_context.get("projection_parameters"), dict)
        else {}
    )

    success_rate = _finite_number(result.get("success_rate"))
    if success_rate is not None:
        if 0.0 <= success_rate <= 1.0:
            metrics["success_probability"] = _metric(
                success_rate,
                unit="probability_0_to_1",
                source_path="$.result.success_rate",
                derivation="identity",
            )
        else:
            normalization_errors.append("result.success_rate_out_of_range")

    bands = result.get("percentile_bands")
    years = result.get("years") if isinstance(result.get("years"), list) else []
    terminal_percentiles = _terminal_percentiles(bands, "Net Worth", normalization_errors)
    if terminal_percentiles:
        metrics["terminal_value_percentiles"] = _metric(
            terminal_percentiles,
            unit="USD",
            source_path="$.result.percentile_bands['Net Worth'][*][-1]",
            derivation="terminal_value_by_percentile",
        )
        median_terminal = terminal_percentiles.get("p50")
        if median_terminal is not None:
            metrics["projected_terminal_value"] = _metric(
                median_terminal,
                unit="USD",
                source_path="$.result.percentile_bands['Net Worth'].Median[-1]",
                derivation="terminal_median_net_worth",
            )
            metrics["ending_balance"] = _metric(
                median_terminal,
                unit="USD",
                source_path="$.result.percentile_bands['Net Worth'].Median[-1]",
                derivation="terminal_median_net_worth",
            )

    # Cash-flow shortfall is an explicit LifeModel liability column.  Negative
    # net worth is a different concept (assets minus all liabilities) and must
    # never be substituted for a cash-flow funding deficit.
    explicit_shortfall_bands = (
        isinstance(bands, dict) and "Cashflow Shortfall Debt" in bands
    )
    shortfall_percentiles = _terminal_percentiles(
        bands,
        "Cashflow Shortfall Debt",
        normalization_errors,
    )
    if explicit_shortfall_bands:
        metrics.pop("shortfall", None)
        metrics.pop("shortfall_percentiles", None)
    if shortfall_percentiles:
        if any(value < 0 for value in shortfall_percentiles.values()):
            normalization_errors.append("negative_cashflow_shortfall_debt_band")
        else:
            metrics["shortfall_percentiles"] = _metric(
                shortfall_percentiles,
                unit="USD",
                source_path="$.result.percentile_bands['Cashflow Shortfall Debt'][*][-1]",
                derivation="terminal_cashflow_shortfall_debt_by_percentile",
            )
            median_shortfall = shortfall_percentiles.get("p50")
            if median_shortfall is not None:
                metrics["shortfall"] = _metric(
                    median_shortfall,
                    unit="USD",
                    source_path="$.result.percentile_bands['Cashflow Shortfall Debt'].Median[-1]",
                    derivation="terminal_median_cashflow_shortfall_debt",
                )

    minimum_liquidity = _minimum_liquidity_from_bands(bands, years, normalization_errors)
    if minimum_liquidity is not None:
        metrics["minimum_liquidity"] = minimum_liquidity
    milestone_trajectory = _milestone_percentile_trajectory(
        bands,
        years,
        projection_parameters=projection_parameters,
    )
    if milestone_trajectory:
        metrics["milestone_percentile_trajectory"] = _metric(
            milestone_trajectory,
            unit="USD_by_calendar_year_and_percentile",
            source_path="$.result.percentile_bands[*][*][milestone_indices]",
            derivation=(
                "bounded_milestone_selection_from_reported_percentile_time_series"
            ),
        )
    detail_columns = [
        str(item)
        for item in projection_parameters.get("projection_columns") or []
        if isinstance(item, str) and str(item).strip()
    ]
    detail_series = _annual_percentile_trajectory(
        bands,
        years,
        columns=detail_columns,
    )
    requested_detail_trajectory = _detail_milestone_percentile_trajectory(
        detail_series,
        projection_parameters=projection_parameters,
    )
    if requested_detail_trajectory:
        metrics["requested_detail_percentile_trajectory"] = _metric(
            requested_detail_trajectory,
            unit="LifeModel_column_value_by_calendar_year_and_percentile",
            source_path="$.result.percentile_bands[requested_columns][p10,p50,p90][milestone_indices]",
            derivation="bounded_milestone_selection_from_explicitly_requested_report_columns",
        )

    event_distributions = (
        result.get("event_distributions")
        if isinstance(result.get("event_distributions"), dict)
        else {}
    )
    for event_key, metric_key in (
        ("net_worth_depletion", "first_depletion_year_distribution"),
        ("cashflow_shortfall", "first_shortfall_year_distribution"),
    ):
        distribution = _validated_event_distribution(
            event_distributions.get(event_key),
            event_key=event_key,
            errors=normalization_errors,
        )
        if distribution is not None:
            metrics[metric_key] = _metric(
                distribution,
                unit="probability_by_calendar_year",
                source_path=f"$.result.event_distributions.{event_key}",
                derivation="first_threshold_crossing_from_individual_paths",
                semantic_type="distribution_not_scalar_year",
            )

    # Defense in depth for malformed or legacy transports: an event cannot be
    # reported as "never" when the terminal series itself is beyond the same
    # threshold. Drop the contradictory timing claim rather than narrating it.
    depletion_metric = metrics.get("first_depletion_year_distribution")
    terminal_value = _finite_number(
        metrics.get("projected_terminal_value", {}).get("value")
        if isinstance(metrics.get("projected_terminal_value"), dict)
        else None
    )
    if (
        terminal_value is not None
        and terminal_value < 0
        and _event_distribution_reports_never(depletion_metric)
    ):
        normalization_errors.append(
            "event_distribution_conflicts_with_terminal_value:net_worth_depletion"
        )
        metrics.pop("first_depletion_year_distribution", None)

    shortfall_metric = metrics.get("first_shortfall_year_distribution")
    terminal_shortfall = _finite_number(
        metrics.get("shortfall", {}).get("value")
        if isinstance(metrics.get("shortfall"), dict)
        else None
    )
    shortfall_threshold = _event_distribution_threshold(shortfall_metric)
    if (
        terminal_shortfall is not None
        and shortfall_threshold is not None
        and terminal_shortfall > shortfall_threshold
        and _event_distribution_reports_never(shortfall_metric)
    ):
        normalization_errors.append(
            "event_distribution_conflicts_with_terminal_value:cashflow_shortfall"
        )
        metrics.pop("first_shortfall_year_distribution", None)

    monte_carlo = metadata.get("monte_carlo") if isinstance(metadata.get("monte_carlo"), dict) else {}
    engine = full_result.get("engine") if isinstance(full_result.get("engine"), dict) else {}
    native_warnings = result.get("warnings") if isinstance(result.get("warnings"), list) else []
    return {
        "schema_version": "awm.cashflow_engine_normalized.v2",
        "transport_schema_version": full_result.get("transport_schema_version"),
        "metrics": metrics,
        "warnings": copy.deepcopy(native_warnings),
        "normalization_errors": normalization_errors,
        "engine": {
            "name": engine.get("name") or "cashflow-model",
            "version": engine.get("version") or metadata.get("version"),
            "implementation": engine.get("implementation"),
        },
        "run_metadata": {
            "simulation_mode": "monte_carlo" if monte_carlo else "deterministic",
            "num_simulations": monte_carlo.get("num_simulations") or (1 if result else None),
            "random_seed": monte_carlo.get("random_seed"),
            "success_column": monte_carlo.get("success_column"),
            "success_threshold": monte_carlo.get("success_threshold"),
            "input_context": input_context,
            "projection_parameters": copy.deepcopy(projection_parameters),
        },
        "detail_series": detail_series,
    }


def _metric(
    value: Any,
    *,
    unit: str,
    source_path: str,
    derivation: str,
    semantic_type: Optional[str] = None,
) -> Dict[str, Any]:
    metric = {
        "value": value,
        "unit": unit,
        "source_path": source_path,
        "provenance": {"source_path": source_path, "derivation": derivation},
    }
    if semantic_type:
        metric["semantic_type"] = semantic_type
    return metric


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _terminal_percentiles(
    bands: Any,
    column: str,
    errors: List[str],
) -> Dict[str, float]:
    if not isinstance(bands, dict):
        return {}
    column_bands = bands.get(column)
    if not isinstance(column_bands, dict):
        return {}
    terminal: Dict[str, float] = {}
    for band_name, percentile_name in _BAND_PERCENTILES.items():
        values = column_bands.get(band_name)
        if values is None:
            continue
        if not isinstance(values, list) or not values:
            errors.append(f"invalid_percentile_band:{column}:{band_name}")
            continue
        value = _finite_number(values[-1])
        if value is None:
            errors.append(f"non_numeric_terminal_band:{column}:{band_name}")
            continue
        terminal[percentile_name] = value
    return terminal


def _milestone_percentile_trajectory(
    bands: Any,
    years: List[Any],
    *,
    projection_parameters: Dict[str, Any],
    max_milestones: int = 12,
) -> Dict[str, Dict[str, Dict[str, float]]]:
    """Return a bounded, agent-readable sample of the reported annual bands."""

    if not isinstance(bands, dict) or not isinstance(years, list) or not years:
        return {}
    normalized_years: List[int] = []
    for value in years:
        number = _finite_number(value)
        if number is None or not number.is_integer():
            return {}
        normalized_years.append(int(number))

    candidate_indices = {
        index
        for index in (0, 1, 3, 5, 10, 15, 20, 25, 30, len(years) - 1)
        if 0 <= index < len(years)
    }
    for age_key, retirement_key in (
        ("primary_current_age", "primary_retirement_age"),
        ("spouse_current_age", "spouse_retirement_age"),
    ):
        current_age = _finite_number(projection_parameters.get(age_key))
        retirement_age = _finite_number(projection_parameters.get(retirement_key))
        if current_age is None or retirement_age is None:
            continue
        retirement_index = int(round(retirement_age - current_age))
        if 0 <= retirement_index < len(years):
            candidate_indices.add(retirement_index)
    selected_indices = sorted(candidate_indices)
    if len(selected_indices) > max_milestones:
        selected_indices = selected_indices[: max_milestones - 1] + [
            selected_indices[-1]
        ]

    columns = {
        "net_worth": "Net Worth",
        "cashflow_shortfall_debt": "Cashflow Shortfall Debt",
        "bank_balance": "Bank Balance",
    }
    trajectory: Dict[str, Dict[str, Dict[str, float]]] = {}
    for index in selected_indices:
        values_for_year: Dict[str, Dict[str, float]] = {}
        for output_key, column_name in columns.items():
            column_bands = bands.get(column_name)
            if not isinstance(column_bands, dict):
                continue
            percentiles: Dict[str, float] = {}
            for band_name, percentile_name in (
                ("Bottom 10%", "p10"),
                ("Median", "p50"),
                ("Top 10%", "p90"),
            ):
                series = column_bands.get(band_name)
                if not isinstance(series, list) or index >= len(series):
                    continue
                number = _finite_number(series[index])
                if number is not None:
                    percentiles[percentile_name] = number
            if percentiles:
                values_for_year[output_key] = percentiles
        if values_for_year:
            trajectory[str(normalized_years[index])] = values_for_year
    return trajectory


def _annual_percentile_trajectory(
    bands: Any,
    years: List[Any],
    *,
    columns: List[str],
) -> Dict[str, Dict[str, Dict[str, float]]]:
    """Retain annual p10/p50/p90 series for explicitly collected columns."""

    if not isinstance(bands, dict) or not isinstance(years, list) or not years:
        return {}
    normalized_years: List[str] = []
    for value in years:
        number = _finite_number(value)
        if number is None or not number.is_integer():
            return {}
        normalized_years.append(str(int(number)))
    selected_columns = list(
        dict.fromkeys(
            [
                "Net Worth",
                "Cashflow Shortfall Debt",
                "Bank Balance",
                *columns,
            ]
        )
    )
    output: Dict[str, Dict[str, Dict[str, float]]] = {}
    for column in selected_columns:
        column_bands = bands.get(column)
        if not isinstance(column_bands, dict):
            continue
        annual: Dict[str, Dict[str, float]] = {}
        for index, year in enumerate(normalized_years):
            percentiles: Dict[str, float] = {}
            for band_name, percentile_name in (
                ("Bottom 10%", "p10"),
                ("Median", "p50"),
                ("Top 10%", "p90"),
            ):
                series = column_bands.get(band_name)
                if not isinstance(series, list) or index >= len(series):
                    continue
                number = _finite_number(series[index])
                if number is not None:
                    percentiles[percentile_name] = number
            if percentiles:
                annual[year] = percentiles
        if annual:
            output[column] = annual
    return output


def _detail_milestone_percentile_trajectory(
    detail_series: Dict[str, Dict[str, Dict[str, float]]],
    *,
    projection_parameters: Dict[str, Any],
    max_milestones: int = 8,
) -> Dict[str, Dict[str, Dict[str, float]]]:
    """Bound explicitly requested detail to retirement-aware milestone years."""

    requested = [
        str(item)
        for item in projection_parameters.get("detail_projection_columns") or []
        if isinstance(item, str) and str(item).strip()
    ]
    requested = [
        column
        for column in dict.fromkeys(requested)
        if column in detail_series
    ]
    if not requested:
        return {}
    all_years = sorted(
        {
            int(year)
            for column in requested
            for year in detail_series[column]
            if str(year).isdigit()
        }
    )
    if not all_years:
        return {}
    candidate_indices = {
        index
        for index in (0, 1, 5, 10, 20, 30, len(all_years) - 1)
        if 0 <= index < len(all_years)
    }
    for age_key, retirement_key in (
        ("primary_current_age", "primary_retirement_age"),
        ("spouse_current_age", "spouse_retirement_age"),
    ):
        current_age = _finite_number(projection_parameters.get(age_key))
        retirement_age = _finite_number(projection_parameters.get(retirement_key))
        if current_age is None or retirement_age is None:
            continue
        index = int(round(retirement_age - current_age))
        if 0 <= index < len(all_years):
            candidate_indices.add(index)
    indices = sorted(candidate_indices)
    if len(indices) > max_milestones:
        indices = indices[: max_milestones - 1] + [indices[-1]]
    output: Dict[str, Dict[str, Dict[str, float]]] = {}
    for index in indices:
        year = str(all_years[index])
        values = {
            column: copy.deepcopy(detail_series[column][year])
            for column in requested
            if year in detail_series[column]
        }
        if values:
            output[year] = values
    return output


def _minimum_liquidity_from_bands(
    bands: Any,
    years: List[Any],
    errors: List[str],
) -> Optional[Dict[str, Any]]:
    if not isinstance(bands, dict):
        return None
    bank_bands = bands.get("Bank Balance")
    if not isinstance(bank_bands, dict):
        return None
    band_name = "Bottom 10%" if isinstance(bank_bands.get("Bottom 10%"), list) else "Median"
    values = bank_bands.get(band_name)
    if not isinstance(values, list) or not values:
        return None
    candidates = [
        (value, index)
        for index, raw_value in enumerate(values)
        if (value := _finite_number(raw_value)) is not None
    ]
    if not candidates:
        errors.append("bank_balance_bands_have_no_numeric_values")
        return None
    value, index = min(candidates, key=lambda pair: pair[0])
    year = years[index] if index < len(years) else None
    return {
        **_metric(
            value,
            unit="USD",
            source_path=f"$.result.percentile_bands['Bank Balance']['{band_name}'][{index}]",
            derivation="minimum_over_projection_horizon",
        ),
        "year": year,
        "percentile": _BAND_PERCENTILES.get(band_name),
    }


def _validated_event_distribution(
    value: Any,
    *,
    event_key: str,
    errors: List[str],
) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("type") != "first_threshold_crossing_distribution":
        errors.append(f"invalid_event_distribution:{event_key}")
        return None
    probabilities = value.get("probability_by_year")
    probability_never = _finite_number(value.get("probability_never"))
    sample_count = _finite_number(value.get("sample_count"))
    threshold = _finite_number(value.get("threshold"))
    column = value.get("column")
    operator = value.get("operator")
    if (
        not isinstance(probabilities, dict)
        or probability_never is None
        or sample_count is None
        or threshold is None
        or not isinstance(column, str)
        or not column
        or operator not in {"less_than", "greater_than"}
    ):
        errors.append(f"incomplete_event_distribution:{event_key}")
        return None
    if value.get("event") != event_key or value.get("opening_baseline_excluded") is not True:
        errors.append(f"invalid_event_distribution_semantics:{event_key}")
        return None
    if probability_never < 0 or probability_never > 1:
        errors.append(f"invalid_event_probability:{event_key}:never")
        return None
    if sample_count < 1 or not sample_count.is_integer():
        errors.append(f"invalid_event_sample_count:{event_key}")
        return None
    normalized_probabilities: Dict[str, float] = {}
    for year, raw_probability in probabilities.items():
        try:
            normalized_year = str(int(str(year)))
        except (TypeError, ValueError):
            errors.append(f"invalid_event_year:{event_key}:{year}")
            return None
        probability = _finite_number(raw_probability)
        if probability is None or probability < 0 or probability > 1:
            errors.append(f"invalid_event_probability:{event_key}:{year}")
            return None
        normalized_probabilities[normalized_year] = probability
    total_probability = sum(normalized_probabilities.values()) + probability_never
    if not 0.999999 <= total_probability <= 1.000001:
        errors.append(f"event_distribution_not_reconciled:{event_key}")
        return None
    if value.get("source") not in {
        "life_model.monte_carlo.raw_paths",
        "life_model.deterministic_projection",
    }:
        errors.append(f"event_distribution_missing_path_provenance:{event_key}")
        return None
    return {
        "type": "first_threshold_crossing_distribution",
        "event": str(value.get("event") or event_key),
        "column": column,
        "operator": operator,
        "threshold": threshold,
        "probability_by_year": normalized_probabilities,
        "probability_never": probability_never,
        "sample_count": int(sample_count),
        "source": value.get("source"),
        "opening_baseline_excluded": bool(value.get("opening_baseline_excluded")),
    }


def _event_distribution_reports_never(metric: Any) -> bool:
    if not isinstance(metric, dict) or not isinstance(metric.get("value"), dict):
        return False
    value = metric["value"]
    probability_never = _finite_number(value.get("probability_never"))
    probabilities = value.get("probability_by_year")
    if probability_never != 1.0 or not isinstance(probabilities, dict):
        return False
    return not any(
        probability is not None and probability > 0
        for probability in (_finite_number(item) for item in probabilities.values())
    )


def _event_distribution_threshold(metric: Any) -> Optional[float]:
    if not isinstance(metric, dict) or not isinstance(metric.get("value"), dict):
        return None
    return _finite_number(metric["value"].get("threshold"))


def _legacy_cashflow_metrics(
    full_result: Dict[str, Any],
    errors: List[str],
) -> Dict[str, Any]:
    summary = full_result.get("summary") if isinstance(full_result.get("summary"), dict) else {}
    details = full_result.get("details") if isinstance(full_result.get("details"), dict) else {}
    metrics: Dict[str, Any] = {}
    for key, unit in (
        ("success_probability", "probability_0_to_1"),
        ("shortfall", "USD"),
        ("projected_terminal_value", "USD"),
        ("ending_balance", "USD"),
    ):
        if _finite_number(summary.get(key)) is not None:
            metrics[key] = _metric(
                _finite_number(summary[key]),
                unit=unit,
                source_path=f"$.summary.{key}",
                derivation="legacy_identity",
            )
    for key in ("terminal_value_percentiles", "shortfall_percentiles"):
        if isinstance(details.get(key), dict):
            metrics[key] = _metric(
                copy.deepcopy(details[key]),
                unit="USD",
                source_path=f"$.details.{key}",
                derivation="legacy_identity",
            )
    failure = details.get("failure_diagnostics") if isinstance(details.get("failure_diagnostics"), dict) else {}
    reserve = _finite_number(failure.get("probability_bank_breaches_reserve_floor"))
    if reserve is not None:
        metrics["reserve_breach_probability"] = _metric(
            reserve,
            unit="probability_0_to_1",
            source_path="$.details.failure_diagnostics.probability_bank_breaches_reserve_floor",
            derivation="legacy_identity",
        )
    for source_key, metric_key in (
        ("first_depletion_year_distribution", "first_depletion_year_distribution"),
        ("first_shortfall_year_distribution", "first_shortfall_year_distribution"),
    ):
        event_key = (
            "net_worth_depletion"
            if source_key == "first_depletion_year_distribution"
            else "cashflow_shortfall"
        )
        distribution = _validated_event_distribution(
            failure.get(source_key),
            event_key=event_key,
            errors=errors,
        )
        if distribution is not None:
            metrics[metric_key] = _metric(
                distribution,
                unit="probability_by_calendar_year",
                source_path=f"$.details.failure_diagnostics.{source_key}",
                derivation="legacy_transport_of_first_threshold_crossing_from_individual_paths",
                semantic_type="distribution_not_scalar_year",
            )
    return metrics


def run_cashflow_model_tool(
    args: Dict[str, Any],
    client_payload: Dict[str, Any],
    state: Any,
    *,
    config: Any,
    http_session: requests.Session,
    request_timeout_seconds: int,
    resolve_cashflow_payload: ResolveCashflowPayload,
    log_debug: LogDebug,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """Execute numeric cashflow simulation tool call."""
    if read_only_tool_execution_cancelled():
        return _cashflow_execution_cancelled_result()
    tool_args = build_cashflow_tool_args(args)
    payload = copy.deepcopy(resolve_cashflow_payload(client_payload))
    if read_only_tool_execution_cancelled():
        return _cashflow_execution_cancelled_result()

    payload_override = tool_args.payload_override
    if isinstance(payload_override, dict):
        payload = deep_merge(payload, payload_override)

    payload.setdefault("simulation_config", {})
    payload["simulation_config"]["mode"] = tool_args.simulation_mode
    payload["simulation_config"]["num_simulations"] = tool_args.num_simulations
    if tool_args.seed is not None:
        payload["simulation_config"]["seed"] = tool_args.seed
    if tool_args.return_individual_runs is not None:
        payload["simulation_config"]["return_individual_runs"] = tool_args.return_individual_runs
    if tool_args.num_individual_runs is not None:
        payload["simulation_config"]["num_individual_runs"] = tool_args.num_individual_runs

    if tool_args.use_latest_asset_allocation and getattr(state, "latest_asset_allocation", None):
        payload["asset_allocation"] = state.latest_asset_allocation

    bank_balance_override = tool_args.bank_balance_override
    investment_balance_override = tool_args.investment_balance_override
    if bank_balance_override is not None:
        accounts = payload.setdefault("accounts", {})
        bank = accounts.get("bank", {})
        if isinstance(bank, list):
            if bank:
                bank[0]["balance"] = float(bank_balance_override)
            else:
                accounts["bank"] = [{"name": "Bank", "balance": float(bank_balance_override)}]
        else:
            accounts.setdefault("bank", {})["balance"] = float(bank_balance_override)
    if investment_balance_override is not None:
        accounts = payload.setdefault("accounts", {})
        brokerage = accounts.get("brokerage", {})
        if isinstance(brokerage, list):
            if brokerage:
                brokerage[0]["balance"] = float(investment_balance_override)
            else:
                accounts["brokerage"] = [
                    {"name": "Brokerage", "balance": float(investment_balance_override)}
                ]
        else:
            accounts.setdefault("brokerage", {})["balance"] = float(investment_balance_override)

    url = f"{_cashflow_model_url(config)}/cashflow/api/v1/simulate"
    active_session = session or http_session

    cache_key = compute_tool_cache_key(
        "cashflow",
        payload,
        namespace=CASHFLOW_CACHE_NAMESPACE,
    )
    request_envelope = {
        "call_id": cache_key,
        "requested_input": copy.deepcopy(args or {}),
        "effective_input": copy.deepcopy(payload),
    }
    cache_lock = _ensure_cache_coordination(state)
    with cache_lock:
        cached = state._tool_result_cache.get(cache_key)
        if cached is not None and hasattr(state._tool_result_cache, "move_to_end"):
            state._tool_result_cache.move_to_end(cache_key)
        inflight = None
        is_inflight_leader = False
        if cached is None:
            inflight = state._tool_inflight.get(cache_key)
            if inflight is None:
                inflight = Future()
                state._tool_inflight[cache_key] = inflight
                is_inflight_leader = True
    if cached is not None:
        def commit_cache_hit() -> None:
            if cached.get("success") and cached.get("full_result"):
                state.latest_cashflow_full = cached["full_result"]

        committed, _ = commit_read_only_tool_state(commit_cache_hit)
        if not committed:
            return _cashflow_execution_cancelled_result(request_envelope)
        log_debug(f"Cashflow cache HIT - skipping HTTP call (key={cache_key[:12]})")
        return copy.deepcopy(cached)

    if not is_inflight_leader:
        log_debug(f"Cashflow in-flight JOIN (key={cache_key[:12]})")
        while True:
            if read_only_tool_execution_cancelled():
                return _cashflow_execution_cancelled_result(request_envelope)
            try:
                shared_result = inflight.result(timeout=0.05)
                return copy.deepcopy(shared_result)
            except FutureTimeoutError:
                continue

    def complete_inflight(result: Dict[str, Any]) -> Dict[str, Any]:
        with cache_lock:
            active = state._tool_inflight.pop(cache_key, None)
            if active is not None and not active.done():
                active.set_result(copy.deepcopy(result))
        return result

    last_response = None
    for attempt in range(2):
        if read_only_tool_execution_cancelled():
            return complete_inflight(
                _cashflow_execution_cancelled_result(request_envelope)
            )
        try:
            response = active_session.post(
                url,
                json=payload,
                headers=build_cashflow_headers(config),
                timeout=effective_read_only_request_timeout(request_timeout_seconds),
            )
            if read_only_tool_execution_cancelled():
                return complete_inflight(
                    _cashflow_execution_cancelled_result(request_envelope)
                )
            last_response = response
            if response.status_code == 200:
                break
            if response.status_code in (500, 502, 503, 504) and attempt == 0:
                if wait_for_read_only_tool_cancellation(5):
                    return complete_inflight(
                        _cashflow_execution_cancelled_result(request_envelope)
                    )
                continue
            break
        except requests.Timeout as exc:
            if read_only_tool_execution_cancelled():
                return _cashflow_execution_cancelled_result(request_envelope)
            return complete_inflight({
                "success": False,
                "error": f"Cashflow API request timed out: {exc}",
            })
        except requests.ConnectionError as exc:
            if attempt == 0:
                if wait_for_read_only_tool_cancellation(5):
                    return complete_inflight(
                        _cashflow_execution_cancelled_result(request_envelope)
                    )
                continue
            return complete_inflight({
                "success": False,
                "error": f"Cashflow API connection failed: {exc}",
            })
        except requests.RequestException as exc:
            return complete_inflight({
                "success": False,
                "error": f"Cashflow API request failed: {exc}",
            })
        except Exception as exc:
            return complete_inflight({
                "success": False,
                "error": f"Cashflow API adapter failed: {exc}",
            })

    if last_response is None or last_response.status_code != 200:
        return complete_inflight({
            "success": False,
            "error": "Cashflow API call failed",
            "status_code": last_response.status_code if last_response else 0,
            "details": last_response.text[:600] if last_response else "No response",
        })

    try:
        full_result = last_response.json()
    except (TypeError, ValueError) as exc:
        return complete_inflight({
            "success": False,
            "error": f"Cashflow API returned malformed JSON: {exc}",
            "request": request_envelope,
        })
    if not isinstance(full_result, dict):
        return complete_inflight({
            "success": False,
            "error": "Cashflow API returned a non-object response",
        })
    if full_result.get("success") is not True:
        return complete_inflight({
            "success": False,
            "error": str(full_result.get("error") or "Cashflow model reported failure"),
            "request": request_envelope,
            "full_result": full_result,
        })
    try:
        normalized_result = normalize_cashflow_engine_response(full_result)
    except Exception as exc:
        return complete_inflight({
            "success": False,
            "error": f"Cashflow API response normalization failed: {exc}",
            "request": request_envelope,
            "full_result": full_result,
        })

    result = {
        "success": True,
        "request": request_envelope,
        "full_result": full_result,
        "normalized_result": normalized_result,
    }
    cached_result = copy.deepcopy(result)

    def commit_success() -> None:
        state.latest_cashflow_full = full_result
        with cache_lock:
            state._tool_result_cache[cache_key] = cached_result
            if hasattr(state._tool_result_cache, "move_to_end"):
                state._tool_result_cache.move_to_end(cache_key)
            max_entries = max(
                1,
                int(getattr(state, "_tool_result_cache_max_entries", 64)),
            )
            while len(state._tool_result_cache) > max_entries:
                if hasattr(state._tool_result_cache, "popitem"):
                    try:
                        state._tool_result_cache.popitem(last=False)
                    except TypeError:
                        oldest = next(iter(state._tool_result_cache))
                        del state._tool_result_cache[oldest]
                else:
                    oldest = next(iter(state._tool_result_cache))
                    del state._tool_result_cache[oldest]

    committed, _ = commit_read_only_tool_state(commit_success)
    if not committed:
        return complete_inflight(
            _cashflow_execution_cancelled_result(request_envelope)
        )
    log_debug(f"Cashflow cache STORE (key={cache_key[:12]})")
    return complete_inflight(result)
