"""Cash-flow capability routing and deterministic scenario compilation.

This module keeps three decisions separate:

* the conversational skill that owns the turn;
* whether the request needs a cash-flow lookup or a model simulation;
* whether the Client File has enough data to execute that simulation safely.

No LLM performs financial arithmetic here. Free-text user intent and scenario
parameter extraction must come from the LLM as structured tool args
(``scenario`` / ``scenario_changes``). This module validates and compiles those
structured changes — it does not regex-parse natural language for intent.

Legacy facts with no authority label remain trusted through 2026-10-01 while
pre-provenance rows are migrated. New typed facts must be ``client_confirmed``.
"""

from __future__ import annotations

import copy
from dataclasses import InitVar, asdict, dataclass, field
from datetime import datetime, timezone
import math
import re
from typing import Any, Dict, Iterable, Optional

from advisor.assumptions.compatibility import (
    build_variable_source_policy_context,
)
from client_file.fact_vocabulary import fact_aliases_for_engine_field
from client_file.lifecycle import normalize_fact_keys

CASHFLOW_CAPABILITY = "cashflow_scenario_analysis"
CASHFLOW_ACTIONS = ("none", "answer_from_snapshot", "run_cashflow_model")
CASHFLOW_READINESS_REQUIRED_FIELDS = (
    "current_age",
    "retirement_age",
    "annual_income",
    "annual_spending",
)
CONFLICT_PRECEDENCE = {
    "account_total": {
        "error_code": "account_total_conflict:{account_kind}",
        "precedence": "confirmed_total_within_tolerance",
    },
    "allocation_weights": {
        "error_code": "allocation_weights_conflict:{account_kind}",
        "precedence": "confirmed_explicit_weights_within_tolerance",
    },
    "household_income": {
        "error_code": "household_income_conflict",
        "precedence": "none",
    },
    "spending_components": {
        "error_code": "spending_component_conflict",
        "precedence": "none",
    },
}
CASHFLOW_CHANGE_KINDS = (
    "one_off_expense",
    "recurring_expense_increase",
    "retirement_age",
    "retirement_age_delta",
    "life_expectancy",
    "immediate_income_loss",
    "future_income_loss",
    "market_return_shock",
    "temporary_income_reduction",
    "temporary_inflation_override",
    "spouse_retirement_age",
    "recurring_investment_contribution",
    "account_balance_haircut",
    "mortgage_payoff",
    "inflation_shock_unspecified",
    "recurring_support_unspecified",
    "expense_increase_unspecified",
    "solve_for_investment_capacity",
    "solve_for_maximum_spending",
    "scenario_parameter_unspecified",
)
# Only these change kinds can be compiled into a LifeModel request today. The
# broader internal list above remains useful for intent parsing and for
# explaining unsupported requests, but the SDK must not advertise operations
# that are guaranteed to abstain.
CASHFLOW_PUBLIC_CHANGE_KINDS = (
    "one_off_expense",
    "recurring_expense_increase",
    "retirement_age",
    "retirement_age_delta",
    "life_expectancy",
    "immediate_income_loss",
    "future_income_loss",
    "temporary_income_reduction",
    "temporary_inflation_override",
    "spouse_retirement_age",
    "recurring_investment_contribution",
    "account_balance_haircut",
    "mortgage_payoff",
)
CASHFLOW_METRICS = (
    "success_probability",
    "shortfall",
    "projected_terminal_value",
    "ending_balance",
    "terminal_value_percentiles",
    "shortfall_percentiles",
    "first_depletion_year_distribution",
    "first_shortfall_year_distribution",
    "reserve_breach_probability",
    "minimum_liquidity",
    "material_change",
)
CASHFLOW_REQUIRED_METRICS = (
    "ending_balance",
    "shortfall",
    "success_probability",
)


@dataclass(frozen=True)
class CashflowCapabilityDecision:
    """Normalized cross-skill cash-flow routing decision."""

    requested: bool = False
    action: str = "none"
    confidence: float = 0.0
    source: str = "deterministic"
    reason: str = "No cash-flow analysis intent detected."
    evidence: list[str] = field(default_factory=list)
    requested_metrics: list[str] = field(default_factory=list)
    scenario_summary: Optional[str] = None
    scenario_rationale: Optional[str] = None
    scenario_changes: list[Dict[str, Any]] = field(default_factory=list)
    negated: bool = False
    validation_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "CashflowCapabilityDecision":
        validation_errors: list[str] = []

        raw_action = value.get("action")
        action = str(raw_action or "none")
        if raw_action is not None and action not in CASHFLOW_ACTIONS:
            validation_errors.append(f"unsupported_cashflow_action:{action}")
            action = "none"
        elif raw_action is None and (
            value.get("requested") or value.get("scenario_changes") is not None
        ):
            # Action and mandatory metrics are server-owned so the model cannot
            # downgrade or broaden execution semantics.
            action = "run_cashflow_model"

        scenario_changes: list[Dict[str, Any]] = []
        raw_changes = value.get("scenario_changes", [])
        if not isinstance(raw_changes, list):
            validation_errors.append("scenario_changes_must_be_an_array")
        else:
            for index, item in enumerate(raw_changes):
                if not isinstance(item, dict):
                    validation_errors.append(f"scenario_change_{index}_must_be_an_object")
                    continue
                kind = str(item.get("kind") or "")
                if kind not in CASHFLOW_CHANGE_KINDS:
                    validation_errors.append(
                        f"unsupported_scenario_change_kind:{kind or '<missing>'}"
                    )
                scenario_changes.append(dict(item))

        supplied_metrics = value.get("requested_metrics", [])
        if not isinstance(supplied_metrics, list):
            validation_errors.append("requested_metrics_must_be_an_array")
            supplied_metrics = []
        unknown_metrics = [
            str(metric) for metric in supplied_metrics if str(metric) not in CASHFLOW_METRICS
        ]
        validation_errors.extend(
            f"unsupported_requested_metric:{metric}" for metric in unknown_metrics
        )
        requested_metrics = cashflow_required_metrics()
        for metric in supplied_metrics:
            normalized_metric = str(metric)
            if normalized_metric in CASHFLOW_METRICS and normalized_metric not in requested_metrics:
                requested_metrics.append(normalized_metric)

        scenario_summary = _scenario_record_text(
            value.get("scenario_summary"),
            field_name="scenario_summary",
            max_length=160,
            validation_errors=validation_errors,
        )
        scenario_rationale = _scenario_record_text(
            value.get("scenario_rationale"),
            field_name="scenario_rationale",
            max_length=600,
            validation_errors=validation_errors,
        )

        return cls(
            requested=bool(
                value.get("requested")
                or action == "run_cashflow_model"
            ),
            action=action,
            confidence=_confidence(value.get("confidence")),
            source=str(value.get("source") or "persisted_checkpoint"),
            reason=str(value.get("reason") or "Resumed pending cash-flow request."),
            evidence=[str(item) for item in value.get("evidence", []) if item],
            requested_metrics=requested_metrics,
            scenario_summary=scenario_summary,
            scenario_rationale=scenario_rationale,
            scenario_changes=scenario_changes,
            negated=bool(value.get("negated")),
            validation_errors=validation_errors,
        )


_NEGATED_CASHFLOW_PATTERNS = (
    r"\brather than (?:projecting|forecasting|modeling|modelling) (?:our |my |household )?cash[-\s]*flow\b",
    r"\bnot (?:a |the )?(?:household )?(?:cash[-\s]*flow|projection|forecast)\b",
    r"\bdo not (?:run|project|forecast|model|simulate) (?:a |the )?(?:household )?cash[-\s]*flow\b",
    r"\bdon't (?:run|project|forecast|model|simulate) (?:a |the )?(?:household )?cash[-\s]*flow\b",
    r"\bno (?:cash[-\s]*flow|projection|forecast|monte carlo)\b",
    r"\b(?:explain|define|teach me).+\bwithout (?:running|doing|calculating)\b",
    r"\b(?:forget|pause|stop)\b.+\b(?:scenario|model|projection|forecast)\b",
    r"\b(?:do not|don't) forecast (?:our |my |the )?finances\b",
    r"\bnot ready\b.+\b(?:projection|forecast|model)\b",
)

_EDUCATIONAL_NO_RUN_PHRASES = (
    "what does cash flow mean",
    "what does cashflow mean",
    "explain monte carlo simulation",
    "difference between cash flow and net worth",
    "teach me what a percentile means",
    "what does a retirement success probability represent",
    "why do advisors care about liquidity",
    "why might someone keep an emergency reserve",
)

_PROBABILITY_PHRASES = (
    "probability",
    "what are the chances",
    "what is the chance",
    "chance of",
    "how likely",
    "how often",
    "monte carlo",
    "percentile",
    "distribution of",
    "uncertain market paths",
    "plan fail",
)
_AFFORDABILITY_PHRASES = (
    "can we afford",
    "can i afford",
    "is it affordable",
    "is retiring",
    "most we can safely spend",
    "finances support",
    "can our finances support",
    "can we handle",
    "realistic given",
    "still cover all obligations",
    "leave us enough liquidity",
    "annual gifting can we sustain",
)
_STRESS_PHRASES = (
    "stress test",
    "what happens if",
    "what if",
    "model a job loss",
    "simulate a recession",
    "test the impact",
    "show the downside",
    "market drops",
    "lose my bonus",
    "job loss",
    "unexpected expense",
    "inflation keeps",
    "costs rise",
    "expenses increase",
    "supporting a parent",
    "account loses",
    "salary falls",
    "retire five years earlier",
)
_SUSTAINABILITY_PHRASES = (
    "money last",
    "plan survive",
    "lifestyle is sustainable",
    "sustainable long term",
    "saving enough today",
    "resources cover the life",
    "first point at which",
    "become fragile",
    "comfortable buffer",
    "current path leaves room",
    "room for surprises",
    "financially on track",
    "eventually run out",
    "account depletion",
    "run short",
)
_LIQUIDITY_PHRASES = (
    "emergency reserve",
    "reserve drops",
    "liquidity",
    "liquid assets",
    "cash shortfall",
    "comfortable buffer",
)
_INVESTMENT_CAPACITY_PHRASES = (
    "how much can i invest",
    "how much can we invest",
    "safely invest",
    "invest without weakening",
    "invest without reducing",
)
_BASELINE_MODEL_PHRASES = (
    "cash-flow baseline",
    "cash flow baseline",
    "project whether",
    "cash-flow projection",
    "cash flow projection",
    "cashflow projection",
    "forecast our cash",
    "project our cash",
    "cash flow for the next",
    "cashflow for the next",
    "forecast our household cash flow",
    "future spending and account balances",
    "flows in and out each year under the current plan",
    "use the cash flow model",
)
_CURRENT_LOOKUP_PHRASES = (
    "annual cash surplus",
    "monthly surplus",
    "savings rate",
    "current cash flow",
    "current cashflow",
    "cash flow right now",
    "cashflow right now",
    "money flows in and out",
)


