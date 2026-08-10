# Copyright 2022 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

from typing import Optional

from ..limits import federal_retirement_age, early_withdrawal_penalty_rate
from ..tax.federal import FilingStatus
from ..tax.regime import TaxInput
from ..tax.tax import TaxesDue
from ..model import LifeModelAgent, LifeModel, ModelSetupException


class Family(LifeModelAgent):
    def __init__(self, model: LifeModel, *args):
        """Family

        Args:
            model (LifeModel): LifeModel.
            args (Person): Family members.
        """
        super().__init__(model)
        self.name = next((arg for arg in args if isinstance(arg, str)), None)
        self.members = [arg for arg in args if not isinstance(arg, str)]
        self.investment_returns = []

    @property
    def federal_deductions(self) -> float:
        """Get federal deductions - use greater of standard or itemized"""
        standard_deduction = self.model.tax_regime.standard_deduction(self.filing_status)
        itemized_deductions = self.total_itemized_deductions
        return max(standard_deduction, itemized_deductions)

    @property
    def total_itemized_deductions(self) -> float:
        """Calculate total itemized deductions for the family

        For married filing jointly, combine all family members' itemized deductions
        """
        return sum(member.total_itemized_deductions for member in self.members)

    @property
    def bank_account_balance(self) -> float:
        return sum(x.bank_account_balance for x in self.members)

    @property
    def debt(self) -> float:
        return sum(x.debt for x in self.members)

    @property
    def filing_status(self) -> FilingStatus:
        # TODO - Currently using the first member's filing status for the whole family
        if not self.members:
            return FilingStatus.SINGLE
        return self.members[0].filing_status

    @property
    def state(self) -> Optional[str]:
        states = {member.state for member in self.members if member.state is not None}
        if len(states) > 1:
            raise NotImplementedError('Multi-state joint filing is not modeled yet')
        return next(iter(states), None)

    @property
    def combined_spending(self) -> float:
        return sum(x.spending.base for x in self.members)

    @property
    def owns_home(self) -> bool:
        return any(member.homes for member in self.members)

    @property
    def rents_apartment(self) -> bool:
        return any(member.apartments for member in self.members)

    @property
    def combined_cash_expenses(self) -> float:
        child_cash_expenses = sum(
            child.stat_child_cash_outflow
            for member in self.members
            for child in member.children
        )
        return sum(x.stat_money_spent + x.stat_housing_costs for x in self.members) + child_cash_expenses

    @property
    def combined_taxable_income(self) -> float:
        return sum(x.taxable_income for x in self.members)

    @property
    def combined_earned_income(self) -> float:
        return sum(x.earned_income for x in self.members)

    @property
    def combined_social_security_income(self) -> float:
        return sum(x.social_security_income for x in self.members)

    @property
    def pretax_401k_balance(self) -> float:
        return sum(account.pretax_balance for member in self.members for account in member.all_retirement_accounts)

    @property
    def available_pretax_401k_balance(self) -> float:
        """Pre-tax retirement balance available for planned withdrawals."""
        return sum(
            member.pretax_retirement_balance
            for member in self.members
            if member.is_retired
        )

    def __getitem__(self, key):
        matches = {x.name: x for x in self.members}
        return matches[key]

    def _repr_html_(self):
        return '<b>Family:</b><ul>' + ''.join(f"<li>{x._repr_html_()}</li>" for x in self.members) + '</ul>'

    def deposit_into_bank_account(self, amount: float):
        if amount <= 0:
            return
        for member in self.members:
            if member.bank_accounts:
                member.deposit_into_bank_account(amount)
                return
        raise ModelSetupException('No Bank Account. Create a bank account before making deposits.')

    def deduct_from_bank_accounts(self, amount: float) -> float:
        remaining = amount
        for member in self.members:
            if remaining == 0:
                break
            remaining = member.deduct_from_bank_accounts(remaining)
        return remaining

    def deduct_from_roth_401ks(self, amount: float) -> float:
        remaining = amount
        for member in self.members:
            if remaining == 0:
                break
            remaining = member.deduct_from_roth_401ks(remaining)
        return remaining

    def withdraw_from_pretax_401ks(self, amount: float) -> float:
        total_withdrawn = 0.0
        remaining = amount
        for member in self.members:
            if remaining == 0:
                break
            if not member.is_retired:
                continue
            not_withdrawn = member.deduct_from_pretax_401ks(remaining)
            withdrawn = remaining - not_withdrawn
            if withdrawn > 0:
                member.taxable_income += withdrawn
                if member.age < federal_retirement_age():
                    member.early_withdrawal_penalty += withdrawn * (early_withdrawal_penalty_rate() / 100)
                total_withdrawn += withdrawn
                self.deposit_into_bank_account(withdrawn)
            remaining = not_withdrawn
        return total_withdrawn

    def deduct_from_investments(self, amount: float) -> float:
        """Liquidates family and member investment balances to cover spending."""
        remaining = amount
        for investment_return in self.investment_returns:
            if remaining == 0:
                break
            remaining -= investment_return.withdraw(remaining)
        for member in self.members:
            if remaining == 0:
                break
            remaining = member.deduct_from_investments(remaining)
        return remaining

    def pay_bills(self, spending_balance: float) -> float:
        """Pay bills for the year.

        Liquidation order: bank accounts, then taxable investments, then Roth
        401k balances last so tax-free growth is preserved as long as possible.

        Args:
            spending_balance (float): Amount of spending left to pay.

        Returns:
            float: Balance remaining after paying bills.
        """
        remaining = self.deduct_from_bank_accounts(spending_balance)
        if remaining == 0:
            return 0
        remaining = self.deduct_from_investments(remaining)
        if remaining == 0:
            return 0
        return self.deduct_from_roth_401ks(remaining)

    def get_income_taxes_due(self, additional_income: float = 0) -> TaxesDue:
        """Get income taxes due for the year.

        Args:
            additional_income (float, optional): Additional income to include in calculation. Defaults to 0.

        Raises:
            NotImplementedError: Unsupported filing status.

        Returns:
            float: Federal taxes due.
        """
        income_amount = self.combined_taxable_income + additional_income
        if self.filing_status == FilingStatus.MARRIED_FILING_JOINTLY:
            # FICA wage-base caps apply per worker, so pass each member's
            # earned income separately rather than the household total.
            taxes = self.model.tax_regime.income_taxes(
                TaxInput(
                    income_amount,
                    self.federal_deductions,
                    self.filing_status,
                    self.state,
                    [member.earned_income for member in self.members],
                    ss_benefits=self.combined_social_security_income,
                )
            )
            taxes.penalties = sum(member.early_withdrawal_penalty for member in self.members)
            return taxes
        else:
            raise NotImplementedError(f"Unsupported filing status: {self.filing_status}")

    def step(self):
        pass

    def _refresh_housing_indicators(self):
        self.stat_owns_home = 1 if self.owns_home else 0
        self.stat_rents_apartment = 1 if self.rents_apartment else 0

    def prepare_start_year_stats(self):
        self._refresh_housing_indicators()

    def _anticipated_early_withdrawal_penalty(self, amount: float) -> float:
        """Estimate the 10% early-withdrawal penalty a pretax withdrawal of
        ``amount`` would trigger.

        Mirrors the member allocation order used by ``withdraw_from_pretax_401ks``
        (retired members only, in order), applying the penalty rate only to the
        portion funded by members below the federal retirement age, so the
        gross-up funds the penalty rather than leaving the household short.
        """
        penalty_rate = early_withdrawal_penalty_rate() / 100
        remaining = amount
        penalty = 0.0
        for member in self.members:
            if remaining <= 0:
                break
            if not member.is_retired:
                continue
            fundable = min(remaining, member.pretax_retirement_balance)
            if member.age < federal_retirement_age():
                penalty += fundable * penalty_rate
            remaining -= fundable
        return penalty

    def _calculate_pretax_withdrawal(self, cash_need_without_taxes: float, taxes: TaxesDue) -> float:
        cash_need = cash_need_without_taxes + taxes.total
        cash_deficit = max(0, cash_need - self.bank_account_balance)
        if cash_deficit == 0:
            return 0.0

        # The withdrawal itself is taxable, so gross it up using the observed
        # marginal rate (mirrors TaxCalculationService for single filers).
        # Without this the household comes up short by the tax-on-withdrawal
        # every year and rolls a phantom debt balance.
        taxes_after = self.get_income_taxes_due(cash_deficit)
        tax_increase = taxes_after.total - taxes.total
        # Withdrawals by retired members below the federal retirement age also
        # incur the early-withdrawal penalty, which the tax-only difference
        # above never sees (the penalty accumulator is identical in both trial
        # calculations). Fold the anticipated penalty into the gross-up so it
        # is funded, mirroring the single-filer path.
        tax_increase += self._anticipated_early_withdrawal_penalty(cash_deficit)
        if tax_increase > 0:
            observed_marginal_rate = min(max(tax_increase / cash_deficit, 0.0), 0.95)
            cash_deficit += tax_increase / (1 - observed_marginal_rate)

        return min(cash_deficit, self.available_pretax_401k_balance)

    def _set_household_debt(self, amount: float):
        if not self.members:
            return
        self.members[0].debt = amount
        for member in self.members[1:]:
            member.debt = 0

    def _refresh_member_balance_stats(self, zero_member_debt: bool = True):
        for member in self.members:
            member.stat_bank_balance = member.bank_account_balance
            if zero_member_debt:
                member.stat_debt = 0
            for bank_account in member.bank_accounts:
                bank_account.stat_useable_balance = bank_account.balance
            for account in member.all_retirement_accounts:
                account.stat_401k_balance = account.balance
                account.stat_useable_balance = account.balance if account.is_useable else 0

    def _settle_joint_cashflow(self) -> TaxesDue:
        cash_investment_return = self._apply_cash_investment_returns()
        for member in self.members:
            member.flush_brokerage_gains()
        cash_need_without_taxes = self.combined_cash_expenses + self.debt
        yearly_taxes = self.get_income_taxes_due()
        planned_withdrawal = self._calculate_pretax_withdrawal(cash_need_without_taxes, yearly_taxes)
        actual_withdrawal = self.withdraw_from_pretax_401ks(planned_withdrawal)

        if actual_withdrawal:
            yearly_taxes = self.get_income_taxes_due()

        total_cash_need = cash_need_without_taxes + yearly_taxes.total
        unpaid = self.pay_bills(total_cash_need)
        self._set_household_debt(unpaid)
        self.stat_retirement_withdrawals = actual_withdrawal
        self.stat_cash_investment_return = cash_investment_return
        return yearly_taxes

    def _apply_cash_investment_returns(self) -> float:
        cash_investment_return = 0.0
        for investment_return in self.investment_returns:
            cash_investment_return += investment_return.apply_to_family_cashflow()
        for member in self.members:
            for investment_return in member.investment_returns:
                cash_investment_return += investment_return.apply_to_person_cashflow()
        return cash_investment_return

    def post_step(self):
        if not self.members:
            return

        if self.filing_status == FilingStatus.MARRIED_FILING_JOINTLY:
            yearly_taxes = self._settle_joint_cashflow()
        else:
            yearly_taxes = TaxesDue()
            self.stat_retirement_withdrawals = 0
            self.stat_cash_investment_return = 0
            self.stat_debt = 0

        self.stat_taxes_paid = yearly_taxes.total
        if self.filing_status == FilingStatus.MARRIED_FILING_JOINTLY:
            self.stat_debt = self.debt
        self._refresh_housing_indicators()
        self._refresh_member_balance_stats(
            zero_member_debt=self.filing_status == FilingStatus.MARRIED_FILING_JOINTLY
        )

        # Additional tax stats
        self.stat_taxes_paid_federal = yearly_taxes.federal
        self.stat_taxes_paid_state = yearly_taxes.state
        self.stat_taxes_paid_ss = yearly_taxes.ss
        self.stat_taxes_paid_medicare = yearly_taxes.medicare
        self.stat_taxes_paid_penalties = yearly_taxes.penalties
        self.stat_tax_agi = yearly_taxes.agi
        self.stat_taxable_income = yearly_taxes.taxable_income
        self.stat_tax_deductions = yearly_taxes.deductions
        self.stat_federal_marginal_rate = yearly_taxes.federal_marginal_rate
        self.stat_effective_tax_rate = yearly_taxes.effective_tax_rate
