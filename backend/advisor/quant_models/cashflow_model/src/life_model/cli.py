# Copyright 2026 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

import argparse
from contextlib import nullcontext
from copy import deepcopy
from datetime import date
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

from . import __version__
from .account.asset_allocation import AssetAllocation, AssetReturnRates
from .account.bank import BankAccount
from .account.brokerage import BrokerageAccount
from .charity.daf import DonorAdvisedFund
from .charity.donation import Donation, DonationType
from .debt.car_loan import CarLoan
from .debt.credit_card import CreditCard
from .account.hsa import HealthSavingsAccount, HSAType
from .account.investment_return import InvestmentReturn
from .account.job401k import Job401kAccount
from .account.pension import Pension
from .account.roth_IRA import RothIRA
from .account.traditional_IRA import TraditionalIRA
from .account.trust import Trust, TrustType
from .assets.tangible_asset import TangibleAsset, AssetType
from .dependents.child import Child
from .dependents.plan529 import Plan529
from .healthcare.healthcare import Healthcare
from .housing.apartment import Apartment
from .housing.home import Home, HomeExpenses, Mortgage, MortgageType
from .insurance.annuity import Annuity, AnnuityPayoutType, AnnuityType
from .insurance.general_insurance import Insurance, InsuranceType
from .insurance.life_insurance import LifeInsurance, LifeInsuranceType
from .insurance.social_security import SocialSecurity
from .lifeevents import LifeEvent, LifeEvents
from .config.config_manager import config
from .model import LifeModel, MoneyStat
from .montecarlo import (
    MarketAssumptions,
    MonteCarloConfig,
    MonteCarloSimulator,
    YearVaryingMarketAssumptions,
)
from .people.family import Family
from .people.person import Person, Spending
from .tax.fica import authorized_social_security_max_income
from .work.job import Job, Salary


PROJECTION_COLUMN_ALIASES = {
    "Debt": "Cashflow Shortfall Debt",
}

SOURCE_DETAIL_MONEY_COLUMN_PREFIXES = (
    "Income Source: ",
    "Asset Source: ",
    "Liability Source: ",
)

CASHFLOW_INFLOW_COMPONENT_COLUMNS = [
    "Income",
    "SS Income",
    "Pension Income",
    "Trust Distributions",
    "Annuity Payouts",
    "Death Benefits",
    "Insurance Claim Payouts",
    "Child Family Contributions",
    "401k Withdrawals",
    "RMDs",
    "Cash Investment Return",
    "Bank Interest",
]

CASHFLOW_SUPPORTING_INFLOW_COLUMNS = [
    "529 Withdrawals",
    "Investment Return",
    "401k Match",
    "Annuity Interest",
]

CASHFLOW_OUTFLOW_BEFORE_TAX_COLUMNS = [
    "Base Living Spending",
    "One-time Expenses",
    "Education Costs",
    "Home Purchase Costs",
    "Asset Sale Shortfalls",
    "Child Costs",
    "529 Contributions",
    "Healthcare Costs",
    "Real Asset Costs",
    "Housing",
    "Loan Payments",
    "401k Contrib",
    "Charity",
    "Life Ins Premiums",
    "Insurance Premiums",
    "Insurance Deductibles",
    "Annuity Surrender Charges",
]

CASHFLOW_CALCULATED_COLUMNS = [
    "Total Cash Inflows",
    "Total Cash Outflows Before Taxes",
    "Total Cash Outflows",
    "Net Cashflow Before Taxes",
    "Net Cashflow",
]

DYNAMIC_PROJECTION_COLUMN_PLACEMENTS = {
    "cash_inflows": {
        "One-time Income": ("Income Source: ",),
    },
    "assets": {
        "Annuity Balance": ("Asset Source: ",),
    },
    "liabilities": {
        "Life Ins Loan Balance": ("Liability Source: ",),
    },
}

PROJECTION_COLUMN_GROUPS = [
    ("period", "Period", ["Year"]),
    (
        "cash_inflows",
        "Cash inflow items",
        [
            "Income",
            "One-time Income",
            "SS Income",
            "Pension Income",
            "Trust Distributions",
            "Annuity Payouts",
            "Annuity Interest",
            "Death Benefits",
            "Insurance Claim Payouts",
            "Child Family Contributions",
            "529 Withdrawals",
            "401k Withdrawals",
            "RMDs",
            "Cash Investment Return",
            "Bank Interest",
            "Investment Return",
            "401k Match",
        ],
    ),
    (
        "cash_outflows",
        "Cash outflow items",
        [
            "Base Living Spending",
            "One-time Expenses",
            "Education Costs",
            "Home Purchase Costs",
            "Asset Sale Shortfalls",
            "Child Costs",
            "529 Contributions",
            "Healthcare Costs",
            "Real Asset Costs",
            "Housing",
            "Loan Payments",
            "401k Contrib",
            "Charity",
            "Life Ins Premiums",
            "Insurance Premiums",
            "Insurance Deductibles",
            "Annuity Surrender Charges",
        ],
    ),
    (
        "cash_outflow_totals_and_details",
        "Cash outflow totals and supporting details",
        [
            "Spending",
            "Housing",
            "Mortgage Payments",
            "Mortgage Principal Paid",
            "Mortgage Interest Paid",
            "Loan Payments",
            "Loan Principal Paid",
            "Loan Interest Paid",
            "Interest Paid",
            "401k Contrib",
            "529 Contributions",
            "Child Costs",
            "Charity",
            "Healthcare Costs",
            "Long-Term Care Costs",
            "Real Asset Costs",
            "Life Ins Premiums",
            "Insurance Premiums",
            "Insurance Deductibles",
            "Annuity Surrender Charges",
        ],
    ),
    (
        "taxes",
        "Taxes",
        [
            "Taxes",
            "Federal Taxes",
            "State Taxes",
            "SS Taxes",
            "Medicare Taxes",
            "Early Withdrawal Penalties",
            "AGI",
            "Taxable Income",
            "Tax Deductions",
            "Federal Marginal Rate",
            "Effective Tax Rate",
            "Retirement Liquidation Income Tax",
            "Retirement Early Withdrawal Tax",
            "Retirement Liquidation Tax Cost",
        ],
    ),
    (
        "net_cashflow_shortfall",
        "Net cashflow / shortfall",
        [
            "Total Cash Inflows",
            "Total Cash Outflows Before Taxes",
            "Total Cash Outflows",
            "Net Cashflow Before Taxes",
            "Net Cashflow",
            "Useable Balance",
            "Cashflow Shortfall Debt",
        ],
    ),
    (
        "assets",
        "Asset items",
        [
            "Bank Balance",
            "Brokerage Balance",
            "Investment Balance",
            "401k Balance",
            "Traditional IRA Balance",
            "Roth IRA Balance",
            "Taxable Retirement Balance",
            "Roth Retirement Balance",
            "After-Tax Retirement Value",
            "HSA Balance",
            "529 Balance",
            "Home Value",
            "Real Asset Value",
            "Trust Balance",
            "Life Ins Cash Value",
            "Annuity Balance",
            "Total Assets",
        ],
    ),
    (
        "liabilities",
        "Liability items",
        [
            "Mortgage Balance",
            "Loan Balance",
            "Life Ins Loan Balance",
            "Total Liabilities",
            "Fixed Rate Mortgages",
            "Adjustable Rate Mortgages",
            "Interest Only Mortgages",
            "Balloon Mortgages",
            "Car Loans",
            "Student Loans",
            "Credit Cards",
            "Federal Subsidized Student Loans",
            "Federal Unsubsidized Student Loans",
            "Private Student Loans",
            "PLUS Student Loans",
        ],
    ),
    (
        "net_worth",
        "Net worth",
        [
            "Net Worth",
            "Tax-Adjusted Net Worth",
        ],
    ),
    (
        "status_and_event_indicators",
        "Status and event indicators",
        [
            "Owns Home",
            "Rents Apartment",
            "Insurance Claims Filed",
            "Child Birth/Adoption Events",
            "Childcare Events",
            "School Activity Events",
            "College Savings/Education Events",
            "Child Independence Events",
            "Child Work Contribution Events",
        ],
    ),
]


def _flatten_projection_column_groups() -> List[str]:
    return [
        column
        for _name, _title, columns in PROJECTION_COLUMN_GROUPS
        for column in columns
    ]


DEFAULT_COLUMNS = _flatten_projection_column_groups()
SUMMARY_COLUMNS = [column for column in DEFAULT_COLUMNS if column != "Year"]

AGENT_REQUEST_SCHEMA_VERSION = 1

AGENT_REQUEST_ALLOWED_FIELDS = {
    "schema_version",
    "user_profile",
    "scenario",
    "financial_decisions",
    "financial_decision",
    "decisions",
    "projection",
    "return_resolved_scenario",
    "input_context",
    "monte_carlo",
    "authorized_public_model_inputs",
}

AUTHORIZED_PUBLIC_MODEL_INPUT_FIELDS = {
    "schema_version",
    "variable_key",
    "value",
    "unit",
    "jurisdiction",
    "effective_year",
    "content_sha256",
    "sources",
}

AUTHORIZED_PUBLIC_MODEL_SOURCE_FIELDS = {
    "publisher",
    "title",
    "url",
    "published_at",
}

SCENARIO_ALLOWED_FIELDS = {
    "name",
    "description",
    "start_year",
    "end_year",
    "family_name",
    "people",
    "married_couples",
    "events",
    "market_assumptions",
    "input_context",
}

PERSON_ALLOWED_FIELDS = {
    "id",
    "name",
    "age",
    "retirement_age",
    "state",
    "spending",
    "bank_accounts",
    "apartments",
    "homes",
    "jobs",
    "pensions",
    "social_security",
    "trusts",
    "tangible_assets",
    "annuities",
    "hsa_accounts",
    "brokerage_accounts",
    "donor_advised_funds",
    "insurance_policies",
    "car_loans",
    "credit_cards",
    "donations",
    "plan_529s",
    "traditional_iras",
    "roth_iras",
    "life_insurance",
    "healthcare",
    "investment_returns",
    "children",
}

COMPONENT_ALLOWED_FIELDS = {
    "spending": {"base", "yearly_increase"},
    "bank_accounts": {"company", "type", "balance", "interest_rate", "compound_rate"},
    "apartments": {"name", "monthly_rent", "yearly_increase"},
    "homes": {
        "name",
        "current_value",
        "tax_basis",
        "value_yearly_increase",
        "mortgage",
        "expenses",
        "mortgage_included_in_base_spending",
    },
    "home_mortgage": {
        "principal_balance",
        "yearly_interest_rate",
        "remaining_term_years",
        "monthly_payment",
        "mortgage_type",
    },
    "home_expenses": {
        "property_tax_percent",
        "home_insurance_percent",
        "maintenance_amount",
        "maintenance_increase",
        "improvement_amount",
        "improvement_increase",
        "hoa_amount",
        "hoa_increase",
    },
    "jobs": {"company", "role", "salary", "401k"},
    "salary": {"base", "yearly_increase", "yearly_bonus"},
    "401k": {
        "pretax_balance",
        "pretax_contrib_percent",
        "roth_balance",
        "roth_contrib_percent",
        "average_growth",
        "company_match_percent",
        "asset_allocation",
        "asset_return_rates",
    },
    "pensions": {
        "name",
        "final_average_salary",
        "years_of_service",
        "benefit_multiplier_percent",
        "vesting_years",
        "payout_start_age",
        "cola_percent",
        "benefit_amount",
    },
    "social_security": {"withdrawal_start_age", "earnings_history"},
    "trusts": {
        "name",
        "trust_type",
        "beneficiary",
        "initial_balance",
        "growth_rate",
        "annual_distribution",
        "distribution_percent",
    },
    "tangible_assets": {
        "name",
        "asset_type",
        "value",
        "value_yearly_change_percent",
        "maintenance_annual",
        "maintenance_increase_percent",
        "insurance_annual",
        "insurance_increase_percent",
        "rental_income_annual",
        "loan_amount",
        "loan_interest_rate",
        "loan_term_years",
        "sell_year",
        "selling_cost_percent",
    },
    "annuities": {
        "type",
        "initial_balance",
        "interest_rate",
        "payout_type",
        "payout_start_age",
        "monthly_payout",
        "period_certain_years",
        "surrender_charge_years",
        "surrender_charge_rate",
        "survivor_benefit_percent",
    },
    "hsa_accounts": {
        "type",
        "balance",
        "contribution_limit",
        "employer_contribution",
        "growth_rate",
    },
    "brokerage_accounts": {
        "company",
        "balance",
        "growth_rate",
        "asset_allocation",
        "asset_return_rates",
    },
    "donor_advised_funds": {
        "fund_name",
        "balance",
        "growth_rate",
        "management_fee",
        "distribution_rate",
    },
    "insurance_policies": {
        "insurance_type",
        "company",
        "annual_premium",
        "coverage_amount",
        "deductible",
        "coverage_start_age",
        "coverage_end_age",
        "premium_increase_rate",
        "max_claims_per_year",
    },
    "car_loans": {"loan_amount", "length_years", "yearly_interest_rate", "name", "principal"},
    "credit_cards": {
        "card_name",
        "credit_limit",
        "current_balance",
        "yearly_interest_rate",
        "minimum_payment_percent",
    },
    "donations": {
        "charity_name",
        "annual_amount",
        "donation_type",
        "tax_deductible",
        "frequency_years",
        "start_year",
        "end_year",
    },
    "plan_529s": {
        "balance",
        "state",
        "growth_rate",
        "annual_contribution_limit",
        "lifetime_contribution_limit",
        "asset_allocation",
        "asset_return_rates",
    },
    "traditional_iras": {
        "balance",
        "growth_rate",
        "contribution_limit",
        "asset_allocation",
        "asset_return_rates",
    },
    "roth_iras": {
        "balance",
        "growth_rate",
        "contribution_limit",
        "asset_allocation",
        "asset_return_rates",
    },
    "life_insurance": {
        "type",
        "death_benefit",
        "monthly_premium",
        "term_years",
        "premium_increase_rate",
        "cash_value_growth_rate",
        "loan_interest_rate",
        "max_missed_payments",
    },
    "healthcare": {
        "pre_medicare_annual_premium",
        "annual_out_of_pocket",
        "medicare_start_age",
        "medicare_part_b_monthly",
        "medicare_part_d_monthly",
        "medigap_monthly",
        "healthcare_inflation_percent",
        "age_cost_multipliers",
        "ltc_start_age",
        "ltc_years",
        "ltc_annual_cost",
    },
    "investment_returns": {
        "balance",
        "growth_rate",
        "asset_allocation",
        "asset_return_rates",
        "payout_to_bank",
        "cash_payout_rate",
        "taxable",
    },
    "children": {
        "name",
        "birth_year",
        "adoption_year",
        "childcare_annual_cost",
        "school_activity_annual_cost",
        "college_start_age",
        "college_years",
        "college_annual_cost",
        "college_cost_increase",
        "college_savings_annual_contribution",
        "independence_age",
        "work_start_age",
        "annual_family_contribution",
        "plan529",
    },
}

FINANCIAL_DECISION_EVENT_TYPE_ALIASES = {
    "marriage": "marriage",
    "divorce": "divorce",
    "child_birth_or_adoption": "child_birth_or_adoption",
    "child birth or adoption": "child_birth_or_adoption",
    "child birth/adoption": "child_birth_or_adoption",
    "child birth adoption": "child_birth_or_adoption",
    "home_purchase": "home_purchase",
    "home purchase": "home_purchase",
    "go_to_college": "go_to_college",
    "go to college": "go_to_college",
    "college": "go_to_college",
    "college education": "go_to_college",
    "one_time_expense": "one_time_expense",
    "one time expense": "one_time_expense",
    "one-time expense": "one_time_expense",
    "one_time_income": "one_time_income",
    "one time income": "one_time_income",
    "one-time income": "one_time_income",
    "set_income_multiplier": "set_income_multiplier",
    "set income multiplier": "set_income_multiplier",
    "set_spending_growth": "set_spending_growth",
    "set spending growth": "set_spending_growth",
    "recurring_investment_contribution": "recurring_investment_contribution",
    "recurring investment contribution": "recurring_investment_contribution",
}

FINANCIAL_DECISION_COLLECTION_KEYS = (
    "financial_decisions",
    "financial_decision_events",
    "decisions",
    "events",
)

FINANCIAL_DECISION_CONTROL_KEYS = {
    "event_name",
    "event_type",
    "type",
    "year",
    "payload",
    "description",
    "decision_id",
    "id",
}


def get_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="life-model",
        description="Run cashflow projections from an Agent-friendly JSON scenario.",
    )
    parser.add_argument("--version", "-v", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run a cashflow projection JSON scenario")
    run_parser.add_argument("scenario", help="Path to a scenario JSON file")
    run_parser.add_argument(
        "--financial-decisions",
        "--decisions",
        action="append",
        dest="financial_decision_paths",
        help=(
            "Path to a financial-decision JSON file to append to scenario events. "
            "Can be provided more than once."
        ),
    )
    run_parser.add_argument(
        "--financial-decision-json",
        "--decision-json",
        action="append",
        dest="financial_decision_jsons",
        help=(
            "Inline financial-decision JSON object or list to append to scenario events. "
            "Can be provided more than once."
        ),
    )
    run_parser.add_argument(
        "--economic-scenario",
        dest="economic_scenario",
        help=(
            "Name of an economic scenario from config/scenarios (e.g. recession, "
            "high_inflation, tax_reform). Applies the scenario's configuration "
            "overrides for this run and restores defaults afterwards."
        ),
    )
    run_parser.add_argument(
        "--monte-carlo",
        dest="monte_carlo",
        type=int,
        metavar="N",
        help=(
            "Run N Monte Carlo simulations instead of a single deterministic "
            "projection. Output contains per-year percentile bands and a "
            "success rate. Market assumptions come from the scenario's "
            "market_assumptions block when present, otherwise from the "
            "configuration defaults."
        ),
    )
    run_parser.add_argument(
        "--mc-seed",
        dest="mc_seed",
        type=int,
        help="Random seed for reproducible Monte Carlo runs.",
    )
    run_parser.add_argument(
        "--mc-success-column",
        dest="mc_success_column",
        default="Net Worth",
        help="Projection column checked for the success rate. Defaults to Net Worth.",
    )
    run_parser.add_argument(
        "--format",
        choices=("json", "csv"),
        default="json",
        help="Output format. Defaults to JSON with metadata, events, summary, and projection rows.",
    )
    run_parser.add_argument("--output", "-o", help="Optional output file path. Defaults to stdout.")
    run_parser.add_argument(
        "--columns",
        help="Comma-separated projection columns to return. Defaults to every collected projection column.",
    )
    run_parser.add_argument(
        "--all-columns",
        action="store_true",
        help="Return every collected projection column.",
    )
    run_parser.add_argument(
        "--projection-system",
        choices=("nominal", "real", "both"),
        default=None,
        help=(
            "Dollar basis to emit for CSV output. JSON always includes both "
            "nominal and start-year real-dollar projection systems by default."
        ),
    )
    run_parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    run_parser.set_defaults(func=_run_command)

    example_parser = subparsers.add_parser("example", help="Print an example scenario JSON")
    example_parser.add_argument("--pretty", action="store_true", help="Pretty-print the example JSON.")
    example_parser.set_defaults(func=_example_command)

    profile_example_parser = subparsers.add_parser(
        "profile-example",
        help="Print an example user profile JSON for build-scenario.",
    )
    profile_example_parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print the example user profile JSON.",
    )
    profile_example_parser.set_defaults(func=_profile_example_command)

    build_parser = subparsers.add_parser(
        "build-scenario",
        help="Build a runnable scenario JSON from a compact user profile JSON.",
    )
    build_parser.add_argument("profile", help="Path to a user profile JSON file")
    build_parser.add_argument("--output", "-o", help="Optional scenario output path. Defaults to stdout.")
    build_parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    build_parser.set_defaults(func=_build_scenario_command)

    agent_run_parser = subparsers.add_parser(
        "agent-run",
        help="Run one Agent request JSON containing profile/scenario, decisions, and projection options.",
    )
    agent_run_parser.add_argument("request", nargs="?", help="Path to an Agent request JSON file")
    agent_run_parser.add_argument("--stdin", action="store_true", help="Read Agent request JSON from stdin.")
    agent_run_parser.add_argument("--output", "-o", help="Optional output path. Defaults to stdout.")
    agent_run_parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output when requested.")
    agent_run_parser.set_defaults(func=_agent_run_command)

    validate_parser = subparsers.add_parser(
        "validate-request",
        help="Validate an Agent request JSON without running the projection.",
    )
    validate_parser.add_argument("request", nargs="?", help="Path to an Agent request JSON file")
    validate_parser.add_argument("--stdin", action="store_true", help="Read Agent request JSON from stdin.")
    validate_parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    validate_parser.set_defaults(func=_validate_agent_request_command)

    schema_parser = subparsers.add_parser(
        "print-agent-schema",
        help="Print the Agent request contract and an example request.",
    )
    schema_parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    schema_parser.set_defaults(func=_print_agent_schema_command)

    return parser