def classify_cashflow_request(
    user_message: str,
    *,
    semantic_capabilities: Optional[Iterable[str]] = None,
    semantic_action: Optional[str] = None,
    scenario_changes: Optional[Iterable[Dict[str, Any]]] = None,
) -> CashflowCapabilityDecision:
    """Accept structured cashflow intent only — no free-text NL regex/keyword parsing.

    User-message intent and scenario parameters must come from the LLM as structured
    fields (semantic capabilities / scenario_changes) or as tool arguments.
    This helper no longer scans the raw user string for retirement age, amounts, or
    execution intent.
    """

    del user_message  # Intentionally unused: NL intent recognition is not done here.
    structured_changes = [
        dict(item) for item in (scenario_changes or []) if isinstance(item, dict)
    ]
    semantic_requested = CASHFLOW_CAPABILITY in set(semantic_capabilities or [])
    if not semantic_requested and semantic_action is None:
        if structured_changes:
            return CashflowCapabilityDecision(
                requested=True,
                action="run_cashflow_model",
                confidence=0.9,
                source="structured_scenario_changes",
                reason="Structured scenario_changes were supplied without free-text classification.",
                evidence=["structured_scenario_changes"],
                scenario_changes=structured_changes,
            )
        return CashflowCapabilityDecision()

    action = semantic_action if semantic_action in CASHFLOW_ACTIONS else "run_cashflow_model"
    return CashflowCapabilityDecision(
        requested=True,
        action=action,
        confidence=0.9,
        source="semantic_router",
        reason=_decision_reason(action=action),
        evidence=["semantic_router"],
        requested_metrics=cashflow_required_metrics(),
        scenario_changes=structured_changes,
    )


