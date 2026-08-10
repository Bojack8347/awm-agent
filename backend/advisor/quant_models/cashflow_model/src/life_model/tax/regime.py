# Copyright 2026
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

from dataclasses import dataclass
from typing import Optional, Protocol, Sequence, Union, runtime_checkable

from .federal import FilingStatus, get_federal_standard_deduction
from .tax import TaxesDue, get_income_taxes_due


PayrollIncome = Optional[Union[float, Sequence[float]]]


@dataclass(frozen=True)
class TaxInput:
    """Tax calculation request independent of any particular tax regime."""

    gross_income: float
    deductions: float
    filing_status: FilingStatus
    state: Optional[str] = None
    fica_income: PayrollIncome = None
    ss_benefits: float = 0.0


@runtime_checkable
class TaxRegime(Protocol):
    """Interface for horizontally reusable and jurisdiction-specific tax law."""

    name: str
    federal_policy_year: Optional[int]
    state_policy_year: Optional[int]
    future_policy_changes_modeled: bool
    precision: str

    def standard_deduction(self, filing_status: FilingStatus) -> float:
        """Return the federal/primary standard deduction for this regime."""

    def income_taxes(self, tax_input: TaxInput) -> TaxesDue:
        """Calculate income/payroll taxes for one tax filing unit."""


class CurrentLawTaxRegime:
    """Current US federal plus configured state tax approximation.

    This preserves the legacy model behavior while giving future regimes a
    single object to replace for different jurisdictions, policy years, or
    scenario-driven tax reforms.
    """

    name = "current_law_constant"
    federal_policy_year = 2026
    state_policy_year = 2025
    future_policy_changes_modeled = False
    precision = "planning_approximation_not_tax_preparation"

    def standard_deduction(self, filing_status: FilingStatus) -> float:
        return get_federal_standard_deduction(filing_status)

    def income_taxes(self, tax_input: TaxInput) -> TaxesDue:
        return get_income_taxes_due(
            tax_input.gross_income,
            tax_input.deductions,
            tax_input.filing_status,
            tax_input.state,
            tax_input.fica_income,
            ss_benefits=tax_input.ss_benefits,
        )

    def policy_assumptions(self) -> dict:
        return {
            "tax_law_basis": self.name,
            "federal_policy_year": self.federal_policy_year,
            "new_york_policy_year": self.state_policy_year,
            "future_policy_changes_modeled": self.future_policy_changes_modeled,
            "tax_precision": self.precision,
        }


def coerce_tax_regime(regime: Optional[TaxRegime] = None) -> TaxRegime:
    """Return the provided regime or the default current-law regime."""

    return regime if regime is not None else CurrentLawTaxRegime()
