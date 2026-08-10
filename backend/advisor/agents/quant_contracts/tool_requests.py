from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from advisor.tools.deterministic_tools.run_cashflow_projection.scenarios import (
    CASHFLOW_PUBLIC_CHANGE_KINDS,
)
from advisor.tools.deterministic_tools.run_cashflow_projection.tool import (
    DEFAULT_REPORT_COLUMNS,
    DETAIL_REPORT_COLUMNS,
    DETAIL_REPORT_GROUP_COLUMNS,
)


class CashflowScenarioChangeContract(BaseModel):
    """One compiler-supported scenario change.

    Optional fields become required-but-nullable in the SDK schema.  That is the
    representation required by OpenAI strict function schemas, while local callers
    may still omit irrelevant fields before normalization.
    """

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    kind: str
    amount: Optional[float] = Field(default=None, gt=0)
    value: Optional[float] = None
    percentage: Optional[float] = Field(default=None, ge=0, le=1)
    target_year: Optional[int] = Field(default=None, ge=1900, le=2200)
    horizon_years: Optional[int] = Field(default=None, ge=0, le=100)
    duration_years: Optional[int] = Field(default=None, ge=1, le=100)
    duration_months: Optional[int] = Field(default=None, ge=1, le=1200)
    person: Optional[Literal["primary", "spouse"]] = None
    account_type: Optional[Literal["bank", "brokerage", "retirement", "education"]] = None
    label: Optional[str] = Field(default=None, min_length=1, max_length=160)
    unit: Optional[Literal["USD"]] = None

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in CASHFLOW_PUBLIC_CHANGE_KINDS:
            raise ValueError(f"unsupported cash-flow scenario change: {value}")
        return value

    @model_validator(mode="after")
    def _required_fields_for_kind(self) -> "CashflowScenarioChangeContract":
        amount_kinds = {
            "one_off_expense",
            "recurring_expense_increase",
            "recurring_investment_contribution",
        }
        if self.kind in amount_kinds and self.amount is None:
            raise ValueError(f"{self.kind} requires amount")
        if self.kind in amount_kinds and self.unit != "USD":
            raise ValueError(f"{self.kind} requires unit=USD")
        if self.kind in {
            "retirement_age",
            "retirement_age_delta",
            "life_expectancy",
            "spouse_retirement_age",
        } and self.value is None:
            raise ValueError(f"{self.kind} requires value")
        if self.kind in {
            "account_balance_haircut",
            "temporary_income_reduction",
            "temporary_inflation_override",
        } and self.percentage is None:
            raise ValueError(f"{self.kind} requires percentage")
        if self.kind == "account_balance_haircut" and self.account_type is None:
            raise ValueError("account_balance_haircut requires account_type")
        timed_kinds = {
            "one_off_expense",
            "future_income_loss",
            "temporary_income_reduction",
            "temporary_inflation_override",
            "recurring_investment_contribution",
        }
        if (
            self.kind in timed_kinds
            and self.target_year is None
            and self.horizon_years is None
        ):
            raise ValueError(f"{self.kind} requires target_year or horizon_years")
        if self.kind in {"future_income_loss", "temporary_income_reduction"}:
            supplied = sum(
                value is not None
                for value in (self.duration_years, self.duration_months)
            )
            if supplied != 1:
                raise ValueError(
                    f"{self.kind} requires exactly one of duration_years or duration_months"
                )
        elif (
            self.kind in timed_kinds - {"one_off_expense"}
            and self.duration_years is None
        ):
            raise ValueError(f"{self.kind} requires duration_years")
        if self.kind == "spouse_retirement_age" and self.person not in {None, "spouse"}:
            raise ValueError("spouse_retirement_age must target spouse")
        return self


