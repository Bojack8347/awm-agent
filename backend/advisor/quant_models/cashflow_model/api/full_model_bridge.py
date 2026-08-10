"""AWM transport adapter for the migrated cashflow-model Agent contract."""
from __future__ import annotations

import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

def _default_monte_carlo_paths() -> int:
    """Monte Carlo paths when the caller does not specify a count.

    100 is the standardized default and the count percentile output is graded
    against. AWM_CASHFLOW_MONTE_CARLO_PATHS lowers it for fast local iteration
    (e.g. 10); results at low path counts are too noisy to read as p10/p50/p90.
    """
    try:
        return max(1, int(os.getenv("AWM_CASHFLOW_MONTE_CARLO_PATHS", "100")))
    except ValueError:
        return 100


ENGINE_ROOT = Path(__file__).resolve().parent.parent
ENGINE_SRC = ENGINE_ROOT / "src"
if str(ENGINE_SRC) not in sys.path:
    sys.path.insert(0, str(ENGINE_SRC))

from life_model import __version__ as LIFE_MODEL_VERSION
from life_model.cli import agent_run_request
from life_model.config.config_manager import config as life_model_config
from life_model.montecarlo import MarketAssumptions


_ARC_DEFAULT_PROJECTION_COLUMNS = (
    "Net Worth",
    "Cashflow Shortfall Debt",
    "Bank Balance",
)
_ARC_DETAIL_REPORT_COLUMNS = {
    "income": (
        "Income",
        "One-time Income",
        "SS Income",
        "Pension Income",
        "Total Cash Inflows",
    ),
    "spending": (
        "Spending",
        "Base Living Spending",
        "One-time Expenses",
        "Housing",
        "Total Cash Outflows",
    ),
    "taxes": (
        "Taxes",
        "Federal Taxes",
        "State Taxes",
        "SS Taxes",
        "Medicare Taxes",
    ),
    "withdrawals": (
        "401k Withdrawals",
        "RMDs",
        "529 Withdrawals",
        "Early Withdrawal Penalties",
    ),
    "account_balances": (
        "Brokerage Balance",
        "Investment Balance",
        "401k Balance",
        "Traditional IRA Balance",
        "Roth IRA Balance",
        "Total Assets",
        "Total Liabilities",
    ),
    "mortgage": (
        "Mortgage Balance",
        "Mortgage Payments",
        "Mortgage Principal Paid",
        "Mortgage Interest Paid",
        "Housing",
    ),
}

_AUTHORIZED_PUBLIC_MODEL_INPUT_KEYS = {
    "schema_version",
    "variable_key",
    "value",
    "unit",
    "jurisdiction",
    "effective_year",
    "session_fact_id",
    "content_sha256",
    "expires_at",
    "sources",
}
_AUTHORIZED_PUBLIC_MODEL_SOURCE_KEYS = {
    "publisher",
    "title",
    "url",
    "published_at",
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _required_number(source: Dict[str, Any], key: str, path: str) -> float:
    if key not in source or source.get(key) is None:
        raise ValueError(f"{path} is required; AWM does not apply a financial default")
    if isinstance(source.get(key), bool):
        raise ValueError(f"{path} must be a finite number")
    try:
        value = float(source[key])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path} must be a finite number") from exc
    if not math.isfinite(value):
        raise ValueError(f"{path} must be a finite number")
    return value


def _integer(value: Any, default: int) -> int:
    try:
        return int(float(value)) if value is not None else default
    except (TypeError, ValueError):
        return default


def _configured_number(path: str, fallback: float) -> float:
    """Read one validated LifeModel financial assumption as a finite number."""

    value = _number(life_model_config.financial.get(path, fallback), fallback)
    if not math.isfinite(value):
        raise ValueError(f"LifeModel configured assumption {path} must be finite")
    return value