def main(args: Optional[List[str]] = None) -> int:
    """CLI entry point."""
    parser = get_parser()
    parsed_args = parser.parse_args(args)
    if not hasattr(parsed_args, "func"):
        parser.print_help()
        return 0
    try:
        return parsed_args.func(parsed_args)
    except Exception as exc:  # pragma: no cover - exercised through CLI behavior
        print(f"life-model: error: {exc}", file=sys.stderr)
        return 1


def load_scenario(path: Path) -> Dict[str, Any]:
    """Load a scenario JSON file."""
    with path.open("r", encoding="utf-8") as scenario_file:
        scenario = json.load(scenario_file)
    if not isinstance(scenario, dict):
        raise ValueError("Scenario JSON must be an object")
    return scenario


def load_financial_decisions(path: Path) -> Any:
    """Load a financial-decision JSON file."""
    with path.open("r", encoding="utf-8") as decision_file:
        return json.load(decision_file)


def load_user_profile(path: Path) -> Dict[str, Any]:
    """Load a compact user profile JSON file."""
    with path.open("r", encoding="utf-8") as profile_file:
        profile = json.load(profile_file)
    if not isinstance(profile, dict):
        raise ValueError("User profile JSON must be an object")
    return profile


def parse_financial_decision_json(value: str) -> Any:
    """Parse an inline financial-decision JSON value."""
    return json.loads(value)


def load_agent_request(path: Optional[Path] = None, read_stdin: bool = False) -> Dict[str, Any]:
    """Load a single Agent request JSON object."""
    if path is not None and read_stdin:
        raise ValueError("Use either an Agent request path or --stdin, not both")
    if read_stdin:
        text = sys.stdin.read()
    elif path is not None:
        text = path.read_text(encoding="utf-8")
    else:
        raise ValueError("Agent request path is required unless --stdin is used")

    if not text.strip():
        raise ValueError("Agent request JSON is empty")
    request = json.loads(text)
    if not isinstance(request, dict):
        raise ValueError("Agent request JSON must be an object")
    return request


def scenario_with_financial_decisions(
    scenario: Dict[str, Any],
    financial_decision_inputs: Iterable[Any],
) -> Dict[str, Any]:
    """Return a scenario with separate financial-decision inputs appended."""
    merged = deepcopy(scenario)
    events = list(_as_list(merged.get("events", []), "events"))
    for financial_decision_input in financial_decision_inputs:
        events.extend(financial_decisions_to_event_specs(financial_decision_input))
    merged["events"] = events
    return merged


def financial_decisions_to_event_specs(financial_decision_input: Any) -> List[Dict[str, Any]]:
    """Normalize Agent financial-decision JSON into CLI event specs."""
    if financial_decision_input is None:
        return []

    if isinstance(financial_decision_input, list):
        decisions = financial_decision_input
    elif isinstance(financial_decision_input, dict):
        for collection_key in FINANCIAL_DECISION_COLLECTION_KEYS:
            if collection_key in financial_decision_input:
                return financial_decisions_to_event_specs(financial_decision_input[collection_key])
        decisions = [financial_decision_input]
    else:
        raise ValueError("Financial decision input must be an object or list")

    return [_financial_decision_to_event_spec(decision) for decision in decisions]


def agent_run_request(request: Dict[str, Any]) -> Dict[str, Any]:
    """Run an Agent request and return a structured JSON-safe response."""
    _validate_agent_request_fields(request)
    scenario, scenario_source = _agent_request_to_scenario(request)
    projection_format = _agent_projection_format(request)
    projection_system = _agent_projection_system(request)

    monte_carlo_spec = _agent_monte_carlo_spec(request)
    if monte_carlo_spec is not None:
        result = monte_carlo_projection_payload(
            scenario,
            request,
            monte_carlo_spec,
        )
    else:
        model, projection = run_scenario(scenario)
        selected_columns = _agent_projection_columns(request, list(projection.columns))
        if projection_format == "csv":
            result = {
                "format": "csv",
                "projection_system": projection_system,
                "projection_csv": projection_system_csv(model, projection, selected_columns, projection_system),
            }
        else:
            result = projection_payload(model, projection, selected_columns, projection_system)

    if projection_format == "csv" and monte_carlo_spec is not None:
        result = {
            "format": "csv",
            "projection_csv": result,
        }

    response = {
        "ok": True,
        "schema_version": AGENT_REQUEST_SCHEMA_VERSION,
        "scenario_source": scenario_source,
        "projection_format": projection_format,
        "result": result,
    }
    if request.get("return_resolved_scenario"):
        response["resolved_scenario"] = scenario
    return response


def validate_agent_request(request: Dict[str, Any]) -> Dict[str, Any]:
    """Validate an Agent request without running the projection."""
    _validate_agent_request_fields(request)
    scenario, scenario_source = _agent_request_to_scenario(request)
    with _authorized_public_input_context(scenario):
        build_model_from_scenario(scenario)
    selected_columns = _agent_projection_columns(request, _available_projection_columns())
    monte_carlo_spec = _agent_monte_carlo_spec(request)
    if monte_carlo_spec is not None:
        success_column = monte_carlo_spec["success_column"]
        if success_column not in _available_projection_columns():
            raise ValueError(f"Unknown Monte Carlo success column: {success_column}")
    projection_format = _agent_projection_format(request)
    projection_system = _agent_projection_system(request)
    return {
        "ok": True,
        "schema_version": AGENT_REQUEST_SCHEMA_VERSION,
        "valid": True,
        "scenario_source": scenario_source,
        "projection": {
            "format": projection_format,
            "systems": projection_system,
            "columns": selected_columns,
        },
        "monte_carlo": monte_carlo_spec,
    }


def run_scenario(scenario: Dict[str, Any]) -> Tuple[LifeModel, pd.DataFrame]:
    """Build and run a model from a scenario object."""
    with _authorized_public_input_context(scenario):
        model, _family, _people = build_model_from_scenario(scenario)
        model.run()
        projection = model.datacollector.get_model_vars_dataframe()
        projection = _append_source_detail_columns(model, projection)
    return model, _append_cashflow_reconciliation_columns(projection)


def _append_source_detail_columns(model: LifeModel, projection: pd.DataFrame) -> pd.DataFrame:
    """Add per-source income, asset, and liability columns for AI reasoning."""
    agent_projection = model.datacollector.get_agent_vars_dataframe()
    if agent_projection.empty:
        return projection

    enriched = projection.copy()
    existing_columns = set(enriched.columns)
    for column_name, agent, stat_column in _source_detail_specs(model):
        series = _agent_stat_series(agent_projection, agent, stat_column, enriched.index)
        if series is None or not _series_has_signal(series):
            continue
        column_name = _unique_source_column_name(existing_columns, column_name)
        enriched[column_name] = series
        existing_columns.add(column_name)
    return enriched


def _append_cashflow_reconciliation_columns(projection: pd.DataFrame) -> pd.DataFrame:
    enriched = projection.copy()
    total_cash_inflows = _projection_column_sum(enriched, CASHFLOW_INFLOW_COMPONENT_COLUMNS)
    total_cash_outflows_before_taxes = _projection_column_sum(
        enriched,
        CASHFLOW_OUTFLOW_BEFORE_TAX_COLUMNS,
    )
    taxes = _projection_column_sum(enriched, ["Taxes"])

    enriched["Total Cash Inflows"] = total_cash_inflows
    enriched["Total Cash Outflows Before Taxes"] = total_cash_outflows_before_taxes
    enriched["Total Cash Outflows"] = total_cash_outflows_before_taxes + taxes
    enriched["Net Cashflow Before Taxes"] = total_cash_inflows - total_cash_outflows_before_taxes
    enriched["Net Cashflow"] = total_cash_inflows - enriched["Total Cash Outflows"]
    return enriched


def _projection_column_sum(projection: pd.DataFrame, columns: Iterable[str]) -> pd.Series:
    total = pd.Series(0.0, index=projection.index)
    for column in columns:
        if column in projection:
            total = total + pd.to_numeric(projection[column], errors="coerce").fillna(0.0)
    return total


def _source_detail_specs(model: LifeModel) -> Iterable[Tuple[str, Any, str]]:
    for agent in model.agents:
        class_name = agent.__class__.__name__
        if class_name == "Job":
            yield (
                _source_detail_column(
                    "Income Source",
                    _person_name(getattr(agent, "owner", None)),
                    getattr(agent, "company", None),
                    getattr(agent, "role", None),
                ),
                agent,
                "Income",
            )
        elif class_name == "Person":
            yield (
                _source_detail_column(
                    "Income Source",
                    _person_name(agent),
                    "One-time income",
                ),
                agent,
                "One-time Income",
            )
            yield (
                _source_detail_column(
                    "Liability Source",
                    _person_name(agent),
                    "Cashflow shortfall",
                ),
                agent,
                "Cashflow Shortfall Debt",
            )
        elif class_name == "BankAccount":
            yield (
                _source_detail_column(
                    "Asset Source",
                    _person_name(getattr(agent, "person", None)),
                    "Bank",
                    getattr(agent, "company", None),
                    getattr(agent, "type", None),
                ),
                agent,
                "Useable Balance",
            )
        elif class_name == "BrokerageAccount":
            yield (
                _source_detail_column(
                    "Asset Source",
                    _person_name(getattr(agent, "person", None)),
                    "Brokerage",
                    getattr(agent, "company", None),
                ),
                agent,
                "Brokerage Balance",
            )
        elif class_name == "Job401kAccount":
            job = getattr(agent, "job", None)
            yield (
                _source_detail_column(
                    "Asset Source",
                    _person_name(getattr(agent, "person", None)),
                    "401k",
                    getattr(job, "company", None),
                    getattr(job, "role", None),
                ),
                agent,
                "401k Balance",
            )
        elif class_name == "TraditionalIRA":
            yield (
                _source_detail_column(
                    "Asset Source",
                    _person_name(getattr(agent, "person", None)),
                    "Traditional IRA",
                ),
                agent,
                "Traditional IRA Balance",
            )
        elif class_name == "RothIRA":
            yield (
                _source_detail_column(
                    "Asset Source",
                    _person_name(getattr(agent, "person", None)),
                    "Roth IRA",
                ),
                agent,
                "Roth IRA Balance",
            )
        elif class_name == "HealthSavingsAccount":
            yield (
                _source_detail_column(
                    "Asset Source",
                    _person_name(getattr(agent, "person", None)),
                    "HSA",
                    _enum_value(getattr(agent, "hsa_type", None)),
                ),
                agent,
                "HSA Balance",
            )
        elif class_name == "Plan529":
            yield (
                _source_detail_column(
                    "Asset Source",
                    _person_name(getattr(agent, "person", None)),
                    "529",
                    getattr(agent, "state", None),
                    _beneficiary_name(getattr(agent, "beneficiary", None)),
                ),
                agent,
                "529 Balance",
            )
        elif class_name == "Home":
            yield (
                _source_detail_column(
                    "Asset Source",
                    _person_name(getattr(agent, "person", None)),
                    "Home",
                    getattr(agent, "name", None),
                ),
                agent,
                "Home Value",
            )
            mortgage = getattr(agent, "mortgage", None)
            yield (
                _source_detail_column(
                    "Liability Source",
                    _person_name(getattr(agent, "person", None)),
                    "Mortgage",
                    getattr(agent, "name", None),
                    _enum_value(getattr(mortgage, "mortgage_type", None)),
                ),
                agent,
                "Mortgage Balance",
            )
        elif class_name == "TangibleAsset":
            yield (
                _source_detail_column(
                    "Asset Source",
                    _person_name(getattr(agent, "person", None)),
                    _enum_value(getattr(agent, "asset_type", None)),
                    getattr(agent, "name", None),
                ),
                agent,
                "Real Asset Value",
            )
        elif class_name == "Trust":
            yield (
                _source_detail_column(
                    "Asset Source",
                    _person_name(getattr(agent, "grantor", None)),
                    "Trust",
                    getattr(agent, "name", None),
                ),
                agent,
                "Trust Balance",
            )
        elif class_name == "LifeInsurance":
            yield (
                _source_detail_column(
                    "Asset Source",
                    _person_name(getattr(agent, "person", None)),
                    "Life Insurance",
                    _enum_value(getattr(agent, "policy_type", None)),
                ),
                agent,
                "Life Ins Cash Value",
            )
            yield (
                _source_detail_column(
                    "Liability Source",
                    _person_name(getattr(agent, "person", None)),
                    "Life Insurance Loan",
                    _enum_value(getattr(agent, "policy_type", None)),
                ),
                agent,
                "Life Ins Loan Balance",
            )
        elif class_name == "Annuity":
            yield (
                _source_detail_column(
                    "Asset Source",
                    _person_name(getattr(agent, "person", None)),
                    "Annuity",
                    _enum_value(getattr(agent, "annuity_type", None)),
                ),
                agent,
                "Annuity Balance",
            )
        elif class_name == "InvestmentReturn":
            owner = getattr(agent, "owner", None) or getattr(agent, "family", None)
            yield (
                _source_detail_column(
                    "Asset Source",
                    _person_name(owner),
                    "Investment Return",
                ),
                agent,
                "Investment Balance",
            )

        if class_name in {"CarLoan", "StudentLoan", "CreditCard", "AssetLoan"}:
            yield (
                _liability_source_column(agent, class_name),
                agent,
                "Loan Balance",
            )


def _agent_stat_series(
    agent_projection: pd.DataFrame,
    agent: Any,
    stat_column: str,
    projection_index: pd.Index,
) -> Optional[pd.Series]:
    if stat_column not in agent_projection.columns:
        return None
    if not isinstance(agent_projection.index, pd.MultiIndex):
        return None

    index_names = list(agent_projection.index.names)
    agent_level = "AgentID" if "AgentID" in index_names else index_names[-1]
    step_level = "Step" if "Step" in index_names else index_names[0]
    try:
        series = agent_projection.xs(agent.unique_id, level=agent_level)[stat_column]
    except KeyError:
        return None
    if isinstance(series.index, pd.MultiIndex):
        series = series.droplevel([name for name in series.index.names if name != step_level])
    series = pd.to_numeric(series, errors="coerce").fillna(0.0)
    series = series.reindex(projection_index, fill_value=0.0)
    series.index = projection_index
    return series.astype(float)


def _series_has_signal(series: pd.Series) -> bool:
    return bool(series.fillna(0.0).abs().gt(1e-9).any())


def _unique_source_column_name(existing_columns: Iterable[str], column_name: str) -> str:
    existing = set(existing_columns)
    if column_name not in existing:
        return column_name
    suffix = 2
    while f"{column_name} #{suffix}" in existing:
        suffix += 1
    return f"{column_name} #{suffix}"


def _source_detail_column(category: str, *parts: Any) -> str:
    clean_parts = [_source_label_part(part) for part in parts]
    clean_parts = [part for part in clean_parts if part]
    if not clean_parts:
        clean_parts = ["Unlabeled"]
    return f"{category}: {' | '.join(clean_parts)}"


def _source_label_part(value: Any) -> str:
    value = _enum_value(value)
    if value is None:
        return ""
    text = " ".join(str(value).replace("|", "/").split())
    return text


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _person_name(person: Any) -> str:
    return str(getattr(person, "name", "") or "")


def _beneficiary_name(beneficiary: Any) -> str:
    name = _person_name(beneficiary)
    return f"Beneficiary {name}" if name else ""


def _liability_source_column(agent: Any, class_name: str) -> str:
    if class_name == "CarLoan":
        return _source_detail_column(
            "Liability Source",
            _person_name(getattr(agent, "person", None)),
            "Car Loan",
            getattr(agent, "name", None),
        )
    if class_name == "StudentLoan":
        return _source_detail_column(
            "Liability Source",
            _person_name(getattr(agent, "person", None)),
            "Student Loan",
            getattr(agent, "school_name", None),
            _enum_value(getattr(agent, "loan_type", None)),
        )
    if class_name == "CreditCard":
        return _source_detail_column(
            "Liability Source",
            _person_name(getattr(agent, "person", None)),
            "Credit Card",
            getattr(agent, "card_name", None),
        )
    return _source_detail_column(
        "Liability Source",
        _person_name(getattr(agent, "person", None)),
        "Real Asset Loan",
    )


def _market_override_entries(assets: Any, context: str) -> Dict[str, Dict[str, Any]]:
    if assets is None:
        return {}
    assets = _as_dict(assets, context)
    return {
        str(name): _as_dict(entry, f"{context} entry for {name}")
        for name, entry in assets.items()
    }


def market_assumptions_from_scenario(scenario: Dict[str, Any]):
    """Build market assumptions from a scenario's market_assumptions block.

    The block mirrors the configuration units (annual percentages):

        "market_assumptions": {
            "asset_classes": {"US Equity": {"expected_return": 7.0, "volatility": 22.0}},
            "yearly": {"2030": {"US Equity": {"expected_return": -10.0, "volatility": 30.0}}}
        }

    ``asset_classes`` overrides the configured defaults for the whole run;
    ``yearly`` supplies per-year overrides (e.g. outsourced from an upstream
    model). Returns None when the scenario has no block, so the configured
    defaults apply.
    """
    spec = scenario.get("market_assumptions")
    if spec is None:
        return None
    spec = _as_dict(spec, "market_assumptions")

    base = MarketAssumptions.create_default().with_asset_overrides(
        _market_override_entries(spec.get("asset_classes"), "market_assumptions.asset_classes")
    )

    yearly_spec = spec.get("yearly")
    if not yearly_spec:
        return base

    yearly = {}
    for year, assets in _as_dict(yearly_spec, "market_assumptions.yearly").items():
        try:
            year_key = int(year)
        except (TypeError, ValueError):
            raise ValueError(f"market_assumptions.yearly keys must be years, got: {year!r}")
        yearly[year_key] = _market_override_entries(
            assets, f"market_assumptions.yearly.{year_key}")
    return YearVaryingMarketAssumptions(base, yearly)


def first_threshold_crossing_distribution(
    simulation_paths: List[pd.DataFrame],
    *,
    column: str,
    threshold: float,
    operator: str,
    event: str,
) -> Optional[Dict[str, Any]]:
    """Calculate a first-event distribution from raw Monte Carlo paths.

    The distribution intentionally retains probability-by-year semantics. It is
    not a scalar "first year" and is never inferred from percentile bands.
    """

    if operator == "less_than":
        predicate: Callable[[float], bool] = lambda value: value < threshold
    elif operator == "greater_than":
        predicate = lambda value: value > threshold
    else:
        raise ValueError(f"Unsupported threshold operator: {operator}")

    event_counts: Dict[int, int] = {}
    eligible_paths = 0
    never_count = 0
    for path in simulation_paths:
        if not isinstance(path, pd.DataFrame) or column not in path or "Year" not in path:
            continue
        eligible_paths += 1
        first_year: Optional[int] = None
        for row_index, (_index, row) in enumerate(path.iterrows()):
            year = int(row["Year"])
            if row_index == 0:
                continue
            value = row[column]
            if pd.notna(value) and predicate(float(value)):
                first_year = year
                break
        if first_year is None:
            never_count += 1
        else:
            event_counts[first_year] = event_counts.get(first_year, 0) + 1

    if eligible_paths == 0:
        return None
    return {
        "type": "first_threshold_crossing_distribution",
        "event": event,
        "column": column,
        "operator": operator,
        "threshold": float(threshold),
        "probability_by_year": {
            str(year): count / eligible_paths for year, count in sorted(event_counts.items())
        },
        "probability_never": never_count / eligible_paths,
        "sample_count": eligible_paths,
        "source": "life_model.monte_carlo.raw_paths",
        "opening_baseline_excluded": True,
    }


