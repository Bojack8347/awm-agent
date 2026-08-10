# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

from typing import TYPE_CHECKING
from ..limits import federal_retirement_age, early_withdrawal_penalty_rate
from ..tax.tax import TaxesDue

if TYPE_CHECKING:
    from ..people.person import Person


class TaxCalculationService:
    """Service for handling tax calculations and 401k withdrawal planning"""

    def __init__(self, person: 'Person'):
        self.person = person

    def calculate_pretax_401k_withdrawal_needed(self, total_expenses: float) -> float:
        """Calculate how much needs to be withdrawn from pre-tax 401k accounts

        Args:
            total_expenses: Total expenses including bills and current taxes

        Returns:
            Amount needed from pre-tax 401k (0 if bank balance is sufficient)
        """
        return max(0, total_expenses - self.person.bank_account_balance)

    def calculate_taxes_on_401k_withdrawal(self, withdrawal_amount: float) -> float:
        """Calculate grossed-up withdrawal needed to cover 401k withdrawal taxes.

        Args:
            withdrawal_amount: Amount being withdrawn from pre-tax 401k

        Returns:
            Additional withdrawal amount needed to cover the tax caused by the
            base withdrawal. This approximates the recursive tax-on-tax effect.
        """
        if withdrawal_amount <= 0:
            return 0.0

        # Calculate taxes before and after 401k withdrawal
        taxes_before = self.person.get_income_taxes_due()
        taxes_after = self.person.get_income_taxes_due(withdrawal_amount)
        base_tax_increase = taxes_after.total - taxes_before.total

        # Withdrawals before the federal retirement age also incur the
        # early-withdrawal penalty, which must be funded by the same gross-up.
        if self.person.age < federal_retirement_age():
            base_tax_increase += withdrawal_amount * (early_withdrawal_penalty_rate() / 100)

        if base_tax_increase <= 0:
            return 0.0

        observed_marginal_rate = base_tax_increase / withdrawal_amount
        observed_marginal_rate = min(max(observed_marginal_rate, 0.0), 0.95)

        return base_tax_increase / (1 - observed_marginal_rate)

    def calculate_total_401k_withdrawal(self, expenses_without_taxes: float) -> tuple[float, TaxesDue]:
        """Calculate and perform the 401k withdrawal needed including taxes

        Args:
            expenses_without_taxes: Total expenses excluding taxes

        Returns:
            Tuple of (amount actually withdrawn, final_taxes_due). The
            withdrawal request is grossed up for the taxes it triggers, but
            the reported amount is what the accounts could actually fund,
            which may be less when balances run out.
        """
        # Get initial tax calculation
        initial_taxes = self.person.get_income_taxes_due()
        total_expenses_with_taxes = expenses_without_taxes + initial_taxes.total

        # Calculate base withdrawal needed
        base_withdrawal = self.calculate_pretax_401k_withdrawal_needed(total_expenses_with_taxes)

        if base_withdrawal <= 0:
            return 0.0, initial_taxes

        # Calculate additional withdrawal needed to cover taxes on withdrawal.
        tax_gross_up_withdrawal = self.calculate_taxes_on_401k_withdrawal(base_withdrawal)
        total_withdrawal = base_withdrawal + tax_gross_up_withdrawal

        # Perform the withdrawal; accounts may fund less than requested
        actual_withdrawal = self.person.withdraw_from_pretax_401ks(total_withdrawal)

        # Recalculate final taxes after withdrawal
        final_taxes = self.person.get_income_taxes_due()

        return actual_withdrawal, final_taxes