@dataclass(frozen=True)
class CashflowClientInput:
    """Canonical Client File input shared by readiness and engine execution.

    Required financial values come from Client File fact stores, never from the
    mapper's demo defaults. ``cashflow_state`` remains useful for optional
    account detail, but it cannot make a recommendation path ready by inventing
    age, retirement age, income, or spending.
    """

    current_age: Optional[float]
    spouse_age: Optional[float]
    marital_status: Optional[str]
    retirement_age: Optional[float]
    annual_income: Optional[float]
    annual_spending: Optional[float]
    retirement_contribution_pct: Optional[float]
    monthly_retirement_contribution: Optional[float]
    life_expectancy: Optional[float]
    cash_balance: Optional[float]
    brokerage_balance: Optional[float]
    retirement_balance: Optional[float]
    education_balance: Optional[float]
    mortgage_balance: Optional[float]
    home_value: Optional[float]
    home_tax_basis: Optional[float]
    home_appreciation_rate: Optional[float]
    mortgage_interest_rate: Optional[float]
    mortgage_remaining_term_years: Optional[float]
    mortgage_monthly_payment: Optional[float]
    mortgage_type: Optional[str]
    annual_spending_includes_mortgage: Optional[bool]
    brokerage_asset_allocation: Optional[Dict[str, Any]]
    retirement_asset_allocation: Optional[Dict[str, Any]]
    education_goal_amount: Optional[float]
    education_horizon_years: Optional[float]
    emergency_reserve_months: Optional[float]
    source_client_file_version: InitVar[Optional[int]] = None
    source_by_field: Dict[str, str] = field(default_factory=dict)
    authority_by_field: Dict[str, Optional[str]] = field(default_factory=dict)
    validation_errors: list[str] = field(default_factory=list)
    validation_error_details: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    cashflow_state: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self, source_client_file_version: Optional[int]) -> None:
        object.__setattr__(self, "_source_client_file_version", source_client_file_version)

    @classmethod
    def from_client_file(cls, client_file: Dict[str, Any]) -> "CashflowClientInput":
        committed, structured, _drafts, cashflow_state = _cashflow_fact_sources(client_file)
        sources = (
            ("canonical_typed_facts" if client_file.get("typed_facts") else "committed_facts", committed),
            ("legacy_structured_adapter", structured),
        )
        source_by_field: Dict[str, str] = {}
        authority_by_field: Dict[str, Optional[str]] = {}
        typed_authorities = (
            committed.get("__authority_by_field__")
            if isinstance(committed.get("__authority_by_field__"), dict)
            else {}
        )
        validation_error_details = (
            copy.deepcopy(committed.get("__typed_error_details__"))
            if isinstance(committed.get("__typed_error_details__"), dict)
            else {}
        )

        def record_source(field_name: str, source_name: str, alias: str) -> None:
            source_by_field[field_name] = source_name
            authority_by_field[field_name] = (
                typed_authorities.get(alias)
                if source_name == "canonical_typed_facts"
                else None
            )

        def select(field_name: str, *aliases: str) -> Optional[float]:
            for source_name, values in sources:
                value = _first_number(values, *aliases)
                if value is not None:
                    matched_alias = next(
                        (alias for alias in aliases if _number(values.get(alias)) is not None),
                        aliases[0],
                    )
                    record_source(field_name, source_name, matched_alias)
                    return value
            return None

        def select_text(field_name: str, *aliases: str) -> Optional[str]:
            for source_name, values in sources:
                for alias in aliases:
                    raw_value = values.get(alias)
                    if not isinstance(raw_value, str) or not raw_value.strip():
                        continue
                    record_source(field_name, source_name, alias)
                    return raw_value.strip()
            return None

        def select_mapping(field_name: str, *aliases: str) -> Optional[Dict[str, Any]]:
            for source_name, values in sources:
                for alias in aliases:
                    raw_value = values.get(alias)
                    if not isinstance(raw_value, dict) or not raw_value:
                        continue
                    record_source(field_name, source_name, alias)
                    return copy.deepcopy(raw_value)
            return None

        state_accounts = (
            cashflow_state.get("accounts")
            if isinstance(cashflow_state.get("accounts"), dict)
            else {}
        )
        state_expenses = (
            cashflow_state.get("expenses")
            if isinstance(cashflow_state.get("expenses"), dict)
            else {}
        )
        state_housing = (
            state_expenses.get("housing")
            if isinstance(state_expenses.get("housing"), dict)
            else {}
        )
        state_liabilities = (
            cashflow_state.get("liabilities")
            if isinstance(cashflow_state.get("liabilities"), dict)
            else {}
        )

        def select_mortgage_balance() -> Optional[float]:
            value = select(
                "mortgage_balance",
                *fact_aliases_for_engine_field("mortgage_balance"),
            )
            if value is not None:
                return value
            value = _coalesce_number(
                _first_number(state_liabilities, "mortgage_balance"),
                _first_number(state_housing, "mortgage_balance"),
            )
            if value is not None:
                source_by_field["mortgage_balance"] = "cashflow_state_housing"
                authority_by_field["mortgage_balance"] = None
            return value

        def select_housing_number(
            field_name: str,
            fact_aliases: tuple[str, ...],
            state_aliases: tuple[str, ...],
            *,
            positive_only: bool = False,
        ) -> Optional[float]:
            value = select(field_name, *fact_aliases)
            if value is not None:
                return value
            value = _first_number(state_housing, *state_aliases)
            if value is None or (positive_only and value <= 0):
                return None
            source_by_field[field_name] = "cashflow_state_housing"
            authority_by_field[field_name] = None
            return value

        def select_bool(field_name: str, *aliases: str) -> Optional[bool]:
            for source_name, source_values in sources:
                for alias in aliases:
                    value = _boolean(source_values.get(alias))
                    if value is not None:
                        record_source(field_name, source_name, alias)
                        return value
            for alias in aliases:
                value = _boolean(state_housing.get(alias))
                if value is not None:
                    source_by_field[field_name] = "cashflow_state_housing"
                    authority_by_field[field_name] = None
                    return value
            return None

        def select_housing_text(field_name: str, *aliases: str) -> Optional[str]:
            value = select_text(field_name, *aliases)
            if value is not None:
                return value
            for alias in aliases:
                raw_value = state_housing.get(alias)
                if isinstance(raw_value, str) and raw_value.strip():
                    source_by_field[field_name] = "cashflow_state_housing"
                    authority_by_field[field_name] = None
                    return raw_value.strip()
            return None

        def select_balance(field_name: str, account_kind: str, *aliases: str) -> Optional[float]:
            value = select(field_name, *aliases)
            if value is not None:
                return value
            value = _cashflow_state_pool_balance(state_accounts, account_kind)
            if value is not None:
                source_by_field[field_name] = "cashflow_state_accounts"
                authority_by_field[field_name] = None
            return value

        values: Dict[str, Optional[float]] = {
            "current_age": select(
                "current_age",
                *fact_aliases_for_engine_field("current_age"),
            ),
            "spouse_age": select(
                "spouse_age",
                *fact_aliases_for_engine_field("spouse_age"),
            ),
            "retirement_age": select(
                "retirement_age",
                *fact_aliases_for_engine_field("retirement_age"),
            ),
            "annual_income": select(
                "annual_income",
                *fact_aliases_for_engine_field("annual_income"),
            ),
            "annual_spending": select(
                "annual_spending",
                *fact_aliases_for_engine_field("annual_spending"),
            ),
            "retirement_contribution_pct": select(
                "retirement_contribution_pct",
                *fact_aliases_for_engine_field("retirement_contribution_pct"),
            ),
            "monthly_retirement_contribution": select(
                "monthly_retirement_contribution",
                *fact_aliases_for_engine_field("monthly_retirement_contribution"),
            ),
            "life_expectancy": select(
                "life_expectancy",
                *fact_aliases_for_engine_field("life_expectancy"),
            ),
            "cash_balance": select_balance(
                "cash_balance",
                "bank",
                *fact_aliases_for_engine_field("cash_balance"),
            ),
            "brokerage_balance": select_balance(
                "brokerage_balance",
                "brokerage",
                *fact_aliases_for_engine_field("brokerage_balance"),
            ),
            "retirement_balance": select_balance(
                "retirement_balance",
                "retirement",
                *fact_aliases_for_engine_field("retirement_balance"),
            ),
            "education_balance": select_balance(
                "education_balance",
                "education",
                *fact_aliases_for_engine_field("education_balance"),
            ),
            "mortgage_balance": select_mortgage_balance(),
            "home_value": select_housing_number(
                "home_value",
                fact_aliases_for_engine_field("home_value"),
                ("home_value", "current_home_value", "current_value"),
                positive_only=True,
            ),
            "home_tax_basis": select_housing_number(
                "home_tax_basis",
                fact_aliases_for_engine_field("home_tax_basis"),
                ("tax_basis", "home_tax_basis"),
                positive_only=True,
            ),
            "home_appreciation_rate": select_housing_number(
                "home_appreciation_rate",
                fact_aliases_for_engine_field("home_appreciation_rate"),
                ("home_appreciation_rate", "value_yearly_increase", "appreciation_rate"),
            ),
            "mortgage_interest_rate": select_housing_number(
                "mortgage_interest_rate",
                fact_aliases_for_engine_field("mortgage_interest_rate"),
                ("mortgage_interest_rate", "interest_rate", "yearly_interest_rate"),
            ),
            "mortgage_remaining_term_years": select_housing_number(
                "mortgage_remaining_term_years",
                fact_aliases_for_engine_field("mortgage_remaining_term_years"),
                ("mortgage_remaining_term_years", "remaining_term_years"),
                positive_only=True,
            ),
            "mortgage_monthly_payment": select_housing_number(
                "mortgage_monthly_payment",
                fact_aliases_for_engine_field("mortgage_monthly_payment"),
                ("monthly_principal_interest", "mortgage_monthly_payment", "monthly_payment"),
                positive_only=True,
            ),
            "education_goal_amount": select(
                "education_goal_amount",
                *fact_aliases_for_engine_field("education_goal_amount"),
            ),
            "education_horizon_years": select(
                "education_horizon_years",
                *fact_aliases_for_engine_field("education_horizon_years"),
            ),
            "emergency_reserve_months": select(
                "emergency_reserve_months",
                *fact_aliases_for_engine_field("emergency_reserve_months"),
            ),
        }
        # AWM stores percentage-point inputs for cashflow rates. Agent extraction
        # may legitimately emit decimal fractions (3% -> 0.03); normalize that
        # representation at the deterministic adapter boundary.
        for rate_field in ("home_appreciation_rate", "mortgage_interest_rate"):
            rate_value = values.get(rate_field)
            if rate_value is not None and 0 < abs(rate_value) <= 1:
                values[rate_field] = rate_value * 100.0
        contribution_rate = values.get("retirement_contribution_pct")
        if contribution_rate is not None and 0 < contribution_rate <= 1:
            values["retirement_contribution_pct"] = contribution_rate * 100.0
        validation_errors = _cashflow_client_input_errors(values)
        validation_errors.extend(committed.get("__typed_errors__", []))
        marital_status = select_text(
            "marital_status",
            *fact_aliases_for_engine_field("marital_status"),
        )
        normalized_marital_status = (
            marital_status.strip().lower().replace("-", "_").replace(" ", "_")
            if marital_status
            else None
        )
        if (
            normalized_marital_status
            in {"married", "married_filing_jointly", "mfj"}
            and values["spouse_age"] is None
        ):
            validation_errors.append("incomplete_input:spouse_age")
        mortgage_type = select_housing_text(
            "mortgage_type",
            *fact_aliases_for_engine_field("mortgage_type"),
        )
        annual_spending_includes_mortgage = select_bool(
            "annual_spending_includes_mortgage",
            *fact_aliases_for_engine_field("annual_spending_includes_mortgage"),
        )
        brokerage_asset_allocation = select_mapping(
            "brokerage_asset_allocation",
            "brokerage_asset_allocation",
            "brokerage_allocation",
            "taxable_brokerage_asset_allocation",
            "taxable_brokerage_allocation",
        )
        retirement_asset_allocation = select_mapping(
            "retirement_asset_allocation",
            "retirement_asset_allocation",
            "retirement_allocation",
            "retirement_accounts_asset_allocation",
        )
        return cls(
            **values,
            marital_status=marital_status,
            mortgage_type=mortgage_type,
            annual_spending_includes_mortgage=annual_spending_includes_mortgage,
            brokerage_asset_allocation=brokerage_asset_allocation,
            retirement_asset_allocation=retirement_asset_allocation,
            source_by_field=source_by_field,
            authority_by_field=authority_by_field,
            validation_errors=validation_errors,
            validation_error_details=validation_error_details,
            cashflow_state=copy.deepcopy(cashflow_state),
            source_client_file_version=(
                int(client_file["client_file_version"])
                if isinstance(client_file.get("client_file_version"), int)
                else None
            ),
        )

    @property
    def starting_assets(self) -> Optional[float]:
        balances = (
            self.cash_balance,
            self.brokerage_balance,
            self.retirement_balance,
        )
        if not any(value is not None for value in balances):
            return None
        return sum(value for value in balances if value is not None)

    @property
    def all_inputs_client_confirmed(self) -> bool:
        return all(
            authority in {None, "client_confirmed"}
            for authority in self.authority_by_field.values()
        )

    def canonical_values(self) -> Dict[str, Any]:
        return {
            "current_age": self.current_age,
            "spouse_age": self.spouse_age,
            "marital_status": self.marital_status,
            "retirement_age": self.retirement_age,
            "annual_income": self.annual_income,
            "annual_spending": self.annual_spending,
            "retirement_contribution_pct": self.retirement_contribution_pct,
            "monthly_retirement_contribution": self.monthly_retirement_contribution,
            "life_expectancy": self.life_expectancy,
            "cash_balance": self.cash_balance,
            "brokerage_balance": self.brokerage_balance,
            "retirement_balance": self.retirement_balance,
            "education_balance": self.education_balance,
            "mortgage_balance": self.mortgage_balance,
            "home_value": self.home_value,
            "home_tax_basis": self.home_tax_basis,
            "home_appreciation_rate": self.home_appreciation_rate,
            "mortgage_interest_rate": self.mortgage_interest_rate,
            "mortgage_remaining_term_years": self.mortgage_remaining_term_years,
            "mortgage_monthly_payment": self.mortgage_monthly_payment,
            "mortgage_type": self.mortgage_type,
            "annual_spending_includes_mortgage": self.annual_spending_includes_mortgage,
            "brokerage_asset_allocation": copy.deepcopy(self.brokerage_asset_allocation),
            "retirement_asset_allocation": copy.deepcopy(self.retirement_asset_allocation),
            "starting_assets": self.starting_assets,
        }

    def to_engine_payload(
        self,
        *,
        current_year: Optional[int] = None,
        allow_mortgage_defaults: bool = False,
    ) -> Dict[str, Any]:
        """Build the exact AWM engine payload with guarded mortgage fallback consent."""

        current_year = int(current_year or datetime.now(timezone.utc).year)
        payload = copy.deepcopy(self.cashflow_state)
        profile = payload.get("client_profile")
        if not isinstance(profile, dict):
            profile = {}
            payload["client_profile"] = profile
        income = payload.get("income")
        if not isinstance(income, dict):
            income = {}
            payload["income"] = income
        expenses = payload.get("expenses")
        if not isinstance(expenses, dict):
            expenses = {}
            payload["expenses"] = expenses

        _set_or_remove(profile, "age", self.current_age)
        _set_or_remove(profile, "spouse_age", self.spouse_age)
        _set_or_remove(profile, "marital_status", self.marital_status)
        _set_or_remove(profile, "retirement_age", self.retirement_age)
        _set_or_remove(profile, "life_expectancy", self.life_expectancy)
        _set_or_remove(income, "salary", self.annual_income)
        if self.annual_income is None:
            income.pop("salary_scope", None)
        else:
            # The canonical AWM readiness question and fact alias represent
            # total household income, even when the LifeModel ultimately needs
            # individual workers for payroll-tax precision.
            income["salary_scope"] = "household_total"
        _set_or_remove(expenses, "base_spending", self.annual_spending)

        accounts = payload.get("accounts")
        if not isinstance(accounts, dict):
            accounts = {}
            payload["accounts"] = accounts
        assumptions: list[str] = []
        unsupported_inputs: list[str] = []
        if self.retirement_contribution_pct is not None:
            income["retirement_contribution_pct"] = self.retirement_contribution_pct
        elif (
            self.monthly_retirement_contribution is not None
            and self.annual_income is not None
            and self.annual_income > 0
        ):
            derived_contribution_pct = (
                self.monthly_retirement_contribution * 12.0 / self.annual_income * 100.0
            )
            income["retirement_contribution_pct"] = derived_contribution_pct
            assumptions.append(
                "The stated monthly retirement contribution is annualized and "
                "converted to a percentage of household income for the retirement-account model."
            )
        canonical_pools = (
            ("bank", self.cash_balance, "Cash"),
            ("brokerage", self.brokerage_balance, "Taxable brokerage"),
            ("retirement", self.retirement_balance, "Retirement accounts"),
            ("education", self.education_balance, "529 college accounts"),
        )
        stated_allocations = {
            "brokerage": self.brokerage_asset_allocation,
            "retirement": self.retirement_asset_allocation,
        }
        for account_kind, balance, label in canonical_pools:
            if balance is None:
                continue
            source = self.source_by_field.get(f"{account_kind}_balance")
            if account_kind == "bank":
                source = self.source_by_field.get("cash_balance")
            if source == "cashflow_state_accounts" and isinstance(accounts.get(account_kind), list):
                _apply_stated_pool_allocation(
                    accounts[account_kind],
                    stated_allocations.get(account_kind),
                )
                if account_kind != "bank" and not _pool_has_asset_allocation(
                    accounts[account_kind]
                ):
                    if _apply_default_education_allocation_if_needed(
                        accounts, account_kind, assumptions
                    ):
                        pass
                    else:
                        unsupported_inputs.append(
                            f"allocation_weights_invalid:{account_kind}"
                            if stated_allocations.get(account_kind)
                            else f"missing_asset_allocation:{account_kind}"
                        )
                continue
            existing_pool = accounts.get(account_kind)
            reconciled, distributed, conflict = _reconcile_cashflow_account_pool(
                existing_pool,
                balance,
                label=label,
            )
            _apply_stated_pool_allocation(
                reconciled,
                stated_allocations.get(account_kind),
            )
            accounts[account_kind] = reconciled
            if conflict:
                code = _conflict_code("account_total", account_kind=account_kind)
                unsupported_inputs.append(code)
                self.validation_error_details.setdefault(code, conflict)
            elif distributed:
                assumptions.append(
                    f"{account_kind} authoritative total was distributed only across sleeves without stated balances"
                )
            if account_kind != "bank" and not _pool_has_asset_allocation(reconciled):
                if _apply_default_education_allocation_if_needed(
                    accounts, account_kind, assumptions
                ):
                    pass
                else:
                    unsupported_inputs.append(
                        f"allocation_weights_invalid:{account_kind}"
                        if stated_allocations.get(account_kind)
                        else f"missing_asset_allocation:{account_kind}"
                    )

        liabilities = payload.get("liabilities")
        if not isinstance(liabilities, dict):
            liabilities = {}
            payload["liabilities"] = liabilities
        _set_or_remove(liabilities, "mortgage_balance", self.mortgage_balance)
        housing = expenses.get("housing")
        if not isinstance(housing, dict):
            housing = {}
            expenses["housing"] = housing
        _set_or_remove(housing, "home_value", self.home_value)
        _set_or_remove(housing, "tax_basis", self.home_tax_basis)
        _set_or_remove(housing, "home_appreciation_rate", self.home_appreciation_rate)
        _set_or_remove(housing, "mortgage_interest_rate", self.mortgage_interest_rate)
        _set_or_remove(
            housing,
            "mortgage_remaining_term_years",
            self.mortgage_remaining_term_years,
        )
        _set_or_remove(housing, "monthly_principal_interest", self.mortgage_monthly_payment)
        _set_or_remove(housing, "mortgage_type", self.mortgage_type)
        _set_or_remove(
            housing,
            "annual_spending_includes_mortgage",
            self.annual_spending_includes_mortgage,
        )
        if self.mortgage_balance is not None and self.mortgage_balance > 0:
            required_mortgage_inputs = {
                "home_value": self.home_value,
                "home_appreciation_rate": self.home_appreciation_rate,
                "mortgage_interest_rate": self.mortgage_interest_rate,
                "mortgage_remaining_term_years": self.mortgage_remaining_term_years,
                "mortgage_type": self.mortgage_type,
                "annual_spending_includes_mortgage": self.annual_spending_includes_mortgage,
            }
            missing_mortgage_inputs = [
                field_name
                for field_name, value in required_mortgage_inputs.items()
                if value is None
            ]
            if not allow_mortgage_defaults:
                unsupported_inputs.extend(
                    f"missing_mortgage_input:{field_name}"
                    for field_name in missing_mortgage_inputs
                )
            normalized_type = str(self.mortgage_type or "").strip().lower().replace("-", "_").replace(" ", "_")
            if self.mortgage_type is not None and normalized_type not in {
                "fixed",
                "fixed_rate",
                "fixed_rate_mortgage",
            }:
                unsupported_inputs.append(
                    f"unsupported_existing_mortgage_type:{normalized_type}"
                )
            if not any(
                marker.startswith("missing_mortgage_input:")
                or marker.startswith("unsupported_existing_mortgage_type:")
                for marker in unsupported_inputs
            ):
                housing["status"] = "own"

        if self.education_goal_amount is not None and self.education_horizon_years is not None:
            target_year = current_year + int(self.education_horizon_years)
            payload["goals"] = [
                {
                    "type": "education",
                    "label": "College education",
                    "target_amount": self.education_goal_amount,
                    "target_year": target_year,
                }
            ]
        elif self.education_goal_amount is not None:
            payload.pop("goals", None)
            unsupported_inputs.append("education_goal_timing_required")
        preferences = payload.get("preferences")
        if not isinstance(preferences, dict):
            preferences = {}
            payload["preferences"] = preferences
        _set_or_remove(
            preferences,
            "maintain_emergency_reserve_months",
            self.emergency_reserve_months,
        )

        variable_source_policy = build_variable_source_policy_context(
            self.source_by_field
        )
        payload["awm_input_contract"] = {
            "schema_version": "awm.cashflow_client_input.v1",
            "source_by_field": dict(self.source_by_field),
            "variable_source_policy": variable_source_policy,
            "all_inputs_client_confirmed": self.all_inputs_client_confirmed,
            # Compatibility field for one release; remove after consumers adopt
            # ``all_inputs_client_confirmed``.
            "uses_draft_facts": not self.all_inputs_client_confirmed,
            "source_client_file_version": getattr(self, "_source_client_file_version", None),
            "mortgage_defaults_authorized": bool(allow_mortgage_defaults),
            "assumptions": assumptions,
            "unsupported_inputs": unsupported_inputs,
        }
        return payload


