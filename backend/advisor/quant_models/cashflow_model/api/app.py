from __future__ import annotations

import os
import random
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
from flask import Flask, jsonify, request

from full_model_bridge import LIFE_MODEL_VERSION, run_full_model


app = Flask(__name__)

_MC_PERCENTILES = (10, 25, 50, 75, 90)


def _default_monte_carlo_paths() -> int:
    """Monte Carlo paths when the request omits a count (see AWM_CASHFLOW_MONTE_CARLO_PATHS)."""
    try:
        return max(1, int(os.getenv("AWM_CASHFLOW_MONTE_CARLO_PATHS", "100")))
    except ValueError:
        return 100


def _contains_authorized_public_model_input(payload: Dict[str, Any]) -> bool:
    if "authorized_public_model_inputs" in payload:
        return True
    input_context = payload.get("input_context")
    if isinstance(input_context, dict) and "authorized_public_model_inputs" in input_context:
        return True
    scenario = payload.get("scenario")
    scenario_context = scenario.get("input_context") if isinstance(scenario, dict) else None
    return isinstance(scenario_context, dict) and (
        "authorized_public_model_inputs" in scenario_context
    )


def _authorized_public_model_input_transport_allowed() -> bool:
    expected_key = os.getenv("CASHFLOW_API_KEY", "").strip()
    supplied_key = request.headers.get("X-Api-Key", "").strip()
    if expected_key:
        return secrets.compare_digest(supplied_key, expected_key)
    return request.remote_addr in {"127.0.0.1", "::1"}


# Cashflow is a projection engine. Capital-market assumptions live in
# backend/domain/finance/capital_markets.py and are sourced from the same
# CMA workbook used by the asset allocation model.
def _resolve_backend_root() -> Path:
    """Find the backend root in both source-tree and Docker layouts."""
    override = os.getenv("AWM_BACKEND_ROOT", "").strip()
    if override:
        return Path(override).expanduser()

    current = Path(__file__).resolve()
    candidates = (current.parent, *current.parents)
    for candidate in candidates:
        if (candidate / "domain" / "finance" / "capital_markets.py").exists():
            return candidate

    return current.parents[2] if len(current.parents) > 2 else current.parent


_BACKEND_ROOT = _resolve_backend_root()
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from domain.finance.capital_markets import (  # noqa: E402
    allocation_assumptions as _shared_allocation_assumptions,
    blend_account_pool_allocation as _shared_blend_account_pool_allocation,
    load_capital_market_assumptions,
    normalize_weights as _shared_normalize_weights,
)

_CMA = load_capital_market_assumptions()
_ASSET_CLASS_ORDER = list(_CMA.asset_classes)
_EXPECTED_RETURNS: Dict[str, float] = dict(_CMA.expected_returns)
_EXPECTED_VOLATILITIES: Dict[str, float] = dict(_CMA.expected_volatilities)
_EXPECTED_CORRELATION: np.ndarray = _CMA.expected_correlation_matrix
_COVARIANCE_MATRIX: np.ndarray = _CMA.covariance_matrix


@dataclass
class SimulationInputs:
    age: int
    retirement_age: int
    life_expectancy: int
    salary: float
    bonus: float
    spouse_income: float
    income_growth: float
    annual_expenses: float
    expense_growth: float
    bank_balance: float
    brokerage_balance: float
    retirement_balance: float
    education_balance: float
    mortgage_balance: float
    monthly_housing_cost: float
    primary_401k_contrib_pct: float
    employer_match_pct: float
    effective_tax_rate: float
    emergency_reserve_months: float
    brokerage_return: float
    brokerage_volatility: float
    retirement_return: float
    retirement_volatility: float
    education_return: float
    education_volatility: float
    bank_return: float
    bank_volatility: float
    education_goal_amount: float
    education_goal_year: int | None
    one_off_expenses: List[Dict[str, Any]] = field(default_factory=list)