class CashflowScenarioToolContract(BaseModel):
    """Agent-selected baseline changes consumed by the deterministic compiler."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    # Required by the public SDK schema. Optional here only so legacy internal
    # callers can continue to submit type-free structured changes directly.
    scenario_summary: Optional[str] = Field(default=None, min_length=1, max_length=160)
    scenario_rationale: Optional[str] = Field(default=None, min_length=1, max_length=600)
    scenario_changes: List[CashflowScenarioChangeContract] = Field(default_factory=list, max_length=20)


class CashflowPublicFactRefContract(BaseModel):
    """Opaque reference to one session-bound, server-resolved public fact."""

    model_config = ConfigDict(extra="forbid", strict=True)

    variable_key: Literal["social_security_taxable_maximum"]
    session_fact_id: str = Field(
        pattern=r"^session-public-fact:[a-f0-9]{32}$",
    )


class CashflowAgentToolRequest(BaseModel):
    """Strict SDK arguments for the cash-flow specialist tool."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    question: str = Field(min_length=1, max_length=1000)
    allocation_analysis_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=160,
    )
    allocation_analysis_ids: Optional[List[str]] = Field(
        default=None,
        min_length=1,
        max_length=10,
    )
    monte_carlo_paths: Optional[int] = Field(default=None, ge=10, le=1000)
    detail_report_groups: Optional[
        List[
            Literal[
                "income",
                "spending",
                "taxes",
                "withdrawals",
                "account_balances",
                "mortgage",
            ]
        ]
    ] = Field(default=None, min_length=1, max_length=6)
    calendar_years: Optional[List[int]] = Field(
        default=None,
        min_length=1,
        max_length=12,
    )
    detail_columns: Optional[List[str]] = Field(
        default=None,
        min_length=1,
        max_length=20,
    )
    public_fact_refs: Optional[List[CashflowPublicFactRefContract]] = Field(
        default=None,
        min_length=1,
        max_length=1,
    )
    scenario: CashflowScenarioToolContract

    @model_validator(mode="after")
    def _allocation_link_contract(self) -> "CashflowAgentToolRequest":
        if self.allocation_analysis_id and self.allocation_analysis_ids:
            raise ValueError(
                "use allocation_analysis_id for one allocation or "
                "allocation_analysis_ids for multiple allocations, not both"
            )
        if self.allocation_analysis_ids and len(set(self.allocation_analysis_ids)) != len(
            self.allocation_analysis_ids
        ):
            raise ValueError("allocation_analysis_ids must be unique")
        if self.detail_report_groups and len(set(self.detail_report_groups)) != len(
            self.detail_report_groups
        ):
            raise ValueError("detail_report_groups must be unique")
        if self.calendar_years:
            if len(set(self.calendar_years)) != len(self.calendar_years):
                raise ValueError("calendar_years must be unique")
            if any(year < 1900 or year > 2200 for year in self.calendar_years):
                raise ValueError("calendar_years must be between 1900 and 2200")
        if self.detail_columns:
            if self.calendar_years is None:
                raise ValueError("detail_columns require calendar_years")
            if len(set(self.detail_columns)) != len(self.detail_columns):
                raise ValueError("detail_columns must be unique")
            unsupported = [
                column
                for column in self.detail_columns
                if column not in DETAIL_REPORT_COLUMNS
            ]
            if unsupported:
                raise ValueError(
                    "unsupported detail_columns: " + ", ".join(unsupported)
                )
            selected_groups = set(self.detail_report_groups or [])
            uncollected_columns = [
                column
                for column in self.detail_columns
                if column not in DEFAULT_REPORT_COLUMNS
                and not any(
                    group in selected_groups and column in columns
                    for group, columns in DETAIL_REPORT_GROUP_COLUMNS.items()
                )
            ]
            if uncollected_columns:
                raise ValueError(
                    "detail_columns require a matching detail_report_group: "
                    + ", ".join(uncollected_columns)
                )
        return self


class SignedAssessmentReferenceContract(BaseModel):
    """Identity-only reference to one durable, versioned signed assessment."""

    model_config = ConfigDict(extra="forbid", strict=True)

    assessment_id: str = Field(min_length=1, max_length=160)
    assessment_version: int = Field(ge=1)
    money_pool_id: str = Field(min_length=1, max_length=160)


class AssetAllocationAgentToolRequest(BaseModel):
    """Strict SDK arguments; mandate values are resolved server-side."""

    model_config = ConfigDict(extra="forbid", strict=True)

    assessment_ref: SignedAssessmentReferenceContract


class CashflowContributionSolverRequest(BaseModel):
    """Strict bounded-search inputs for recurring contributions."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    question: str = Field(min_length=1, max_length=1000)
    objective: Literal["maximum_sustainable", "minimum_for_terminal_goal"]
    target_terminal_value: Optional[float] = Field(default=None, ge=0)
    minimum_success_probability: float = Field(ge=0, le=1)
    minimum_p10_liquidity: float
    maximum_monthly_contribution: float = Field(gt=0, le=1_000_000)
    monthly_tolerance: float = Field(gt=0, le=10_000)
    start_horizon_years: int = Field(ge=0, le=20)
    duration_years: int = Field(ge=1, le=100)
    monte_carlo_paths: Optional[int] = Field(default=None, ge=10, le=1000)
    allocation_analysis_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=160,
    )

    @model_validator(mode="after")
    def _target_matches_objective(self) -> "CashflowContributionSolverRequest":
        if (
            self.objective == "minimum_for_terminal_goal"
            and self.target_terminal_value is None
        ):
            raise ValueError(
                "minimum_for_terminal_goal requires target_terminal_value"
            )
        if (
            self.objective == "maximum_sustainable"
            and self.target_terminal_value is not None
        ):
            raise ValueError(
                "maximum_sustainable requires target_terminal_value=null"
            )
        return self