def cashflow_input_readiness(
    client_file: Dict[str, Any],
    *,
    decision: Optional[CashflowCapabilityDecision] = None,
    client_input: Optional[CashflowClientInput] = None,
    engine_payload: Optional[Dict[str, Any]] = None,
    monte_carlo_paths: Optional[int] = None,
) -> Dict[str, Any]:
    """Return projection input readiness without applying hidden defaults."""

    canonical = client_input or CashflowClientInput.from_client_file(client_file)
    values = canonical.canonical_values()
    if decision is not None:
        values = _apply_scenario_values_for_readiness(values, decision)
    required = list(CASHFLOW_READINESS_REQUIRED_FIELDS)
    if monte_carlo_paths is not None:
        required.append("starting_assets")

    missing = [key for key in required if values.get(key) is None]
    missing.extend(canonical.validation_errors)
    if decision is not None:
        missing.extend(decision.validation_errors)
        change_kinds = {
            str(change.get("kind") or "")
            for change in decision.scenario_changes
            if isinstance(change, dict)
        }
        if any(
            change.get("kind") == "one_off_expense"
            and change.get("horizon_years") is None
            and change.get("target_year") is None
            for change in decision.scenario_changes
            if isinstance(change, dict)
        ):
            missing.append("scenario_target_timing")
        if "scenario_parameter_unspecified" in change_kinds:
            missing.append("scenario_amount_or_change")
        if "inflation_shock_unspecified" in change_kinds:
            missing.append("inflation_assumption")
        if "recurring_support_unspecified" in change_kinds:
            missing.append("annual_support_amount")
        if "expense_increase_unspecified" in change_kinds:
            missing.append("expense_increase_amount")
    resolved_engine_payload = (
        engine_payload if isinstance(engine_payload, dict) else canonical.to_engine_payload()
    )
    awm_contract = (
        resolved_engine_payload.get("awm_input_contract")
        if isinstance(resolved_engine_payload.get("awm_input_contract"), dict)
        else {}
    )
    variable_source_policy = (
        awm_contract.get("variable_source_policy")
        if isinstance(awm_contract.get("variable_source_policy"), dict)
        else build_variable_source_policy_context(canonical.source_by_field)
    )
    unsupported_inputs = [
        str(item)
        for item in awm_contract.get("unsupported_inputs", [])
        if str(item).strip()
    ]
    missing.extend(
        marker
        for marker in unsupported_inputs
        if marker.startswith("missing_mortgage_input:")
        or marker.startswith("unsupported_existing_mortgage_type:")
        or marker.startswith("account_total_conflict:")
        or marker.startswith("allocation_weights_conflict:")
    )
    if monte_carlo_paths is not None:
        missing.extend(
            marker
            for marker in unsupported_inputs
            if marker.startswith("missing_asset_allocation:")
        )
    missing = list(dict.fromkeys(missing))
    optional = [
        key
        for key in ("cash_balance", "brokerage_balance", "retirement_balance", "life_expectancy")
        if values.get(key) is None
    ]
    return {
        "ready": not missing,
        "available_inputs": sorted(key for key, value in values.items() if value is not None),
        "missing_required_inputs": missing,
        "missing_optional_inputs": optional,
        "next_question": (
            _next_input_question(missing[0], canonical.validation_error_details)
            if missing
            else None
        ),
        "canonical_values": values,
        "source_by_field": dict(canonical.source_by_field),
        "variable_source_policy": copy.deepcopy(variable_source_policy),
        "all_inputs_client_confirmed": canonical.all_inputs_client_confirmed,
        "uses_draft_facts": not canonical.all_inputs_client_confirmed,
    }


def _apply_scenario_values_for_readiness(
    values: Dict[str, Any],
    decision: CashflowCapabilityDecision,
) -> Dict[str, Any]:
    """Fill readiness gaps from explicit scenario changes in the current request."""

    updated = dict(values)
    for change in decision.scenario_changes:
        if not isinstance(change, dict):
            continue
        kind = str(change.get("kind") or "")
        if kind == "retirement_age":
            retirement_age = _whole_number(change.get("value"))
            if retirement_age is not None and 0 <= retirement_age <= 120:
                updated["retirement_age"] = retirement_age
        elif kind == "life_expectancy":
            life_expectancy = _whole_number(change.get("value"))
            if life_expectancy is not None and 0 <= life_expectancy <= 130:
                updated["life_expectancy"] = life_expectancy
    return updated