def _run_monte_carlo_process_batch(args):
    """Execute one contiguous pre-generated Monte Carlo path batch."""

    scenario, result_columns, return_paths, batch_size = args
    simulator = MonteCarloSimulator(
        market_assumptions=market_assumptions_from_scenario(scenario),
        config=MonteCarloConfig(num_simulations=int(batch_size)),
    )
    with _authorized_public_input_context(scenario):
        results = simulator.run(
            lambda: build_model_from_scenario(scenario)[0],
            result_columns=result_columns,
            precomputed_return_paths=return_paths,
        )
    return (
        results.to_array(copy=False),
        results.get_available_columns(),
        results.get_years(),
    )


def _process_batched_monte_carlo(
    scenario: Dict[str, Any],
    *,
    assumptions,
    config: MonteCarloConfig,
    result_columns: List[str],
    max_workers: int,
):
    """Run path batches in child processes while preserving seeded path order."""

    from concurrent.futures import ProcessPoolExecutor

    worker_count = max(1, min(int(max_workers), config.num_simulations))
    if worker_count == 1:
        with _authorized_public_input_context(scenario):
            return MonteCarloSimulator(
                market_assumptions=assumptions,
                config=config,
            ).run(
                lambda: build_model_from_scenario(scenario)[0],
                result_columns=result_columns,
            )

    parent_simulator = MonteCarloSimulator(
        market_assumptions=assumptions,
        config=config,
    )
    with _authorized_public_input_context(scenario):
        return_paths = parent_simulator.prepare_return_paths(
            lambda: build_model_from_scenario(scenario)[0]
        )
    if return_paths is None:
        batch_sizes = [
            len(indices)
            for indices in np.array_split(
                np.arange(config.num_simulations),
                worker_count,
            )
            if len(indices)
        ]
        return_batches = [None] * len(batch_sizes)
    else:
        return_batches = [
            batch
            for batch in np.array_split(return_paths, worker_count, axis=0)
            if len(batch)
        ]
        batch_sizes = [len(batch) for batch in return_batches]

    jobs = [
        (scenario, result_columns, batch, batch_size)
        for batch, batch_size in zip(return_batches, batch_sizes)
    ]
    with ProcessPoolExecutor(max_workers=len(jobs)) as executor:
        batches = list(executor.map(_run_monte_carlo_process_batch, jobs))

    columns = batches[0][1]
    years = batches[0][2]
    for _values, batch_columns, batch_years in batches[1:]:
        if batch_columns != columns or batch_years != years:
            raise ValueError("Process-batched Monte Carlo result shape changed")
    from .montecarlo.results import MonteCarloResults

    return MonteCarloResults.from_array(
        np.concatenate([values for values, _columns, _years in batches], axis=0),
        columns=columns,
        years=years,
    )


def monte_carlo_projection_payload(
    scenario: Dict[str, Any],
    request: Dict[str, Any],
    monte_carlo_spec: Dict[str, Any],
):
    """Run an Agent-requested Monte Carlo projection and return JSON/CSV data."""
    assumptions = market_assumptions_from_scenario(scenario)
    mc_config = MonteCarloConfig(
        num_simulations=monte_carlo_spec["num_simulations"],
        random_seed=monte_carlo_spec.get("random_seed"),
    )
    success_column = monte_carlo_spec["success_column"]
    success_threshold = float(monte_carlo_spec.get("success_threshold", 0))
    distribution_specs = (
        (
            "net_worth_depletion",
            "Net Worth",
            success_threshold,
            "less_than",
        ),
        (
            "cashflow_shortfall",
            "Cashflow Shortfall Debt",
            1e-6,
            "greater_than",
        ),
        ("liquid_cash_depletion", "Bank Balance", 0.0, "less_than"),
    )

    projection_spec = _agent_projection_spec(request)
    collect_all_columns = (
        bool(projection_spec.get("all_columns", False))
        or projection_spec.get("columns") is None
    )
    requested_columns = None
    if not collect_all_columns:
        requested_columns = [
            column
            for column in _agent_projection_columns(
                request,
                _available_projection_columns(),
            )
            if column != "Year"
        ]

    result_columns = None
    if requested_columns is not None:
        result_columns = list(
            dict.fromkeys(
                [
                    *requested_columns,
                    success_column,
                    *[column for _event, column, _threshold, _operator in distribution_specs],
                ]
            )
        )

    process_threshold = int(
        os.getenv("AWM_MONTE_CARLO_PROCESS_THRESHOLD", "1000") or 1000
    )
    use_process_batches = (
        process_threshold > 0
        and monte_carlo_spec["num_simulations"] >= process_threshold
        and result_columns is not None
    )
    if use_process_batches:
        configured_workers = int(
            os.getenv(
                "AWM_MONTE_CARLO_PROCESS_WORKERS",
                str(min(4, os.cpu_count() or 1)),
            )
            or 1
        )
        results = _process_batched_monte_carlo(
            scenario,
            assumptions=assumptions,
            config=mc_config,
            result_columns=result_columns,
            max_workers=configured_workers,
        )
    else:
        simulator = MonteCarloSimulator(
            market_assumptions=assumptions,
            config=mc_config,
        )
        with _authorized_public_input_context(scenario):
            results = simulator.run(
                lambda: build_model_from_scenario(scenario)[0],
                result_columns=result_columns,
            )

    available = _ordered_projection_columns(
        [column for column in results.get_available_columns() if column != "Year"]
    )
    if requested_columns is None:
        selected = available
    else:
        selected = requested_columns
        missing = [column for column in selected if column not in available]
        if missing:
            raise ValueError(f"Unknown projection columns: {', '.join(missing)}")

    if success_column not in available:
        raise ValueError(f"Unknown Monte Carlo success column: {success_column}")
    success_rate = float(
        results.success_rate(column=success_column, min_balance=success_threshold)
    )

    if _agent_projection_format(request) == "csv":
        frames = []
        for column in selected:
            band_df = results.get_percentile_df(column).reset_index()
            band_df.insert(1, "Metric", column)
            frames.append(band_df)
        return projection_csv(pd.concat(frames, ignore_index=True))

    event_distributions: Dict[str, Dict[str, Any]] = {}
    for event, column, threshold, operator in distribution_specs:
        distribution = results.first_threshold_crossing_distribution(
            column=column,
            threshold=threshold,
            operator=operator,
            event=event,
        )
        if distribution is not None:
            event_distributions[event] = distribution

    return {
        "metadata": {
            "version": __version__,
            **_projection_metadata_context(scenario),
            "monte_carlo": {
                "num_simulations": monte_carlo_spec["num_simulations"],
                "random_seed": monte_carlo_spec.get("random_seed"),
                "success_column": success_column,
                "success_threshold": success_threshold,
                "market_assumptions_source": "scenario" if assumptions is not None else "config",
            },
            "row_timing": (
                "The start_year row is the opening baseline; percentile bands "
                "summarize later simulated activity by calendar year."
            ),
        },
        "warnings": _projection_warnings(scenario),
        "success_rate": success_rate,
        "years": [int(year) for year in results.get_years()],
        "event_distributions": event_distributions,
        "percentile_bands": {
            column: {
                name: [float(value) for value in values]
                for name, values in results.get_percentile_data(column).items()
            }
            for column in selected
        },
    }


def _monte_carlo_output(scenario: Dict[str, Any], args: argparse.Namespace,
                        economic_scenario: Optional[str]) -> str:
    """Run a Monte Carlo projection for the scenario and format the output."""
    assumptions = market_assumptions_from_scenario(scenario)
    mc_config = MonteCarloConfig(
        num_simulations=args.monte_carlo,
        random_seed=getattr(args, "mc_seed", None),
    )
    simulator = MonteCarloSimulator(market_assumptions=assumptions, config=mc_config)
    with _authorized_public_input_context(scenario):
        results = simulator.run(lambda: build_model_from_scenario(scenario)[0])

    available = _ordered_projection_columns(
        [column for column in results.raw_results[0].columns if column != "Year"]
    )
    if getattr(args, "all_columns", False) or not getattr(args, "columns", None):
        selected = available
    else:
        requested = [
            PROJECTION_COLUMN_ALIASES.get(column.strip(), column.strip())
            for column in args.columns.split(",") if column.strip()
        ]
        selected = [column for column in requested if column != "Year"]
        missing = [column for column in selected if column not in available]
        if missing:
            raise ValueError(f"Unknown projection columns: {', '.join(missing)}")

    success_column = getattr(args, "mc_success_column", "Net Worth")
    success_column = PROJECTION_COLUMN_ALIASES.get(success_column, success_column)
    if success_column not in available:
        raise ValueError(f"Unknown Monte Carlo success column: {success_column}")
    success_rate = float(results.success_rate(column=success_column))

    years = [int(year) for year in results.get_years()]

    if args.format == "csv":
        frames = []
        for column in selected:
            band_df = results.get_percentile_df(column).reset_index()
            band_df.insert(1, "Metric", column)
            frames.append(band_df)
        combined = pd.concat(frames, ignore_index=True)
        return projection_csv(combined)

    payload = {
        "metadata": {
            "version": __version__,
            "monte_carlo": {
                "num_simulations": args.monte_carlo,
                "random_seed": getattr(args, "mc_seed", None),
                "success_column": success_column,
                "market_assumptions_source": "scenario" if assumptions is not None else "config",
            },
            "row_timing": (
                "The start_year row is the opening baseline; every later row "
                "shows the activity and end-of-year balances of that calendar year."
            ),
        },
        "success_rate": success_rate,
        "years": years,
        "percentile_bands": {
            column: {
                name: [float(value) for value in values]
                for name, values in results.get_percentile_data(column).items()
            }
            for column in selected
        },
    }
    if economic_scenario:
        payload["metadata"]["economic_scenario"] = economic_scenario
    return json.dumps(payload, indent=2 if args.pretty else None)


def build_model_from_scenario(scenario: Dict[str, Any]) -> Tuple[LifeModel, Family, Dict[str, Person]]:
    """Build a LifeModel from an Agent-friendly scenario dictionary."""
    start_year = _required_int(scenario, "start_year")
    end_year = int(scenario.get("end_year", start_year + 50))
    model = LifeModel(start_year=start_year, end_year=end_year)
    model.input_scenario = deepcopy(scenario)
    family = Family(model, scenario.get("family_name", "Cashflow Projection"))

    people_specs = _as_list(scenario.get("people", []), "people")
    if not people_specs:
        raise ValueError("Scenario requires at least one person in 'people'")

    people: Dict[str, Person] = {}
    activation_callbacks: Dict[str, List[Callable[[], None]]] = {}
    component_specs: List[Tuple[str, Dict[str, Any], bool]] = []

    for person_spec in people_specs:
        person_id = _person_id(person_spec)
        if person_id in people:
            raise ValueError(f"Duplicate person id: {person_id}")
        person = _create_person(family, person_spec, active=True, activation_year=None)
        people[person_id] = person
        activation_callbacks[person_id] = []
        component_specs.append((person_id, person_spec, True))

    life_event_specs = _as_list(scenario.get("events", []), "events")
    life_events_to_schedule: List[LifeEvent] = []

    for event_spec in life_event_specs:
        if not isinstance(event_spec, dict):
            raise ValueError("Each event must be an object")
        if event_spec.get("type") == "marriage" and isinstance(event_spec.get("spouse"), dict):
            spouse_spec = event_spec["spouse"]
            spouse_id = _person_id(spouse_spec)
            if spouse_id in people:
                raise ValueError(f"Duplicate person id: {spouse_id}")
            person = _create_person(
                family,
                spouse_spec,
                active=False,
                activation_year=_required_int(event_spec, "year"),
            )
            people[spouse_id] = person
            activation_callbacks[spouse_id] = []
            component_specs.append((spouse_id, spouse_spec, False))

    # Creating LifeEvents before jobs/accounts lets event mutations happen before
    # job pre_step deposits in the same model year.
    life_events = LifeEvents(model)

    for person_id, person_spec, active in component_specs:
        _create_person_components(
            people[person_id],
            person_spec,
            active=active,
            activation_callbacks=activation_callbacks[person_id],
        )

    for pair in _as_list(scenario.get("married_couples", []), "married_couples"):
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("Each married_couples entry must be a two-item list")
        people[str(pair[0])].get_married(people[str(pair[1])])

    for event_spec in life_event_specs:
        life_events_to_schedule.append(
            _create_life_event(event_spec, people, activation_callbacks)
        )
    life_events.add_events(life_events_to_schedule)

    return model, family, people


def _default_input_context() -> Dict[str, Any]:
    return {
        "facts": [],
        "assumptions": [],
        "provenance": [],
        "confidence": {},
        "unknowns": [],
        "planner_overrides": [],
    }


def _projection_metadata_context(scenario: Dict[str, Any], model: Optional[LifeModel] = None) -> Dict[str, Any]:
    input_context = deepcopy(_default_input_context())
    supplied_context = scenario.get("input_context") if isinstance(scenario, dict) else None
    if isinstance(supplied_context, dict):
        input_context.update(deepcopy(supplied_context))
    tax_regime = getattr(model, "tax_regime", None)
    if tax_regime is not None and hasattr(tax_regime, "policy_assumptions"):
        policy_assumptions = tax_regime.policy_assumptions()
    else:
        policy_assumptions = {
            "tax_law_basis": getattr(tax_regime, "name", "current_law_constant"),
            "federal_policy_year": getattr(tax_regime, "federal_policy_year", 2026),
            "new_york_policy_year": getattr(tax_regime, "state_policy_year", 2025),
            "future_policy_changes_modeled": getattr(tax_regime, "future_policy_changes_modeled", False),
            "tax_precision": getattr(tax_regime, "precision", "planning_approximation_not_tax_preparation"),
        }
    return {
        "input_context": input_context,
        "policy_assumptions": policy_assumptions,
    }


def _projection_warnings(scenario: Dict[str, Any]) -> List[Dict[str, Any]]:
    warnings = [
        {
            "code": "TAX_SCOPE_LIMITATION",
            "severity": "warning",
            "message": (
                "Tax output is a planning approximation and omits AMT, NIIT, "
                "IRMAA, ACA credits, many credits and deductions, and "
                "multi-state allocation."
            ),
        },
        {
            "code": "CURRENT_LAW_CONSTANT",
            "severity": "info",
            "message": (
                "Federal 2026 and New York 2025 policy defaults are held "
                "constant unless an economic scenario overrides them."
            ),
        },
    ]
    if not isinstance(scenario, dict):
        return warnings
    people = scenario.get("people", [])
    if any(person.get("brokerage_accounts") for person in people if isinstance(person, dict)):
        warnings.append({
            "code": "TAXABLE_INVESTMENT_TAX_APPROXIMATION",
            "severity": "warning",
            "message": (
                "Brokerage liquidation funds spending and realizes aggregate "
                "cost-basis gains as ordinary income, but lot-level basis, "
                "holding periods, qualified dividends, capital-loss "
                "carryforwards, and LTCG rate schedules are not a complete "
                "investment-tax subsystem."
            ),
        })
    if any(person.get("healthcare") for person in people if isinstance(person, dict)):
        warnings.append({
            "code": "HEALTHCARE_SCOPE_LIMITATION",
            "severity": "info",
            "message": (
                "Healthcare costs can be modeled and HSA funds can offset "
                "qualified expenses; IRMAA, ACA premium credits, and detailed "
                "prescription-drug modeling are outside scope."
            ),
        })
    return warnings


def projection_payload(
    model: LifeModel,
    projection: pd.DataFrame,
    columns: Optional[Iterable[str]] = None,
    projection_system: Optional[str] = None,
) -> Dict[str, Any]:
    """Create the JSON payload returned by the CLI."""
    inflation_rate = _projection_inflation_rate()
    scenario = getattr(model, "input_scenario", {})
    systems = projection_systems(model, projection, inflation_rate)
    requested_systems = _json_projection_system_names(projection_system)
    selected_systems = {
        system_name: select_columns(system_projection, columns)
        for system_name, system_projection in systems.items()
        if system_name in requested_systems
    }
    nominal_selected = select_columns(systems["nominal"], columns)
    records = _records(nominal_selected)
    summary = _projection_summary(model, nominal_selected, systems["nominal"])
    system_payloads = {
        system_name: {
            **_projection_system_metadata(model, system_name, inflation_rate),
            "summary": _projection_summary(model, selected_projection, systems[system_name]),
            "projection": _records(selected_projection),
        }
        for system_name, selected_projection in selected_systems.items()
    }
    return {
        "metadata": {
            "version": __version__,
            "start_year": model.start_year,
            "end_year": model.end_year,
            **_projection_metadata_context(scenario, model),
            "projection_systems": {
                system_name: _projection_system_metadata(model, system_name, inflation_rate)
                for system_name in ("nominal", "real_start_year_dollars")
            },
            "row_timing": (
                "The start_year row is the opening baseline; every later row "
                "shows the activity and end-of-year balances of that calendar year."
            ),
        },
        "events": [
            {"year": event.year, "message": event.message}
            for event in model.event_log.list
        ],
        "warnings": _projection_warnings(scenario),
        "summary": summary,
        "projection_column_groups": _projection_column_groups(nominal_selected.columns),
        "projection_reconciliation_groups": _projection_reconciliation_groups(nominal_selected.columns),
        "projection_systems": system_payloads,
        "projection": records,
    }


def projection_systems(
    model: LifeModel,
    projection: pd.DataFrame,
    inflation_rate: Optional[float] = None,
) -> Dict[str, pd.DataFrame]:
    """Return nominal and start-year real-dollar projection systems."""
    if inflation_rate is None:
        inflation_rate = _projection_inflation_rate()
    return {
        "nominal": projection.copy(),
        "real_start_year_dollars": real_dollar_projection(model, projection, inflation_rate),
    }


def real_dollar_projection(
    model: LifeModel,
    projection: pd.DataFrame,
    inflation_rate: Optional[float] = None,
) -> pd.DataFrame:
    """Deflate monetary columns into start-year dollars."""
    if "Year" not in projection.columns:
        raise ValueError("Projection must include a Year column to calculate real dollars")
    if inflation_rate is None:
        inflation_rate = _projection_inflation_rate()
    if inflation_rate <= -100:
        raise ValueError("Inflation rate must be greater than -100% for real-dollar projection")

    real_projection = projection.copy()
    money_columns = _money_projection_columns(real_projection)
    if not money_columns:
        return real_projection

    year_offsets = real_projection["Year"] - model.start_year
    deflators = (1 + inflation_rate / 100) ** year_offsets
    for column in money_columns:
        real_projection[column] = real_projection[column].astype(float) / deflators
    return real_projection


def select_columns(projection: pd.DataFrame, columns: Optional[Iterable[str]] = None) -> pd.DataFrame:
    """Select projection columns with a clear error for unsupported names."""
    if columns is None:
        columns = _ordered_projection_columns(projection.columns)
    else:
        columns = [PROJECTION_COLUMN_ALIASES.get(column, column) for column in columns]
    missing = [column for column in columns if column not in projection.columns]
    if missing:
        raise ValueError(f"Unknown projection columns: {', '.join(missing)}")
    return projection[columns].copy()


def projection_csv(projection: pd.DataFrame) -> str:
    """Create a readable CSV projection with stable numeric formatting."""
    return projection.to_csv(index=False, float_format="%.2f", lineterminator="\n")