def _aware_datetime(value: Any, path: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{path} must be an ISO-8601 timestamp") from exc
    else:
        raise ValueError(f"{path} must be an ISO-8601 timestamp")
    if parsed.tzinfo is None:
        raise ValueError(f"{path} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _validated_authorized_public_model_inputs(
    raw_inputs: Any,
    *,
    projection_start_year: int,
) -> List[Dict[str, Any]]:
    """Validate and sanitize the server-resolved model-input envelope."""

    if raw_inputs is None:
        return []
    if not isinstance(raw_inputs, list) or len(raw_inputs) != 1:
        raise ValueError("authorized_public_model_inputs must contain exactly one item")
    raw = raw_inputs[0]
    if not isinstance(raw, dict) or set(raw) != _AUTHORIZED_PUBLIC_MODEL_INPUT_KEYS:
        raise ValueError("authorized_public_model_inputs[0] has an invalid server envelope")
    if raw.get("schema_version") != "awm.authorized_public_model_input.v1":
        raise ValueError("authorized_public_model_inputs[0].schema_version is unsupported")
    if raw.get("variable_key") != "social_security_taxable_maximum":
        raise ValueError("authorized public model input variable is unsupported")
    if raw.get("unit") != "USD_annual" or raw.get("jurisdiction") != "US":
        raise ValueError("social_security_taxable_maximum requires USD_annual in US")
    effective_year = raw.get("effective_year")
    if isinstance(effective_year, bool) or not isinstance(effective_year, int):
        raise ValueError("authorized public model input effective_year must be an integer")
    if effective_year != projection_start_year:
        raise ValueError(
            "authorized public model input effective_year must match projection start_year"
        )
    value = raw.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("authorized public model input value must be a number")
    numeric_value = float(value)
    if not math.isfinite(numeric_value) or numeric_value <= 0:
        raise ValueError("authorized public model input value must be finite and positive")
    session_fact_id = raw.get("session_fact_id")
    if not isinstance(session_fact_id, str) or re.fullmatch(
        r"session-public-fact:[a-f0-9]{32}", session_fact_id
    ) is None:
        raise ValueError("authorized public model input session_fact_id is invalid")
    content_sha256 = raw.get("content_sha256")
    if not isinstance(content_sha256, str) or re.fullmatch(
        r"sha256:[a-f0-9]{64}", content_sha256
    ) is None:
        raise ValueError("authorized public model input content_sha256 is invalid")
    expires_at = _aware_datetime(
        raw.get("expires_at"),
        "authorized_public_model_inputs[0].expires_at",
    )
    if expires_at <= datetime.now(timezone.utc):
        raise ValueError("authorized public model input session authorization has expired")

    raw_sources = raw.get("sources")
    if not isinstance(raw_sources, list) or not 1 <= len(raw_sources) <= 3:
        raise ValueError("authorized public model input requires one to three sources")
    sources: List[Dict[str, Any]] = []
    for index, raw_source in enumerate(raw_sources):
        if (
            not isinstance(raw_source, dict)
            or set(raw_source) != _AUTHORIZED_PUBLIC_MODEL_SOURCE_KEYS
        ):
            raise ValueError(
                f"authorized_public_model_inputs[0].sources[{index}] is invalid"
            )
        publisher = raw_source.get("publisher")
        title = raw_source.get("title")
        url = raw_source.get("url")
        if not isinstance(publisher, str) or not publisher.strip() or len(publisher) > 240:
            raise ValueError("authorized public model input source publisher is invalid")
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            raise ValueError("authorized public model input source title is invalid")
        if not isinstance(url, str) or not 8 <= len(url) <= 2000:
            raise ValueError("authorized public model input source URL is invalid")
        parsed_url = urlsplit(url)
        if parsed_url.scheme.lower() != "https" or not parsed_url.hostname:
            raise ValueError("authorized public model input source URL must use HTTPS")
        raw_published_at = raw_source.get("published_at")
        published_at = (
            _aware_datetime(
                raw_published_at,
                f"authorized_public_model_inputs[0].sources[{index}].published_at",
            ).isoformat()
            if raw_published_at is not None
            else None
        )
        sources.append(
            {
                "publisher": publisher.strip(),
                "title": title.strip(),
                "url": url,
                "published_at": published_at,
            }
        )

    return [
        {
            "schema_version": "awm.authorized_public_model_input.v1",
            "variable_key": "social_security_taxable_maximum",
            "value": numeric_value,
            "unit": "USD_annual",
            "jurisdiction": "US",
            "effective_year": effective_year,
            "content_sha256": content_sha256,
            "sources": sources,
        }
    ]


def _record_resolved_assumption(
    assumptions: List[str],
    resolved: List[Dict[str, Any]],
    *,
    parameter: str,
    label: str,
    value: Any,
    unit: str,
    source: str,
) -> None:
    """Record the effective value and provenance of one bridge-owned default."""

    if unit == "percent_annual":
        display = f"{float(value):.2f}% annually"
    elif unit == "age_years":
        display = f"age {int(value)}"
    elif unit == "calendar_year":
        display = str(int(value))
    else:
        display = str(value)
    assumptions.append(
        f"{label} defaults to {display} because no client assumption was supplied "
        f"(source: {source})"
    )
    resolved.append(
        {
            "parameter": parameter,
            "value": value,
            "unit": unit,
            "source": source,
            "reason": "client_value_not_supplied",
        }
    )


def _record_effective_parameter(
    assumptions: List[str],
    resolved: List[Dict[str, Any]],
    *,
    parameter: str,
    value: Any,
    unit: str,
    source: str,
    reason: str,
    disclosure: str,
) -> None:
    """Expose an effective parameter even when it is not a missing-input default."""

    assumptions.append(disclosure)
    resolved.append(
        {
            "parameter": parameter,
            "value": value,
            "unit": unit,
            "source": source,
            "reason": reason,
        }
    )


def _normalized_marital_status(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "mfj": "married",
        "married_filing_jointly": "married",
        "married": "married",
        "single": "single",
        "unmarried": "single",
        "partnered": "partnered",
        "domestic_partner": "partnered",
    }
    return aliases.get(normalized, normalized)


def _json_safe_tax_brackets(raw_brackets: Any) -> List[Dict[str, Any]]:
    brackets: List[Dict[str, Any]] = []
    for entry in raw_brackets if isinstance(raw_brackets, list) else []:
        if not isinstance(entry, (list, tuple)) or len(entry) != 3:
            continue
        lower, upper, rate = entry
        upper_value = _number(upper)
        brackets.append(
            {
                "taxable_income_over": _number(lower),
                "taxable_income_not_over": (
                    upper_value if math.isfinite(upper_value) else None
                ),
                "rate_percent": _number(rate),
            }
        )
    return brackets


def _record_new_york_tax_parameters(
    assumptions: List[str],
    resolved: List[Dict[str, Any]],
    *,
    filing_key: str,
) -> None:
    """Disclose the configured NY resident schedule used by LifeModel."""

    policy_year = int(_configured_number("tax.state.tax_year", 2025))
    standard_deduction = _configured_number(
        f"tax.state.standard_deduction.{filing_key}",
        0.0,
    )
    brackets = _json_safe_tax_brackets(
        life_model_config.financial.get(f"tax.state.tax_brackets.{filing_key}", [])
    )
    tax_table = life_model_config.financial.get("tax.state.tax_table", {})
    high_income = life_model_config.financial.get(
        f"tax.state.high_income_tax_computation.{filing_key}",
        {},
    )
    minimum_rate = brackets[0]["rate_percent"] if brackets else None
    maximum_rate = brackets[-1]["rate_percent"] if brackets else None
    schedule = {
        "jurisdiction": "NY",
        "policy_year": policy_year,
        "filing_status": filing_key,
        "standard_deduction": standard_deduction,
        "tax_table": tax_table if isinstance(tax_table, dict) else {},
        "brackets": brackets,
        "high_income_tax_computation": (
            high_income if isinstance(high_income, dict) else {}
        ),
        "social_security_benefits_taxed": False,
        "future_policy_changes_modeled": False,
    }
    _record_effective_parameter(
        assumptions,
        resolved,
        parameter="new_york_state_tax_schedule",
        value=schedule,
        unit="configured_resident_tax_schedule",
        source="life_model.config.tax.state",
        reason="configured_tax_policy",
        disclosure=(
            f"New York state tax uses the configured {policy_year} {filing_key.replace('_', ' ')} "
            f"resident schedule, a ${standard_deduction:,.0f} standard deduction, "
            f"progressive rates from {minimum_rate:.2f}% to {maximum_rate:.2f}%, and "
            "configured high-income recapture; policy is held constant across projection years"
        ),
    )


def _configured_investment_growth(accounts: Dict[str, Any]) -> tuple[float, str]:
    """Balance-weight configured brokerage/education defaults without inventing a mix."""

    configured = {
        "brokerage": (
            _configured_number("accounts.brokerage.default_growth_rate", 7.0),
            "life_model.config.accounts.brokerage.default_growth_rate",
        ),
        "education": (
            _configured_number("accounts.plan_529.default_growth_rate", 7.0),
            "life_model.config.accounts.plan_529.default_growth_rate",
        ),
    }
    weighted_total = 0.0
    balance_total = 0.0
    sources: List[str] = []
    for kind, (rate, source) in configured.items():
        balance = _balance(accounts, kind)
        if balance <= 0:
            continue
        weighted_total += balance * rate
        balance_total += balance
        sources.append(source)
    if balance_total <= 0:
        return configured["brokerage"]
    return weighted_total / balance_total, " + ".join(sources)


def _allocation_expected_return(allocation: Dict[str, float]) -> tuple[float, Dict[str, float]]:
    """Resolve the weighted configured return for an exact target allocation."""

    rates = _life_model_return_rates_for_allocation(allocation)
    return sum(float(weight) * rates[asset] for asset, weight in allocation.items()), rates


def _balance(accounts: Dict[str, Any], kind: str) -> float:
    entries = accounts.get(kind, [])
    if not isinstance(entries, list):
        return 0.0
    return sum(_number(item.get("balance")) for item in entries if isinstance(item, dict))


def _account_assumption(accounts: Dict[str, Any], kind: str, *keys: str) -> Optional[float]:
    entries = accounts.get(kind, [])
    if not isinstance(entries, list):
        return None
    funded = [
        item
        for item in entries
        if isinstance(item, dict) and _number(item.get("balance")) > 0
    ]
    if not funded:
        return None
    weighted_total = 0.0
    balance_total = 0.0
    for item in funded:
        balance = _number(item.get("balance"))
        raw_value = next((item.get(key) for key in keys if item.get(key) is not None), None)
        if raw_value is None:
            return None
        value = _number(raw_value)
        if abs(value) <= 1:
            value *= 100.0
        weighted_total += balance * value
        balance_total += balance
    return weighted_total / balance_total if balance_total > 0 else None


def _combined_account_assumption(
    accounts: Dict[str, Any],
    kinds: tuple[str, ...],
    *keys: str,
) -> Optional[float]:
    weighted_total = 0.0
    balance_total = 0.0
    for kind in kinds:
        balance = _balance(accounts, kind)
        if balance <= 0:
            continue
        assumption = _account_assumption(accounts, kind, *keys)
        if assumption is None:
            return None
        weighted_total += balance * assumption
        balance_total += balance
    return weighted_total / balance_total if balance_total > 0 else None


def _combined_asset_allocation(
    accounts: Dict[str, Any],
    *kinds: str,
) -> Optional[Dict[str, float]]:
    """Balance-weight exact account allocations without inventing a residual mix."""

    weighted: Dict[str, float] = {}
    total_balance = 0.0
    for kind in kinds:
        entries = accounts.get(kind, [])
        if not isinstance(entries, list):
            continue
        for item in entries:
            if not isinstance(item, dict):
                continue
            balance = _number(item.get("balance"))
            if balance <= 0:
                continue
            allocation = item.get("asset_allocation")
            if not isinstance(allocation, dict):
                allocation = item.get("allocation")
            if not isinstance(allocation, dict) or not allocation:
                return None
            normalized: Dict[str, float] = {}
            for asset_name, raw_weight in allocation.items():
                if isinstance(raw_weight, bool):
                    raise ValueError(
                        f"Asset allocation weight for {asset_name} must be a finite number"
                    )
                weight = _number(raw_weight, float("nan"))
                if not math.isfinite(weight) or weight < 0:
                    raise ValueError(
                        f"Asset allocation weight for {asset_name} must be finite and nonnegative"
                    )
                normalized[str(asset_name)] = weight
            allocation_total = sum(normalized.values())
            if math.isclose(allocation_total, 1.0, abs_tol=0.001):
                allocation_scale = 1.0
            elif math.isclose(allocation_total, 100.0, abs_tol=0.001):
                allocation_scale = 100.0
            else:
                raise ValueError(
                    "Asset allocation weights must use exact decimal or percentage units "
                    f"and sum to 1.0 or 100.0, got {allocation_total}"
                )
            for asset_name, weight in normalized.items():
                weighted[asset_name] = weighted.get(asset_name, 0.0) + (
                    balance * weight / allocation_scale
                )
            total_balance += balance
    if total_balance <= 0:
        return None
    return {asset_name: value / total_balance for asset_name, value in weighted.items()}


def _life_model_return_rates_for_allocation(
    allocation: Dict[str, float],
) -> Dict[str, float]:
    """Resolve per-asset deterministic rates from LifeModel's own assumptions."""

    assumptions = MarketAssumptions.create_default()
    unknown = sorted(set(allocation) - set(assumptions.asset_classes))
    if unknown:
        raise ValueError(
            "LifeModel cannot accept target allocation asset classes: "
            + ", ".join(unknown)
        )
    return {
        asset_name: assumptions.asset_classes[asset_name].expected_return * 100.0
        for asset_name in allocation
    }


def awm_payload_to_agent_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Map AWM's stable payload to the full model's supported Agent JSON input."""
    if payload.get("user_profile") is not None or payload.get("scenario") is not None:
        if payload.get("authorized_public_model_inputs") is not None:
            raise ValueError(
                "authorized_public_model_inputs must enter through the AWM server payload"
            )
        result = dict(payload)
        result.setdefault("schema_version", 1)
        result.setdefault("projection", {"systems": "both", "format": "json"})
        return result

    client = payload.get("client_profile") if isinstance(payload.get("client_profile"), dict) else {}
    income = payload.get("income") if isinstance(payload.get("income"), dict) else {}
    expenses = payload.get("expenses") if isinstance(payload.get("expenses"), dict) else {}
    accounts = payload.get("accounts") if isinstance(payload.get("accounts"), dict) else {}
    config = payload.get("simulation_config") if isinstance(payload.get("simulation_config"), dict) else {}
    goals = payload.get("goals") if isinstance(payload.get("goals"), list) else []
    one_off_expenses = (
        payload.get("one_off_expenses")
        if isinstance(payload.get("one_off_expenses"), list)
        else []
    )
    income_events = (
        payload.get("income_events")
        if isinstance(payload.get("income_events"), list)
        else []
    )
    spending_growth_events = (
        payload.get("spending_growth_events")
        if isinstance(payload.get("spending_growth_events"), list)
        else []
    )
    recurring_investment_contributions = (
        payload.get("recurring_investment_contributions")
        if isinstance(payload.get("recurring_investment_contributions"), list)
        else []
    )
    awm_contract = (
        payload.get("awm_input_contract")
        if isinstance(payload.get("awm_input_contract"), dict)
        else {}
    )
    liabilities = (
        payload.get("liabilities")
        if isinstance(payload.get("liabilities"), dict)
        else {}
    )
    assumptions = [
        str(item) for item in awm_contract.get("assumptions", []) if item
    ]
    resolved_assumptions: List[Dict[str, Any]] = []
    unknowns = [
        str(item) for item in awm_contract.get("unsupported_inputs", []) if item
    ]
    allocation_model_inputs: List[Dict[str, Any]] = []

    mortgage_balance = _number(liabilities.get("mortgage_balance"))

    if str(config.get("mode", "deterministic")).lower() == "monte_carlo":
        missing_allocations = [
            kind
            for kind in ("brokerage", "retirement", "education")
            if _balance(accounts, kind) > 0
            and _combined_asset_allocation(accounts, kind) is None
        ]
        if missing_allocations:
            raise ValueError(
                "Monte Carlo requires an exact asset allocation for every funded "
                "investment pool; missing allocation for: "
                + ", ".join(missing_allocations)
                + ". No deterministic growth default was used as a stochastic allocation."
            )

    current_year = datetime.now(timezone.utc).year
    start_year = _integer(payload.get("start_year"), current_year)
    authorized_public_model_inputs = _validated_authorized_public_model_inputs(
        payload.get("authorized_public_model_inputs"),
        projection_start_year=start_year,
    )
    if payload.get("start_year") is None:
        _record_resolved_assumption(
            assumptions,
            resolved_assumptions,
            parameter="projection_start_year",
            label="Projection start year",
            value=start_year,
            unit="calendar_year",
            source="awm_bridge.current_utc_year",
        )
    if authorized_public_model_inputs:
        public_input = authorized_public_model_inputs[0]
        _record_effective_parameter(
            assumptions,
            resolved_assumptions,
            parameter="social_security_taxable_maximum",
            value=public_input["value"],
            unit="USD_annual",
            source="agent_selected_session_public_fact",
            reason="agent_selected_session_model_input",
            disclosure=(
                "Social Security taxable maximum uses the agent-selected session public fact "
                f"for {public_input['effective_year']} and is held constant by the "
                "current-law projection"
            ),
        )
    age = int(_required_number(client, "age", "client_profile.age"))
    retirement_age = int(
        _required_number(client, "retirement_age", "client_profile.retirement_age")
    )
    salary = _required_number(income, "salary", "income.salary")
    annual_spending = _required_number(
        expenses, "base_spending", "expenses.base_spending"
    )
    if age <= 0 or age > 120:
        raise ValueError("client_profile.age must be between 1 and 120")
    if retirement_age <= 0 or retirement_age > 120:
        raise ValueError("client_profile.retirement_age must be between 1 and 120")
    if salary < 0 or annual_spending < 0:
        raise ValueError("income.salary and expenses.base_spending cannot be negative")

    marital_status = _normalized_marital_status(client.get("marital_status"))
    spouse_age_value = client.get("spouse_age")
    spouse_age: Optional[int] = None
    if spouse_age_value is not None:
        spouse_age = int(
            _required_number(client, "spouse_age", "client_profile.spouse_age")
        )
        if spouse_age <= 0 or spouse_age > 120:
            raise ValueError("client_profile.spouse_age must be between 1 and 120")
    if marital_status == "married" and spouse_age is None:
        raise ValueError(
            "client_profile.spouse_age is required for a married household projection"
        )
    if marital_status == "single" and spouse_age is not None:
        raise ValueError(
            "client_profile.marital_status conflicts with client_profile.spouse_age"
        )

    married_household = spouse_age is not None and marital_status != "partnered"
    filing_key = "married_filing_jointly" if married_household else "single"
    _record_effective_parameter(
        assumptions,
        resolved_assumptions,
        parameter="tax_filing_status",
        value=filing_key,
        unit="filing_status",
        source=(
            "awm_client_file.client_profile.spouse_age"
            if married_household
            else "awm_bridge.single_filing_unit"
        ),
        reason=(
            "derived_from_confirmed_spouse"
            if married_household
            else "no_joint_filing_spouse_supplied"
        ),
        disclosure=(
            "Tax filing status is married filing jointly because a spouse is present in "
            "the confirmed Client File"
            if married_household
            else "Tax filing status is single because no joint-filing spouse was supplied"
        ),
    )
    if marital_status == "partnered" and spouse_age is not None:
        assumptions.append(
            "A non-marital partner is not treated as a married-filing-jointly tax spouse"
        )

    end_year_value = payload.get("end_year")
    if end_year_value is None:
        life_expectancy = client.get("life_expectancy")
        if life_expectancy is None:
            life_expectancy = 95
            _record_resolved_assumption(
                assumptions,
                resolved_assumptions,
                parameter="life_expectancy",
                label="Projection end age",
                value=life_expectancy,
                unit="age_years",
                source="awm_bridge.estimate_grade_life_expectancy",
            )
        end_year = start_year + max(1, int(float(life_expectancy)) - age)
    else:
        end_year = _integer(end_year_value, start_year)

    income_growth_value = income.get("growth_rate", income.get("yearly_increase"))
    if income_growth_value is None:
        income_growth_value = _configured_number(
            "employment.salary.default_yearly_increase",
            3.0,
        )
        _record_resolved_assumption(
            assumptions,
            resolved_assumptions,
            parameter="income_growth_rate",
            label="Income growth",
            value=income_growth_value,
            unit="percent_annual",
            source="life_model.config.employment.salary.default_yearly_increase",
        )
    expense_growth_value = expenses.get("growth_rate", expenses.get("yearly_increase"))
    if expense_growth_value is None:
        expense_growth_value = _configured_number("economy.inflation_rate", 0.0)
        _record_resolved_assumption(
            assumptions,
            resolved_assumptions,
            parameter="spending_growth_rate",
            label="Spending growth",
            value=expense_growth_value,
            unit="percent_annual",
            source="life_model.config.economy.inflation_rate",
        )

    state_value = client.get("state")
    if not state_value:
        state_value = str(life_model_config.financial.get("tax.state.jurisdiction", "NY"))
        _record_resolved_assumption(
            assumptions,
            resolved_assumptions,
            parameter="state_tax_jurisdiction",
            label="State tax jurisdiction",
            value=state_value,
            unit="jurisdiction",
            source="life_model.config.tax.state.jurisdiction",
        )
    if str(state_value).strip().upper() == "NY":
        _record_new_york_tax_parameters(
            assumptions,
            resolved_assumptions,
            filing_key=filing_key,
        )

    cash_interest = _account_assumption(
        accounts,
        "bank",
        "interest_rate",
        "expected_return",
        "growth_rate",
    )
    if cash_interest is None:
        cash_interest = _configured_number("accounts.bank.default_interest_rate", 0.0)
        _record_resolved_assumption(
            assumptions,
            resolved_assumptions,
            parameter="cash_interest_rate",
            label="Cash interest",
            value=cash_interest,
            unit="percent_annual",
            source="life_model.config.accounts.bank.default_interest_rate",
        )

    education_balance = _balance(accounts, "education")
    profile: Dict[str, Any] = {
        "start_year": start_year,
        "end_year": end_year,
        "family_name": str(client.get("family_name") or "AWM Client Cashflow Scenario"),
        "person": {
            "id": "primary",
            "name": str(client.get("name") or "Primary Client"),
            "age": age,
            "retirement_age": retirement_age,
            "state": str(state_value),
        },
        "income": {
            "salary": salary,
            "yearly_increase": _number(income_growth_value),
            "company": str(income.get("company") or "Employer"),
            "role": str(income.get("role") or "Employee"),
        },
        "spending": {
            "annual_base": annual_spending,
            "yearly_increase": _number(expense_growth_value),
        },
        "cash": {
            "checking_balance": _balance(accounts, "bank"),
            "checking_interest_rate": cash_interest,
        },
    }
    if mortgage_balance > 0:
        housing = (
            expenses.get("housing")
            if isinstance(expenses.get("housing"), dict)
            else {}
        )
        defaultable_mortgage_fields = (
            "home_value",
            "home_appreciation_rate",
            "mortgage_interest_rate",
            "mortgage_remaining_term_years",
            "mortgage_type",
            "annual_spending_includes_mortgage",
        )
        missing_mortgage_fields = [
            field_name
            for field_name in defaultable_mortgage_fields
            if housing.get(field_name) is None
        ]
        mortgage_defaults_authorized = (
            awm_contract.get("mortgage_defaults_authorized") is True
        )
        if missing_mortgage_fields and not mortgage_defaults_authorized:
            raise ValueError(
                "Existing mortgage inputs are missing: "
                + ", ".join(missing_mortgage_fields)
                + ". The user must supply these values or explicitly state that they "
                "cannot provide them and authorize configured mortgage defaults."
            )
        if missing_mortgage_fields:
            _record_effective_parameter(
                assumptions,
                resolved_assumptions,
                parameter="mortgage_defaults_authorization",
                value={
                    "authorized": True,
                    "defaulted_fields": missing_mortgage_fields,
                },
                unit="explicit_user_fallback_authorization",
                source="awm_input_contract.current_user_turn",
                reason="user_cannot_provide_values_or_requested_configured_defaults",
                disclosure=(
                    "The user explicitly authorized configured mortgage fallback assumptions "
                    "for the missing fields: "
                    + ", ".join(missing_mortgage_fields)
                ),
            )
        mortgage_defaults_path = "housing.opening_mortgage_defaults"
        if housing.get("home_value") is None:
            home_value_multiple = _configured_number(
                f"{mortgage_defaults_path}.home_value_to_mortgage_balance_multiple",
                1.0,
            )
            if home_value_multiple <= 0:
                raise ValueError(
                    "LifeModel configured opening-mortgage home-value multiple must be positive"
                )
            home_value = mortgage_balance * home_value_multiple
            _record_effective_parameter(
                assumptions,
                resolved_assumptions,
                parameter="home_value",
                value={
                    "mortgage_balance": mortgage_balance,
                    "home_value_to_mortgage_balance_multiple": home_value_multiple,
                    "effective_home_value": home_value,
                },
                unit="derived_currency_proxy",
                source=(
                    "life_model.config.housing.opening_mortgage_defaults."
                    "home_value_to_mortgage_balance_multiple"
                ),
                reason="client_value_not_supplied_balance_only_mortgage_proxy",
                disclosure=(
                    f"Current home value was not supplied; AWM uses ${home_value:,.2f}, "
                    f"or {home_value_multiple:.2f} times the ${mortgage_balance:,.2f} "
                    "mortgage balance, as an estimate-grade property-value proxy"
                ),
            )
        else:
            home_value = _required_number(
                housing,
                "home_value",
                "expenses.housing.home_value",
            )
        if housing.get("home_appreciation_rate") is None:
            home_appreciation_rate = _configured_number(
                f"{mortgage_defaults_path}.home_appreciation_rate",
                3.0,
            )
            _record_resolved_assumption(
                assumptions,
                resolved_assumptions,
                parameter="home_appreciation_rate",
                label="Home appreciation",
                value=home_appreciation_rate,
                unit="percent_annual",
                source=(
                    "life_model.config.housing.opening_mortgage_defaults."
                    "home_appreciation_rate"
                ),
            )
        else:
            home_appreciation_rate = _required_number(
                housing,
                "home_appreciation_rate",
                "expenses.housing.home_appreciation_rate",
            )
        if housing.get("mortgage_interest_rate") is None:
            mortgage_interest_rate = _configured_number(
                f"{mortgage_defaults_path}.interest_rate",
                6.5,
            )
            _record_resolved_assumption(
                assumptions,
                resolved_assumptions,
                parameter="mortgage_interest_rate",
                label="Mortgage interest rate",
                value=mortgage_interest_rate,
                unit="percent_annual",
                source=(
                    "life_model.config.housing.opening_mortgage_defaults.interest_rate"
                ),
            )
        else:
            mortgage_interest_rate = _required_number(
                housing,
                "mortgage_interest_rate",
                "expenses.housing.mortgage_interest_rate",
            )
        if housing.get("mortgage_remaining_term_years") is None:
            remaining_term_value = _configured_number(
                f"{mortgage_defaults_path}.remaining_term_years",
                30,
            )
            _record_resolved_assumption(
                assumptions,
                resolved_assumptions,
                parameter="mortgage_remaining_term_years",
                label="Remaining mortgage term",
                value=int(remaining_term_value),
                unit="years",
                source=(
                    "life_model.config.housing.opening_mortgage_defaults."
                    "remaining_term_years"
                ),
            )
        else:
            remaining_term_value = _required_number(
                housing,
                "mortgage_remaining_term_years",
                "expenses.housing.mortgage_remaining_term_years",
            )
        if home_value <= 0:
            raise ValueError("expenses.housing.home_value must be positive")
        if home_appreciation_rate <= -100 or home_appreciation_rate > 100:
            raise ValueError(
                "expenses.housing.home_appreciation_rate must be greater than -100 and at most 100"
            )
        if mortgage_interest_rate < 0 or mortgage_interest_rate > 100:
            raise ValueError(
                "expenses.housing.mortgage_interest_rate must be between 0 and 100 percentage points"
            )
        if remaining_term_value <= 0 or not float(remaining_term_value).is_integer():
            raise ValueError(
                "expenses.housing.mortgage_remaining_term_years must be a positive whole number"
            )
        raw_mortgage_type = housing.get("mortgage_type")
        if raw_mortgage_type is None:
            raw_mortgage_type = life_model_config.financial.get(
                f"{mortgage_defaults_path}.mortgage_type",
                "fixed_rate",
            )
            _record_resolved_assumption(
                assumptions,
                resolved_assumptions,
                parameter="mortgage_type",
                label="Mortgage type",
                value=str(raw_mortgage_type),
                unit="mortgage_type",
                source=(
                    "life_model.config.housing.opening_mortgage_defaults.mortgage_type"
                ),
            )
        mortgage_type = str(raw_mortgage_type or "").strip().lower()
        normalized_mortgage_type = mortgage_type.replace("-", "_").replace(" ", "_")
        if normalized_mortgage_type not in {
            "fixed",
            "fixed_rate",
            "fixed_rate_mortgage",
        }:
            raise ValueError(
                "expenses.housing.mortgage_type must be fixed_rate for an opening-position mortgage"
            )
        spending_includes_mortgage = housing.get("annual_spending_includes_mortgage")
        if spending_includes_mortgage is None:
            spending_includes_mortgage = life_model_config.financial.get(
                f"{mortgage_defaults_path}.annual_spending_includes_mortgage",
                True,
            )
            _record_resolved_assumption(
                assumptions,
                resolved_assumptions,
                parameter="annual_spending_includes_mortgage",
                label="Annual-spending mortgage treatment",
                value=spending_includes_mortgage,
                unit="boolean",
                source=(
                    "life_model.config.housing.opening_mortgage_defaults."
                    "annual_spending_includes_mortgage"
                ),
            )
        if not isinstance(spending_includes_mortgage, bool):
            raise ValueError(
                "expenses.housing.annual_spending_includes_mortgage must be a boolean"
            )
        mortgage: Dict[str, Any] = {
            "principal_balance": mortgage_balance,
            "yearly_interest_rate": mortgage_interest_rate,
            "remaining_term_years": int(remaining_term_value),
            "mortgage_type": "fixed_rate",
        }
        monthly_payment_value = housing.get("monthly_principal_interest")
        if monthly_payment_value is not None:
            monthly_payment = _required_number(
                housing,
                "monthly_principal_interest",
                "expenses.housing.monthly_principal_interest",
            )
            if monthly_payment <= 0:
                raise ValueError(
                    "expenses.housing.monthly_principal_interest must be positive"
                )
            mortgage["monthly_payment"] = monthly_payment
        else:
            _record_effective_parameter(
                assumptions,
                resolved_assumptions,
                parameter="mortgage_monthly_principal_and_interest",
                value={
                    "principal_balance": mortgage_balance,
                    "annual_interest_rate_percent": mortgage_interest_rate,
                    "remaining_term_years": int(remaining_term_value),
                },
                unit="derived_fixed_rate_payment",
                source="life_model.housing.Mortgage.get_monthly_payment",
                reason="derived_from_effective_mortgage_terms",
                disclosure=(
                    "Monthly mortgage principal and interest is derived by LifeModel from "
                    "the effective balance, fixed annual interest rate, and remaining term; "
                    "each effective term is separately disclosed as confirmed or configured"
                ),
            )
        profile_housing: Dict[str, Any] = {
            "status": "own",
            "name": str(housing.get("name") or "Primary Home"),
            "current_value": home_value,
            "value_yearly_increase": home_appreciation_rate,
            "mortgage": mortgage,
            "mortgage_included_in_annual_spending": spending_includes_mortgage,
            "expenses": {},
        }
        tax_basis_value = housing.get("tax_basis")
        if tax_basis_value is not None:
            tax_basis = _required_number(
                housing,
                "tax_basis",
                "expenses.housing.tax_basis",
            )
            if tax_basis < 0:
                raise ValueError("expenses.housing.tax_basis cannot be negative")
            profile_housing["tax_basis"] = tax_basis
        profile["housing"] = profile_housing
        assumptions.append(
            "Opening home value and fixed-rate mortgage principal are modeled from "
            "confirmed Client File inputs"
        )
        assumptions.append(
            "Confirmed annual spending includes mortgage principal and interest, so "
            "LifeModel removes scheduled mortgage payments from base living spending "
            "before adding modeled mortgage cash flows"
            if spending_includes_mortgage
            else "Confirmed annual spending excludes mortgage principal and interest, so "
            "LifeModel adds modeled mortgage cash flows to base living spending"
        )
    if married_household and spouse_age is not None:
        spouse_retirement_age = int(
            _number(client.get("spouse_retirement_age"), retirement_age)
        )
        if spouse_retirement_age <= 0 or spouse_retirement_age > 120:
            raise ValueError(
                "client_profile.spouse_retirement_age must be between 1 and 120"
            )
        salary_scope = str(income.get("salary_scope") or "").strip().lower()
        spouse_salary = 0.0
        if (
            salary_scope != "household_total"
            and income.get("spouse_income") is not None
        ):
            spouse_salary = _required_number(
                income,
                "spouse_income",
                "income.spouse_income",
            )
            if spouse_salary < 0:
                raise ValueError("income.spouse_income cannot be negative")
        profile["spouse"] = {
            "id": "spouse",
            "name": str(client.get("spouse_name") or "Spouse"),
            "age": spouse_age,
            "retirement_age": spouse_retirement_age,
            "state": str(client.get("spouse_state") or state_value),
            "income": {
                "salary": spouse_salary,
                "yearly_increase": _number(income_growth_value),
                "company": str(income.get("spouse_company") or "Spouse Employer"),
                "role": str(income.get("spouse_role") or "Employee"),
            },
            "spending": {"annual_base": 0.0, "yearly_increase": 0.0},
            "cash": {},
        }
        if spouse_salary == 0.0:
            _record_effective_parameter(
                assumptions,
                resolved_assumptions,
                parameter="household_income_worker_allocation",
                value="all_income_assigned_to_primary",
                unit="income_allocation_method",
                source="awm_bridge.household_income_mapping",
                reason="worker_level_income_split_not_supplied",
                disclosure=(
                    "Household income is assigned to the primary earner because a worker-level "
                    "income split was not supplied; joint federal and New York income tax uses "
                    "the household total, while payroll-tax estimates remain approximate"
                ),
            )

    retirement = _balance(accounts, "retirement")
    if retirement > 0:
        retirement_allocation = _combined_asset_allocation(accounts, "retirement")
        retirement_growth = _account_assumption(
            accounts, "retirement", "expected_return", "growth_rate", "average_growth"
        )
        if retirement_growth is None:
            if retirement_allocation is not None:
                retirement_growth, retirement_return_rates = _allocation_expected_return(
                    retirement_allocation
                )
                retirement_growth_source = "life_model.config.market_assumptions.asset_classes"
            else:
                retirement_growth = _configured_number(
                    "retirement.ira.default_growth_rate",
                    7.0,
                )
                retirement_return_rates = None
                retirement_growth_source = (
                    "life_model.config.retirement.ira.default_growth_rate"
                )
            _record_resolved_assumption(
                assumptions,
                resolved_assumptions,
                parameter="retirement_growth_rate",
                label="Aggregated retirement-account growth",
                value=retirement_growth,
                unit="percent_annual",
                source=retirement_growth_source,
            )
        else:
            retirement_return_rates = None
        retirement_account: Dict[str, Any] = {
            "pretax_balance": retirement,
            "pretax_contrib_percent": _number(income.get("retirement_contribution_pct")),
            "company_match_percent": _number(income.get("employer_match_pct")),
            "average_growth": retirement_growth,
        }
        if retirement_allocation is not None:
            retirement_account["asset_allocation"] = retirement_allocation
            retirement_account["asset_return_rates"] = (
                retirement_return_rates
                or _life_model_return_rates_for_allocation(retirement_allocation)
            )
            allocation_model_inputs.append(
                {
                    "account": "retirement.401k",
                    "target_allocation": retirement_allocation,
                    "return_rate_source": "life_model.market_assumptions.default",
                }
            )
        profile["retirement"] = {"401k": retirement_account}
    invested = _balance(accounts, "brokerage") + education_balance
    if invested > 0:
        investment_allocation = _combined_asset_allocation(
            accounts, "brokerage", "education"
        )
        investment_growth = _combined_account_assumption(
            accounts,
            ("brokerage", "education"),
            "expected_return",
            "growth_rate",
            "average_growth",
        )
        if investment_growth is None:
            if investment_allocation is not None:
                investment_growth, investment_return_rates = _allocation_expected_return(
                    investment_allocation
                )
                investment_growth_source = (
                    "life_model.config.market_assumptions.asset_classes"
                )
            else:
                investment_growth, investment_growth_source = (
                    _configured_investment_growth(accounts)
                )
                investment_return_rates = None
            _record_resolved_assumption(
                assumptions,
                resolved_assumptions,
                parameter="invested_asset_growth_rate",
                label="Brokerage and education-account growth",
                value=investment_growth,
                unit="percent_annual",
                source=investment_growth_source,
            )
        else:
            investment_return_rates = None
        investment: Dict[str, Any] = {
            "balance": invested,
            "growth_rate": investment_growth,
            "payout_to_bank": False,
            "taxable": True,
        }
        if investment_allocation is not None:
            investment["asset_allocation"] = investment_allocation
            investment["asset_return_rates"] = (
                investment_return_rates
                or _life_model_return_rates_for_allocation(investment_allocation)
            )
            allocation_model_inputs.append(
                {
                    "account": "investment",
                    "target_allocation": investment_allocation,
                    "return_rate_source": "life_model.market_assumptions.default",
                }
            )
        profile["investment"] = investment
    if education_balance:
        assumptions.append(
            "Education opening balance is included in invested assets until the LifeModel contract exposes a dedicated 529 field"
        )

    decisions: List[Dict[str, Any]] = []
    decision_inputs = [
        *[("goal", index, item) for index, item in enumerate(goals)],
        *[("one_off_expense", index, item) for index, item in enumerate(one_off_expenses)],
    ]
    for source_kind, index, item in decision_inputs:
        if not isinstance(item, dict):
            continue
        amount_key = "target_amount" if source_kind == "goal" else "amount"
        year_key = "target_year" if source_kind == "goal" else "year"
        if item.get(amount_key) is None or item.get(year_key) is None:
            unknowns.append(f"incomplete_{source_kind}:{index}")
            continue
        amount = _number(item.get(amount_key))
        year = _integer(item.get(year_key), start_year - 1)
        if amount > 0 and year < start_year:
            raise ValueError(
                f"{source_kind}[{index}].{year_key} must be at or after "
                f"projection start year {start_year}"
            )
        if amount > 0:
            decisions.append({
                "event_name": "One-time expense",
                "year": year,
                "payload": {
                    "person": "primary",
                    "name": str(
                        item.get("label")
                        or item.get("type")
                        or f"{source_kind.replace('_', ' ').title()} {index + 1}"
                    ),
                    "amount": amount,
                },
            })

    for index, item in enumerate(income_events):
        if not isinstance(item, dict):
            unknowns.append(f"invalid_income_event:{index}")
            continue
        start = _integer(item.get("start_year"), start_year - 1)
        end = _integer(item.get("end_year"), start - 1)
        multiplier = _number(item.get("income_multiplier"))
        person = str(item.get("person") or "primary")
        if start < start_year or end < start or multiplier < 0 or multiplier > 1:
            raise ValueError(
                f"income_events[{index}] requires valid start/end years and a multiplier from 0 to 1"
            )
        label = str(item.get("label") or f"Income event {index + 1}")
        decisions.append(
            {
                "event_name": "Set income multiplier",
                "year": start,
                "payload": {
                    "person": person,
                    "name": label,
                    "multiplier": multiplier,
                },
            }
        )
        if end + 1 <= end_year:
            decisions.append(
                {
                    "event_name": "Set income multiplier",
                    "year": end + 1,
                    "payload": {
                        "person": person,
                        "name": f"Restore income after {label}",
                        "multiplier": 1.0,
                    },
                }
            )
        assumptions.append(
            f"{label} applies an income multiplier of {multiplier:.4f} to {person} "
            f"from {start} through {end}, then restores the multiplier to 1.0"
        )

    for index, item in enumerate(spending_growth_events):
        if not isinstance(item, dict):
            unknowns.append(f"invalid_spending_growth_event:{index}")
            continue
        start = _integer(item.get("start_year"), start_year - 1)
        end = _integer(item.get("end_year"), start - 1)
        annual_rate = _number(item.get("annual_rate_percent"))
        restore_rate = item.get("restore_rate_percent")
        restore_rate = (
            _number(restore_rate)
            if restore_rate is not None
            else _number(expense_growth_value)
        )
        person = str(item.get("person") or "primary")
        if (
            start < start_year
            or end < start
            or annual_rate <= -100
            or annual_rate > 100
            or restore_rate <= -100
            or restore_rate > 100
        ):
            raise ValueError(
                f"spending_growth_events[{index}] has invalid years or annual rates"
            )
        label = str(item.get("label") or f"Spending growth event {index + 1}")
        decisions.append(
            {
                "event_name": "Set spending growth",
                "year": start,
                "payload": {
                    "person": person,
                    "name": label,
                    "annual_rate_percent": annual_rate,
                },
            }
        )
        if end + 1 <= end_year:
            decisions.append(
                {
                    "event_name": "Set spending growth",
                    "year": end + 1,
                    "payload": {
                        "person": person,
                        "name": f"Restore spending growth after {label}",
                        "annual_rate_percent": restore_rate,
                    },
                }
            )
        assumptions.append(
            f"{label} sets {person} spending growth to {annual_rate:.4f}% from "
            f"{start} through {end}, then restores it to {restore_rate:.4f}%"
        )

    for index, item in enumerate(recurring_investment_contributions):
        if not isinstance(item, dict):
            unknowns.append(f"invalid_recurring_investment_contribution:{index}")
            continue
        start = _integer(item.get("start_year"), start_year - 1)
        end = _integer(item.get("end_year"), start - 1)
        annual_amount = _number(item.get("annual_amount"))
        person = str(item.get("person") or "primary")
        if start < start_year or end < start or annual_amount <= 0:
            raise ValueError(
                f"recurring_investment_contributions[{index}] requires valid years "
                "and a positive annual amount"
            )
        label = str(
            item.get("label") or f"Recurring investment contribution {index + 1}"
        )
        decisions.append(
            {
                "event_name": "Recurring investment contribution",
                "year": start,
                "payload": {
                    "person": person,
                    "name": label,
                    "annual_amount": annual_amount,
                    "end_year": end,
                },
            }
        )
        assumptions.append(
            f"{label} transfers up to ${annual_amount:,.2f} of available cash per "
            f"year for {person} from {start} through {end}; the model does not borrow "
            "to fund a contribution"
        )

    provenance: List[Dict[str, Any]] = [
        {
            "source": "awm_client_file",
            "schema_version": awm_contract.get("schema_version"),
            "source_by_field": awm_contract.get("source_by_field", {}),
            "all_inputs_client_confirmed": awm_contract.get(
                "all_inputs_client_confirmed",
                not bool(awm_contract.get("uses_draft_facts")),
            ),
            "uses_draft_facts": bool(awm_contract.get("uses_draft_facts")),
        }
    ]
    allocation_provenance = payload.get("allocation_provenance")
    if isinstance(allocation_provenance, dict):
        provenance.append(
            {
                "source": "asset_allocation_policy",
                "details": allocation_provenance,
            }
        )
    if authorized_public_model_inputs:
        public_input = authorized_public_model_inputs[0]
        provenance.append(
            {
                "source": "agent_selected_session_public_fact",
                "variable_key": public_input["variable_key"],
                "effective_year": public_input["effective_year"],
                "content_sha256": public_input["content_sha256"],
                "sources": public_input["sources"],
            }
        )
    if resolved_assumptions:
        provenance.append(
            {
                "source": "awm_bridge.resolved_assumptions",
                "purpose": "explicit_effective_parameters_and_default_provenance",
                "assumptions": resolved_assumptions,
            }
        )
    if allocation_model_inputs:
        allocation_descriptions = []
        for allocation_input in allocation_model_inputs:
            target = allocation_input.get("target_allocation")
            if not isinstance(target, dict):
                continue
            weights = ", ".join(
                f"{asset_class} {_number(weight) * 100:.2f}%"
                for asset_class, weight in target.items()
            )
            allocation_descriptions.append(
                f"{allocation_input.get('account', 'investment account')} ({weights})"
            )
        if allocation_descriptions:
            assumptions.append(
                "Exact confirmed target allocations used by the model: "
                + "; ".join(allocation_descriptions)
            )
        if str(config.get("mode", "deterministic")).lower() == "monte_carlo":
            assumptions.append(
                "Monte Carlo investment paths use LifeModel configured per-asset expected "
                "returns, volatilities, and cross-asset correlations"
            )
        else:
            assumptions.append(
                "Deterministic allocation returns use LifeModel configured per-asset market assumptions"
            )
        provenance.append(
            {
                "source": "life_model.market_assumptions",
                "purpose": "per_asset_returns_for_exact_target_allocations",
                "inputs": allocation_model_inputs,
            }
        )

    raw_detail_groups = config.get("detail_report_groups")
    detail_report_groups = (
        [
            str(item).strip()
            for item in raw_detail_groups
            if isinstance(item, str) and str(item).strip()
        ]
        if isinstance(raw_detail_groups, list)
        else []
    )
    unknown_detail_groups = [
        item
        for item in detail_report_groups
        if item not in _ARC_DETAIL_REPORT_COLUMNS
    ]
    if unknown_detail_groups:
        raise ValueError(
            "Unsupported AWM cash-flow detail report groups: "
            + ", ".join(sorted(set(unknown_detail_groups)))
        )
    detail_projection_columns = list(
        dict.fromkeys(
            column
            for group in detail_report_groups
            for column in _ARC_DETAIL_REPORT_COLUMNS[group]
        )
    )
    projection_columns = list(
        dict.fromkeys(
            [
                *_ARC_DEFAULT_PROJECTION_COLUMNS,
                *detail_projection_columns,
            ]
        )
    )
    if detail_report_groups:
        assumptions.append(
            "AWM explicitly requested LifeModel detail report groups "
            + ", ".join(detail_report_groups)
            + "; the optimized default remains Net Worth, Cashflow Shortfall Debt, "
            "and Bank Balance"
        )

    request: Dict[str, Any] = {
        "schema_version": 1,
        "user_profile": profile,
        "financial_decisions": decisions,
        "input_context": {
            "facts": ["Mapped from AWM Client File cashflow payload"],
            "assumptions": assumptions,
            "resolved_assumptions": resolved_assumptions,
            "provenance": provenance,
            "unknowns": list(dict.fromkeys(unknowns)),
            "projection_parameters": {
                "start_year": start_year,
                "end_year": end_year,
                "primary_current_age": age,
                "primary_retirement_age": retirement_age,
                "spouse_current_age": spouse_age,
                "spouse_retirement_age": (
                    profile.get("spouse", {}).get("retirement_age")
                    if isinstance(profile.get("spouse"), dict)
                    else None
                ),
                "detail_report_groups": detail_report_groups,
                "detail_projection_columns": detail_projection_columns,
                "projection_columns": projection_columns,
            },
        },
        "projection": {
            "systems": "both",
            "format": "json",
            "columns": projection_columns,
        },
    }
    if authorized_public_model_inputs:
        request["authorized_public_model_inputs"] = authorized_public_model_inputs
    if str(config.get("mode", "deterministic")).lower() == "monte_carlo":
        num_simulations = max(1, _integer(config.get("num_simulations"), _default_monte_carlo_paths()))
        random_seed = _integer(config.get("seed"), 42)
        request["input_context"]["assumptions"].append(
            f"Monte Carlo configuration uses {num_simulations} paths with random seed "
            f"{random_seed}; success requires Net Worth to remain at or above $0 in every "
            "modeled year"
        )
        request["monte_carlo"] = {
            "num_simulations": num_simulations,
            "random_seed": random_seed,
            "success_column": "Net Worth",
            "success_threshold": 0,
        }
    return request


def _add_deterministic_bands(response: Dict[str, Any]) -> None:
    """Expose deterministic projections through AWM's chart-ready band contract."""
    result = response.get("result")
    if not isinstance(result, dict) or isinstance(result.get("percentile_bands"), dict):
        return
    rows = result.get("projection")
    if not isinstance(rows, list) or not rows:
        return
    years = _deterministic_projection_years(result, rows)
    columns = {key for row in rows if isinstance(row, dict) for key in row if key != "Year"}
    result["years"] = years
    result["percentile_bands"] = {
        column: {
            "Bottom 10%": [row.get(column, 0) for row in rows],
            "Median": [row.get(column, 0) for row in rows],
            "Top 10%": [row.get(column, 0) for row in rows],
        }
        for column in sorted(columns)
    }
    result["success_rate"] = 1.0 if all(_number(row.get("Net Worth")) >= 0 for row in rows) else 0.0
    event_distributions: Dict[str, Dict[str, Any]] = {}
    for event, column, threshold, operator in (
        ("net_worth_depletion", "Net Worth", 0.0, "less_than"),
        ("cashflow_shortfall", "Cashflow Shortfall Debt", 1e-6, "greater_than"),
        ("liquid_cash_depletion", "Bank Balance", 0.0, "less_than"),
    ):
        distribution = _deterministic_event_distribution(
            rows,
            years=years,
            event=event,
            column=column,
            threshold=threshold,
            operator=operator,
        )
        if distribution is not None:
            event_distributions[event] = distribution
    result["event_distributions"] = event_distributions


def _deterministic_projection_years(
    result: Dict[str, Any],
    rows: List[Dict[str, Any]],
) -> List[Optional[int]]:
    """Recover calendar years when the optimized projection omits ``Year``.

    LifeModel's optimized column selection can remove the row-level ``Year``
    field even though the projection bounds remain in result metadata. AWM's
    normalized bands and first-event distributions still require those years.
    """

    row_years: List[Optional[int]] = []
    all_rows_have_years = True
    for row in rows:
        raw_year = row.get("Year") if isinstance(row, dict) else None
        if raw_year is None:
            row_years.append(None)
            all_rows_have_years = False
            continue
        year = _integer(raw_year, 0)
        row_years.append(year if year > 0 else None)
        all_rows_have_years = all_rows_have_years and year > 0
    if all_rows_have_years:
        return row_years

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
    start_year = _integer(projection_parameters.get("start_year"), 0)
    end_year = _integer(projection_parameters.get("end_year"), 0)
    if (
        start_year > 0
        and end_year >= start_year
        and end_year - start_year + 1 == len(rows)
    ):
        return [start_year + index for index in range(len(rows))]
    return row_years


def _deterministic_event_distribution(
    rows: List[Dict[str, Any]],
    *,
    years: List[Optional[int]],
    event: str,
    column: str,
    threshold: float,
    operator: str,
) -> Optional[Dict[str, Any]]:
    if not any(isinstance(row, dict) and row.get(column) is not None for row in rows):
        return None
    first_year: Optional[int] = None
    for index, row in enumerate(rows):
        if index == 0 or not isinstance(row, dict) or row.get(column) is None:
            continue
        value = _number(row[column])
        crossed = value < threshold if operator == "less_than" else value > threshold
        if crossed:
            first_year = years[index] if index < len(years) else None
            if first_year is None:
                # Do not misreport a known event as "never" when the transport
                # omitted both row years and usable projection bounds.
                return None
            break
    return {
        "type": "first_threshold_crossing_distribution",
        "event": event,
        "column": column,
        "operator": operator,
        "threshold": threshold,
        "probability_by_year": {str(first_year): 1.0} if first_year else {},
        "probability_never": 0.0 if first_year else 1.0,
        "sample_count": 1,
        "source": "life_model.deterministic_projection",
        "opening_baseline_excluded": True,
    }


def run_full_model(payload: Dict[str, Any]) -> Dict[str, Any]:
    response = agent_run_request(awm_payload_to_agent_request(payload))
    _add_deterministic_bands(response)
    response["success"] = bool(response.get("ok"))
    response["transport_schema_version"] = "awm.cashflow_engine_response.v2"
    response["engine"] = {
        "name": "cashflow-model",
        "version": LIFE_MODEL_VERSION,
        "implementation": "life_model.LifeModel",
        "entrypoint": "life_model.cli.agent_run_request",
    }
    return response