def compile_cashflow_scenario(
    *,
    base_payload: Dict[str, Any],
    decision: CashflowCapabilityDecision,
    monte_carlo_paths: Optional[int] = None,
    current_year: Optional[int] = None,
) -> Dict[str, Any]:
    """Compile extracted scenario changes into the engine's effective payload."""

    current_year = int(current_year or datetime.now(timezone.utc).year)
    effective = copy.deepcopy(base_payload)
    warnings: list[str] = []
    unsupported_changes: list[Dict[str, Any]] = []
    applied_changes: list[Dict[str, Any]] = []

    if decision.validation_errors:
        unsupported_changes.append(
            {
                "kind": "invalid_request",
                "reason": "cashflow_decision_validation_failed",
                "errors": list(decision.validation_errors),
            }
        )
    if decision.action != "run_cashflow_model":
        unsupported_changes.append(
            {
                "kind": "invalid_request",
                "reason": f"action_must_be_run_cashflow_model:{decision.action}",
            }
        )
    projection_start_year = int(_number(base_payload.get("start_year")) or current_year)
    for change in decision.scenario_changes:
        kind = change.get("kind")
        if kind == "one_off_expense":
            amount = _number(change.get("amount"))
            target_year = _target_year(change, current_year=projection_start_year)
            if amount is None or target_year is None:
                unsupported_changes.append({**change, "reason": "amount_and_target_year_required"})
                continue
            if target_year < projection_start_year:
                unsupported_changes.append(
                    {
                        **change,
                        "reason": "target_year_must_not_precede_projection_start",
                        "projection_start_year": projection_start_year,
                    }
                )
                continue
            effective.setdefault("one_off_expenses", []).append(
                {
                    "label": change.get("label") or "Scenario expense",
                    "amount": amount,
                    "year": target_year,
                }
            )
            applied_changes.append(change)
        elif kind == "recurring_expense_increase":
            amount = _number(change.get("amount"))
            expenses = effective.setdefault("expenses", {})
            base_spending = _number(expenses.get("base_spending"))
            if amount is None or base_spending is None:
                unsupported_changes.append({**change, "reason": "amount_and_base_spending_required"})
                continue
            expenses["base_spending"] = base_spending + amount
            applied_changes.append(change)
            warnings.append("Recurring expense change is applied from the first simulated year.")
        elif kind == "retirement_age":
            retirement_age = _whole_number(change.get("value"))
            if retirement_age is None or not 0 <= retirement_age <= 120:
                unsupported_changes.append({**change, "reason": "retirement_age_required"})
                continue
            effective.setdefault("client_profile", {})["retirement_age"] = retirement_age
            applied_changes.append(change)
        elif kind == "retirement_age_delta":
            delta = _whole_number(change.get("value"))
            profile = effective.setdefault("client_profile", {})
            current_retirement_age = _number(profile.get("retirement_age"))
            updated_retirement_age = (
                current_retirement_age + delta
                if delta is not None and current_retirement_age is not None
                else None
            )
            if (
                updated_retirement_age is None
                or not 0 <= updated_retirement_age <= 120
            ):
                unsupported_changes.append({**change, "reason": "current_retirement_age_required"})
                continue
            profile["retirement_age"] = int(updated_retirement_age)
            applied_changes.append(change)
        elif kind == "life_expectancy":
            life_expectancy = _whole_number(change.get("value"))
            if life_expectancy is None or not 0 <= life_expectancy <= 130:
                unsupported_changes.append({**change, "reason": "life_expectancy_required"})
                continue
            effective.setdefault("client_profile", {})["life_expectancy"] = life_expectancy
            applied_changes.append(change)
        elif kind == "immediate_income_loss":
            effective.setdefault("income", {})["salary"] = 0.0
            applied_changes.append(change)
        elif kind == "future_income_loss":
            target_year = _target_year(change, current_year=projection_start_year)
            duration_years = _whole_number(change.get("duration_years"))
            duration_months = _whole_number(change.get("duration_months"))
            person = str(change.get("person") or "primary")
            if (
                target_year is None
                or (duration_years is None) == (duration_months is None)
                or (duration_years is not None and duration_years <= 0)
                or (duration_months is not None and duration_months <= 0)
            ):
                unsupported_changes.append(
                    {
                        **change,
                        "reason": (
                            "target_timing_and_exactly_one_positive_year_or_month_duration_required"
                        ),
                    }
                )
                continue
            if target_year < projection_start_year:
                unsupported_changes.append(
                    {**change, "reason": "target_year_must_not_precede_projection_start"}
                )
                continue
            events, prorated = _bounded_income_events(
                label=str(change.get("label") or "Future income loss"),
                person=person,
                target_year=target_year,
                reduction_percentage=1.0,
                duration_years=duration_years,
                duration_months=duration_months,
            )
            effective.setdefault("income_events", []).extend(events)
            if prorated:
                warnings.append(
                    "A partial-year income loss is modeled with a prorated annual "
                    "income multiplier because LifeModel advances in annual steps."
                )
            applied_changes.append(change)
        elif kind == "market_return_shock":
            unsupported_changes.append(
                {**change, "reason": "engine_contract_has_no_direct_market_return_override"}
            )
        elif kind == "temporary_income_reduction":
            target_year = _target_year(change, current_year=projection_start_year)
            duration_years = _whole_number(change.get("duration_years"))
            duration_months = _whole_number(change.get("duration_months"))
            percentage = _number(change.get("percentage"))
            person = str(change.get("person") or "primary")
            if (
                target_year is None
                or (duration_years is None) == (duration_months is None)
                or (duration_years is not None and duration_years <= 0)
                or (duration_months is not None and duration_months <= 0)
                or percentage is None
                or percentage < 0
                or percentage > 1
            ):
                unsupported_changes.append(
                    {
                        **change,
                        "reason": (
                            "target_timing_positive_duration_and_reduction_percentage_required"
                        ),
                    }
                )
                continue
            if target_year < projection_start_year:
                unsupported_changes.append(
                    {**change, "reason": "target_year_must_not_precede_projection_start"}
                )
                continue
            events, prorated = _bounded_income_events(
                label=str(change.get("label") or "Temporary income reduction"),
                person=person,
                target_year=target_year,
                reduction_percentage=percentage,
                duration_years=duration_years,
                duration_months=duration_months,
            )
            effective.setdefault("income_events", []).extend(events)
            if prorated:
                warnings.append(
                    "A partial-year income reduction is modeled with a prorated "
                    "annual income multiplier because LifeModel advances in annual steps."
                )
            applied_changes.append(change)
        elif kind == "temporary_inflation_override":
            target_year = _target_year(change, current_year=projection_start_year)
            duration_years = _whole_number(change.get("duration_years"))
            percentage = _number(change.get("percentage"))
            person = str(change.get("person") or "primary")
            expenses = (
                effective.get("expenses")
                if isinstance(effective.get("expenses"), dict)
                else {}
            )
            restore_rate = _number(
                expenses.get("growth_rate", expenses.get("yearly_increase"))
            )
            if (
                target_year is None
                or duration_years is None
                or duration_years <= 0
                or percentage is None
                or percentage < 0
                or percentage > 1
            ):
                unsupported_changes.append(
                    {
                        **change,
                        "reason": (
                            "target_timing_positive_duration_and_inflation_rate_required"
                        ),
                    }
                )
                continue
            if target_year < projection_start_year:
                unsupported_changes.append(
                    {**change, "reason": "target_year_must_not_precede_projection_start"}
                )
                continue
            effective.setdefault("spending_growth_events", []).append(
                {
                    "label": change.get("label") or "Temporary inflation override",
                    "person": person,
                    "start_year": target_year,
                    "end_year": target_year + duration_years - 1,
                    "annual_rate_percent": percentage * 100.0,
                    "restore_rate_percent": restore_rate,
                }
            )
            applied_changes.append(change)
            warnings.append(
                "The inflation override changes household spending growth only; it is not a "
                "general override of wages, taxes, or capital-market assumptions."
            )
        elif kind == "spouse_retirement_age":
            retirement_age = _whole_number(change.get("value"))
            profile = effective.setdefault("client_profile", {})
            if (
                retirement_age is None
                or not 0 <= retirement_age <= 120
                or profile.get("spouse_age") is None
            ):
                unsupported_changes.append(
                    {**change, "reason": "spouse_and_spouse_retirement_age_required"}
                )
                continue
            profile["spouse_retirement_age"] = retirement_age
            applied_changes.append(change)
        elif kind == "recurring_investment_contribution":
            amount = _number(change.get("amount"))
            target_year = _target_year(change, current_year=projection_start_year)
            duration_years = _whole_number(change.get("duration_years"))
            person = str(change.get("person") or "primary")
            accounts = (
                effective.get("accounts")
                if isinstance(effective.get("accounts"), dict)
                else {}
            )
            if (
                amount is None
                or target_year is None
                or duration_years is None
                or duration_years <= 0
            ):
                unsupported_changes.append(
                    {
                        **change,
                        "reason": "amount_target_timing_and_positive_duration_required",
                    }
                )
                continue
            if target_year < projection_start_year:
                unsupported_changes.append(
                    {**change, "reason": "target_year_must_not_precede_projection_start"}
                )
                continue
            if person != "primary":
                unsupported_changes.append(
                    {
                        **change,
                        "reason": (
                            "current_account_mapping_assigns_household_brokerage_to_primary"
                        ),
                    }
                )
                continue
            if not isinstance(accounts.get("brokerage"), list) or not accounts["brokerage"]:
                unsupported_changes.append(
                    {**change, "reason": "brokerage_account_required"}
                )
                continue
            effective.setdefault("recurring_investment_contributions", []).append(
                {
                    "label": change.get("label") or "Recurring investment contribution",
                    "person": person,
                    "annual_amount": amount,
                    "start_year": target_year,
                    "end_year": target_year + duration_years - 1,
                }
            )
            applied_changes.append(change)
        elif kind == "account_balance_haircut":
            percentage = _number(change.get("percentage"))
            account_type = str(change.get("account_type") or "")
            accounts = effective.get("accounts") if isinstance(effective.get("accounts"), dict) else {}
            pool = accounts.get(account_type)
            if percentage is None or not isinstance(pool, list) or not pool:
                unsupported_changes.append({**change, "reason": "account_balance_required"})
                continue
            for account in pool:
                balance = _number(account.get("balance")) if isinstance(account, dict) else None
                if balance is not None:
                    account["balance"] = balance * (1.0 - percentage)
            applied_changes.append(change)
        elif kind == "mortgage_payoff":
            liabilities = effective.get("liabilities") if isinstance(effective.get("liabilities"), dict) else {}
            mortgage_balance = _number(liabilities.get("mortgage_balance"))
            if mortgage_balance is None or mortgage_balance <= 0:
                unsupported_changes.append({**change, "reason": "mortgage_balance_required"})
                continue
            effective.setdefault("one_off_expenses", []).append(
                {
                    "label": "Mortgage payoff",
                    "amount": mortgage_balance,
                    "year": current_year,
                }
            )
            liabilities["mortgage_balance"] = 0.0
            housing = effective.get("expenses", {}).get("housing")
            if isinstance(housing, dict):
                housing["monthly_principal_interest"] = 0.0
            applied_changes.append(change)
        elif kind in {
            "inflation_shock_unspecified",
            "recurring_support_unspecified",
            "expense_increase_unspecified",
            "solve_for_investment_capacity",
            "solve_for_maximum_spending",
            "scenario_parameter_unspecified",
        }:
            unsupported_changes.append({**change, "reason": "scenario_parameter_or_solver_required"})
        else:
            unsupported_changes.append({**change, "reason": "unsupported_scenario_change_kind"})

    simulation_mode = "monte_carlo" if monte_carlo_paths is not None else "deterministic"
    num_simulations = monte_carlo_paths if monte_carlo_paths is not None else 1
    effective.setdefault("simulation_config", {}).update(
        {
            "mode": simulation_mode,
            "num_simulations": num_simulations,
            "seed": 42,
        }
    )
    return {
        "requested_input": {
            "requested_metrics": decision.requested_metrics,
            "scenario_summary": decision.scenario_summary,
            "scenario_rationale": decision.scenario_rationale,
            "scenario_changes": decision.scenario_changes,
            "monte_carlo_paths": monte_carlo_paths,
        },
        "effective_input": effective,
        "tool_args": {
            "simulation_mode": simulation_mode,
            "num_simulations": num_simulations,
            "seed": 42,
            "payload_override": _payload_delta(base_payload, effective),
        },
        "applied_changes": applied_changes,
        "unsupported_changes": unsupported_changes,
        "warnings": warnings,
    }