def projection_system_csv(
    model: LifeModel,
    projection: pd.DataFrame,
    columns: Optional[Iterable[str]] = None,
    projection_system: str = "nominal",
) -> str:
    """Create CSV for one or both projection dollar systems."""
    inflation_rate = _projection_inflation_rate()
    systems = projection_systems(model, projection, inflation_rate)
    if projection_system == "real":
        return projection_csv(select_columns(systems["real_start_year_dollars"], columns))
    if projection_system == "nominal":
        return projection_csv(select_columns(systems["nominal"], columns))
    if projection_system != "both":
        raise ValueError(f"Unsupported projection system: {projection_system}")

    stacked = []
    for system_name in ("nominal", "real_start_year_dollars"):
        selected = select_columns(systems[system_name], columns)
        selected.insert(0, "Projection System", system_name)
        stacked.append(selected)
    return projection_csv(pd.concat(stacked, ignore_index=True))


def example_user_profile() -> Dict[str, Any]:
    """Return a compact user profile template for upstream extraction Agents."""
    return {
        "start_year": 2026,
        "end_year": 2076,
        "family_name": "Agent Built Cashflow Scenario",
        "person": {
            "id": "primary",
            "name": "Alex",
            "age": 35,
            "retirement_age": 65,
            "state": "NY",
        },
        "income": {
            "salary": 120000,
            "yearly_increase": 3.0,
            "yearly_bonus": 5.0,
            "company": "Primary Employer",
            "role": "Professional",
        },
        "spending": {
            "annual_base": 60000,
            "yearly_increase": 2.5,
        },
        "cash": {
            "checking_balance": 50000,
            "checking_interest_rate": 1.0,
            "savings_balance": 25000,
            "savings_interest_rate": 2.0,
        },
        "housing": {
            "status": "rent",
            "monthly_rent": 2500,
            "yearly_increase": 2.5,
        },
        "retirement": {
            "401k": {
                "pretax_balance": 150000,
                "pretax_contrib_percent": 10,
                "roth_balance": 20000,
                "roth_contrib_percent": 2,
                "average_growth": 5.0,
                "company_match_percent": 4,
            }
        },
        "market_assumptions": {
            "asset_classes": {"US Equity": {"expected_return": 8.5, "volatility": 19.0}},
            "yearly": {
                "2042": {"US Equity": {"expected_return": -20.0, "volatility": 30.0}}
            },
        },
        "investment": {
            "balance": 100000,
            "growth_rate": 0,
            "asset_allocation": [
                {"asset": "US Equity", "weight": 60},
                {"asset": "US Treasury", "weight": 30},
                {"asset": "Gold", "weight": 10},
            ],
            "asset_return_rates": [
                {"asset": "US Equity", "return_rate": 10.0},
                {"asset": "US Treasury", "return_rate": 4.0},
                {"asset": "Gold", "return_rate": 5.0},
            ],
            "payout_to_bank": True,
            "cash_payout_rate": 50,
            "taxable": True,
        },
    }


def build_scenario_from_user_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Build a runnable cashflow scenario from compact extracted user facts."""
    if not isinstance(profile, dict):
        raise ValueError("User profile must be an object")

    person_spec = _as_dict(profile.get("person", {}), "person")
    income_spec = _as_dict(profile.get("income", {}), "income")
    spending_spec = _as_dict(profile.get("spending", {}), "spending")
    cash_spec = _as_dict(profile.get("cash", {}), "cash")
    housing_spec = _as_dict(profile.get("housing", {}), "housing")
    retirement_spec = _as_dict(profile.get("retirement", {}), "retirement")
    investment_input = profile.get("investment", profile.get("investments", {}))
    spouse_profile = _as_dict(profile.get("spouse", {}), "spouse")

    start_year = int(_profile_value(profile, "start_year", date.today().year))
    end_year = int(_profile_value(profile, "end_year", start_year + 50))
    person_id = str(_profile_value(person_spec, "id", profile.get("person_id", "primary")))
    name = str(_profile_value(person_spec, "name", profile.get("name", "Primary")))
    age = int(_profile_required(profile, person_spec, "age"))
    retirement_age = float(_profile_value(person_spec, "retirement_age", profile.get("retirement_age", 65)))
    state = str(_profile_value(person_spec, "state", profile.get("state", "NY")))
    salary = _profile_required_number(profile, income_spec, ("salary", "base"))

    spending_base = _profile_optional_number(
        spending_spec,
        ("annual_base", "base"),
        salary * 0.6,
    )
    spending_increase = _profile_optional_number(spending_spec, ("yearly_increase",), 2.5)
    salary_increase = _profile_optional_number(income_spec, ("yearly_increase",), 0)
    yearly_bonus = _profile_optional_number(income_spec, ("yearly_bonus", "bonus_percent"), 0)

    person = {
        "id": person_id,
        "name": name,
        "age": age,
        "retirement_age": retirement_age,
        "state": state,
        "spending": {
            "base": spending_base,
            "yearly_increase": spending_increase,
        },
        "bank_accounts": _profile_bank_accounts(cash_spec),
        "jobs": [
            {
                "id": str(_profile_value(income_spec, "job_id", f"{person_id}_job")),
                "company": str(_profile_value(income_spec, "company", "Employer")),
                "role": str(_profile_value(income_spec, "role", "Employee")),
                "salary": {
                    "base": salary,
                    "yearly_increase": salary_increase,
                    "yearly_bonus": yearly_bonus,
                },
            }
        ],
    }

    account_401k = _as_dict(retirement_spec.get("401k", profile.get("401k", {})), "401k")
    if account_401k:
        person["jobs"][0]["401k"] = _profile_401k_account(account_401k)

    apartments = _profile_apartments(housing_spec)
    if apartments:
        person["apartments"] = apartments

    homes = _profile_existing_homes(housing_spec)
    if homes:
        person["homes"] = homes

    investments = _profile_investment_returns(investment_input)
    if investments:
        person["investment_returns"] = investments

    spouse = None
    if spouse_profile:
        spouse_id = str(_profile_value(spouse_profile, "id", "spouse"))
        if spouse_id == person_id:
            raise ValueError("Spouse id must differ from primary person id")
        spouse_income_spec = _as_dict(spouse_profile.get("income", {}), "spouse.income")
        spouse_spending_spec = _as_dict(
            spouse_profile.get("spending", {}),
            "spouse.spending",
        )
        spouse_cash_spec = _as_dict(spouse_profile.get("cash", {}), "spouse.cash")
        spouse_retirement_spec = _as_dict(
            spouse_profile.get("retirement", {}),
            "spouse.retirement",
        )
        spouse_investment_input = spouse_profile.get(
            "investment",
            spouse_profile.get("investments", {}),
        )
        spouse_salary = _profile_optional_number(
            spouse_income_spec,
            ("salary", "base"),
            0,
        )
        spouse_salary_increase = _profile_optional_number(
            spouse_income_spec,
            ("yearly_increase",),
            salary_increase,
        )
        spouse_person = {
            "id": spouse_id,
            "name": str(_profile_value(spouse_profile, "name", "Spouse")),
            "age": int(_profile_required(spouse_profile, spouse_profile, "age")),
            "retirement_age": float(
                _profile_value(spouse_profile, "retirement_age", retirement_age)
            ),
            "state": str(_profile_value(spouse_profile, "state", state)),
            "spending": {
                "base": _profile_optional_number(
                    spouse_spending_spec,
                    ("annual_base", "base"),
                    0,
                ),
                "yearly_increase": _profile_optional_number(
                    spouse_spending_spec,
                    ("yearly_increase",),
                    spending_increase,
                ),
            },
            "bank_accounts": (
                _profile_bank_accounts(spouse_cash_spec) if spouse_cash_spec else []
            ),
            "jobs": [],
        }
        if spouse_income_spec or spouse_salary != 0:
            spouse_job = {
                "id": str(
                    _profile_value(
                        spouse_income_spec,
                        "job_id",
                        f"{spouse_id}_job",
                    )
                ),
                "company": str(
                    _profile_value(spouse_income_spec, "company", "Spouse Employer")
                ),
                "role": str(_profile_value(spouse_income_spec, "role", "Employee")),
                "salary": {
                    "base": spouse_salary,
                    "yearly_increase": spouse_salary_increase,
                    "yearly_bonus": _profile_optional_number(
                        spouse_income_spec,
                        ("yearly_bonus", "bonus_percent"),
                        0,
                    ),
                },
            }
            spouse_401k = _as_dict(
                spouse_retirement_spec.get("401k", spouse_profile.get("401k", {})),
                "spouse.401k",
            )
            if spouse_401k:
                spouse_job["401k"] = _profile_401k_account(spouse_401k)
            spouse_person["jobs"].append(spouse_job)
        spouse_investments = _profile_investment_returns(spouse_investment_input)
        if spouse_investments:
            spouse_person["investment_returns"] = spouse_investments
        spouse = spouse_person

    assumptions = []
    if "annual_base" not in spending_spec and "base" not in spending_spec:
        assumptions.append("spending.base defaulted to 60% of salary")
    if not cash_spec:
        assumptions.append("cash balances defaulted to zero")
    scenario = {
        "template_name": "Scenario Built From User Profile",
        "template_version": 1,
        "source": "user_profile",
        "profile_build_assumptions": assumptions,
        "start_year": start_year,
        "end_year": end_year,
        "family_name": str(_profile_value(profile, "family_name", f"{name} Cashflow Scenario")),
        "married_couples": [[person_id, spouse["id"]]] if spouse else [],
        "people": [person, spouse] if spouse else [person],
        "events": [],
    }
    # Profile-supplied market assumptions (e.g. from an upstream model) carry
    # through so Monte Carlo runs use them instead of the configured defaults
    if profile.get("market_assumptions") is not None:
        scenario["market_assumptions"] = deepcopy(profile["market_assumptions"])
    return scenario


def example_scenario() -> Dict[str, Any]:
    """Return a compact scenario template for future Agents."""
    return {
        "start_year": 2026,
        "end_year": 2076,
        "family_name": "NY Single With Future Marriage",
        "people": [
            {
                "id": "alex",
                "name": "Alex",
                "age": 35,
                "retirement_age": 65,
                "state": "NY",
                "spending": {"base": 60000, "yearly_increase": 2.5},
                "bank_accounts": [
                    {"company": "NY Bank", "type": "Checking", "balance": 50000, "interest_rate": 1.0}
                ],
                "jobs": [
                    {
                        "id": "alex_job",
                        "company": "NY Employer",
                        "role": "Professional",
                        "salary": {"base": 120000, "yearly_increase": 3.0},
                        "401k": {
                            "pretax_balance": 150000,
                            "pretax_contrib_percent": 10,
                            "average_growth": 5.0,
                            "company_match_percent": 4,
                        },
                    }
                ],
                "investment_returns": [
                    {
                        "balance": 100000,
                        "asset_allocation": [
                            {"asset": "US Equity", "weight": 60},
                            {"asset": "US Treasury", "weight": 40},
                        ],
                        "asset_return_rates": [
                            {"asset": "US Equity", "return_rate": 10.0},
                            {"asset": "US Treasury", "return_rate": 4.0},
                        ],
                        "cash_payout_rate": 50,
                        "taxable": True,
                    }
                ],
            }
        ],
        "events": [
            {
                "type": "marriage",
                "year": 2036,
                "person": "alex",
                "spouse": {
                    "id": "jordan",
                    "name": "Jordan",
                    "age": 34,
                    "retirement_age": 65,
                    "state": "NY",
                    "spending": {"base": 35000, "yearly_increase": 2.5},
                    "bank_accounts": [
                        {"company": "NY Bank", "type": "Checking", "balance": 20000, "interest_rate": 1.0}
                    ],
                    "jobs": [
                        {
                            "company": "NY Spouse Employer",
                            "role": "Manager",
                            "salary": {"base": 90000, "yearly_increase": 2.5},
                            "401k": {
                                "pretax_balance": 75000,
                                "pretax_contrib_percent": 8,
                                "average_growth": 4.0,
                                "company_match_percent": 3,
                            },
                        }
                    ],
                },
            }
        ],
    }


def _run_command(args: argparse.Namespace) -> int:
    scenario = load_scenario(Path(args.scenario))
    financial_decision_inputs = [
        load_financial_decisions(Path(path))
        for path in args.financial_decision_paths or []
    ]
    financial_decision_inputs.extend(
        parse_financial_decision_json(value)
        for value in args.financial_decision_jsons or []
    )
    if financial_decision_inputs:
        scenario = scenario_with_financial_decisions(scenario, financial_decision_inputs)

    # Apply the economic scenario before the model is built so that
    # construction-time defaults (inflation, bank interest, growth rates)
    # pick up the overrides, and keep it active through output generation so
    # the real-dollar deflator uses the scenario's inflation rate.
    economic_scenario = getattr(args, "economic_scenario", None)
    if economic_scenario:
        config.apply_predefined_scenario(economic_scenario)
    try:
        if getattr(args, "monte_carlo", None):
            output = _monte_carlo_output(scenario, args, economic_scenario)
            _write_output(output, args.output)
            return 0

        model, projection = run_scenario(scenario)
        if args.all_columns:
            selected_columns = _ordered_projection_columns(projection.columns)
        elif args.columns:
            selected_columns = [column.strip() for column in args.columns.split(",") if column.strip()]
        else:
            selected_columns = None

        if args.format == "csv":
            projection_system = args.projection_system or "nominal"
            output = projection_system_csv(model, projection, selected_columns, projection_system)
        else:
            payload = projection_payload(model, projection, selected_columns, args.projection_system)
            if economic_scenario:
                payload["metadata"]["economic_scenario"] = economic_scenario
            output = json.dumps(payload, indent=2 if args.pretty else None)
    finally:
        if economic_scenario:
            config.reset_to_defaults()

    _write_output(output, args.output)
    return 0


def _example_command(args: argparse.Namespace) -> int:
    output = json.dumps(example_scenario(), indent=2 if args.pretty else None)
    _write_output(output, None)
    return 0


def _profile_example_command(args: argparse.Namespace) -> int:
    output = json.dumps(example_user_profile(), indent=2 if args.pretty else None)
    _write_output(output, None)
    return 0


def _build_scenario_command(args: argparse.Namespace) -> int:
    profile = load_user_profile(Path(args.profile))
    scenario = build_scenario_from_user_profile(profile)
    output = json.dumps(scenario, indent=2 if args.pretty else None)
    _write_output(output, args.output)
    return 0


def _agent_run_command(args: argparse.Namespace) -> int:
    response, status_code = _run_agent_command_safely(
        lambda: agent_run_request(load_agent_request(
            Path(args.request) if args.request else None,
            read_stdin=args.stdin,
        ))
    )
    if status_code == 0 and response.get("projection_format") == "csv":
        output = response["result"]["projection_csv"]
    else:
        output = json.dumps(response, indent=2 if args.pretty else None)
    _write_output(output, args.output)
    return status_code


def _validate_agent_request_command(args: argparse.Namespace) -> int:
    response, status_code = _run_agent_command_safely(
        lambda: validate_agent_request(load_agent_request(
            Path(args.request) if args.request else None,
            read_stdin=args.stdin,
        ))
    )
    output = json.dumps(response, indent=2 if args.pretty else None)
    _write_output(output, None)
    return status_code


def _print_agent_schema_command(args: argparse.Namespace) -> int:
    output = json.dumps(agent_request_schema(), indent=2 if args.pretty else None)
    _write_output(output, None)
    return 0


def agent_request_schema() -> Dict[str, Any]:
    """Return the Agent request contract."""
    return {
        "schema_version": AGENT_REQUEST_SCHEMA_VERSION,
        "description": (
            "Single JSON contract for Agent tool use. Provide exactly one of "
            "'user_profile' or 'scenario', optional financial_decisions, and "
            "projection options."
        ),
        "required_one_of": ["user_profile", "scenario"],
        "properties": {
            "schema_version": {"type": "integer", "const": AGENT_REQUEST_SCHEMA_VERSION},
            "user_profile": {
                "type": "object",
                "description": "Compact extracted user facts accepted by build-scenario.",
            },
            "scenario": {
                "type": "object",
                "description": "Full cashflow scenario object accepted by run.",
            },
            "financial_decisions": {
                "type": ["array", "object"],
                "description": "Optional Agent-selected financial decisions appended as model events.",
            },
            "input_context": {
                "type": "object",
                "description": (
                    "Facts, assumptions, provenance, confidence, unknowns, "
                    "and planner overrides preserved into projection metadata."
                ),
                "properties": {
                    "facts": {"type": "array"},
                    "assumptions": {"type": "array"},
                    "provenance": {"type": "array"},
                    "confidence": {"type": "object"},
                    "unknowns": {"type": "array"},
                    "planner_overrides": {"type": "array"},
                },
            },
            "authorized_public_model_inputs": {
                "type": "array",
                "minItems": 1,
                "maxItems": 1,
                "description": (
                    "Server-authorized public model input supplied by the AWM bridge; "
                    "not accepted from an Agent-authored scenario."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "schema_version": {
                            "type": "string",
                            "const": "awm.authorized_public_model_input.v1",
                        },
                        "variable_key": {
                            "type": "string",
                            "const": "social_security_taxable_maximum",
                        },
                        "value": {"type": "number", "exclusiveMinimum": 0},
                        "unit": {"type": "string", "const": "USD_annual"},
                        "jurisdiction": {"type": "string", "const": "US"},
                        "effective_year": {"type": "integer"},
                        "content_sha256": {"type": "string"},
                        "sources": {"type": "array", "minItems": 1, "maxItems": 3},
                    },
                },
            },
            "monte_carlo": {
                "type": "object",
                "description": "Optional stochastic run controls for agent-run.",
                "properties": {
                    "num_simulations": {"type": "integer", "minimum": 1, "default": 100},
                    "random_seed": {"type": ["integer", "null"]},
                    "success_column": {"type": "string", "default": "Net Worth"},
                    "success_threshold": {"type": "number", "default": 0},
                },
            },
            "projection": {
                "type": "object",
                "properties": {
                    "systems": {
                        "type": ["string", "array"],
                        "enum": ["both", "nominal", "real", "real_start_year_dollars"],
                        "default": "both",
                    },
                    "columns": {
                        "type": ["array", "string", "null"],
                        "description": "Projection columns; omit for every collected projection column.",
                    },
                    "all_columns": {"type": "boolean", "default": True},
                    "format": {"type": "string", "enum": ["csv", "json"], "default": "json"},
                },
            },
            "return_resolved_scenario": {
                "type": "boolean",
                "default": False,
                "description": "When true, include the fully wired scenario used for projection.",
            },
        },
        "accepted_financial_decision_event_names": [
            "Marriage",
            "Divorce",
            "Child birth/adoption",
            "Home purchase",
            "Go to college",
            "One-time expense",
            "One-time income",
        ],
        "scenario_schema": _agent_scenario_schema(),
        "available_projection_columns": _available_projection_columns(),
        "projection_column_groups": _projection_column_groups(_available_projection_columns()),
        "projection_reconciliation_groups": _projection_reconciliation_groups(_available_projection_columns()),
        "example": agent_request_example(),
    }


def _agent_scenario_schema() -> Dict[str, Any]:
    person_properties = {
        name: {"type": "array" if name not in {"spending", "social_security", "healthcare"} else "object"}
        for name in sorted(PERSON_ALLOWED_FIELDS)
    }
    person_properties.update({
        "id": {"type": "string"},
        "name": {"type": "string"},
        "age": {"type": "integer", "unit": "years"},
        "retirement_age": {"type": "number", "unit": "years"},
        "state": {"type": ["string", "null"]},
    })
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "start_year": {"type": "integer", "unit": "calendar_year"},
            "end_year": {"type": "integer", "unit": "calendar_year"},
            "family_name": {"type": "string"},
            "people": {"type": "array", "items": {"$ref": "#/scenario_schema/person_schema"}},
            "married_couples": {"type": "array"},
            "events": {"type": "array"},
            "market_assumptions": {"type": "object"},
            "input_context": {"type": "object"},
        },
        "required": ["start_year", "people"],
        "person_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": person_properties,
            "component_properties": {
                key: {"allowed_fields": sorted(value)}
                for key, value in sorted(COMPONENT_ALLOWED_FIELDS.items())
            },
        },
    }


def agent_request_example() -> Dict[str, Any]:
    """Return a compact Agent request example."""
    return {
        "schema_version": AGENT_REQUEST_SCHEMA_VERSION,
        "user_profile": {
            "start_year": 2026,
            "end_year": 2076,
            "family_name": "Agent Request Example",
            "person": {
                "id": "primary",
                "name": "Alex",
                "age": 35,
                "retirement_age": 65,
                "state": "NY",
            },
            "income": {
                "salary": 120000,
                "yearly_increase": 3.0,
                "company": "Employer",
                "role": "Professional",
            },
            "spending": {
                "annual_base": 60000,
            },
            "cash": {
                "checking_balance": 50000,
            },
            "housing": {
                "status": "rent",
                "monthly_rent": 2500,
            },
        },
        "financial_decisions": [
            {
                "event_name": "One-time expense",
                "year": 2030,
                "payload": {
                    "person": "primary",
                    "name": "Car purchase",
                    "amount": 30000,
                },
            }
        ],
        "projection": {
            "systems": "both",
            "format": "json",
        },
    }


def _run_agent_command_safely(operation: Callable[[], Dict[str, Any]]) -> Tuple[Dict[str, Any], int]:
    try:
        return operation(), 0
    except Exception as exc:
        return _agent_error_response(exc), 1


def _agent_error_response(exc: Exception) -> Dict[str, Any]:
    if isinstance(exc, json.JSONDecodeError):
        code = "INVALID_JSON"
    elif isinstance(exc, FileNotFoundError):
        code = "FILE_NOT_FOUND"
    elif isinstance(exc, ValueError):
        code = "VALIDATION_ERROR"
    else:
        code = "RUNTIME_ERROR"
    return {
        "ok": False,
        "schema_version": AGENT_REQUEST_SCHEMA_VERSION,
        "error": {
            "code": code,
            "message": str(exc),
        },
    }


def _validate_known_fields(value: Dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"Unknown scenario fields at {context}: {', '.join(unknown)}")


def _validated_authorized_public_model_inputs(
    raw_inputs: Any,
    *,
    effective_year: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Validate the bridge-sanitized, server-authorized model input."""

    if raw_inputs is None:
        return []
    if not isinstance(raw_inputs, list) or len(raw_inputs) != 1:
        raise ValueError("authorized_public_model_inputs must contain exactly one item")
    item = raw_inputs[0]
    if not isinstance(item, dict) or set(item) != AUTHORIZED_PUBLIC_MODEL_INPUT_FIELDS:
        raise ValueError("authorized_public_model_inputs[0] has an invalid shape")
    if item.get("schema_version") != "awm.authorized_public_model_input.v1":
        raise ValueError("authorized_public_model_inputs[0].schema_version is unsupported")
    if item.get("variable_key") != "social_security_taxable_maximum":
        raise ValueError("authorized public model input variable is unsupported")
    if item.get("unit") != "USD_annual" or item.get("jurisdiction") != "US":
        raise ValueError("social_security_taxable_maximum requires USD_annual in US")
    item_year = item.get("effective_year")
    if isinstance(item_year, bool) or not isinstance(item_year, int):
        raise ValueError("authorized public model input effective_year must be an integer")
    if effective_year is not None and item_year != effective_year:
        raise ValueError(
            "authorized public model input effective_year must match scenario start_year"
        )
    value = item.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("authorized public model input value must be numeric")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("authorized public model input value must be finite and positive")
    content_sha256 = item.get("content_sha256")
    if not isinstance(content_sha256, str) or re.fullmatch(
        r"sha256:[a-f0-9]{64}", content_sha256
    ) is None:
        raise ValueError("authorized public model input content_sha256 is invalid")
    sources = item.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 3:
        raise ValueError("authorized public model input requires one to three sources")
    for source in sources:
        if not isinstance(source, dict) or set(source) != AUTHORIZED_PUBLIC_MODEL_SOURCE_FIELDS:
            raise ValueError("authorized public model input source has an invalid shape")
        if not all(
            isinstance(source.get(field), str) and bool(source[field].strip())
            for field in ("publisher", "title", "url")
        ):
            raise ValueError("authorized public model input source is incomplete")
        if source.get("published_at") is not None and not isinstance(
            source.get("published_at"), str
        ):
            raise ValueError("authorized public model input source published_at is invalid")
    normalized = deepcopy(item)
    normalized["value"] = value
    return [normalized]


