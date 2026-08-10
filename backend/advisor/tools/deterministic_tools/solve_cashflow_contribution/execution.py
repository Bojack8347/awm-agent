"""Pure bounded-search logic for recurring cash-flow contributions."""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, Optional


CandidateEvaluator = Callable[[float], Dict[str, Any]]


def solve_monthly_contribution(
    *,
    evaluate: CandidateEvaluator,
    objective: str,
    target_terminal_value: Optional[float],
    minimum_success_probability: float,
    minimum_p10_liquidity: float,
    maximum_monthly_contribution: float,
    monthly_tolerance: float,
    max_iterations: int = 16,
) -> Dict[str, Any]:
    """Bisect a validated model boundary and disclose finite search bounds."""

    if objective not in {"maximum_sustainable", "minimum_for_terminal_goal"}:
        return {"success": False, "error": "unsupported_contribution_objective"}
    if objective == "minimum_for_terminal_goal" and target_terminal_value is None:
        return {"success": False, "error": "target_terminal_value_required"}
    if (
        not _finite(maximum_monthly_contribution)
        or maximum_monthly_contribution <= 0
        or not _finite(monthly_tolerance)
        or monthly_tolerance <= 0
    ):
        return {"success": False, "error": "invalid_solver_bounds"}

    cache: Dict[float, Dict[str, Any]] = {}

    def test(monthly: float) -> Dict[str, Any]:
        key = round(float(monthly), 8)
        if key not in cache:
            raw = evaluate(key)
            cache[key] = _assess_candidate(
                monthly=key,
                analysis=raw,
                objective=objective,
                target_terminal_value=target_terminal_value,
                minimum_success_probability=minimum_success_probability,
                minimum_p10_liquidity=minimum_p10_liquidity,
            )
        return cache[key]

    low = 0.0
    high = float(maximum_monthly_contribution)
    low_result = test(low)
    high_result = test(high)
    if not low_result["valid"] or not high_result["valid"]:
        return {
            "success": False,
            "error": "candidate_analysis_invalid",
            "tested_points": _ordered_points(cache),
        }

    if objective == "maximum_sustainable":
        if not low_result["feasible"]:
            return _solver_result(
                objective=objective,
                status="baseline_infeasible",
                selected=None,
                lower_bound=None,
                upper_bound=0.0,
                target_terminal_value=target_terminal_value,
                cache=cache,
                tolerance=monthly_tolerance,
            )
        if high_result["feasible"]:
            return _solver_result(
                objective=objective,
                status="search_ceiling_feasible",
                selected=high_result,
                lower_bound=high,
                upper_bound=None,
                target_terminal_value=target_terminal_value,
                cache=cache,
                tolerance=monthly_tolerance,
            )
        low_feasible = True
    else:
        if low_result["feasible"]:
            return _solver_result(
                objective=objective,
                status="baseline_satisfies_target",
                selected=low_result,
                lower_bound=0.0,
                upper_bound=0.0,
                target_terminal_value=target_terminal_value,
                cache=cache,
                tolerance=monthly_tolerance,
            )
        if not high_result["feasible"]:
            return _solver_result(
                objective=objective,
                status="target_not_reached_within_search_ceiling",
                selected=None,
                lower_bound=None,
                upper_bound=high,
                target_terminal_value=target_terminal_value,
                cache=cache,
                tolerance=monthly_tolerance,
            )
        low_feasible = False

    for _index in range(max(1, min(int(max_iterations), 32))):
        if high - low <= monthly_tolerance:
            break
        midpoint = (low + high) / 2.0
        result = test(midpoint)
        if not result["valid"]:
            return {
                "success": False,
                "error": "candidate_analysis_invalid",
                "tested_points": _ordered_points(cache),
            }
        if result["feasible"] == low_feasible:
            low = midpoint
            low_result = result
        else:
            high = midpoint
            high_result = result

    if not _is_monotonic(cache, objective):
        return {
            "success": False,
            "error": "feasibility_not_monotonic",
            "tested_points": _ordered_points(cache),
            "note": (
                "A single bisection boundary cannot be supported by these model "
                "results; use an explicit scenario grid instead."
            ),
        }

    selected = low_result if objective == "maximum_sustainable" else high_result
    return _solver_result(
        objective=objective,
        status="bounded_solution",
        selected=selected,
        lower_bound=low,
        upper_bound=high,
        target_terminal_value=target_terminal_value,
        cache=cache,
        tolerance=monthly_tolerance,
    )