def pending_cashflow_decision(client_file: Dict[str, Any]) -> Optional[CashflowCapabilityDecision]:
    """Load a pending scenario request from the active consultation checkpoint."""

    checkpoint = client_file.get("active_consultation_checkpoint") if isinstance(client_file, dict) else None
    pending = checkpoint.get("pending_cashflow_request") if isinstance(checkpoint, dict) else None
    if not isinstance(pending, dict) or not pending.get("requested"):
        return None
    return CashflowCapabilityDecision.from_dict(pending)


def _decision_reason(*, action: str) -> str:
    if action == "answer_from_snapshot":
        return "The request can be answered from known Client File values."
    return "The request requires a cash-flow projection rather than a static lookup."


def cashflow_required_metrics() -> list[str]:
    """Return the common minimum metric contract for every projection."""

    return list(CASHFLOW_REQUIRED_METRICS)



def _canonical_client_values(client_file: Dict[str, Any]) -> Dict[str, Any]:
    return CashflowClientInput.from_client_file(client_file).canonical_values()


def _cashflow_fact_sources(
    client_file: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    committed_container = (
        client_file.get("facts") if isinstance(client_file.get("facts"), dict) else {}
    )
    # ``client_state_view.v1`` stores KnowledgeUpdater's snapshot_data at
    # ``facts.knowledge_summary``.  Keep that mapper-owned object out of the
    # committed scalar fact layer so stale/default mapper values cannot beat a
    # directly committed fact for age, income, or spending.
    committed_raw = {
        key: value
        for key, value in committed_container.items()
        if key not in {"knowledge_summary", "diagnosis_summary"}
    }
    typed_rows = (
        client_file.get("typed_facts")
        if isinstance(client_file.get("typed_facts"), list)
        else []
    )
    typed_values = _typed_cashflow_fact_values(typed_rows)
    if typed_rows:
        from client_file.financial_position import resolve_financial_position

        position = resolve_financial_position(
            client_id=str(client_file.get("client_id") or "client"),
            client_file=client_file,
        )
        account_types = {
            str(item.get("entity_id")): str(item.get("account_type") or "")
            for item in position.get("accounts") or []
            if isinstance(item, dict)
        }
        totals = {"taxable_brokerage": 0.0, "retirement_accounts": 0.0, "cash": 0.0, "college_529": 0.0, "home_value": 0.0}
        type_field = {"taxable_brokerage": "taxable_brokerage", "retirement": "retirement_accounts", "cash": "cash", "education": "college_529", "real_estate": "home_value"}
        for operand in position.get("assets") or []:
            if not isinstance(operand, dict):
                continue
            field = type_field.get(account_types.get(str(operand.get("id"))))
            amount = _number(operand.get("value"))
            if field and amount is not None:
                totals[field] += amount
        for field, amount in totals.items():
            if amount:
                typed_values[field] = amount
        if position.get("conflicts"):
            typed_values.setdefault("__typed_errors__", []).extend(
                str(item.get("code") or "financial_position_conflict")
                for item in position["conflicts"] if isinstance(item, dict)
            )
    structured_raw = (
        client_file.get("structured_facts")
        if isinstance(client_file.get("structured_facts"), dict)
        else {}
    )
    top_level_cashflow_state = (
        client_file.get("cashflow_state")
        if isinstance(client_file.get("cashflow_state"), dict)
        else {}
    )
    # Mapper/model-generated Knowledge state is deliberately non-authoritative.
    # A direct legacy cashflow_state may provide optional account detail only;
    # canonical scalar values overwrite it in ``to_engine_payload``.
    cashflow_state = copy.deepcopy(top_level_cashflow_state)
    return (
        typed_values or normalize_fact_keys(_legacy_fact_values(committed_raw)),
        {} if typed_values else normalize_fact_keys(_legacy_fact_values(structured_raw)),
        {},
        cashflow_state,
    )


def _typed_cashflow_fact_values(rows: list[Any]) -> Dict[str, Any]:
    values: Dict[str, Any] = {}
    errors: list[str] = []
    error_details: Dict[str, Dict[str, Any]] = {}
    authority_by_field: Dict[str, Optional[str]] = {}
    income_streams: list[tuple[float, Optional[str]]] = []
    spending_components: list[tuple[float, Optional[str]]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
        authority = provenance.get("authority")
        if authority not in {None, "client_confirmed"}:
            continue
        envelope = row.get("value") if isinstance(row.get("value"), dict) else {}
        entity_id = str(row.get("entity_id") or envelope.get("field") or "")
        entity_type = str(envelope.get("entity_type") or "scalar_fact")
        if entity_type == "scalar_fact" and entity_id:
            values[entity_id] = envelope.get("value")
            authority_by_field[entity_id] = authority
            continue
        if entity_type == "income_stream":
            annualized, error = _annualized_typed_amount(envelope, kind="income")
            if error:
                errors.append(error)
            elif annualized is not None:
                income_streams.append((annualized, authority))
            continue
        if entity_type in {"spending_component", "expense", "expense_stream"}:
            annualized, error = _annualized_typed_amount(envelope, kind="spending")
            if error:
                errors.append(error)
            elif annualized is not None:
                spending_components.append((annualized, authority))
            continue
        if entity_type == "account":
            account_kind = _canonical_account_kind(
                str(envelope.get("account_type") or entity_id).lower()
            )
            holdings = envelope.get("dollar_valued_holdings")
            stated_balance = _number(envelope.get("total_balance"))
            holding_amounts = (
                [_number(item) for item in holdings.values()]
                if isinstance(holdings, dict)
                else []
            )
            holdings_total = (
                sum(item for item in holding_amounts if item is not None)
                if holding_amounts and all(item is not None for item in holding_amounts)
                else None
            )
            balance = stated_balance if stated_balance is not None else holdings_total
            if (
                stated_balance is not None
                and holdings_total is not None
                and _outside_rounding_tolerance(stated_balance, holdings_total)
            ):
                code = _conflict_code("account_total", account_kind=account_kind)
                errors.append(code)
                error_details[code] = {
                    "account_kind": account_kind,
                    "stated_total": stated_balance,
                    "components_total": holdings_total,
                }
            weights = envelope.get("percentage_allocation_weights")
            if isinstance(weights, dict) and weights and isinstance(holdings, dict) and holdings:
                explicit = _normalized_allocation_weights(weights)
                derived = _normalized_allocation_weights(holdings)
                if explicit and derived and any(
                    abs(explicit.get(name, 0.0) - derived.get(name, 0.0)) > 0.005
                    for name in explicit.keys() | derived.keys()
                ):
                    code = _conflict_code("allocation_weights", account_kind=account_kind)
                    errors.append(code)
                    error_details[code] = {
                        "account_kind": account_kind,
                        "explicit_weights": explicit,
                        "derived_weights": derived,
                    }
            if account_kind == "brokerage" and balance is not None:
                values["taxable_brokerage"] = float(values.get("taxable_brokerage") or 0) + balance
                authority_by_field["taxable_brokerage"] = authority
                if isinstance(weights, dict) and weights:
                    values["brokerage_asset_allocation"] = weights
                    authority_by_field["brokerage_asset_allocation"] = authority
            elif account_kind == "retirement" and balance is not None:
                values["retirement_accounts"] = float(values.get("retirement_accounts") or 0) + balance
                authority_by_field["retirement_accounts"] = authority
            elif account_kind == "bank" and balance is not None:
                values["cash"] = float(values.get("cash") or 0) + balance
                authority_by_field["cash"] = authority

    normalized = normalize_fact_keys(values)
    normalized_authorities: Dict[str, Optional[str]] = {}
    for raw_field, authority in authority_by_field.items():
        normalized_field = next(iter(normalize_fact_keys({raw_field: 0})), raw_field)
        normalized_authorities[normalized_field] = authority

    if income_streams:
        streams_total = sum(amount for amount, _authority in income_streams)
        aggregate = _first_number(normalized, "annual_income")
        if aggregate is None:
            normalized["annual_income"] = streams_total
            normalized_authorities["annual_income"] = (
                "client_confirmed"
                if all(authority == "client_confirmed" for _amount, authority in income_streams)
                else None
            )
        elif _outside_rounding_tolerance(aggregate, streams_total):
            code = _conflict_code("household_income")
            errors.append(code)
            error_details[code] = {
                "aggregate_total": aggregate,
                "components_total": streams_total,
            }
    if spending_components:
        components_total = sum(amount for amount, _authority in spending_components)
        aggregate = _first_number(normalized, "annual_spending")
        if aggregate is None:
            normalized["annual_spending"] = components_total
            normalized_authorities["annual_spending"] = (
                "client_confirmed"
                if all(authority == "client_confirmed" for _amount, authority in spending_components)
                else None
            )
        elif _outside_rounding_tolerance(aggregate, components_total):
            code = _conflict_code("spending_components")
            errors.append(code)
            error_details[code] = {
                "aggregate_total": aggregate,
                "components_total": components_total,
            }
    if not normalized and not errors:
        return {}
    if errors:
        normalized["__typed_errors__"] = sorted(set(errors))
    if error_details:
        normalized["__typed_error_details__"] = error_details
    normalized["__authority_by_field__"] = normalized_authorities
    return normalized


def _annualized_typed_amount(
    envelope: Dict[str, Any],
    *,
    kind: str,
) -> tuple[Optional[float], Optional[str]]:
    amount = _number(envelope.get("amount"))
    basis = str(envelope.get("basis") or "annual").lower()
    if amount is None:
        return None, None
    if basis == "annual":
        return amount, None
    if basis == "monthly":
        return amount * 12.0, None
    if basis == "weekly":
        weeks = _number(envelope.get("paid_weeks_per_year"))
        if weeks is None:
            return None, f"weekly_{kind}_weeks_missing"
        return amount * weeks, None
    if basis == "hourly":
        hours = _number(envelope.get("hours_per_week"))
        weeks = _number(envelope.get("paid_weeks_per_year"))
        if hours is None or weeks is None:
            return None, f"hourly_{kind}_hours_missing"
        return amount * hours * weeks, None
    return None, f"unsupported_{kind}_basis:{basis}"


def _outside_rounding_tolerance(left: float, right: float) -> bool:
    return abs(left - right) > max(1.0, 0.005 * max(abs(left), abs(right)))


def _conflict_code(conflict_class: str, **values: str) -> str:
    return str(CONFLICT_PRECEDENCE[conflict_class]["error_code"]).format(**values)


def _canonical_account_kind(raw_kind: str) -> str:
    if "broker" in raw_kind:
        return "brokerage"
    if "retire" in raw_kind:
        return "retirement"
    if "cash" in raw_kind or "bank" in raw_kind:
        return "bank"
    if "education" in raw_kind or "529" in raw_kind:
        return "education"
    return raw_kind.replace(" ", "_") or "account"


def _normalized_allocation_weights(allocation: Dict[str, Any]) -> Dict[str, float]:
    canonical = _canonicalize_stated_asset_allocation(allocation)
    total = sum(float(value) for value in canonical.values())
    if total <= 0:
        return {}
    return {name: float(value) / total for name, value in canonical.items()}


def _legacy_fact_values(facts: Dict[str, Any]) -> Dict[str, Any]:
    """Temporary bounded adapter; never recursively hoist arbitrary nested keys."""

    allowed_categories = {
        "assets",
        "goals",
        "household",
        "income",
        "liabilities",
        "profile",
        "rates",
        "spending",
        "wealth",
    }
    flattened: Dict[str, Any] = {}
    for key, value in facts.items():
        if not isinstance(value, dict):
            flattened[key] = value
        elif "allocation" in str(key):
            flattened[key] = value
        elif key in allowed_categories:
            for nested_key, nested_value in value.items():
                if not isinstance(nested_value, dict) or "allocation" in str(nested_key):
                    flattened.setdefault(str(nested_key), nested_value)
    return flattened


def _cashflow_state_pool_balance(accounts: Dict[str, Any], account_kind: str) -> Optional[float]:
    pool = accounts.get(account_kind)
    if isinstance(pool, dict):
        pool = [pool]
    if not isinstance(pool, list):
        return None
    balances = [
        _number(item.get("balance"))
        for item in pool
        if isinstance(item, dict) and _number(item.get("balance")) is not None
    ]
    return sum(value for value in balances if value is not None) if balances else None


def _reconcile_cashflow_account_pool(
    pool: Any,
    authoritative_balance: float,
    *,
    label: str,
) -> tuple[list[Dict[str, Any]], bool, Optional[Dict[str, Any]]]:
    """Preserve stated balances and fill only sleeves with no stated balance."""

    entries = [copy.deepcopy(item) for item in pool if isinstance(item, dict)] if isinstance(pool, list) else []
    if not entries:
        return [{"label": label, "balance": authoritative_balance}], False, None

    source_balances = [_number(item.get("balance")) for item in entries]
    known_total = sum(value for value in source_balances if value is not None)
    missing_indexes = [
        index for index, source_balance in enumerate(source_balances)
        if source_balance is None
    ]
    if missing_indexes:
        remainder = authoritative_balance - known_total
        if remainder < -max(1.0, 0.005 * max(authoritative_balance, known_total)):
            return entries, False, {
                "stated_total": authoritative_balance,
                "components_total": known_total,
            }
        per_sleeve = max(0.0, remainder) / len(missing_indexes)
        for index in missing_indexes:
            entries[index]["balance"] = per_sleeve
        return entries, True, None

    if _outside_rounding_tolerance(authoritative_balance, known_total):
        return entries, False, {
            "stated_total": authoritative_balance,
            "components_total": known_total,
        }
    return entries, False, None


_DEFAULT_EDUCATION_ALLOCATION = {
    "US Equity": 70.0,
    "Global Investment Grade Corporate Bond": 30.0,
}


def _apply_default_education_allocation_if_needed(
    accounts: Dict[str, Any],
    account_kind: str,
    assumptions: list[str],
) -> bool:
    """Fill an empty education sleeve with a model-only default mix.

    Used only when the 529 balance is funded but no exact allocation was
    provided, so college Monte Carlo is not blocked by an empty sleeve while
    brokerage/retirement already have exact weights.
    """

    if account_kind != "education":
        return False
    pool = accounts.get("education")
    if not isinstance(pool, list) or not pool:
        return False
    changed = False
    for item in pool:
        if not isinstance(item, dict):
            continue
        if (_number(item.get("balance")) or 0.0) <= 0:
            continue
        allocation = (
            item.get("asset_allocation")
            if isinstance(item.get("asset_allocation"), dict)
            else item.get("allocation")
        )
        if cashflow_asset_allocation_is_exact(allocation):
            continue
        item["allocation"] = dict(_DEFAULT_EDUCATION_ALLOCATION)
        item.pop("asset_allocation", None)
        changed = True
    if not changed:
        return False
    if not _pool_has_asset_allocation(accounts["education"]):
        return False
    assumptions.append(
        "Education-account allocation was not provided; used a model default "
        "70/30 US Equity / Global Investment Grade Corporate Bond mix for "
        "projection only (not recorded as a confirmed Client File fact)."
    )
    return True


def _apply_stated_pool_allocation(
    pool: Any,
    allocation: Optional[Dict[str, Any]],
) -> None:
    """Apply a confirmed aggregate allocation to each funded account sleeve.

    The conversation layer persists account allocations as canonical Client File
    facts. The cashflow adapter owns the deterministic shape conversion into the
    engine's per-account ``asset_allocation`` field.
    """

    if not isinstance(pool, list) or not isinstance(allocation, dict) or not allocation:
        return
    canonical_allocation = _canonicalize_stated_asset_allocation(allocation)
    for item in pool:
        if isinstance(item, dict):
            item["asset_allocation"] = copy.deepcopy(canonical_allocation)
            item.pop("allocation", None)


def _canonicalize_stated_asset_allocation(
    allocation: Dict[str, Any],
) -> Dict[str, Any]:
    """Map common conversational asset labels to LifeModel asset classes."""

    aliases = {
        "stocks_pct": "US Equity",
        "bonds_pct": "Global Investment Grade Corporate Bond",
        "cash_pct": "Cash",
        "us_stocks": "US Equity",
        "us_stock": "US Equity",
        "us_equity": "US Equity",
        "us_equity_pct": "US Equity",
        "u_s_equity_pct": "US Equity",
        "bonds": "Global Investment Grade Corporate Bond",
        "bond": "Global Investment Grade Corporate Bond",
        "international_equity": "Dev. Europe ex UK Equity",
        "international_equity_pct": "Dev. Europe ex UK Equity",
        "international_stocks": "Dev. Europe ex UK Equity",
        "developed_international_stocks": "Dev. Europe ex UK Equity",
        "intl_developed_stocks": "Dev. Europe ex UK Equity",
        "international_developed_stocks": "Dev. Europe ex UK Equity",
        "developed_international_equity": "Dev. Europe ex UK Equity",
        "investment_grade_bonds": "Global Investment Grade Corporate Bond",
        "investment_grade_bond": "Global Investment Grade Corporate Bond",
        "global_investment_grade_bonds": "Global Investment Grade Corporate Bond",
        "global_investment_grade_corporate_bond": (
            "Global Investment Grade Corporate Bond"
        ),
        "cash": "Cash",
        "diversified_equities": "US Equity",
        "diversified_equity_pct": "US Equity",
        "diversified_equity_index_funds": "US Equity",
        "employer_stock": "US Equity",
        "employer_stock_from_rsu": "US Equity",
        "employer_stock_from_vested_rsus": "US Equity",
    }
    canonical: Dict[str, Any] = {}
    percentage_named = {
        str(name)
        for name in allocation
        if str(name).lower().endswith(("_pct", "_percentage", "_weight"))
    }
    canonical_asset_names = {
        "Cash",
        "US Treasury",
        "Global Investment Grade Corporate Bond",
        "Global High Yield Bond BB-B",
        "Emerging Market Local Currency Government Bonds",
        "Emerging Market Hard Currency Debt",
        "US Equity",
        "Dev. Europe ex UK Equity",
        "Japan Equity",
        "China Equity",
        "India Equity",
        "Commodities",
        "Gold",
        "Hedge Funds",
        "Bitcoin",
    }
    for raw_name, weight in allocation.items():
        if (
            percentage_named
            and str(raw_name) not in percentage_named
            and str(raw_name) not in canonical_asset_names
        ):
            continue
        normalized_name = re.sub(
            r"_+",
            "_",
            re.sub(r"[^a-z0-9]+", "_", str(raw_name).strip().lower()),
        ).strip("_")
        canonical_name = aliases.get(normalized_name, str(raw_name).strip())
        numeric_weight = _number(weight)
        if canonical_name and numeric_weight is not None:
            canonical[canonical_name] = (
                (_number(canonical.get(canonical_name)) or 0.0) + numeric_weight
            )
    total = sum(float(weight) for weight in canonical.values())
    if total > 0 and not (
        abs(total - 1.0) <= 0.001 or abs(total - 100.0) <= 0.001
    ):
        canonical = {
            name: float(weight) / total
            for name, weight in canonical.items()
        }
    return canonical


def _pool_has_asset_allocation(pool: list[Dict[str, Any]]) -> bool:
    """Return true when every funded sleeve has an exact asset-class allocation.

    Expected return is optional here: LifeModel can derive sleeve growth from the
    capital-market assumptions for exact allocations. Requiring a separate
    expected_return caused false missing-allocation blocks when Client File
    already had exact weights.
    """

    funded = [
        item
        for item in pool
        if isinstance(item, dict) and (_number(item.get("balance")) or 0.0) > 0
    ]
    return bool(funded) and all(
        cashflow_asset_allocation_is_exact(
            item.get("asset_allocation")
            if isinstance(item.get("asset_allocation"), dict)
            else item.get("allocation")
        )
        for item in funded
    )


def cashflow_asset_allocation_is_exact(value: Any) -> bool:
    """Validate an exact decimal-or-percentage allocation without normalizing it."""

    if not isinstance(value, dict) or not value:
        return False
    weights = [_number(raw_weight) for raw_weight in value.values()]
    if any(weight is None or weight < 0 for weight in weights):
        return False
    total = sum(weight for weight in weights if weight is not None)
    return abs(total - 1.0) <= 0.001 or abs(total - 100.0) <= 0.001


def _merge_cashflow_state(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-merge two mapper-owned cash-flow states without losing arrays."""

    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_cashflow_state(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _cashflow_client_input_errors(values: Dict[str, Optional[float]]) -> list[str]:
    errors: list[str] = []
    bounds = {
        "current_age": (0, 120),
        "spouse_age": (0, 120),
        "retirement_age": (0, 120),
        "life_expectancy": (0, 130),
        "annual_income": (0, None),
        "annual_spending": (0, None),
        "retirement_contribution_pct": (0, 100),
        "monthly_retirement_contribution": (0, None),
        "cash_balance": (0, None),
        "brokerage_balance": (0, None),
        "retirement_balance": (0, None),
        "education_balance": (0, None),
        "mortgage_balance": (0, None),
        "home_value": (0, None),
        "home_tax_basis": (0, None),
        "home_appreciation_rate": (-100, 100),
        "mortgage_interest_rate": (0, 100),
        "mortgage_remaining_term_years": (0, 100),
        "mortgage_monthly_payment": (0, None),
        "education_goal_amount": (0, None),
        "education_horizon_years": (0, None),
        "emergency_reserve_months": (0, None),
    }
    for field_name, (minimum, maximum) in bounds.items():
        value = values.get(field_name)
        if value is None:
            continue
        if value < minimum or (maximum is not None and value > maximum):
            errors.append(f"invalid_input:{field_name}")
    if (
        values.get("education_goal_amount") is not None
        and values.get("education_horizon_years") is None
    ):
        errors.append("incomplete_input:education_goal_timing")
    if (
        values.get("education_horizon_years") is not None
        and values.get("education_goal_amount") is None
    ):
        errors.append("incomplete_input:education_goal_amount")
    return errors


def _set_or_remove(target: Dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        target.pop(key, None)
    else:
        target[key] = value


def _next_input_question(
    field_name: str,
    error_details: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    details = (error_details or {}).get(field_name, {})
    if field_name.startswith("account_total_conflict:"):
        account_kind = field_name.split(":", 1)[1].replace("_", " ")
        return (
            f"You've told me the {account_kind} total is "
            f"{_format_currency(details.get('stated_total'))}, and the holdings you listed "
            f"add to {_format_currency(details.get('components_total'))}. Which should I use?"
        )
    if field_name.startswith("allocation_weights_conflict:"):
        account_kind = field_name.split(":", 1)[1].replace("_", " ")
        return (
            f"You've given {account_kind} allocation weights of "
            f"{_format_weights(details.get('explicit_weights'))}, while the listed holdings imply "
            f"{_format_weights(details.get('derived_weights'))}. Which allocation should I use?"
        )
    if field_name == "household_income_conflict":
        return (
            "You've told me household income is "
            f"{_format_currency(details.get('aggregate_total'))}, while the individual incomes "
            f"add to {_format_currency(details.get('components_total'))}. Which should I use?"
        )
    if field_name == "spending_component_conflict":
        return (
            "You've told me annual household spending is "
            f"{_format_currency(details.get('aggregate_total'))}, while the spending components "
            f"add to {_format_currency(details.get('components_total'))}. Which should I use?"
        )
    questions = {
        "current_age": "What is your current age?",
        "retirement_age": "At what age would you like to retire?",
        "annual_income": "What is your current annual household income?",
        "annual_spending": "About how much does your household spend per year?",
        "starting_assets": "What are the current balances of your cash and investment accounts?",
        "scenario_target_timing": "When should the modeled expense occur?",
        "scenario_amount_or_change": "What amount or financial change should I model?",
        "inflation_assumption": "What inflation or spending-growth assumption should I use?",
        "annual_support_amount": "About how much support per year should I model?",
        "expense_increase_amount": "How much should I increase the modeled expenses?",
        "incomplete_input:education_goal_timing": "When should the education cost occur?",
        "incomplete_input:education_goal_amount": "What education cost should the model use?",
        "incomplete_input:spouse_age": "What is your spouse's current age?",
    }
    mortgage_questions = {
        "home_value": "What is the home's current market value?",
        "home_appreciation_rate": "What annual home-value growth assumption should the model use?",
        "mortgage_interest_rate": "What is the mortgage's annual interest rate in percentage points?",
        "mortgage_remaining_term_years": "How many years remain on the mortgage?",
        "mortgage_type": "Is the existing mortgage fixed-rate, adjustable-rate, interest-only, or balloon?",
        "annual_spending_includes_mortgage": "Does confirmed annual spending already include mortgage principal and interest?",
    }
    if field_name.startswith("missing_mortgage_input:"):
        mortgage_field = field_name.split(":", 1)[1]
        return mortgage_questions.get(
            mortgage_field,
            f"What should AWM use for {mortgage_field.replace('_', ' ')}?",
        )
    if field_name.startswith("unsupported_existing_mortgage_type:"):
        return (
            "Opening-position mortgage projections currently support confirmed fixed-rate "
            "loans only."
        )
    if field_name.startswith("missing_asset_allocation:"):
        account_kind = field_name.split(":", 1)[1].replace("_", " ")
        return (
            f"What confirmed asset allocation should the Monte Carlo model use for the "
            f"funded {account_kind} account?"
        )
    if field_name.startswith("unsupported_cashflow_action:") or field_name == "missing_cashflow_action":
        return "Please retry with a valid cash-flow model request."
    if field_name.startswith("invalid_input:"):
        invalid_name = field_name.split(":", 1)[1].replace("_", " ")
        return f"What valid value should AWM use for {invalid_name}?"
    return questions.get(field_name, f"What should AWM use for {field_name.replace('_', ' ')}?")


def _format_currency(value: Any) -> str:
    number = _number(value)
    return f"${number:,.0f}" if number is not None else "the stated amount"


def _format_weights(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "the stated weights"
    return ", ".join(
        f"{name} {float(weight) * 100:.1f}%"
        for name, weight in sorted(value.items())
    )


def _target_year(change: Dict[str, Any], *, current_year: int) -> Optional[int]:
    target_year = _number(change.get("target_year"))
    if target_year is not None:
        return int(target_year)
    horizon = _number(change.get("horizon_years"))
    return current_year + int(horizon) if horizon is not None else None


def _bounded_income_events(
    *,
    label: str,
    person: str,
    target_year: int,
    reduction_percentage: float,
    duration_years: Optional[int],
    duration_months: Optional[int],
) -> tuple[list[Dict[str, Any]], bool]:
    """Compile a start-of-year income disruption into annual model windows."""

    if duration_years is not None:
        return (
            [
                {
                    "label": label,
                    "person": person,
                    "start_year": target_year,
                    "end_year": target_year + duration_years - 1,
                    "income_multiplier": 1.0 - reduction_percentage,
                }
            ],
            False,
        )

    total_months = int(duration_months or 0)
    full_years, remaining_months = divmod(total_months, 12)
    events: list[Dict[str, Any]] = []
    if full_years:
        events.append(
            {
                "label": label,
                "person": person,
                "start_year": target_year,
                "end_year": target_year + full_years - 1,
                "income_multiplier": 1.0 - reduction_percentage,
            }
        )
    if remaining_months:
        events.append(
            {
                "label": f"{label} (prorated final year)",
                "person": person,
                "start_year": target_year + full_years,
                "end_year": target_year + full_years,
                "income_multiplier": (
                    1.0 - reduction_percentage * remaining_months / 12.0
                ),
            }
        )
    return events, bool(remaining_months)


def _payload_delta(base: Any, effective: Any) -> Any:
    if isinstance(base, dict) and isinstance(effective, dict):
        delta: Dict[str, Any] = {}
        for key, value in effective.items():
            if key not in base:
                delta[key] = copy.deepcopy(value)
                continue
            nested = _payload_delta(base[key], value)
            if nested not in ({}, None):
                delta[key] = nested
        return delta
    if base != effective:
        return copy.deepcopy(effective)
    return None


def _first_number(source: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        value = _number(source.get(key))
        if value is not None:
            return value
    return None


def _coalesce_number(*values: Optional[float]) -> Optional[float]:
    return next((value for value in values if value is not None), None)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        try:
            number = float(value.replace(",", "").replace("$", "").strip())
            return number if math.isfinite(number) else None
        except ValueError:
            return None
    return None


def _whole_number(value: Any) -> Optional[int]:
    number = _number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _boolean(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "y", "included", "include"}:
            return True
        if normalized in {"false", "no", "n", "excluded", "exclude"}:
            return False
    return None


def _scenario_record_text(
    value: Any,
    *,
    field_name: str,
    max_length: int,
    validation_errors: list[str],
) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        validation_errors.append(f"{field_name}_must_be_a_string")
        return None
    normalized = " ".join(value.split())
    if not normalized:
        validation_errors.append(f"{field_name}_must_not_be_empty")
        return None
    if len(normalized) > max_length:
        validation_errors.append(f"{field_name}_too_long")
        return None
    return normalized


def _confidence(value: Any) -> float:
    try:
        number = float(value)
        return min(1.0, max(0.0, number)) if math.isfinite(number) else 0.0
    except (TypeError, ValueError):
        return 0.0