def _to_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def _to_int(value: Any, default: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return default
    return default


def _nested_get(payload: Dict[str, Any], path: List[str], default: Any = None) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return default
        if key not in current:
            return default
        current = current[key]
    return current


def _pick_first(payload: Dict[str, Any], paths: List[List[str]], default: Any = None) -> Any:
    for path in paths:
        value = _nested_get(payload, path, None)
        if value is not None:
            return value
    return default


def _parse_reserve_months(raw: Any) -> float:
    if isinstance(raw, str) and "-" in raw:
        parts = raw.split("-", 1)
        low = _to_float(parts[0], 6.0)
        high = _to_float(parts[1], low)
        return (low + high) / 2.0
    return _to_float(raw, 6.0)


def _normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
    return _shared_normalize_weights(weights)


def _allocation_assumptions(weights: Dict[str, float], default_return: float, default_vol: float) -> Tuple[float, float]:
    """Compatibility wrapper around the shared capital-markets estimator."""
    return _shared_allocation_assumptions(weights, default_return, default_vol)


def _sum_pool_balances(pool: Any) -> float:
    """Sum balances across an array-based account pool."""
    if not isinstance(pool, list):
        return 0.0
    total = 0.0
    for acct in pool:
        if isinstance(acct, dict):
            total += _to_float(acct.get("balance"), 0.0)
    return total


def _blend_pool_allocation(pool: Any) -> Dict[str, float]:
    """Compatibility wrapper around the shared account-pool blender."""
    return _shared_blend_account_pool_allocation(pool)


def _extract_pool_contrib(pool: Any) -> Tuple[float, float]:
    """Extract 401k-style contribution and match percentages from a retirement pool.

    Scans the list for the first account with non-zero contrib_percent.
    """
    if not isinstance(pool, list):
        return 0.0, 0.0
    for acct in pool:
        if not isinstance(acct, dict):
            continue
        contrib = _to_float(acct.get("contrib_percent"), 0.0)
        match = _to_float(acct.get("company_match_percent"), 0.0)
        if contrib > 0:
            if contrib > 1:
                contrib /= 100.0
            if match > 1:
                match /= 100.0
            return contrib, match
    return 0.0, 0.0


def _infer_assumptions(payload: Dict[str, Any]) -> SimulationInputs:
    client = payload.get("client_profile", {})
    if not isinstance(client, dict):
        client = {}
    accounts = payload.get("accounts", {})
    if not isinstance(accounts, dict):
        accounts = {}

    age = _to_int(_pick_first(payload, [["client_profile", "age"], ["client", "age"]], 35), 35)
    retirement_age = _to_int(
        _pick_first(payload, [["client_profile", "retirement_age"], ["client", "retirement_age"]], 65),
        65,
    )
    life_expectancy = _to_int(
        _pick_first(payload, [["client_profile", "life_expectancy"], ["client", "life_expectancy"]], 90),
        90,
    )

    salary = _to_float(
        _pick_first(payload, [["income", "salary"]], 0.0)
    )
    bonus = _to_float(
        _pick_first(payload, [["income", "bonus"]], 0.0)
    )
    spouse_income = _to_float(
        _pick_first(payload, [["income", "spouse_income"]], 0.0)
    )
    income_growth = _to_float(_pick_first(payload, [["income", "yearly_increase"]], 3.0)) / 100.0
    annual_expenses = _to_float(
        _pick_first(payload, [["expenses", "base_spending"]], 0.0)
    )
    expense_growth = _to_float(_pick_first(payload, [["expenses", "yearly_increase"]], 3.0)) / 100.0

    # --- Account balances (array-based pools) ---
    bank_pool = accounts.get("bank", [])
    brokerage_pool = accounts.get("brokerage", [])
    retirement_pool = accounts.get("retirement", [])
    education_pool = accounts.get("education", [])

    bank_balance = _sum_pool_balances(bank_pool)
    brokerage_balance = _sum_pool_balances(brokerage_pool)
    retirement_balance = _sum_pool_balances(retirement_pool)
    education_balance = _sum_pool_balances(education_pool)

    mortgage_balance = _to_float(
        _pick_first(payload, [["liabilities", "mortgage_balance"], ["expenses", "housing", "mortgage_balance"]], 0.0)
    )
    monthly_housing_cost = (
        _to_float(_pick_first(payload, [["expenses", "housing", "monthly_principal_interest"]], 0.0))
        + _to_float(_pick_first(payload, [["expenses", "housing", "monthly_property_tax_and_homeowners_insurance"]], 0.0))
    )

    # 401k contribution/match from retirement pool entries
    primary_401k_contrib_pct, employer_match_pct = _extract_pool_contrib(retirement_pool)

    gross_income = salary + bonus + spouse_income
    annual_net_take_home = (
        _to_float(_pick_first(payload, [["income", "net_monthly_take_home_min"]], 0.0))
        + _to_float(_pick_first(payload, [["income", "net_monthly_take_home_max"]], 0.0))
    ) / 2.0 * 12.0
    if gross_income > 0 and annual_net_take_home > 0:
        effective_tax_rate = max(0.10, min(0.45, 1.0 - annual_net_take_home / gross_income))
    else:
        effective_tax_rate = 0.27 if gross_income < 300000 else 0.31

    emergency_reserve_months = _parse_reserve_months(
        _pick_first(payload, [["preferences", "maintain_emergency_reserve_months"]], 6.0)
    )

    # --- Per-pool blended allocations → return & volatility ---
    brokerage_alloc = _blend_pool_allocation(brokerage_pool)
    retirement_alloc = _blend_pool_allocation(retirement_pool)
    education_alloc = _blend_pool_allocation(education_pool)

    # Default return/vol: use CMA expected values for a typical balanced portfolio
    brokerage_return, brokerage_volatility = _allocation_assumptions(brokerage_alloc, 0.07, 0.16)
    retirement_return, retirement_volatility = _allocation_assumptions(retirement_alloc, 0.06, 0.12)
    education_return, education_volatility = _allocation_assumptions(education_alloc, 0.06, 0.12)
    bank_return = _EXPECTED_RETURNS["Cash"]
    bank_volatility = _EXPECTED_VOLATILITIES["Cash"]

    current_year = 2026
    education_goal_amount = 0.0
    education_goal_year: int | None = None
    goals = payload.get("goals", [])
    if isinstance(goals, list):
        for goal in goals:
            if not isinstance(goal, dict):
                continue
            if str(goal.get("type", "")).strip().lower() != "education":
                continue
            amount = _to_float(goal.get("target_amount"), 0.0)
            if amount <= 0:
                notes = str(goal.get("notes", "") or "").lower()
                amount = 180000.0 if "in-state" in notes else 220000.0
            year = goal.get("target_year")
            if year is None and age >= 0:
                dependent_age = 0
                dependents_detail = client.get("dependents_detail", [])
                if isinstance(dependents_detail, list) and dependents_detail:
                    dependent_age = _to_int(dependents_detail[0].get("age"), 0)
                year = 2026 + max(1, 18 - dependent_age)
            education_goal_amount += amount
            if education_goal_year is None and year is not None:
                education_goal_year = _to_int(year, 2026 + 18)

    # Parse one-off (non-recurring) expenses
    one_off_expenses: List[Dict[str, Any]] = []
    if isinstance(goals, list):
        for goal in goals:
            if not isinstance(goal, dict):
                continue
            goal_type = str(goal.get("type", "")).strip().lower()
            if goal_type == "education":
                continue
            amount = _to_float(goal.get("target_amount") or goal.get("amount"), 0.0)
            year = goal.get("target_year") or goal.get("year")
            if amount > 0 and year is not None:
                one_off_expenses.append({
                    "year": _to_int(year, current_year + 5),
                    "amount": amount,
                    "label": goal.get("label", goal_type or "one-off expense"),
                })
    raw_one_off = payload.get("one_off_expenses", [])
    if isinstance(raw_one_off, list):
        for item in raw_one_off:
            if not isinstance(item, dict):
                continue
            amount = _to_float(item.get("amount"), 0.0)
            year = item.get("year")
            if amount > 0 and year is not None:
                one_off_expenses.append({
                    "year": _to_int(year, current_year + 5),
                    "amount": amount,
                    "label": item.get("label", "one-off expense"),
                })

    return SimulationInputs(
        age=age,
        retirement_age=max(retirement_age, age + 1),
        life_expectancy=max(life_expectancy, retirement_age + 1),
        salary=salary,
        bonus=bonus,
        spouse_income=spouse_income,
        income_growth=income_growth,
        annual_expenses=max(annual_expenses, 0.0),
        expense_growth=expense_growth,
        bank_balance=max(bank_balance, 0.0),
        brokerage_balance=max(brokerage_balance, 0.0),
        retirement_balance=max(retirement_balance, 0.0),
        education_balance=max(education_balance, 0.0),
        mortgage_balance=max(mortgage_balance, 0.0),
        monthly_housing_cost=max(monthly_housing_cost, 0.0),
        primary_401k_contrib_pct=max(primary_401k_contrib_pct, 0.0),
        employer_match_pct=max(employer_match_pct, 0.0),
        effective_tax_rate=effective_tax_rate,
        emergency_reserve_months=max(emergency_reserve_months, 1.0),
        brokerage_return=brokerage_return,
        brokerage_volatility=brokerage_volatility,
        retirement_return=retirement_return,
        retirement_volatility=retirement_volatility,
        education_return=education_return,
        education_volatility=education_volatility,
        bank_return=bank_return,
        bank_volatility=bank_volatility,
        education_goal_amount=max(education_goal_amount, 0.0),
        education_goal_year=education_goal_year,
        one_off_expenses=one_off_expenses,
    )


def _annual_return(mean: float, volatility: float, rng: random.Random, stochastic: bool) -> float:
    if not stochastic:
        return mean
    return max(-0.85, rng.gauss(mean, volatility))


def _round_money(value: float) -> float:
    return round(float(value), 2)


def _round_probability(value: float) -> float:
    return round(float(value), 4)


def _percentile_summary(values: List[float]) -> Dict[str, float | None]:
    if not values:
        return {f"p{pct}": None for pct in _MC_PERCENTILES}

    array = np.array(values, dtype=float)
    return {
        f"p{pct}": _round_money(float(np.percentile(array, pct)))
        for pct in _MC_PERCENTILES
    }


def _year_distribution(years: List[int | None], total_count: int) -> Dict[str, float]:
    if total_count <= 0:
        return {}

    counts: Dict[str, int] = {}
    for year in years:
        key = "never" if year is None else str(int(year))
        counts[key] = counts.get(key, 0) + 1

    ordered_keys = sorted((key for key in counts if key != "never"), key=int)
    if "never" in counts:
        ordered_keys.append("never")

    return {
        key: _round_probability(counts[key] / total_count)
        for key in ordered_keys
    }


def _expected_future_expense_key(
    expense_type: str,
    label: str,
    year: int,
    target_amount: float,
) -> str:
    normalized_label = str(label or expense_type).strip().lower().replace(" ", "_")
    return f"{expense_type}:{int(year)}:{normalized_label}:{round(float(target_amount), 2):.2f}"


def _build_non_recurring_expected_future_expense_definitions(
    inputs: SimulationInputs,
) -> List[Dict[str, Any]]:
    expenses: List[Dict[str, Any]] = []

    if inputs.education_goal_year is not None and inputs.education_goal_amount > 0:
        label = "education_expected_future_expense"
        expenses.append(
            {
                "key": _expected_future_expense_key(
                    "education",
                    label,
                    inputs.education_goal_year,
                    inputs.education_goal_amount,
                ),
                "type": "education",
                "label": label,
                "year": int(inputs.education_goal_year),
                "target_amount": _round_money(inputs.education_goal_amount),
            }
        )

    for expense in inputs.one_off_expenses:
        year = _to_int(expense.get("year"), 2026)
        amount = _to_float(expense.get("amount"), 0.0)
        if amount <= 0:
            continue
        label = str(expense.get("label", "one-off expense") or "one-off expense")
        expenses.append(
            {
                "key": _expected_future_expense_key("one_off", label, year, amount),
                "type": "one_off",
                "label": label,
                "year": int(year),
                "target_amount": _round_money(amount),
            }
        )

    return expenses


def _build_monte_carlo_milestones(inputs: SimulationInputs) -> List[Dict[str, Any]]:
    start_year = 2026
    end_year = start_year + max(0, inputs.life_expectancy - inputs.age) - 1
    milestones_by_year: Dict[int, Dict[str, Any]] = {}

    def add_milestone(year: int, label: str, horizon_years: int | None = None) -> None:
        if year < start_year or year > end_year:
            return
        entry = milestones_by_year.setdefault(
            int(year),
            {
                "year": int(year),
                "labels": [],
                "horizon_years": None,
            },
        )
        if label not in entry["labels"]:
            entry["labels"].append(label)
        if horizon_years is not None and entry["horizon_years"] is None:
            entry["horizon_years"] = int(horizon_years)

    for horizon in (1, 3, 5, 10):
        add_milestone(start_year + horizon - 1, f"{horizon}_year", horizon)

    for expense in _build_non_recurring_expected_future_expense_definitions(inputs):
        add_milestone(expense["year"], expense["label"])

    add_milestone(end_year, "life_expectancy")

    milestones = [milestones_by_year[year] for year in sorted(milestones_by_year)]
    for milestone in milestones:
        if milestone["horizon_years"] is None:
            milestone.pop("horizon_years")
    return milestones


def _build_path_milestones(
    yearly_snapshots: List[Dict[str, float]],
    milestones: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    snapshots_by_year = {
        int(snapshot["year"]): snapshot
        for snapshot in yearly_snapshots
        if "year" in snapshot
    }
    summarized: List[Dict[str, Any]] = []
    for milestone in milestones:
        snapshot = snapshots_by_year.get(int(milestone["year"]))
        if snapshot is None:
            continue

        balances = {
            "bank": _round_money(snapshot["bank"]),
            "brokerage": _round_money(snapshot["brokerage"]),
            "retirement": _round_money(snapshot["retirement"]),
            "education": _round_money(snapshot["education"]),
            "liquid_assets": _round_money(snapshot["bank"] + snapshot["brokerage"]),
            "total_assets": _round_money(snapshot["total_assets"]),
        }
        summarized.append(
            {
                "year": int(milestone["year"]),
                "labels": list(milestone["labels"]),
                **(
                    {"horizon_years": int(milestone["horizon_years"])}
                    if "horizon_years" in milestone
                    else {}
                ),
                "balances": balances,
            }
        )
    return summarized


def _select_representative_paths(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not results:
        return []

    terminal_values = np.array([path["ending_balance"] for path in results], dtype=float)
    target_specs = [
        ("downside", 10),
        ("median", 50),
        ("upside", 90),
    ]
    chosen_indices: set[int] = set()
    selected: List[Dict[str, Any]] = []

    for label, percentile in target_specs:
        target_value = float(np.percentile(terminal_values, percentile))
        best_idx = None
        best_distance = None
        for idx, path in enumerate(results):
            if idx in chosen_indices and len(chosen_indices) < len(results):
                continue
            distance = abs(path["ending_balance"] - target_value)
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_idx = idx

        if best_idx is None:
            best_idx = 0

        chosen_indices.add(best_idx)
        selected.append(
            {
                "label": label,
                "representative_percentile": f"p{percentile}",
                "simulation_index": int(best_idx),
                "path": results[best_idx],
            }
        )

    return selected


def _run_single_path(inputs: SimulationInputs, stochastic: bool, rng: random.Random) -> Dict[str, Any]:
    age = inputs.age
    bank = inputs.bank_balance
    brokerage = inputs.brokerage_balance
    retirement = inputs.retirement_balance
    education = inputs.education_balance
    salary = inputs.salary
    bonus = inputs.bonus
    spouse_income = inputs.spouse_income
    expenses = inputs.annual_expenses
    reserve_target = expenses / 12.0 * inputs.emergency_reserve_months if expenses > 0 else 0.0
    education_shortfall = 0.0
    yearly_snapshots: List[Dict[str, float]] = []
    non_recurring_expected_future_expense_outcomes: List[Dict[str, Any]] = []
    worst_drawdown_pct = 0.0
    running_peak = max(bank + brokerage + retirement + education, 0.0)
    reserve_breach_any = False
    liquid_assets_zero_before_retirement = False
    first_depletion_year: int | None = None
    first_shortfall_year: int | None = None
    years_below_reserve_target = 0
    years_investable_assets_funded_spending = 0

    current_year = 2026
    while age < inputs.life_expectancy:
        retired = age >= inputs.retirement_age
        gross_income = 0.0 if retired else salary + bonus + spouse_income
        employee_401k = 0.0 if retired else salary * inputs.primary_401k_contrib_pct
        employer_match = 0.0 if retired else salary * inputs.employer_match_pct
        net_income = gross_income * (1.0 - inputs.effective_tax_rate)
        spend_need = expenses
        annual_free_cash = net_income - spend_need

        bank += annual_free_cash
        retirement += employee_401k + employer_match
        investable_assets_funded_spending_this_year = False

        if inputs.education_goal_year is not None and current_year == inputs.education_goal_year:
            required = inputs.education_goal_amount
            covered = min(education + bank + brokerage, required)
            draw = required
            funding_gap = max(0.0, required - covered)
            non_recurring_expected_future_expense_outcomes.append(
                {
                    "key": _expected_future_expense_key(
                        "education",
                        "education_expected_future_expense",
                        current_year,
                        required,
                    ),
                    "type": "education",
                    "label": "education_expected_future_expense",
                    "year": int(current_year),
                    "expected_amount": _round_money(required),
                    "funded_amount": _round_money(covered),
                    "funding_gap": _round_money(funding_gap),
                    "covered": funding_gap <= 0.0,
                }
            )
            if funding_gap > 0.0 and first_shortfall_year is None:
                first_shortfall_year = int(current_year)

            use_education = min(education, draw)
            education -= use_education
            draw -= use_education

            use_bank = min(bank, draw)
            bank -= use_bank
            draw -= use_bank

            use_brokerage = min(brokerage, draw)
            brokerage -= use_brokerage
            draw -= use_brokerage

            education_shortfall += max(0.0, required - covered)

        # One-off (non-recurring) expenses — draw from bank → brokerage
        for expense in inputs.one_off_expenses:
            if expense["year"] == current_year:
                required = expense["amount"]
                eligible_resources = max(bank, 0.0) + max(brokerage, 0.0) + max(retirement, 0.0)
                covered = min(eligible_resources, required)
                funding_gap = max(0.0, required - covered)
                non_recurring_expected_future_expense_outcomes.append(
                    {
                        "key": _expected_future_expense_key(
                            "one_off",
                            expense["label"],
                            current_year,
                            required,
                        ),
                        "type": "one_off",
                        "label": expense["label"],
                        "year": int(current_year),
                        "expected_amount": _round_money(required),
                        "funded_amount": _round_money(covered),
                        "funding_gap": _round_money(funding_gap),
                        "covered": funding_gap <= 0.0,
                    }
                )
                if funding_gap > 0.0 and first_shortfall_year is None:
                    first_shortfall_year = int(current_year)
                draw = required

                use_bank = min(max(bank, 0.0), draw)
                bank -= use_bank
                draw -= use_bank

                use_brokerage = min(max(brokerage, 0.0), draw)
                brokerage -= use_brokerage
                draw -= use_brokerage
                if use_brokerage > 0.0:
                    investable_assets_funded_spending_this_year = True

                # Remaining shortfall reduces bank (goes negative → triggers
                # deficit handling below)
                if draw > 0:
                    bank -= draw

        if bank > reserve_target:
            brokerage += bank - reserve_target
            bank = reserve_target
        elif bank < 0:
            deficit = -bank
            bank = 0.0

            use_brokerage = min(brokerage, deficit)
            brokerage -= use_brokerage
            deficit -= use_brokerage

            use_retirement = min(retirement, deficit)
            retirement -= use_retirement
            deficit -= use_retirement
            if use_brokerage > 0.0 or use_retirement > 0.0:
                investable_assets_funded_spending_this_year = True

            if deficit > 0:
                bank = -deficit
                if first_shortfall_year is None:
                    first_shortfall_year = int(current_year)

        bank *= 1.0 + _annual_return(inputs.bank_return, inputs.bank_volatility, rng, stochastic)
        brokerage *= 1.0 + _annual_return(
            inputs.brokerage_return, inputs.brokerage_volatility, rng, stochastic
        )
        retirement *= 1.0 + _annual_return(
            inputs.retirement_return, inputs.retirement_volatility, rng, stochastic
        )
        education *= 1.0 + _annual_return(
            inputs.education_return, inputs.education_volatility, rng, stochastic
        )

        total_assets = bank + brokerage + retirement + education
        liquid_assets = bank + brokerage
        running_peak = max(running_peak, total_assets)
        if running_peak > 0:
            drawdown = max(0.0, (running_peak - total_assets) / running_peak)
            worst_drawdown_pct = max(worst_drawdown_pct, drawdown)
        if bank < reserve_target:
            reserve_breach_any = True
            years_below_reserve_target += 1
        if not retired and liquid_assets <= 0.0:
            liquid_assets_zero_before_retirement = True
        if first_depletion_year is None and total_assets <= 0.0:
            first_depletion_year = int(current_year)
        if investable_assets_funded_spending_this_year:
            years_investable_assets_funded_spending += 1
        yearly_snapshots.append(
            {
                "age": float(age),
                "year": float(current_year),
                "bank": round(bank, 2),
                "brokerage": round(brokerage, 2),
                "retirement": round(retirement, 2),
                "education": round(education, 2),
                "total_assets": round(total_assets, 2),
                "gross_income": round(gross_income, 2),
                "expenses": round(expenses, 2),
            }
        )

        salary *= 1.0 + inputs.income_growth
        bonus *= 1.0 + inputs.income_growth
        spouse_income *= 1.0 + inputs.income_growth
        expenses *= 1.0 + inputs.expense_growth
        reserve_target = expenses / 12.0 * inputs.emergency_reserve_months
        current_year += 1
        age += 1

    ending_balance = bank + brokerage + retirement + education
    shortfall = max(0.0, -ending_balance) + education_shortfall
    return {
        "ending_balance": round(max(0.0, ending_balance), 2),
        "shortfall": round(shortfall, 2),
        "success": shortfall <= 0.0 and ending_balance >= 0.0,
        "yearly_snapshots": yearly_snapshots,
        "reserve_breach_any": reserve_breach_any,
        "liquid_assets_zero_before_retirement": liquid_assets_zero_before_retirement,
        "first_depletion_year": first_depletion_year,
        "first_shortfall_year": first_shortfall_year,
        "non_recurring_expected_future_expense_outcomes": non_recurring_expected_future_expense_outcomes,
        "worst_drawdown_pct": _round_probability(worst_drawdown_pct),
        "years_below_reserve_target": int(years_below_reserve_target),
        "years_investable_assets_funded_spending": int(years_investable_assets_funded_spending),
    }


def _simulate(payload: Dict[str, Any]) -> Dict[str, Any]:
    inputs = _infer_assumptions(payload)
    simulation_config = payload.get("simulation_config", {})
    if not isinstance(simulation_config, dict):
        simulation_config = {}

    mode = str(simulation_config.get("mode", "deterministic") or "deterministic").strip()
    num_simulations = max(1, _to_int(simulation_config.get("num_simulations"), _default_monte_carlo_paths()))
    seed = _to_int(simulation_config.get("seed"), 42)
    stochastic = mode == "monte_carlo"

    if not stochastic:
        result = _run_single_path(inputs, stochastic=False, rng=random.Random(seed))
        return {
            "success": True,
            "simulation_mode": mode,
            "summary": {
                "non_recurring_expected_future_expense_shortfall": result["shortfall"],
                "non_recurring_expected_future_expense_success_probability": (
                    1.0 if result["success"] else 0.0
                ),
                "projected_terminal_value": result["ending_balance"],
                "ending_balance": result["ending_balance"],
                "shortfall": result["shortfall"],
                "success_probability": 1.0 if result["success"] else 0.0,
            },
            "details": {
                "inputs": inputs.__dict__,
                "yearly_snapshots": result["yearly_snapshots"],
            },
        }

    results = []
    success_count = 0
    terminal_values: List[float] = []
    shortfalls: List[float] = []
    for idx in range(num_simulations):
        path_result = _run_single_path(inputs, stochastic=True, rng=random.Random(seed + idx))
        results.append(path_result)
        terminal_values.append(path_result["ending_balance"])
        shortfalls.append(path_result["shortfall"])
        if path_result["success"]:
            success_count += 1

    terminal_values_array = np.array(terminal_values, dtype=float)
    shortfalls_array = np.array(shortfalls, dtype=float)
    median_terminal = float(np.percentile(terminal_values_array, 50))
    median_shortfall = float(np.percentile(shortfalls_array, 50))
    success_probability = success_count / len(results)
    milestones = _build_monte_carlo_milestones(inputs)

    milestone_percentile_trajectory: List[Dict[str, Any]] = []
    for milestone in milestones:
        bank_values: List[float] = []
        brokerage_values: List[float] = []
        retirement_values: List[float] = []
        education_values: List[float] = []
        liquid_values: List[float] = []
        total_asset_values: List[float] = []
        for path_result in results:
            snapshot = next(
                (
                    item
                    for item in path_result["yearly_snapshots"]
                    if int(item["year"]) == int(milestone["year"])
                ),
                None,
            )
            if snapshot is None:
                continue
            bank_values.append(snapshot["bank"])
            brokerage_values.append(snapshot["brokerage"])
            retirement_values.append(snapshot["retirement"])
            education_values.append(snapshot["education"])
            liquid_values.append(snapshot["bank"] + snapshot["brokerage"])
            total_asset_values.append(snapshot["total_assets"])

        if not total_asset_values:
            continue

        milestone_percentile_trajectory.append(
            {
                "year": int(milestone["year"]),
                "labels": list(milestone["labels"]),
                **(
                    {"horizon_years": int(milestone["horizon_years"])}
                    if "horizon_years" in milestone
                    else {}
                ),
                "balances": {
                    "bank": _percentile_summary(bank_values),
                    "brokerage": _percentile_summary(brokerage_values),
                    "retirement": _percentile_summary(retirement_values),
                    "education": _percentile_summary(education_values),
                    "liquid_assets": _percentile_summary(liquid_values),
                    "total_assets": _percentile_summary(total_asset_values),
                },
            }
        )

    expected_future_expense_definitions = _build_non_recurring_expected_future_expense_definitions(
        inputs
    )
    non_recurring_expected_future_expense_diagnostics: List[Dict[str, Any]] = []
    for expected_future_expense in expected_future_expense_definitions:
        funding_gaps: List[float] = []
        covered_count = 0
        for path_result in results:
            expense_outcome = next(
                (
                    outcome
                    for outcome in path_result["non_recurring_expected_future_expense_outcomes"]
                    if outcome["key"] == expected_future_expense["key"]
                ),
                None,
            )
            if expense_outcome is None:
                funding_gaps.append(expected_future_expense["target_amount"])
                continue
            funding_gaps.append(expense_outcome["funding_gap"])
            if expense_outcome["covered"]:
                covered_count += 1

        non_recurring_expected_future_expense_diagnostics.append(
            {
                "key": expected_future_expense["key"],
                "type": expected_future_expense["type"],
                "label": expected_future_expense["label"],
                "expected_year": int(expected_future_expense["year"]),
                "expected_amount": _round_money(expected_future_expense["target_amount"]),
                "probability_covered": _round_probability(covered_count / len(results)),
                "funding_gap_percentiles": _percentile_summary(funding_gaps),
            }
        )

    representative_paths = []
    for selection in _select_representative_paths(results):
        path_result = selection["path"]
        representative_paths.append(
            {
                "label": selection["label"],
                "representative_percentile": selection["representative_percentile"],
                "simulation_index": selection["simulation_index"],
                "ending_balance": _round_money(path_result["ending_balance"]),
                "shortfall": _round_money(path_result["shortfall"]),
                "success": bool(path_result["success"]),
                "milestones": _build_path_milestones(path_result["yearly_snapshots"], milestones),
            }
        )

    return {
        "success": True,
        "simulation_mode": mode,
        "summary": {
            "non_recurring_expected_future_expense_shortfall": round(median_shortfall, 2),
            "non_recurring_expected_future_expense_success_probability": round(
                success_probability, 4
            ),
            "projected_terminal_value": round(median_terminal, 2),
            "ending_balance": round(median_terminal, 2),
            "shortfall": round(median_shortfall, 2),
            "success_probability": round(success_probability, 4),
        },
        "details": {
            "inputs": inputs.__dict__,
            "num_simulations": len(results),
            "terminal_value_percentiles": _percentile_summary(terminal_values),
            "shortfall_percentiles": _percentile_summary(shortfalls),
            "milestone_percentile_trajectory": milestone_percentile_trajectory,
            "failure_diagnostics": {
                "probability_bank_breaches_reserve_floor": _round_probability(
                    sum(1 for path in results if path["reserve_breach_any"]) / len(results)
                ),
                "probability_liquid_assets_hit_zero_before_retirement": _round_probability(
                    sum(
                        1
                        for path in results
                        if path["liquid_assets_zero_before_retirement"]
                    ) / len(results)
                ),
                "first_depletion_year_distribution": _year_distribution(
                    [path["first_depletion_year"] for path in results],
                    len(results),
                ),
                "first_shortfall_year_distribution": _year_distribution(
                    [path["first_shortfall_year"] for path in results],
                    len(results),
                ),
            },
            "non_recurring_expected_future_expense_diagnostics": (
                non_recurring_expected_future_expense_diagnostics
            ),
            "representative_path_summaries": representative_paths,
            "risk_features": {
                "worst_drawdown_percentiles": _percentile_summary(
                    [path["worst_drawdown_pct"] * 100.0 for path in results]
                ),
                "years_below_reserve_target_percentiles": _percentile_summary(
                    [float(path["years_below_reserve_target"]) for path in results]
                ),
                "years_investable_assets_forced_to_fund_spending_percentiles": _percentile_summary(
                    [float(path["years_investable_assets_funded_spending"]) for path in results]
                ),
                "probability_investable_assets_forced_to_fund_spending": _round_probability(
                    sum(
                        1
                        for path in results
                        if path["years_investable_assets_funded_spending"] > 0
                    ) / len(results)
                ),
            },
        },
    }


@app.get("/health")
def health() -> Tuple[Any, int]:
    return jsonify({"ok": True, "service": "cashflow-model-api", "model": "life_model.LifeModel", "model_version": LIFE_MODEL_VERSION}), 200


@app.post("/cashflow/api/v1/simulate")
def simulate() -> Tuple[Any, int]:
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"success": False, "error": "Request JSON body is required"}), 400
    if not isinstance(payload, dict):
        return jsonify({"success": False, "error": "Request JSON body must be an object"}), 400
    if (
        _contains_authorized_public_model_input(payload)
        and not _authorized_public_model_input_transport_allowed()
    ):
        return jsonify(
            {
                "success": False,
                "error": "Authorized public model inputs require trusted server transport",
                "code": "FORBIDDEN",
            }
        ), 403
    try:
        return jsonify(run_full_model(payload)), 200
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc), "code": "VALIDATION_ERROR"}), 400
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc), "code": "MODEL_RUNTIME_ERROR"}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8001"))
    app.run(host="0.0.0.0", port=port, debug=False)
