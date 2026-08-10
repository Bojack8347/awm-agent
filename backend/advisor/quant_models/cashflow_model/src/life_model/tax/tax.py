# Copyright 2023 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

from typing import Optional, Sequence, Union

from .federal import FilingStatus, federal_income_tax, federal_marginal_tax_rate, taxable_social_security
from .state import state_income_tax
from .fica import social_security_tax, medicare_tax


class TaxesDue:
    def __init__(
        self,
        federal: float = 0,
        state: float = 0,
        ss: float = 0,
        medicare: float = 0,
        gross_income: float = 0,
        agi: Optional[float] = None,
        deductions: float = 0,
        taxable_income: Optional[float] = None,
        federal_marginal_rate: float = 0,
        effective_tax_rate: float = 0,
        penalties: float = 0,
    ):
        """Taxes due for the year, split up by type of tax."""
        self.federal = federal
        self.state = state
        self.ss = ss
        self.medicare = medicare
        self.gross_income = gross_income
        self.agi = gross_income if agi is None else agi
        self.deductions = deductions
        self.taxable_income = (
            max(self.agi - deductions, 0)
            if taxable_income is None else taxable_income
        )
        self.federal_marginal_rate = federal_marginal_rate
        self.effective_tax_rate = effective_tax_rate
        # Additional taxes that are not a function of income, such as the
        # 10% early-withdrawal penalty on retirement distributions.
        self.penalties = penalties

    @property
    def total(self) -> float:
        """Total taxes due for the year."""
        return self.federal + self.state + self.ss + self.medicare + self.penalties


class TaxReturnResult(TaxesDue):
    """Planning-friendly tax result with return-level intermediate values.

    The existing model historically returned ``TaxesDue``. This subclass keeps
    the same tax-total API while exposing the values tax-planning workflows need
    to compare scenarios: gross income, AGI, deductions, taxable income,
    marginal rate, and effective rate.
    """


def get_income_taxes_due(
    gross_income: float,
    deductions: float,
    filing_status: FilingStatus,
    state: Optional[str] = None,
    fica_income: Optional[Union[float, Sequence[float]]] = None,
    ss_benefits: float = 0,
) -> TaxesDue:
    """Gets income taxes due for the year for a person or family.

    Args:
        gross_income (float): Income subject to income taxes, excluding
            Social Security benefits.
        deductions (float): Deductions from income.
        filing_status (FilingStatus): Filing status.
        state (str, optional): State tax jurisdiction override. If None, the
            configured model default is used.
        fica_income (float or sequence of floats, optional): Earned income
            subject to FICA and Medicare. Pass one value per worker so the
            Social Security wage-base cap applies per worker rather than to
            the household total. If None, gross_income is used for backwards
            compatibility.
        ss_benefits (float, optional): Social Security benefits received for
            the year. The taxable portion (IRC §86 worksheet) is added to AGI
            for federal tax. Benefits are excluded from the state calculation
            (NY fully exempts Social Security).

    Returns:
        float: Income taxes due.
    """

    income_excluding_ss = max(gross_income, 0)
    taxable_ss = taxable_social_security(ss_benefits, income_excluding_ss, filing_status)
    agi = income_excluding_ss + taxable_ss
    taxable_income = max(agi - deductions, 0)
    tax_federal = federal_income_tax(taxable_income, filing_status)

    # State handling uses AGI as the current resident-tax proxy. NY-specific
    # additions, subtractions, and credits are modeled separately in state.py.
    # Social Security benefits are excluded: NY does not tax them.
    tax_state = state_income_tax(income_excluding_ss, filing_status, state)

    # FICA taxes are based on earned wage income, not retirement withdrawals.
    # The Social Security wage-base cap is per worker, so each worker's wages
    # are capped separately; the additional Medicare tax threshold applies to
    # the household total per the filing status.
    if fica_income is None:
        payroll_incomes = [gross_income]
    elif isinstance(fica_income, (int, float)):
        payroll_incomes = [fica_income]
    else:
        payroll_incomes = list(fica_income)
    tax_ss = sum(social_security_tax(income) for income in payroll_incomes)
    tax_medicare = medicare_tax(sum(payroll_incomes), filing_status)
    total_tax = tax_federal + tax_state + tax_ss + tax_medicare
    effective_tax_rate = (total_tax / agi * 100) if agi > 0 else 0.0

    return TaxReturnResult(
        tax_federal,
        tax_state,
        tax_ss,
        tax_medicare,
        gross_income=gross_income,
        agi=agi,
        deductions=deductions,
        taxable_income=taxable_income,
        federal_marginal_rate=federal_marginal_tax_rate(taxable_income, filing_status),
        effective_tax_rate=effective_tax_rate,
    )
