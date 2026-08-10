# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE
from enum import Enum
from ..people.person import Person
from ..base_classes import FinancialAccount


class HSAType(Enum):
    """Types of Health Savings Accounts"""
    INDIVIDUAL = "Individual"
    FAMILY = "Family"


class HealthSavingsAccount(FinancialAccount):
    def __init__(self, person: Person, hsa_type: HSAType, balance: float = 0,
                 contribution_limit: float = 4400, employer_contribution: float = 0,
                 growth_rate: float = 0):
        """ Models a Health Savings Account (HSA)

        Args:
            person: The person who owns this HSA
            hsa_type: Type of HSA (Individual or Family)
            balance: Current HSA balance
            contribution_limit: Annual contribution limit
            employer_contribution: Annual employer contribution
            growth_rate: Annual investment growth rate percentage
        """
        super().__init__(person, balance)
        self.hsa_type = hsa_type
        self.contribution_limit = contribution_limit
        self.employer_contribution = employer_contribution
        self.growth_rate = growth_rate
        self.annual_contributions = 0
        self.model.registries.hsa_accounts.register(person, self)

    def contribute(self, amount: float) -> bool:
        """Make contribution to HSA (tax-deductible)"""
        remaining_limit = self.contribution_limit - self.annual_contributions
        actual_contribution = min(amount, remaining_limit)

        if actual_contribution > 0:
            self.balance += actual_contribution
            self.annual_contributions += actual_contribution
            return True
        return False

    def withdraw_medical(self, amount: float) -> float:
        """Withdraw for qualified medical expenses (tax-free)"""
        return self.withdraw(amount)

    def withdraw_non_medical(self, amount: float) -> float:
        """Withdraw for non-medical expenses (taxable + penalty if under 65)"""
        return self.withdraw(amount)

    def get_balance(self) -> float:
        return self.balance

    def deposit(self, amount: float) -> bool:
        return self.contribute(amount)

    def withdraw(self, amount: float) -> float:
        if amount <= 0:
            return 0.0
        actual_withdrawal = min(amount, self.balance)
        self.balance -= actual_withdrawal
        return actual_withdrawal

    def reset_annual_contributions(self):
        """Reset annual contribution tracking (called at year end)"""
        self.annual_contributions = 0

    def _repr_html_(self):
        desc = '<ul>'
        desc += f'<li>HSA Type: {self.hsa_type.value}</li>'
        desc += f'<li>Balance: ${self.balance:,.2f}</li>'
        desc += f'<li>Contribution Limit: ${self.contribution_limit:,.2f}</li>'
        desc += f'<li>Contributions This Year: ${self.annual_contributions:,.2f}</li>'
        desc += f'<li>Remaining Limit: ${self.contribution_limit - self.annual_contributions:,.2f}</li>'
        desc += '</ul>'
        return desc

    def prepare_start_year_stats(self):
        self.stat_hsa_balance = self.balance

    def step(self):
        """Add annual employer contribution, apply growth, and track balance."""
        if self.employer_contribution > 0:
            self.contribute(self.employer_contribution)
        investment_return = self.balance * (self.growth_rate / 100)
        self.balance += investment_return
        self.stat_investment_return = investment_return
        self.stat_hsa_balance = self.balance
        super().step()

    def post_step(self):
        self.reset_annual_contributions()