def _authorized_public_input_context(scenario: Dict[str, Any]):
    input_context = scenario.get("input_context") if isinstance(scenario, dict) else None
    raw_inputs = (
        input_context.get("authorized_public_model_inputs")
        if isinstance(input_context, dict)
        else None
    )
    inputs = _validated_authorized_public_model_inputs(
        raw_inputs,
        effective_year=_required_int(scenario, "start_year"),
    )
    if not inputs:
        return nullcontext()
    return authorized_social_security_max_income(inputs[0]["value"])


def _validate_agent_request_fields(request: Dict[str, Any]) -> None:
    request = _as_dict(request, "agent request")
    _validate_known_fields(request, AGENT_REQUEST_ALLOWED_FIELDS, "agent request")
    _validated_authorized_public_model_inputs(
        request.get("authorized_public_model_inputs")
    )
    input_context = request.get("input_context")
    if isinstance(input_context, dict) and "authorized_public_model_inputs" in input_context:
        raise ValueError(
            "input_context.authorized_public_model_inputs is server-reserved"
        )
    if request.get("scenario") is not None:
        _validate_scenario_contract(_as_dict(request["scenario"], "scenario"))


def _validate_scenario_contract(scenario: Dict[str, Any]) -> None:
    _validate_known_fields(scenario, SCENARIO_ALLOWED_FIELDS, "scenario")
    input_context = scenario.get("input_context")
    if isinstance(input_context, dict) and "authorized_public_model_inputs" in input_context:
        raise ValueError(
            "scenario.input_context.authorized_public_model_inputs is server-reserved"
        )
    for index, person in enumerate(_as_list(scenario.get("people", []), "people")):
        person = _as_dict(person, f"people[{index}]")
        _validate_person_contract(person, f"people[{index}]")
    for index, event in enumerate(_as_list(scenario.get("events", []), "events")):
        if isinstance(event, dict) and isinstance(event.get("spouse"), dict):
            _validate_person_contract(event["spouse"], f"events[{index}].spouse")


def _validate_person_contract(person: Dict[str, Any], context: str) -> None:
    _validate_known_fields(person, PERSON_ALLOWED_FIELDS, context)
    spending = person.get("spending")
    if spending is not None:
        _validate_known_fields(_as_dict(spending, f"{context}.spending"),
                               COMPONENT_ALLOWED_FIELDS["spending"],
                               f"{context}.spending")

    for collection_name, allowed in COMPONENT_ALLOWED_FIELDS.items():
        if collection_name in {"spending", "salary", "401k", "home_mortgage", "home_expenses"}:
            continue
        if collection_name not in person or person[collection_name] is None:
            continue
        if isinstance(person[collection_name], dict):
            item = _as_dict(person[collection_name], f"{context}.{collection_name}")
            _validate_known_fields(
                item,
                allowed,
                f"{context}.{collection_name}",
            )
            if collection_name == "homes":
                if "mortgage" in item:
                    _validate_known_fields(
                        _as_dict(item["mortgage"], f"{context}.homes.mortgage"),
                        COMPONENT_ALLOWED_FIELDS["home_mortgage"],
                        f"{context}.homes.mortgage",
                    )
                if "expenses" in item:
                    _validate_known_fields(
                        _as_dict(item["expenses"], f"{context}.homes.expenses"),
                        COMPONENT_ALLOWED_FIELDS["home_expenses"],
                        f"{context}.homes.expenses",
                    )
            continue
        for index, item in enumerate(_as_list(person[collection_name], collection_name)):
            item = _as_dict(item, f"{context}.{collection_name}[{index}]")
            _validate_known_fields(item, allowed, f"{context}.{collection_name}[{index}]")
            if collection_name == "jobs":
                if "salary" in item:
                    _validate_known_fields(
                        _as_dict(item["salary"], f"{context}.{collection_name}[{index}].salary"),
                        COMPONENT_ALLOWED_FIELDS["salary"],
                        f"{context}.{collection_name}[{index}].salary",
                    )
                if "401k" in item:
                    _validate_known_fields(
                        _as_dict(item["401k"], f"{context}.{collection_name}[{index}].401k"),
                        COMPONENT_ALLOWED_FIELDS["401k"],
                        f"{context}.{collection_name}[{index}].401k",
                    )
            if collection_name == "homes":
                if "mortgage" in item:
                    _validate_known_fields(
                        _as_dict(item["mortgage"], f"{context}.homes[{index}].mortgage"),
                        COMPONENT_ALLOWED_FIELDS["home_mortgage"],
                        f"{context}.homes[{index}].mortgage",
                    )
                if "expenses" in item:
                    _validate_known_fields(
                        _as_dict(item["expenses"], f"{context}.homes[{index}].expenses"),
                        COMPONENT_ALLOWED_FIELDS["home_expenses"],
                        f"{context}.homes[{index}].expenses",
                    )