def _assess_candidate(
    *,
    monthly: float,
    analysis: Dict[str, Any],
    objective: str,
    target_terminal_value: Optional[float],
    minimum_success_probability: float,
    minimum_p10_liquidity: float,
) -> Dict[str, Any]:
    status = analysis.get("status") if isinstance(analysis.get("status"), dict) else {}
    metrics = analysis.get("metrics") if isinstance(analysis.get("metrics"), dict) else {}
    success_probability = _metric_number(metrics, "success_probability")
    minimum_liquidity = _metric_number(metrics, "minimum_liquidity")
    shortfall = _metric_number(metrics, "shortfall")
    terminal_value = _metric_percentile(metrics, "terminal_value_percentiles", "p50")
    valid = bool(
        status.get("execution") == "succeeded"
        and status.get("validation") not in {"failed", "invalid_request", "missing_data"}
        and all(
            value is not None
            for value in (
                success_probability,
                minimum_liquidity,
                shortfall,
                terminal_value,
            )
        )
    )
    constraints = {
        "success_probability": (
            success_probability is not None
            and success_probability >= minimum_success_probability
        ),
        "median_shortfall": shortfall is not None and shortfall <= 1e-6,
        "p10_minimum_liquidity": (
            minimum_liquidity is not None
            and minimum_liquidity >= minimum_p10_liquidity
        ),
    }
    if objective == "minimum_for_terminal_goal":
        constraints["median_terminal_value"] = bool(
            terminal_value is not None
            and target_terminal_value is not None
            and terminal_value >= target_terminal_value
        )
    return {
        "monthly_contribution": monthly,
        "annual_contribution": monthly * 12.0,
        "valid": valid,
        "feasible": bool(valid and all(constraints.values())),
        "constraints": constraints,
        "metrics": {
            "success_probability": success_probability,
            "median_shortfall": shortfall,
            "p10_minimum_liquidity": minimum_liquidity,
            "median_terminal_value": terminal_value,
        },
        "_analysis": analysis,
    }


def _solver_result(
    *,
    objective: str,
    status: str,
    selected: Optional[Dict[str, Any]],
    lower_bound: Optional[float],
    upper_bound: Optional[float],
    target_terminal_value: Optional[float],
    cache: Dict[float, Dict[str, Any]],
    tolerance: float,
) -> Dict[str, Any]:
    selected_public = _public_point(selected) if selected is not None else None
    boundary_interpretation = _boundary_interpretation(
        objective=objective,
        status=status,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
        cache=cache,
    )
    return {
        "success": True,
        "selected_analysis": selected.get("_analysis") if selected else None,
        "selected_monthly_contribution": (
            selected.get("monthly_contribution") if selected else None
        ),
        "full_result": {
            "schema_version": "awm.cashflow_contribution_solver.v1",
            "objective": objective,
            "status": status,
            "selected": selected_public,
            "monthly_boundary": {
                "lower_tested_boundary": lower_bound,
                "upper_tested_boundary": upper_bound,
                "search_tolerance": tolerance,
                "unit": "USD_per_month",
                "interpretation": (
                    "For maximum_sustainable, the lower boundary is feasible and "
                    "the upper boundary is infeasible when both are present. For "
                    "minimum_for_terminal_goal, the lower boundary misses the target "
                    "and the upper boundary satisfies it when both are present."
                ),
            },
            "boundary_interpretation": boundary_interpretation,
            "target_terminal_value": target_terminal_value,
            "tested_points": _ordered_points(cache),
            "methodology": {
                "method": "validated_bisection_with_common_model_configuration",
                "arithmetic_authority": "deterministic_solver",
                "model_authority": "cashflow_model",
                "search_ceiling_policy": (
                    "If the ceiling remains feasible, the result is reported as at "
                    "least that amount, not as an exact maximum."
                ),
                "monotonicity_policy": (
                    "The solver abstains if observed feasibility changes direction "
                    "across tested points."
                ),
            },
            "limitations": [
                "The result is conditional on the selected model assumptions, path count, seed, contribution window, and explicit constraints.",
                "A monthly label is implemented as one annual contribution equal to twelve monthly amounts; within-year monthly timing is not modeled.",
                "This bounded numerical result is reporting-only unless a separately versioned recommendation policy permits advice narration.",
            ],
        },
    }