def _agent_monte_carlo_spec(request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    raw_spec = request.get("monte_carlo")
    if raw_spec in (None, False):
        return None
    spec = _as_dict(raw_spec, "monte_carlo")
    allowed = {"num_simulations", "random_seed", "seed", "success_column", "success_threshold"}
    _validate_known_fields(spec, allowed, "monte_carlo")
    num_simulations = int(spec.get("num_simulations", 100))
    if num_simulations <= 0:
        raise ValueError("monte_carlo.num_simulations must be positive")
    success_column = PROJECTION_COLUMN_ALIASES.get(
        str(spec.get("success_column", "Net Worth")),
        str(spec.get("success_column", "Net Worth")),
    )
    return {
        "num_simulations": num_simulations,
        "random_seed": spec.get("random_seed", spec.get("seed")),
        "success_column": success_column,
        "success_threshold": float(spec.get("success_threshold", 0)),
    }


def _agent_request_to_scenario(request: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    request = _as_dict(request, "agent request")
    schema_version = int(request.get("schema_version", AGENT_REQUEST_SCHEMA_VERSION))
    if schema_version != AGENT_REQUEST_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported Agent request schema_version {schema_version}; "
            f"expected {AGENT_REQUEST_SCHEMA_VERSION}"
        )

    has_user_profile = request.get("user_profile") is not None
    has_scenario = request.get("scenario") is not None
    if has_user_profile == has_scenario:
        raise ValueError("Agent request must provide exactly one of 'user_profile' or 'scenario'")

    if has_user_profile:
        scenario = build_scenario_from_user_profile(_as_dict(request["user_profile"], "user_profile"))
        scenario_source = "user_profile"
    else:
        scenario = deepcopy(_as_dict(request["scenario"], "scenario"))
        scenario_source = "scenario"

    if request.get("input_context") is not None:
        scenario["input_context"] = deepcopy(_as_dict(request["input_context"], "input_context"))

    authorized_inputs = _validated_authorized_public_model_inputs(
        request.get("authorized_public_model_inputs"),
        effective_year=_required_int(scenario, "start_year"),
    )
    if authorized_inputs:
        scenario.setdefault("input_context", {})[
            "authorized_public_model_inputs"
        ] = authorized_inputs

    financial_decision_inputs = _agent_financial_decision_inputs(request)
    if financial_decision_inputs:
        scenario = scenario_with_financial_decisions(scenario, financial_decision_inputs)

    return scenario, scenario_source


def _agent_financial_decision_inputs(request: Dict[str, Any]) -> List[Any]:
    inputs = []
    for key in ("financial_decisions", "financial_decision", "decisions"):
        if key in request and request[key] is not None:
            inputs.append(request[key])
    return inputs


def _agent_projection_columns(
    request: Dict[str, Any],
    available_columns: Iterable[str],
) -> List[str]:
    projection_spec = _agent_projection_spec(request)
    available = list(available_columns)
    if bool(projection_spec.get("all_columns", False)):
        return _ordered_projection_columns(available)

    columns = projection_spec.get("columns")
    if columns is None:
        return _ordered_projection_columns(available)
    if isinstance(columns, str):
        selected = [column.strip() for column in columns.split(",") if column.strip()]
    elif isinstance(columns, list):
        selected = [str(column) for column in columns]
    else:
        raise ValueError("Agent request projection.columns must be a list, comma-separated string, or null")
    selected = [PROJECTION_COLUMN_ALIASES.get(column, column) for column in selected]

    missing = [column for column in selected if column not in available]
    if missing:
        raise ValueError(f"Unknown projection columns: {', '.join(missing)}")
    return selected


def _agent_projection_system(request: Dict[str, Any]) -> str:
    projection_spec = _agent_projection_spec(request)
    return _normalize_agent_projection_system(
        projection_spec.get(
            "systems",
            projection_spec.get("system", projection_spec.get("projection_system", "both")),
        )
    )


def _agent_projection_format(request: Dict[str, Any]) -> str:
    projection_spec = _agent_projection_spec(request)
    value = projection_spec.get(
        "format",
        projection_spec.get("output_format", projection_spec.get("projection_format", "json")),
    )
    if value is None:
        return "json"
    normalized = str(value).strip().lower()
    if normalized in {"csv", "json"}:
        return normalized
    raise ValueError(f"Agent request projection.format must be 'csv' or 'json', got: {value}")


def _agent_projection_spec(request: Dict[str, Any]) -> Dict[str, Any]:
    return _as_dict(request.get("projection", {}), "projection")


def _normalize_agent_projection_system(value: Any) -> str:
    if value is None:
        return "both"
    if isinstance(value, list):
        normalized_values = {_normalize_agent_projection_system(item) for item in value}
        if len(normalized_values) > 1 or "both" in normalized_values:
            return "both"
        return next(iter(normalized_values))

    normalized = str(value).strip().lower().replace("-", "_")
    if normalized in {"both", "all"}:
        return "both"
    if normalized in {"nominal", "nominal_dollars", "projected_year_nominal_dollars"}:
        return "nominal"
    if normalized in {
        "real",
        "real_dollars",
        "start_year_real_dollars",
        "real_start_year_dollars",
        "start_year_dollars",
    }:
        return "real"
    raise ValueError(f"Unsupported projection system: {value}")


def _ordered_projection_columns(columns: Iterable[str]) -> List[str]:
    available = list(columns)
    ordered: List[str] = []
    seen = set()
    for name, _title, group_columns in PROJECTION_COLUMN_GROUPS:
        group_present = _ordered_projection_group_columns(name, group_columns, available, seen)
        ordered.extend(group_present)
        seen.update(group_present)
    ordered.extend(column for column in available if column not in seen)
    return ordered


def _projection_column_groups(columns: Iterable[str]) -> List[Dict[str, Any]]:
    available = list(columns)
    grouped = set()
    groups = []
    for name, title, group_columns in PROJECTION_COLUMN_GROUPS:
        present = _ordered_projection_group_columns(name, group_columns, available, grouped)
        if present:
            groups.append({"name": name, "title": title, "columns": present})
            grouped.update(present)
    other = [column for column in _ordered_projection_columns(available) if column not in grouped]
    if other:
        groups.append({"name": "other", "title": "Other modeled items", "columns": other})
    return groups


def _projection_reconciliation_groups(columns: Iterable[str]) -> List[Dict[str, Any]]:
    available = list(columns)
    available_set = set(available)
    groups: List[Dict[str, Any]] = []

    def present(column_names: Iterable[str]) -> List[str]:
        return [column for column in column_names if column in available_set]

    def add_group(
        name: str,
        title: str,
        component_columns: Iterable[str],
        reconciles_to: str,
        formula: str,
        notes: Optional[List[str]] = None,
        supporting_columns: Optional[Iterable[str]] = None,
    ) -> None:
        components = present(component_columns)
        if reconciles_to not in available_set or not components:
            return
        group = {
            "name": name,
            "title": title,
            "columns": components,
            "reconciles_to": reconciles_to,
            "formula": formula,
        }
        supporting = present(supporting_columns or [])
        if supporting:
            group["supporting_columns_excluded_from_total"] = supporting
        if notes:
            group["notes"] = notes
        groups.append(group)

    income_source_columns = [column for column in available if column.startswith("Income Source: ")]
    asset_source_columns = [column for column in available if column.startswith("Asset Source: ")]
    liability_source_columns = [column for column in available if column.startswith("Liability Source: ")]
    spending_subcategory_columns = [
        "Base Living Spending",
        "One-time Expenses",
        "Education Costs",
        "Home Purchase Costs",
        "Asset Sale Shortfalls",
        "Child Costs",
        "Healthcare Costs",
        "Real Asset Costs",
    ]

    add_group(
        "income_sources",
        "Income source reconciliation",
        income_source_columns,
        "Income",
        "Income = sum(Income Source:*)",
    )
    add_group(
        "cash_inflows",
        "Cash inflow reconciliation",
        CASHFLOW_INFLOW_COMPONENT_COLUMNS,
        "Total Cash Inflows",
        "Total Cash Inflows = sum(cash inflow columns)",
        notes=[
            "This total uses spendable or cash-balance inflows. It excludes non-cash or restricted supporting inflow details.",
            "Gross job income is included in Income; employee 401k contributions are shown separately as cash outflows.",
        ],
        supporting_columns=CASHFLOW_SUPPORTING_INFLOW_COLUMNS,
    )
    add_group(
        "cash_outflows_before_taxes",
        "Cash outflow reconciliation before taxes",
        CASHFLOW_OUTFLOW_BEFORE_TAX_COLUMNS,
        "Total Cash Outflows Before Taxes",
        "Total Cash Outflows Before Taxes = sum(cash outflow columns before taxes)",
    )
    add_group(
        "cash_outflows",
        "Cash outflow reconciliation",
        ["Total Cash Outflows Before Taxes", "Taxes"],
        "Total Cash Outflows",
        "Total Cash Outflows = Total Cash Outflows Before Taxes + Taxes",
    )
    add_group(
        "net_cashflow_before_taxes",
        "Net cashflow before taxes reconciliation",
        ["Total Cash Inflows", "Total Cash Outflows Before Taxes"],
        "Net Cashflow Before Taxes",
        "Net Cashflow Before Taxes = Total Cash Inflows - Total Cash Outflows Before Taxes",
    )
    add_group(
        "net_cashflow",
        "Net cashflow reconciliation",
        ["Total Cash Inflows", "Total Cash Outflows"],
        "Net Cashflow",
        "Net Cashflow = Total Cash Inflows - Total Cash Outflows",
        notes=[
            "Net Cashflow is before balance-sheet funding actions. Cashflow Shortfall Debt is the modeled unpaid shortfall after available cash and modeled funding sources are applied.",
        ],
    )
    add_group(
        "spending_subcategories",
        "Spending subcategory reconciliation",
        spending_subcategory_columns,
        "Spending",
        "Spending = sum(mutually exclusive spending subcategories)",
        notes=["529 Contributions, Housing, Loan Payments, 401k Contrib, Charity, premiums, deductibles, and taxes are separate cash outflow lines, not part of Spending."],
    )
    add_group(
        "asset_sources",
        "Asset source reconciliation",
        asset_source_columns,
        "Total Assets",
        "Total Assets = sum(Asset Source:*)",
    )
    add_group(
        "liability_sources",
        "Liability source reconciliation",
        liability_source_columns,
        "Total Liabilities",
        "Total Liabilities = sum(Liability Source:*)",
    )
    add_group(
        "net_worth",
        "Net worth reconciliation",
        ["Total Assets", "Total Liabilities"],
        "Net Worth",
        "Net Worth = Total Assets - Total Liabilities",
    )
    return groups


def _ordered_projection_group_columns(
    group_name: str,
    group_columns: Iterable[str],
    available: Iterable[str],
    seen: Iterable[str],
) -> List[str]:
    available_list = list(available)
    available_set = set(available_list)
    seen_set = set(seen)
    present: List[str] = []
    placed_prefixes = set()
    placements = DYNAMIC_PROJECTION_COLUMN_PLACEMENTS.get(group_name, {})

    def add_column(column: str) -> None:
        if column in available_set and column not in seen_set and column not in present:
            present.append(column)

    def add_prefix_columns(prefixes: Iterable[str]) -> None:
        for prefix in prefixes:
            placed_prefixes.add(prefix)
            for column in available_list:
                if column.startswith(prefix) and column not in seen_set and column not in present:
                    present.append(column)

    for column in group_columns:
        add_column(column)
        add_prefix_columns(placements.get(column, ()))

    trailing_prefixes = [
        prefix
        for prefixes in placements.values()
        for prefix in prefixes
        if prefix not in placed_prefixes
    ]
    add_prefix_columns(trailing_prefixes)
    return present


def _available_projection_columns() -> List[str]:
    columns = ["Year"]
    for stat in [*LifeModel.STATS, *LifeModel.EXTRA_STATS]:
        if stat.title not in columns:
            columns.append(stat.title)
    for column in CASHFLOW_CALCULATED_COLUMNS:
        if column not in columns:
            columns.append(column)
    return _ordered_projection_columns(columns)


def _create_person(
    family: Family,
    person_spec: Dict[str, Any],
    active: bool,
    activation_year: Optional[int],
) -> Person:
    name = str(_required(person_spec, "name"))
    age = _required_int(person_spec, "age")
    if not active:
        # The spec age is the person's age during the activation year. A
        # person is age A in the baseline row and ages one year per simulated
        # year, so back-date the constructed age accordingly.
        age = age - (activation_year - family.model.start_year)
    spending_spec = _as_dict(person_spec.get("spending", {}), "spending")
    spending = Spending(
        family.model,
        base=float(spending_spec.get("base", 0)) if active else 0,
        yearly_increase=spending_spec.get("yearly_increase"),
    )
    return Person(
        family=family,
        name=name,
        age=age,
        retirement_age=float(_required(person_spec, "retirement_age")),
        spending=spending,
        state=person_spec.get("state"),
    )


def _create_person_components(
    person: Person,
    person_spec: Dict[str, Any],
    active: bool,
    activation_callbacks: List[Callable[[], None]],
) -> None:
    _register_spending_activation(person, person_spec, active, activation_callbacks)
    _create_bank_accounts(person, person_spec, active, activation_callbacks)
    _create_apartments(person, person_spec, active, activation_callbacks)
    _create_homes(person, person_spec, active)
    _create_jobs(person, person_spec, active, activation_callbacks)
    _create_pensions(person, person_spec, active)
    _create_social_security(person, person_spec, active)
    _create_trusts(person, person_spec, active)
    _create_tangible_assets(person, person_spec, active)
    _create_annuities(person, person_spec, active)
    _create_hsa_accounts(person, person_spec, active)
    _create_brokerage_accounts(person, person_spec, active)
    _create_ira_accounts(person, person_spec, active)
    _create_life_insurance(person, person_spec, active)
    _create_standalone_529s(person, person_spec, active)
    _create_donations(person, person_spec, active)
    _create_donor_advised_funds(person, person_spec, active)
    _create_insurance_policies(person, person_spec, active)
    _create_consumer_debt(person, person_spec, active)
    _create_healthcare(person, person_spec, active)
    _create_investment_returns(person, person_spec, active, activation_callbacks)
    if active:
        _create_children(person, person_spec)


def _register_spending_activation(
    person: Person,
    person_spec: Dict[str, Any],
    active: bool,
    activation_callbacks: List[Callable[[], None]],
) -> None:
    if active:
        return
    spending_spec = _as_dict(person_spec.get("spending", {}), "spending")
    base = float(spending_spec.get("base", 0))
    activation_callbacks.append(lambda person=person, base=base: setattr(person.spending, "base", base))


def _create_bank_accounts(
    person: Person,
    person_spec: Dict[str, Any],
    active: bool,
    activation_callbacks: List[Callable[[], None]],
) -> None:
    for account_spec in _as_list(person_spec.get("bank_accounts", []), "bank_accounts"):
        account = BankAccount(
            owner=person,
            company=str(account_spec.get("company", "Bank")),
            type=str(account_spec.get("type", "Bank")),
            balance=float(account_spec.get("balance", 0)) if active else 0,
            interest_rate=account_spec.get("interest_rate"),
        )
        if not active:
            balance = float(account_spec.get("balance", 0))
            activation_callbacks.append(lambda account=account, balance=balance: setattr(account, "balance", balance))


def _create_apartments(
    person: Person,
    person_spec: Dict[str, Any],
    active: bool,
    activation_callbacks: List[Callable[[], None]],
) -> None:
    for apartment_spec in _as_list(person_spec.get("apartments", []), "apartments"):
        apartment = Apartment(
            person=person,
            name=str(apartment_spec.get("name", "Apartment")),
            monthly_rent=float(apartment_spec.get("monthly_rent", 0)) if active else 0,
            yearly_increase=apartment_spec.get("yearly_increase"),
        )
        if not active:
            monthly_rent = float(apartment_spec.get("monthly_rent", 0))
            activation_callbacks.append(
                lambda apartment=apartment, monthly_rent=monthly_rent: setattr(
                    apartment, "monthly_rent", monthly_rent
                )
            )


def _create_homes(person: Person, person_spec: Dict[str, Any], active: bool) -> None:
    """Create homes and mortgages that exist at the opening projection date."""

    for index, home_spec in enumerate(_as_list(person_spec.get("homes", []), "homes")):
        if not active:
            raise ValueError("homes are not supported on event-activated people")
        home_spec = _as_dict(home_spec, f"homes[{index}]")
        mortgage_spec = _as_dict(
            _required(home_spec, "mortgage"),
            f"homes[{index}].mortgage",
        )
        mortgage_type = str(
            mortgage_spec.get("mortgage_type", "")
        ).strip().lower().replace("-", "_").replace(" ", "_")
        if mortgage_type not in {"fixed", "fixed_rate", "fixed_rate_mortgage"}:
            raise ValueError(
                "Opening-position mortgages currently require mortgage_type=fixed_rate"
            )
        principal = float(_required(mortgage_spec, "principal_balance"))
        interest_rate = float(_required(mortgage_spec, "yearly_interest_rate"))
        remaining_term_value = float(_required(mortgage_spec, "remaining_term_years"))
        if not remaining_term_value.is_integer():
            raise ValueError(
                "Opening mortgage remaining_term_years must be a positive whole number"
            )
        remaining_term_years = int(remaining_term_value)
        supplied_payment = mortgage_spec.get("monthly_payment")
        mortgage = Mortgage(
            loan_amount=principal,
            start_date=person.model.start_year,
            length_years=remaining_term_years,
            yearly_interest_rate=interest_rate,
            principal=principal,
            monthly_payment=(
                float(supplied_payment) if supplied_payment is not None else None
            ),
            mortgage_type=MortgageType.FIXED_RATE,
        )
        if supplied_payment is not None:
            derived_payment = Mortgage(
                loan_amount=principal,
                start_date=person.model.start_year,
                length_years=remaining_term_years,
                yearly_interest_rate=interest_rate,
                principal=principal,
                mortgage_type=MortgageType.FIXED_RATE,
            ).monthly_payment
            tolerance = max(5.0, derived_payment * 0.02)
            if not math.isclose(float(supplied_payment), derived_payment, abs_tol=tolerance):
                raise ValueError(
                    "Opening mortgage monthly_payment conflicts with principal, interest "
                    "rate, and remaining term by more than 2%"
                )

        expenses_spec = _as_dict(home_spec.get("expenses", {}), f"homes[{index}].expenses")
        inflation = config.financial.get_inflation_rate()
        home_expenses = HomeExpenses(
            model=person.model,
            property_tax_percent=float(expenses_spec.get("property_tax_percent", 0)),
            home_insurance_percent=float(expenses_spec.get("home_insurance_percent", 0)),
            maintenance_amount=float(expenses_spec.get("maintenance_amount", 0)),
            maintenance_increase=float(expenses_spec.get("maintenance_increase", inflation)),
            improvement_amount=float(expenses_spec.get("improvement_amount", 0)),
            improvement_increase=float(expenses_spec.get("improvement_increase", inflation)),
            hoa_amount=float(expenses_spec.get("hoa_amount", 0)),
            hoa_increase=float(expenses_spec.get("hoa_increase", inflation)),
        )
        current_value = float(_required(home_spec, "current_value"))
        if current_value <= 0:
            raise ValueError("Opening home current_value must be positive")
        tax_basis = home_spec.get("tax_basis")
        home = Home(
            person=person,
            name=str(home_spec.get("name", "Primary Home")),
            purchase_price=(float(tax_basis) if tax_basis is not None else current_value),
            current_value=current_value,
            tax_basis_known=tax_basis is not None,
            value_yearly_increase=float(_required(home_spec, "value_yearly_increase")),
            down_payment=0.0,
            mortgage=mortgage,
            expenses=home_expenses,
        )
        included_in_base = home_spec.get("mortgage_included_in_base_spending")
        if not isinstance(included_in_base, bool):
            raise ValueError(
                "mortgage_included_in_base_spending must be a boolean"
            )
        if included_in_base:
            scheduled_mortgage = mortgage.get_payment_due_for_year()
            if person.spending.base + 0.01 < scheduled_mortgage:
                raise ValueError(
                    "Annual base spending cannot be less than the confirmed mortgage "
                    "payment when mortgage_included_in_base_spending is true"
                )
            person.spending.base -= scheduled_mortgage
        home._refresh_mortgage_stats()


def _create_jobs(
    person: Person,
    person_spec: Dict[str, Any],
    active: bool,
    activation_callbacks: List[Callable[[], None]],
) -> None:
    for job_spec in _as_list(person_spec.get("jobs", []), "jobs"):
        salary_spec = _as_dict(job_spec.get("salary", {}), "salary")
        salary_base = float(salary_spec.get("base", 0))
        salary = Salary(
            model=person.model,
            base=salary_base if active else 0,
            yearly_increase=(
                float(salary_spec["yearly_increase"])
                if salary_spec.get("yearly_increase") is not None
                else None
            ),
            yearly_bonus=float(salary_spec.get("yearly_bonus", 0)),
        )
        job = Job(
            owner=person,
            company=str(job_spec.get("company", "Employer")),
            role=str(job_spec.get("role", "Employee")),
            salary=salary,
        )
        if not active:
            activation_callbacks.append(
                lambda salary=salary, salary_base=salary_base: setattr(salary, "base", salary_base)
            )

        account_spec = job_spec.get("401k")
        if account_spec is not None:
            account_spec = _as_dict(account_spec, "401k")
            pretax_balance = float(account_spec.get("pretax_balance", 0))
            roth_balance = float(account_spec.get("roth_balance", 0))
            account = Job401kAccount(
                job=job,
                pretax_balance=pretax_balance if active else 0,
                pretax_contrib_percent=float(account_spec.get("pretax_contrib_percent", 0)),
                roth_balance=roth_balance if active else 0,
                roth_contrib_percent=float(account_spec.get("roth_contrib_percent", 0)),
                average_growth=float(account_spec.get("average_growth", 0)),
                company_match_percent=float(account_spec.get("company_match_percent", 0)),
                asset_allocation=_asset_allocation(account_spec.get("asset_allocation")),
                asset_return_rates=_asset_return_rates(account_spec.get("asset_return_rates")),
            )
            if not active:
                activation_callbacks.append(
                    lambda account=account, pretax_balance=pretax_balance, roth_balance=roth_balance: _activate_401k(
                        account,
                        pretax_balance,
                        roth_balance,
                    )
                )


def _create_pensions(person: Person, person_spec: Dict[str, Any], active: bool) -> None:
    for pension_spec in _as_list(person_spec.get("pensions", []), "pensions"):
        if not active:
            raise ValueError("pensions are not supported on event-activated people")
        benefit_amount = pension_spec.get("benefit_amount")
        final_average_salary = pension_spec.get("final_average_salary")
        Pension(
            person=person,
            company=str(pension_spec.get("company", "Employer")),
            vesting_years=int(pension_spec.get("vesting_years", 5)),
            benefit_amount=float(benefit_amount) if benefit_amount is not None else None,
            years_of_service=float(pension_spec.get("years_of_service", 0)),
            final_average_salary=(float(final_average_salary)
                                  if final_average_salary is not None else None),
            benefit_multiplier_percent=float(pension_spec.get("benefit_multiplier_percent", 1.5)),
            payout_start_age=float(pension_spec.get("payout_start_age", 65)),
            cola_percent=float(pension_spec.get("cola_percent", 0)),
        )


def _create_social_security(person: Person, person_spec: Dict[str, Any], active: bool) -> None:
    ss_spec = person_spec.get("social_security")
    if ss_spec is None:
        return
    if not active:
        raise ValueError("social_security is not supported on event-activated people")
    ss_spec = _as_dict(ss_spec, "social_security")
    history = [
        (int(entry[0]), float(entry[1]))
        for entry in _as_list(ss_spec.get("income_history", []), "income_history")
    ]
    withdrawal_start_age = ss_spec.get("withdrawal_start_age")
    SocialSecurity(
        person=person,
        withdrawal_start_age=(float(withdrawal_start_age)
                              if withdrawal_start_age is not None else None),
        income_history=history,
    )


def _create_trusts(person: Person, person_spec: Dict[str, Any], active: bool) -> None:
    for trust_spec in _as_list(person_spec.get("trusts", []), "trusts"):
        if not active:
            raise ValueError("trusts are not supported on event-activated people")
        type_name = str(_required(trust_spec, "type")).upper()
        if type_name not in TrustType.__members__:
            raise ValueError(f"Unknown trust type: {type_name}")
        Trust(
            grantor=person,
            name=str(trust_spec.get("name", "Trust")),
            trust_type=TrustType[type_name],
            balance=float(trust_spec.get("balance", 0)),
            growth_rate=trust_spec.get("growth_rate"),
            annual_distribution=float(trust_spec.get("annual_distribution", 0)),
            distribution_percent=trust_spec.get("distribution_percent"),
        )


def _create_tangible_assets(person: Person, person_spec: Dict[str, Any], active: bool) -> None:
    for asset_spec in _as_list(person_spec.get("tangible_assets", []), "tangible_assets"):
        if not active:
            raise ValueError("tangible_assets are not supported on event-activated people")
        type_name = str(asset_spec.get("asset_type", "OTHER")).upper()
        if type_name not in AssetType.__members__:
            raise ValueError(f"Unknown asset type: {type_name}")
        sell_year = asset_spec.get("sell_year")
        TangibleAsset(
            person=person,
            name=str(asset_spec.get("name", "Asset")),
            asset_type=AssetType[type_name],
            value=float(_required(asset_spec, "value")),
            value_yearly_change_percent=float(asset_spec.get("value_yearly_change_percent", 0)),
            maintenance_annual=float(asset_spec.get("maintenance_annual", 0)),
            maintenance_increase_percent=asset_spec.get("maintenance_increase_percent"),
            insurance_annual=float(asset_spec.get("insurance_annual", 0)),
            insurance_increase_percent=asset_spec.get("insurance_increase_percent"),
            rental_income_annual=float(asset_spec.get("rental_income_annual", 0)),
            loan_amount=float(asset_spec.get("loan_amount", 0)),
            loan_interest_rate=float(asset_spec.get("loan_interest_rate", 7.0)),
            loan_term_years=int(asset_spec.get("loan_term_years", 5)),
            sell_year=int(sell_year) if sell_year is not None else None,
            selling_cost_percent=float(asset_spec.get("selling_cost_percent", 5.0)),
        )


def _create_annuities(person: Person, person_spec: Dict[str, Any], active: bool) -> None:
    for annuity_spec in _as_list(person_spec.get("annuities", []), "annuities"):
        if not active:
            raise ValueError("annuities are not supported on event-activated people")
        type_name = str(annuity_spec.get("type", "DEFERRED")).upper()
        if type_name not in AnnuityType.__members__:
            raise ValueError(f"Unknown annuity type: {type_name}")
        payout_type_name = str(annuity_spec.get("payout_type", "LIFE_ONLY")).upper()
        if payout_type_name not in AnnuityPayoutType.__members__:
            raise ValueError(f"Unknown annuity payout type: {payout_type_name}")
        payout_start_age = annuity_spec.get("payout_start_age")
        monthly_payout = annuity_spec.get("monthly_payout")
        Annuity(
            person=person,
            annuity_type=AnnuityType[type_name],
            initial_balance=float(annuity_spec.get("initial_balance", 0)),
            interest_rate=float(annuity_spec.get("interest_rate", 3.0)),
            payout_type=AnnuityPayoutType[payout_type_name],
            payout_start_age=int(payout_start_age) if payout_start_age is not None else None,
            monthly_payout=float(monthly_payout) if monthly_payout is not None else None,
            period_certain_years=int(annuity_spec.get("period_certain_years", 10)),
            surrender_charge_years=int(annuity_spec.get("surrender_charge_years", 7)),
            surrender_charge_rate=float(annuity_spec.get("surrender_charge_rate", 7.0)),
            survivor_benefit_percent=float(annuity_spec.get("survivor_benefit_percent", 100.0)),
        )


def _create_hsa_accounts(person: Person, person_spec: Dict[str, Any], active: bool) -> None:
    for hsa_spec in _as_list(person_spec.get("hsa_accounts", []), "hsa_accounts"):
        if not active:
            raise ValueError("hsa_accounts are not supported on event-activated people")
        type_name = str(hsa_spec.get("type", "INDIVIDUAL")).upper()
        if type_name not in HSAType.__members__:
            raise ValueError(f"Unknown HSA type: {type_name}")
        HealthSavingsAccount(
            person=person,
            hsa_type=HSAType[type_name],
            balance=float(hsa_spec.get("balance", 0)),
            contribution_limit=float(hsa_spec.get("contribution_limit", 4400)),
            employer_contribution=float(hsa_spec.get("employer_contribution", 0)),
            growth_rate=float(hsa_spec.get("growth_rate", 0)),
        )


def _create_brokerage_accounts(person: Person, person_spec: Dict[str, Any], active: bool) -> None:
    for brokerage_spec in _as_list(person_spec.get("brokerage_accounts", []), "brokerage_accounts"):
        if not active:
            raise ValueError("brokerage_accounts are not supported on event-activated people")
        BrokerageAccount(
            person=person,
            company=str(brokerage_spec.get("company", "Brokerage")),
            balance=float(brokerage_spec.get("balance", 0)),
            growth_rate=brokerage_spec.get("growth_rate"),
            asset_allocation=_asset_allocation(brokerage_spec.get("asset_allocation")),
            asset_return_rates=_asset_return_rates(brokerage_spec.get("asset_return_rates")),
        )


def _create_donor_advised_funds(person: Person, person_spec: Dict[str, Any], active: bool) -> None:
    for daf_spec in _as_list(person_spec.get("donor_advised_funds", []), "donor_advised_funds"):
        if not active:
            raise ValueError("donor_advised_funds are not supported on event-activated people")
        DonorAdvisedFund(
            person=person,
            fund_name=str(daf_spec.get("fund_name", "Donor Advised Fund")),
            balance=float(daf_spec.get("balance", 0)),
            growth_rate=float(daf_spec.get("growth_rate", 7.0)),
            management_fee=float(daf_spec.get("management_fee", 0.6)),
            distribution_rate=float(daf_spec.get("distribution_rate", 5.0)),
        )


def _create_insurance_policies(person: Person, person_spec: Dict[str, Any], active: bool) -> None:
    for policy_spec in _as_list(person_spec.get("insurance_policies", []), "insurance_policies"):
        if not active:
            raise ValueError("insurance_policies are not supported on event-activated people")
        type_name = str(_required(policy_spec, "insurance_type")).upper()
        if type_name not in InsuranceType.__members__:
            raise ValueError(f"Unknown insurance type: {type_name}")
        coverage_start_age = policy_spec.get("coverage_start_age")
        coverage_end_age = policy_spec.get("coverage_end_age")
        Insurance(
            person=person,
            insurance_type=InsuranceType[type_name],
            company=str(policy_spec.get("company", "Insurer")),
            annual_premium=float(_required(policy_spec, "annual_premium")),
            coverage_amount=float(_required(policy_spec, "coverage_amount")),
            deductible=float(policy_spec.get("deductible", 0)),
            coverage_start_age=int(coverage_start_age) if coverage_start_age is not None else None,
            coverage_end_age=int(coverage_end_age) if coverage_end_age is not None else None,
            premium_increase_rate=float(policy_spec.get("premium_increase_rate", 3.0)),
            max_claims_per_year=int(policy_spec.get("max_claims_per_year", 3)),
        )


def _create_consumer_debt(person: Person, person_spec: Dict[str, Any], active: bool) -> None:
    for loan_spec in _as_list(person_spec.get("car_loans", []), "car_loans"):
        if not active:
            raise ValueError("car_loans are not supported on event-activated people")
        CarLoan(
            person=person,
            loan_amount=float(_required(loan_spec, "loan_amount")),
            length_years=int(_required(loan_spec, "length_years")),
            yearly_interest_rate=float(_required(loan_spec, "yearly_interest_rate")),
            name=str(loan_spec.get("name", "Car")),
            principal=loan_spec.get("principal"),
        )
    for card_spec in _as_list(person_spec.get("credit_cards", []), "credit_cards"):
        if not active:
            raise ValueError("credit_cards are not supported on event-activated people")
        CreditCard(
            person=person,
            card_name=str(card_spec.get("card_name", "Credit Card")),
            credit_limit=float(card_spec.get("credit_limit", 10000)),
            current_balance=float(card_spec.get("current_balance", 0)),
            yearly_interest_rate=float(card_spec.get("yearly_interest_rate", 18.0)),
            minimum_payment_percent=float(card_spec.get("minimum_payment_percent", 2.0)),
        )


def _create_donations(person: Person, person_spec: Dict[str, Any], active: bool) -> None:
    for donation_spec in _as_list(person_spec.get("donations", []), "donations"):
        if not active:
            raise ValueError("donations are not supported on event-activated people")
        type_name = str(donation_spec.get("donation_type", "CASH")).upper()
        if type_name not in DonationType.__members__:
            raise ValueError(f"Unknown donation type: {type_name}")
        start_year = donation_spec.get("start_year")
        end_year = donation_spec.get("end_year")
        Donation(
            person=person,
            charity_name=str(donation_spec.get("charity_name", "Charity")),
            annual_amount=float(_required(donation_spec, "annual_amount")),
            donation_type=DonationType[type_name],
            tax_deductible=bool(donation_spec.get("tax_deductible", True)),
            frequency_years=int(donation_spec.get("frequency_years", 1)),
            start_year=int(start_year) if start_year is not None else None,
            end_year=int(end_year) if end_year is not None else None,
        )


def _create_standalone_529s(person: Person, person_spec: Dict[str, Any], active: bool) -> None:
    for plan_spec in _as_list(person_spec.get("plan_529s", []), "plan_529s"):
        if not active:
            raise ValueError("plan_529s are not supported on event-activated people")
        Plan529(
            owner=person,
            beneficiary=None,
            balance=float(plan_spec.get("balance", 0)),
            state=str(plan_spec.get("state", person.state or "NY")),
            growth_rate=plan_spec.get("growth_rate"),
            annual_contribution_limit=plan_spec.get("annual_contribution_limit"),
            lifetime_contribution_limit=plan_spec.get("lifetime_contribution_limit"),
            asset_allocation=_asset_allocation(plan_spec.get("asset_allocation")),
            asset_return_rates=_asset_return_rates(plan_spec.get("asset_return_rates")),
        )


def _create_ira_accounts(person: Person, person_spec: Dict[str, Any], active: bool) -> None:
    for ira_spec in _as_list(person_spec.get("traditional_iras", []), "traditional_iras"):
        if not active:
            raise ValueError("traditional_iras are not supported on event-activated people")
        TraditionalIRA(
            person=person,
            balance=float(ira_spec.get("balance", 0)),
            growth_rate=ira_spec.get("growth_rate"),
            contribution_limit=float(ira_spec.get("contribution_limit", 7500)),
            asset_allocation=_asset_allocation(ira_spec.get("asset_allocation")),
            asset_return_rates=_asset_return_rates(ira_spec.get("asset_return_rates")),
        )
    for ira_spec in _as_list(person_spec.get("roth_iras", []), "roth_iras"):
        if not active:
            raise ValueError("roth_iras are not supported on event-activated people")
        RothIRA(
            person=person,
            balance=float(ira_spec.get("balance", 0)),
            growth_rate=ira_spec.get("growth_rate"),
            contribution_limit=float(ira_spec.get("contribution_limit", 7500)),
            asset_allocation=_asset_allocation(ira_spec.get("asset_allocation")),
            asset_return_rates=_asset_return_rates(ira_spec.get("asset_return_rates")),
        )


def _create_life_insurance(person: Person, person_spec: Dict[str, Any], active: bool) -> None:
    for policy_spec in _as_list(person_spec.get("life_insurance", []), "life_insurance"):
        if not active:
            raise ValueError("life_insurance is not supported on event-activated people")
        type_name = str(_required(policy_spec, "type")).upper()
        if type_name not in LifeInsuranceType.__members__:
            raise ValueError(f"Unknown life insurance type: {type_name}")
        term_years = policy_spec.get("term_years")
        LifeInsurance(
            person=person,
            policy_type=LifeInsuranceType[type_name],
            death_benefit=float(_required(policy_spec, "death_benefit")),
            monthly_premium=float(_required(policy_spec, "monthly_premium")),
            term_years=int(term_years) if term_years is not None else None,
            premium_increase_rate=policy_spec.get("premium_increase_rate"),
            cash_value_growth_rate=policy_spec.get("cash_value_growth_rate"),
            loan_interest_rate=policy_spec.get("loan_interest_rate"),
            max_missed_payments=int(policy_spec.get("max_missed_payments", 3)),
        )


def _create_healthcare(person: Person, person_spec: Dict[str, Any], active: bool) -> None:
    healthcare_spec = person_spec.get("healthcare")
    if healthcare_spec is None:
        return
    if not active:
        raise ValueError("healthcare is not supported on event-activated people")
    healthcare_spec = _as_dict(healthcare_spec, "healthcare")
    multipliers = healthcare_spec.get("age_cost_multipliers")
    if multipliers is not None:
        multipliers = {int(age): float(mult)
                       for age, mult in _as_dict(multipliers, "age_cost_multipliers").items()}
    ltc_start_age = healthcare_spec.get("ltc_start_age")
    Healthcare(
        person=person,
        pre_medicare_annual_premium=float(healthcare_spec.get("pre_medicare_annual_premium", 0)),
        annual_out_of_pocket=healthcare_spec.get("annual_out_of_pocket"),
        medicare_start_age=healthcare_spec.get("medicare_start_age"),
        medicare_part_b_monthly=healthcare_spec.get("medicare_part_b_monthly"),
        medicare_part_d_monthly=healthcare_spec.get("medicare_part_d_monthly"),
        medigap_monthly=healthcare_spec.get("medigap_monthly"),
        healthcare_inflation_percent=healthcare_spec.get("healthcare_inflation_percent"),
        age_cost_multipliers=multipliers,
        ltc_start_age=int(ltc_start_age) if ltc_start_age is not None else None,
        ltc_years=int(healthcare_spec.get("ltc_years", 0)),
        ltc_annual_cost=healthcare_spec.get("ltc_annual_cost"),
    )


def _create_investment_returns(
    person: Person,
    person_spec: Dict[str, Any],
    active: bool,
    activation_callbacks: List[Callable[[], None]],
) -> None:
    for investment_spec in _as_list(person_spec.get("investment_returns", []), "investment_returns"):
        balance = float(investment_spec.get("balance", 0))
        investment_return = InvestmentReturn(
            owner=person,
            balance=balance if active else 0,
            growth_rate=float(investment_spec.get("growth_rate", 0)),
            asset_allocation=_asset_allocation(investment_spec.get("asset_allocation")),
            asset_return_rates=_asset_return_rates(investment_spec.get("asset_return_rates")),
            payout_to_bank=bool(investment_spec.get("payout_to_bank", True)),
            cash_payout_rate=investment_spec.get("cash_payout_rate"),
            taxable=bool(investment_spec.get("taxable", True)),
        )
        if not active:
            activation_callbacks.append(
                lambda investment_return=investment_return, balance=balance: setattr(
                    investment_return, "balance", balance
                )
            )


def _create_children(person: Person, person_spec: Dict[str, Any]) -> None:
    for child_spec in _as_list(person_spec.get("children", []), "children"):
        plan_spec = child_spec.get("plan529")
        child_kwargs = {
            key: value
            for key, value in child_spec.items()
            if key not in {"name", "birth_year", "plan529"}
        }
        child = Child(
            person=person,
            name=str(_required(child_spec, "name")),
            birth_year=_required_int(child_spec, "birth_year"),
            **child_kwargs,
        )
        if plan_spec is not None:
            plan_spec = _as_dict(plan_spec, "plan529")
            Plan529(
                owner=person,
                beneficiary=child,
                balance=float(plan_spec.get("balance", 0)),
                state=str(plan_spec.get("state", person.state or "NY")),
                growth_rate=plan_spec.get("growth_rate"),
                annual_contribution_limit=plan_spec.get("annual_contribution_limit"),
                lifetime_contribution_limit=plan_spec.get("lifetime_contribution_limit"),
                asset_allocation=_asset_allocation(plan_spec.get("asset_allocation")),
                asset_return_rates=_asset_return_rates(plan_spec.get("asset_return_rates")),
            )


def _create_life_event(
    event_spec: Dict[str, Any],
    people: Dict[str, Person],
    activation_callbacks: Dict[str, List[Callable[[], None]]],
) -> LifeEvent:
    event_type = str(_required(event_spec, "type"))
    year = _required_int(event_spec, "year")

    if event_type == "marriage":
        person = _person_ref(people, event_spec, "person")
        spouse = _event_spouse(people, event_spec)

        def activate_marriage(person=person, spouse=spouse):
            for callback in activation_callbacks.get(_lookup_person_id(people, spouse), []):
                callback()
            person.get_married(spouse)

        return LifeEvent(year, "Marriage", activate_marriage)

    if event_type == "divorce":
        person = _person_ref(people, event_spec, "person")
        spouse = _person_ref(people, event_spec, "spouse") if "spouse" in event_spec else None
        return LifeEvent.divorce(year, person, spouse)

    if event_type == "one_time_expense":
        person = _person_ref(people, event_spec, "person")
        amount = float(_required(event_spec, "amount"))
        return LifeEvent(year, str(event_spec.get("name", "One-time expense")), person.spending.add_expense, amount)

    if event_type == "one_time_income":
        person = _person_ref(people, event_spec, "person")
        amount = float(_required(event_spec, "amount"))
        taxable = bool(event_spec.get("taxable", True))
        return LifeEvent(
            year,
            str(event_spec.get("name", "One-time income")),
            person.add_one_time_income,
            amount,
            taxable,
        )

    if event_type == "child_birth_or_adoption":
        person = _person_ref(people, event_spec, "person")
        child_kwargs = {
            key: value
            for key, value in event_spec.items()
            if key not in {"type", "year", "person", "name"}
        }
        return LifeEvent.child_birth_or_adoption(year, person, str(_required(event_spec, "name")), **child_kwargs)

    if event_type == "home_purchase":
        person = _person_ref(people, event_spec, "person")
        home_kwargs = {
            key: value
            for key, value in event_spec.items()
            if key not in {"type", "year", "person", "name", "purchase_price", "down_payment"}
        }
        return LifeEvent.home_purchase(
            year,
            person,
            str(_required(event_spec, "name")),
            float(_required(event_spec, "purchase_price")),
            float(_required(event_spec, "down_payment")),
            **home_kwargs,
        )

    if event_type == "go_to_college":
        person = _person_ref(people, event_spec, "person")
        college_kwargs = {
            key: value
            for key, value in event_spec.items()
            if key not in {"type", "year", "person", "annual_cost"}
        }
        return LifeEvent.go_to_college(
            year,
            person,
            float(_required(event_spec, "annual_cost")),
            **college_kwargs,
        )

    if event_type == "death":
        person = _person_ref(people, event_spec, "person")
        return LifeEvent(year, "Death", person.die)

    if event_type == "home_sale":
        person = _person_ref(people, event_spec, "person")
        selling_cost_percent = float(event_spec.get("selling_cost_percent", 6.0))
        home_name = event_spec.get("name")

        def sell_home(person=person, selling_cost_percent=selling_cost_percent, home_name=home_name):
            homes = person.model.registries.homes.get_items(person)
            if home_name is not None:
                homes = [home for home in homes if home.name == home_name]
            if not homes:
                raise ValueError(f"Home sale event found no home for {person.name}")
            return homes[0].sell(selling_cost_percent)

        return LifeEvent(year, "Home sale", sell_home)

    if event_type == "annuity_surrender":
        person = _person_ref(people, event_spec, "person")

        def surrender_annuity(person=person):
            annuities = person.model.registries.annuities.get_items(person)
            active = [annuity for annuity in annuities if annuity.is_active]
            if not active:
                raise ValueError(f"Annuity surrender event found no active annuity for {person.name}")
            return active[0].surrender()

        return LifeEvent(year, "Annuity surrender", surrender_annuity)

    if event_type == "plan529_non_qualified_withdrawal":
        person = _person_ref(people, event_spec, "person")
        amount = float(_required(event_spec, "amount"))

        def raid_529(person=person, amount=amount):
            plans = [plan for plan in person.model.registries.plan_529s.get_items(person)
                     if plan.balance > 0]
            if not plans:
                raise ValueError(f"529 withdrawal event found no funded plan for {person.name}")
            plan = plans[0]
            # The earnings ratio must be captured before the withdrawal
            # mutates the balance; the engine taxes earnings pro-rata and
            # applies the 10% penalty to the earnings portion only.
            earnings_ratio = plan.total_earnings / plan.balance if plan.balance > 0 else 0.0
            withdrawn, penalty = plan.withdraw_non_qualified(amount)
            if withdrawn > 0:
                person.taxable_income += withdrawn * earnings_ratio
                person.early_withdrawal_penalty += penalty
                person.deposit_into_cashflow_bank_account(withdrawn)
            return withdrawn

        return LifeEvent(year, "529 non-qualified withdrawal", raid_529)

    if event_type == "life_insurance_loan":
        person = _person_ref(people, event_spec, "person")
        amount = float(_required(event_spec, "amount"))

        def take_policy_loan(person=person, amount=amount):
            policies = person.model.registries.life_insurance_policies.get_items(person)
            eligible = [policy for policy in policies if policy.is_active]
            if not eligible:
                raise ValueError(f"Life insurance loan event found no active policy for {person.name}")
            return eligible[0].take_loan(amount)

        return LifeEvent(year, "Life insurance loan", take_policy_loan)

    if event_type == "daf_contribution":
        person = _person_ref(people, event_spec, "person")
        amount = float(_required(event_spec, "amount"))

        def contribute_to_daf(person=person, amount=amount):
            funds = person.model.registries.donor_advised_funds.get_items(person)
            if not funds:
                raise ValueError(f"DAF contribution event found no fund for {person.name}")
            return funds[0].contribute(amount)

        return LifeEvent(year, "DAF contribution", contribute_to_daf)

    if event_type == "insurance_claim":
        person = _person_ref(people, event_spec, "person")
        amount = float(_required(event_spec, "amount"))
        description = str(event_spec.get("description", "Insurance claim"))

        def file_insurance_claim(person=person, amount=amount, description=description):
            policies = [policy for policy
                        in person.model.registries.general_insurance_policies.get_items(person)
                        if policy.is_coverage_active]
            if not policies:
                raise ValueError(f"Insurance claim event found no active policy for {person.name}")
            return policies[0].file_claim(amount, description)

        return LifeEvent(year, "Insurance claim", file_insurance_claim)

    if event_type == "trust_dissolve":
        person = _person_ref(people, event_spec, "person")
        trust_name = event_spec.get("name")

        def dissolve_trust(person=person, trust_name=trust_name):
            trusts = person.model.registries.trusts.get_items(person)
            if trust_name is not None:
                trusts = [trust for trust in trusts if trust.name == trust_name]
            if not trusts:
                raise ValueError(f"Trust dissolve event found no trust for {person.name}")
            return trusts[0].dissolve()

        return LifeEvent(year, "Trust dissolution", dissolve_trust)

    if event_type == "set_income_multiplier":
        person = _person_ref(people, event_spec, "person")
        multiplier = float(_required(event_spec, "multiplier"))
        if multiplier < 0 or multiplier > 1:
            raise ValueError("Income multiplier must be between 0 and 1")

        def set_income_multiplier(person=person, multiplier=multiplier):
            for job in person.model.registries.jobs.get_items(person):
                job.income_multiplier = multiplier

        return LifeEvent(
            year,
            str(event_spec.get("name", "Income multiplier change")),
            set_income_multiplier,
            run_in_decision_step=True,
        )

    if event_type == "set_spending_growth":
        person = _person_ref(people, event_spec, "person")
        annual_rate_percent = float(
            _required(event_spec, "annual_rate_percent")
        )
        if annual_rate_percent <= -100 or annual_rate_percent > 100:
            raise ValueError(
                "Spending growth must be greater than -100% and at most 100%"
            )
        return LifeEvent(
            year,
            str(event_spec.get("name", "Spending growth change")),
            setattr,
            person.spending,
            "yearly_increase",
            annual_rate_percent,
            run_in_decision_step=True,
        )

    if event_type == "recurring_investment_contribution":
        person = _person_ref(people, event_spec, "person")
        annual_amount = float(_required(event_spec, "annual_amount"))
        end_year = int(_required(event_spec, "end_year"))
        if end_year < year:
            raise ValueError(
                "Recurring investment contribution end year must not precede start year"
            )

        def activate_contribution(
            person=person,
            annual_amount=annual_amount,
            end_year=end_year,
        ):
            from .lifeevents import RecurringInvestmentContribution

            contribution = RecurringInvestmentContribution(
                person,
                annual_amount,
                end_year,
            )
            contribution.contribute_for_current_year()
            return contribution

        return LifeEvent(
            year,
            str(event_spec.get("name", "Recurring investment contribution")),
            activate_contribution,
        )

    raise ValueError(f"Unsupported event type: {event_type}")


def _financial_decision_to_event_spec(decision: Any) -> Dict[str, Any]:
    if not isinstance(decision, dict):
        raise ValueError("Each financial decision must be an object")

    raw_event_type = decision.get("event_name", decision.get("event_type", decision.get("type")))
    matched_event_type = _matched_financial_decision_event_type(raw_event_type)
    event_type = matched_event_type or "one_time_expense"
    payload = {}
    if decision.get("payload") is not None:
        payload.update(_as_dict(decision["payload"], "payload"))

    for key, value in decision.items():
        if key not in FINANCIAL_DECISION_CONTROL_KEYS:
            payload.setdefault(key, value)

    year = decision.get("year", payload.pop("year", None))
    if year is None:
        raise ValueError("Financial decision requires field 'year'")

    if matched_event_type is None:
        payload.setdefault("name", str(raw_event_type).strip())

    event_spec = {"type": event_type, "year": int(year)}
    event_spec.update(payload)
    return event_spec


def _canonical_financial_decision_event_type(value: Any) -> str:
    matched_event_type = _matched_financial_decision_event_type(value)
    return matched_event_type or "one_time_expense"


def _matched_financial_decision_event_type(value: Any) -> Optional[str]:
    if value is None:
        raise ValueError("Financial decision requires 'event_name', 'event_type', or 'type'")

    event_name = str(value).strip()
    if event_name in FINANCIAL_DECISION_EVENT_TYPE_ALIASES:
        return FINANCIAL_DECISION_EVENT_TYPE_ALIASES[event_name]

    normalized = " ".join(event_name.lower().replace("_", " ").replace("-", " ").split())
    if normalized in FINANCIAL_DECISION_EVENT_TYPE_ALIASES:
        return FINANCIAL_DECISION_EVENT_TYPE_ALIASES[normalized]

    slashless = " ".join(normalized.replace("/", " ").split())
    if slashless in FINANCIAL_DECISION_EVENT_TYPE_ALIASES:
        return FINANCIAL_DECISION_EVENT_TYPE_ALIASES[slashless]

    return None


def _profile_value(spec: Dict[str, Any], key: str, default: Any) -> Any:
    value = spec.get(key, default)
    return default if value is None else value


def _profile_required(profile: Dict[str, Any], section: Dict[str, Any], key: str) -> Any:
    if key in section and section[key] is not None:
        return section[key]
    if key in profile and profile[key] is not None:
        return profile[key]
    raise ValueError(f"User profile requires field '{key}'")


def _profile_required_number(
    profile: Dict[str, Any],
    section: Dict[str, Any],
    keys: Iterable[str],
) -> float:
    for key in keys:
        if key in section and section[key] is not None:
            return _coerce_profile_number(section[key], key)
        if key in profile and profile[key] is not None:
            return _coerce_profile_number(profile[key], key)
    raise ValueError(f"User profile requires one of: {', '.join(keys)}")


def _profile_optional_number(
    spec: Dict[str, Any],
    keys: Iterable[str],
    default: float,
) -> float:
    for key in keys:
        if key in spec and spec[key] is not None:
            return _coerce_profile_number(spec[key], key)
    return float(default)


def _coerce_profile_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"Field '{field_name}' must be a number")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace("$", "").replace(",", "").replace("%", "")
        if cleaned:
            return float(cleaned)
    raise ValueError(f"Field '{field_name}' must be a number")


def _profile_bank_accounts(cash_spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    checking_balance = _profile_optional_number(
        cash_spec,
        ("checking_balance", "bank_balance", "cash_balance"),
        0,
    )
    checking_interest_rate = _profile_optional_number(
        cash_spec,
        ("checking_interest_rate", "bank_interest_rate", "interest_rate"),
        0,
    )
    accounts = [
        {
            "company": str(cash_spec.get("bank_name", cash_spec.get("company", "Bank"))),
            "type": "Checking",
            "balance": checking_balance,
            "interest_rate": checking_interest_rate,
        }
    ]

    if "savings_balance" in cash_spec or "savings_interest_rate" in cash_spec:
        accounts.append(
            {
                "company": str(cash_spec.get("bank_name", cash_spec.get("company", "Bank"))),
                "type": "Savings",
                "balance": _profile_optional_number(cash_spec, ("savings_balance",), 0),
                "interest_rate": _profile_optional_number(cash_spec, ("savings_interest_rate",), 0),
            }
        )
    return accounts


def _profile_apartments(housing_spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    status = str(housing_spec.get("status", "rent")).strip().lower()
    monthly_rent = _profile_optional_number(housing_spec, ("monthly_rent", "rent"), 0)
    if status in {"rent", "renter", "apartment"} or monthly_rent > 0:
        return [
            {
                "name": str(housing_spec.get("name", "Apartment")),
                "monthly_rent": monthly_rent,
                "yearly_increase": _profile_optional_number(housing_spec, ("yearly_increase",), 2.5),
            }
        ]
    return []


def _profile_existing_homes(housing_spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Map a compact opening-position home into the strict scenario contract."""

    status = str(housing_spec.get("status", "rent")).strip().lower()
    if status not in {"own", "owner", "owned"}:
        return []
    mortgage_spec = _as_dict(
        _required(housing_spec, "mortgage"),
        "housing.mortgage",
    )
    included = _required(housing_spec, "mortgage_included_in_annual_spending")
    if not isinstance(included, bool):
        raise ValueError(
            "housing.mortgage_included_in_annual_spending must be a boolean"
        )

    remaining_term = _profile_required_number(
        mortgage_spec,
        mortgage_spec,
        ("remaining_term_years",),
    )
    if remaining_term <= 0 or not float(remaining_term).is_integer():
        raise ValueError(
            "housing.mortgage.remaining_term_years must be a positive whole number"
        )
    mortgage = {
        "principal_balance": _profile_required_number(
            mortgage_spec,
            mortgage_spec,
            ("principal_balance", "balance"),
        ),
        "yearly_interest_rate": _profile_required_number(
            mortgage_spec,
            mortgage_spec,
            ("yearly_interest_rate", "interest_rate"),
        ),
        "remaining_term_years": int(remaining_term),
        "mortgage_type": str(_required(mortgage_spec, "mortgage_type")),
    }
    if mortgage_spec.get("monthly_payment") is not None:
        mortgage["monthly_payment"] = _profile_required_number(
            mortgage_spec,
            mortgage_spec,
            ("monthly_payment",),
        )
    home = {
        "name": str(housing_spec.get("name", "Primary Home")),
        "current_value": _profile_required_number(
            housing_spec,
            housing_spec,
            ("current_value", "home_value"),
        ),
        "value_yearly_increase": _profile_required_number(
            housing_spec,
            housing_spec,
            ("value_yearly_increase", "appreciation_rate"),
        ),
        "mortgage": mortgage,
        "expenses": _as_dict(housing_spec.get("expenses", {}), "housing.expenses"),
        "mortgage_included_in_base_spending": included,
    }
    if housing_spec.get("tax_basis") is not None:
        home["tax_basis"] = _profile_required_number(
            housing_spec,
            housing_spec,
            ("tax_basis",),
        )
    return [home]


def _profile_401k_account(account_spec: Dict[str, Any]) -> Dict[str, Any]:
    account = {
        "pretax_balance": _profile_optional_number(account_spec, ("pretax_balance", "balance"), 0),
        "pretax_contrib_percent": _profile_optional_number(account_spec, ("pretax_contrib_percent",), 0),
        "roth_balance": _profile_optional_number(account_spec, ("roth_balance",), 0),
        "roth_contrib_percent": _profile_optional_number(account_spec, ("roth_contrib_percent",), 0),
        "average_growth": _profile_optional_number(account_spec, ("average_growth", "growth_rate"), 0),
        "company_match_percent": _profile_optional_number(account_spec, ("company_match_percent",), 0),
    }
    if "asset_allocation" in account_spec:
        account["asset_allocation"] = account_spec["asset_allocation"]
    if "asset_return_rates" in account_spec:
        account["asset_return_rates"] = account_spec["asset_return_rates"]
    return account


def _profile_investment_returns(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        investment_specs = value
    else:
        investment_specs = [_as_dict(value, "investment")]

    investments = []
    for investment_spec in investment_specs:
        investment_spec = _as_dict(investment_spec, "investment")
        balance = _profile_optional_number(investment_spec, ("balance",), 0)
        if balance <= 0:
            continue
        investment = {
            "balance": balance,
            "growth_rate": _profile_optional_number(investment_spec, ("growth_rate",), 0),
            "payout_to_bank": bool(investment_spec.get("payout_to_bank", True)),
            "cash_payout_rate": _profile_optional_number(investment_spec, ("cash_payout_rate",), 100),
            "taxable": bool(investment_spec.get("taxable", True)),
        }
        if "asset_allocation" in investment_spec:
            investment["asset_allocation"] = investment_spec["asset_allocation"]
        if "asset_return_rates" in investment_spec:
            investment["asset_return_rates"] = investment_spec["asset_return_rates"]
        investments.append(investment)
    return investments


def _asset_allocation(value: Any):
    if value is None:
        return None
    return AssetAllocation.from_percentages(value)


def _asset_return_rates(value: Any):
    if value is None:
        return None
    return AssetReturnRates.from_rates(value)


def _activate_401k(account: Job401kAccount, pretax_balance: float, roth_balance: float) -> None:
    account.pretax_balance = pretax_balance
    account.roth_balance = roth_balance
    # Activation balances are opening balances: treat the Roth as basis
    account.roth_basis = roth_balance


def _projection_checks(projection: pd.DataFrame) -> Dict[str, bool]:
    checks = {}
    tax_columns = ["Federal Taxes", "State Taxes", "SS Taxes", "Medicare Taxes", "Taxes"]
    if all(column in projection for column in tax_columns):
        component_taxes = (
            projection["Federal Taxes"]
            + projection["State Taxes"]
            + projection["SS Taxes"]
            + projection["Medicare Taxes"]
        )
        if "Early Withdrawal Penalties" in projection:
            component_taxes = component_taxes + projection["Early Withdrawal Penalties"]
        checks["tax_components_match"] = bool(((component_taxes - projection["Taxes"]).abs() < 1e-6).all())
    if "Bank Balance" in projection:
        checks["bank_never_negative"] = bool((projection["Bank Balance"] >= -1e-6).all())
    if "Cashflow Shortfall Debt" in projection:
        checks["cashflow_shortfall_debt_never_negative"] = bool(
            (projection["Cashflow Shortfall Debt"] >= -1e-6).all()
        )
    if "Total Assets" in projection:
        checks["total_assets_never_negative"] = bool((projection["Total Assets"] >= -1e-6).all())
    if "Total Liabilities" in projection:
        checks["total_liabilities_never_negative"] = bool((projection["Total Liabilities"] >= -1e-6).all())
    if {"Total Assets", "Total Liabilities", "Net Worth"}.issubset(projection.columns):
        checks["net_worth_identity_holds"] = bool(
            (
                projection["Net Worth"]
                - (projection["Total Assets"] - projection["Total Liabilities"])
            ).abs().le(1e-6).all()
        )
    retirement_tax_columns = {
        "Retirement Liquidation Income Tax",
        "Retirement Early Withdrawal Tax",
        "Retirement Liquidation Tax Cost",
    }
    if retirement_tax_columns.issubset(projection.columns):
        checks["retirement_liquidation_tax_cost_identity_holds"] = bool(
            (
                projection["Retirement Liquidation Tax Cost"]
                - (
                    projection["Retirement Liquidation Income Tax"]
                    + projection["Retirement Early Withdrawal Tax"]
                )
            ).abs().le(1e-6).all()
        )
    retirement_value_columns = {
        "Taxable Retirement Balance",
        "Roth Retirement Balance",
        "Retirement Liquidation Tax Cost",
        "After-Tax Retirement Value",
    }
    if retirement_value_columns.issubset(projection.columns):
        checks["after_tax_retirement_value_identity_holds"] = bool(
            (
                projection["After-Tax Retirement Value"]
                - (
                    projection["Taxable Retirement Balance"]
                    + projection["Roth Retirement Balance"]
                    - projection["Retirement Liquidation Tax Cost"]
                ).clip(lower=0)
            ).abs().le(1e-6).all()
        )
    if {"Net Worth", "Retirement Liquidation Tax Cost", "Tax-Adjusted Net Worth"}.issubset(projection.columns):
        checks["tax_adjusted_net_worth_identity_holds"] = bool(
            (
                projection["Tax-Adjusted Net Worth"]
                - (projection["Net Worth"] - projection["Retirement Liquidation Tax Cost"])
            ).abs().le(1e-6).all()
        )
    spending_subcategory_columns = {
        "Base Living Spending",
        "One-time Expenses",
        "Education Costs",
        "Home Purchase Costs",
        "Asset Sale Shortfalls",
        "Child Costs",
        "Healthcare Costs",
        "Real Asset Costs",
    }
    if {"Spending", *spending_subcategory_columns}.issubset(projection.columns):
        subcategory_total = sum(projection[column] for column in spending_subcategory_columns)
        checks["spending_subcategories_match"] = bool(
            (projection["Spending"] - subcategory_total).abs().le(1e-6).all()
        )
    if {"Total Cash Inflows", *CASHFLOW_INFLOW_COMPONENT_COLUMNS}.issubset(projection.columns):
        inflow_total = sum(projection[column] for column in CASHFLOW_INFLOW_COMPONENT_COLUMNS)
        checks["cash_inflow_components_match"] = bool(
            (projection["Total Cash Inflows"] - inflow_total).abs().le(1e-6).all()
        )
    if {
        "Total Cash Outflows Before Taxes",
        *CASHFLOW_OUTFLOW_BEFORE_TAX_COLUMNS,
    }.issubset(projection.columns):
        outflow_before_taxes_total = sum(
            projection[column] for column in CASHFLOW_OUTFLOW_BEFORE_TAX_COLUMNS
        )
        checks["cash_outflow_before_tax_components_match"] = bool(
            (
                projection["Total Cash Outflows Before Taxes"]
                - outflow_before_taxes_total
            ).abs().le(1e-6).all()
        )
    if {"Total Cash Outflows", "Total Cash Outflows Before Taxes", "Taxes"}.issubset(projection.columns):
        checks["cash_outflow_components_match"] = bool(
            (
                projection["Total Cash Outflows"]
                - (projection["Total Cash Outflows Before Taxes"] + projection["Taxes"])
            ).abs().le(1e-6).all()
        )
    if {
        "Net Cashflow Before Taxes",
        "Total Cash Inflows",
        "Total Cash Outflows Before Taxes",
    }.issubset(projection.columns):
        checks["net_cashflow_before_taxes_identity_holds"] = bool(
            (
                projection["Net Cashflow Before Taxes"]
                - (projection["Total Cash Inflows"] - projection["Total Cash Outflows Before Taxes"])
            ).abs().le(1e-6).all()
        )
    if {"Net Cashflow", "Total Cash Inflows", "Total Cash Outflows"}.issubset(projection.columns):
        checks["net_cashflow_identity_holds"] = bool(
            (
                projection["Net Cashflow"]
                - (projection["Total Cash Inflows"] - projection["Total Cash Outflows"])
            ).abs().le(1e-6).all()
        )
    return checks


def _projection_summary(
    model: LifeModel,
    selected: pd.DataFrame,
    projection_for_checks: pd.DataFrame,
) -> Dict[str, Any]:
    final = selected.iloc[-1].to_dict() if len(selected) else {}
    summary_columns = [column for column in SUMMARY_COLUMNS if column in selected.columns]
    return {
        "rows": int(len(selected)),
        "start_year": int(selected["Year"].min()) if "Year" in selected and len(selected) else model.start_year,
        "end_year": int(selected["Year"].max()) if "Year" in selected and len(selected) else model.end_year,
        "final": {column: _json_scalar(final[column]) for column in summary_columns if column in final},
        "max": {
            column: _json_scalar(selected[column].max())
            for column in summary_columns
            if column in selected
        },
        "checks": _projection_checks(projection_for_checks),
        "planning_indicators": _planning_indicators(model, projection_for_checks),
    }


def _first_year_where(model: LifeModel, projection: pd.DataFrame, column: str, predicate: Callable[[float], bool]):
    if column not in projection or "Year" not in projection:
        return None
    active_rows = projection[projection["Year"] > model.start_year]
    for _index, row in active_rows.iterrows():
        value = row[column]
        if pd.notna(value) and predicate(float(value)):
            return int(row["Year"])
    return None


def _planning_indicators(model: LifeModel, projection: pd.DataFrame) -> Dict[str, Any]:
    first_negative_cashflow = _first_year_where(
        model,
        projection,
        "Net Cashflow",
        lambda value: value < 0,
    )
    first_unfunded_year = _first_year_where(
        model,
        projection,
        "Cashflow Shortfall Debt",
        lambda value: value > 1e-6,
    )
    asset_exhaustion_year = _first_year_where(
        model,
        projection,
        "Total Assets",
        lambda value: value <= 1e-6,
    )
    active_years = [
        int(year)
        for year in projection["Year"].tolist()
        if "Year" in projection and int(year) > model.start_year
    ]
    liquidity_runway_years = None
    if first_unfunded_year is None:
        liquidity_runway_years = len(active_years)
    elif active_years:
        liquidity_runway_years = max(0, first_unfunded_year - active_years[0])
    return {
        "first_negative_cashflow_year": first_negative_cashflow,
        "first_unfunded_year": first_unfunded_year,
        "liquidity_runway_years": liquidity_runway_years,
        "asset_exhaustion_year": asset_exhaustion_year,
        "major_transition_years": [
            {"year": int(event.year), "message": event.message}
            for event in model.event_log.list
        ],
    }


def _projection_system_metadata(
    model: LifeModel,
    system_name: str,
    inflation_rate: float,
) -> Dict[str, Any]:
    if system_name == "nominal":
        return {
            "name": "nominal",
            "dollar_basis": "projected_year_nominal_dollars",
            "description": "Amounts are shown in each projected year's nominal dollars.",
        }
    if system_name == "real_start_year_dollars":
        return {
            "name": "real_start_year_dollars",
            "dollar_basis": "start_year_real_dollars",
            "base_year": model.start_year,
            "inflation_rate": inflation_rate,
            "formula": "real_value = nominal_value / ((1 + inflation_rate / 100) ** (year - start_year))",
            "description": f"Money columns are deflated to {model.start_year} dollars.",
        }
    raise ValueError(f"Unsupported projection system: {system_name}")


def _json_projection_system_names(projection_system: Optional[str]) -> List[str]:
    if projection_system is None or projection_system == "both":
        return ["nominal", "real_start_year_dollars"]
    if projection_system == "nominal":
        return ["nominal"]
    if projection_system == "real":
        return ["real_start_year_dollars"]
    raise ValueError(f"Unsupported projection system: {projection_system}")


def _projection_inflation_rate() -> float:
    return float(config.financial.get_inflation_rate())


def _money_projection_columns(projection: pd.DataFrame) -> List[str]:
    money_titles = {
        stat.title
        for stat in [*LifeModel.STATS, *LifeModel.EXTRA_STATS]
        if isinstance(stat, MoneyStat)
    }
    return [
        column
        for column in projection.columns
        if (
            column in money_titles
            or column in CASHFLOW_CALCULATED_COLUMNS
            or column.startswith(SOURCE_DETAIL_MONEY_COLUMN_PREFIXES)
        )
    ]


def _records(dataframe: pd.DataFrame) -> List[Dict[str, Any]]:
    return [
        {key: _json_scalar(value) for key, value in record.items()}
        for record in dataframe.to_dict(orient="records")
    ]


def _json_scalar(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def _write_output(output: str, path: Optional[str]) -> None:
    if path:
        Path(path).write_text(output, encoding="utf-8")
    else:
        print(output)


def _person_id(person_spec: Dict[str, Any]) -> str:
    return str(person_spec.get("id") or _required(person_spec, "name"))


def _lookup_person_id(people: Dict[str, Person], person: Person) -> str:
    for person_id, candidate in people.items():
        if candidate is person:
            return person_id
    raise ValueError(f"Person is not registered: {person.name}")


def _person_ref(people: Dict[str, Person], spec: Dict[str, Any], key: str) -> Person:
    person_id = str(_required(spec, key))
    if person_id not in people:
        raise ValueError(f"Unknown person id for '{key}': {person_id}")
    return people[person_id]


def _event_spouse(people: Dict[str, Person], event_spec: Dict[str, Any]) -> Person:
    if "spouse_id" in event_spec:
        spouse_id = str(event_spec["spouse_id"])
    elif isinstance(event_spec.get("spouse"), dict):
        spouse_id = _person_id(event_spec["spouse"])
    else:
        spouse_id = str(_required(event_spec, "spouse"))
    if spouse_id not in people:
        raise ValueError(f"Unknown spouse id: {spouse_id}")
    return people[spouse_id]


def _required(spec: Dict[str, Any], key: str) -> Any:
    if key not in spec:
        raise ValueError(f"Missing required field: {key}")
    return spec[key]


def _required_int(spec: Dict[str, Any], key: str) -> int:
    return int(_required(spec, key))


def _as_dict(value: Any, field_name: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Field '{field_name}' must be an object")
    return value


def _as_list(value: Any, field_name: str) -> List[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"Field '{field_name}' must be a list")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