def _boundary_interpretation(
    *,
    objective: str,
    status: str,
    lower_bound: Optional[float],
    upper_bound: Optional[float],
    cache: Dict[float, Dict[str, Any]],
) -> Dict[str, Any]:
    """Explain the tested boundary without promoting it into advice."""

    ceiling = max(cache) if cache else None
    failed_point: Optional[Dict[str, Any]] = None
    known_feasible_interval: Optional[Dict[str, Any]] = None
    transition_interval: Optional[Dict[str, Any]] = None
    zero_boundary_meaning: Optional[str] = None
    search_ceiling_meaning: Optional[str] = None

    if status == "baseline_infeasible":
        failed_point = _cached_point(cache, 0.0)
        zero_boundary_meaning = (
            "With no additional monthly contribution, the baseline already failed "
            "at least one supplied constraint. Under this solver's required "
            "boundary direction, no positive feasible interval was "
            "established; the zero boundary does not prescribe a contribution."
        )
    elif status == "search_ceiling_feasible":
        known_feasible_interval = _interval(0.0, lower_bound)
        search_ceiling_meaning = (
            "The requested search ceiling still passed every supplied constraint. "
            "It is the end of the tested range, not the household's true maximum."
        )
    elif status == "baseline_satisfies_target":
        known_feasible_interval = _interval(0.0, 0.0)
        zero_boundary_meaning = (
            "The no-additional-contribution baseline already met the supplied "
            "terminal target and other constraints. This means the minimum tested "
            "additional contribution is zero; it does not prescribe a savings amount."
        )
    elif status == "target_not_reached_within_search_ceiling":
        failed_point = _cached_point(cache, upper_bound)
        search_ceiling_meaning = (
            "The requested search ceiling still failed at least one supplied "
            "constraint. The result establishes only that no target-meeting point "
            "was found inside the tested range."
        )
    elif status == "bounded_solution":
        if objective == "maximum_sustainable":
            failed_point = _cached_point(cache, upper_bound)
            known_feasible_interval = _interval(0.0, lower_bound)
            transition_interval = _transition(
                lower_bound,
                upper_bound,
                lower_state="feasible",
                upper_state="infeasible",
            )
        else:
            failed_point = _cached_point(cache, lower_bound)
            known_feasible_interval = _interval(upper_bound, ceiling)
            transition_interval = _transition(
                lower_bound,
                upper_bound,
                lower_state="below_target_or_constraint_failed",
                upper_state="target_and_constraints_satisfied",
            )

    failed_constraints = _failed_constraints(failed_point)
    constraint_labels = [
        {
            "constraint_key": key,
            "label": _constraint_label(key),
        }
        for key in failed_constraints
    ]
    explanation = {
        "baseline_infeasible": (
            "The baseline is the binding failed point for this bounded search."
        ),
        "search_ceiling_feasible": (
            "Every tested point through the ceiling was feasible; the true maximum "
            "was not located."
        ),
        "baseline_satisfies_target": (
            "The first tested point already met the target, so no positive minimum "
            "was needed inside this search."
        ),
        "target_not_reached_within_search_ceiling": (
            "The ceiling is a failed tested point, not proof that the target is "
            "unreachable outside the search range."
        ),
        "bounded_solution": (
            "The adjacent tested points bracket the feasibility transition within "
            "the stated tolerance."
        ),
    }.get(status, "")
    return {
        "explanation": explanation,
        "known_feasible_monthly_interval": known_feasible_interval,
        "transition_interval": transition_interval,
        "binding_failed_constraints": constraint_labels,
        "primary_binding_failed_constraint": (
            constraint_labels[0] if constraint_labels else None
        ),
        "zero_boundary_meaning": zero_boundary_meaning,
        "search_ceiling_meaning": search_ceiling_meaning,
    }


def _cached_point(
    cache: Dict[float, Dict[str, Any]],
    monthly: Optional[float],
) -> Optional[Dict[str, Any]]:
    if monthly is None:
        return None
    key = round(float(monthly), 8)
    return cache.get(key)


def _failed_constraints(point: Optional[Dict[str, Any]]) -> list[str]:
    constraints = (
        point.get("constraints")
        if isinstance(point, dict) and isinstance(point.get("constraints"), dict)
        else {}
    )
    return [
        key
        for key in (
            "success_probability",
            "median_shortfall",
            "p10_minimum_liquidity",
            "median_terminal_value",
        )
        if constraints.get(key) is False
    ]


def _constraint_label(key: str) -> str:
    return {
        "success_probability": "minimum modeled success probability",
        "median_shortfall": "zero median shortfall",
        "p10_minimum_liquidity": "minimum p10 liquidity",
        "median_terminal_value": "minimum median terminal value",
    }.get(key, key.replace("_", " "))


def _interval(
    minimum: Optional[float],
    maximum: Optional[float],
) -> Optional[Dict[str, Any]]:
    if minimum is None or maximum is None:
        return None
    return {
        "minimum": minimum,
        "maximum": maximum,
        "unit": "USD_per_month",
        "meaning": "known_tested_feasible_interval",
    }


def _transition(
    lower: Optional[float],
    upper: Optional[float],
    *,
    lower_state: str,
    upper_state: str,
) -> Optional[Dict[str, Any]]:
    if lower is None or upper is None:
        return None
    return {
        "lower": lower,
        "upper": upper,
        "unit": "USD_per_month",
        "lower_state": lower_state,
        "upper_state": upper_state,
    }


def _ordered_points(cache: Dict[float, Dict[str, Any]]):
    return [_public_point(cache[key]) for key in sorted(cache)]


def _public_point(value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    return {key: item for key, item in value.items() if not key.startswith("_")}


def _is_monotonic(cache: Dict[float, Dict[str, Any]], objective: str) -> bool:
    flags = [cache[key]["feasible"] for key in sorted(cache)]
    if objective == "maximum_sustainable":
        return flags == sorted(flags, reverse=True)
    return flags == sorted(flags)


def _metric_number(metrics: Dict[str, Any], key: str) -> Optional[float]:
    metric = metrics.get(key) if isinstance(metrics.get(key), dict) else {}
    return _number(metric.get("value"))


def _metric_percentile(
    metrics: Dict[str, Any],
    key: str,
    percentile: str,
) -> Optional[float]:
    metric = metrics.get(key) if isinstance(metrics.get(key), dict) else {}
    values = metric.get("value") if isinstance(metric.get("value"), dict) else {}
    return _number(values.get(percentile))


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _finite(value: Any) -> bool:
    return _number(value) is not None
