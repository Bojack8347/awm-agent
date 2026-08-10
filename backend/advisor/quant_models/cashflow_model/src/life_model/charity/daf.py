# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE
import html
from typing import TYPE_CHECKING, cast
from ..model import LifeModel, Event
from ..base_classes import Investment

if TYPE_CHECKING:
    from ..people.person import Person


class DonorAdvisedFund(Investment):
    def __init__(self, person: 'Person', fund_name: str, balance: float = 0,
                 growth_rate: float = 7.0, management_fee: float = 0.6,
                 distribution_rate: float = 5.0):
        """ Models a donor advised fund for a person

        Args:
            person: The person to which this fund belongs
            fund_name: Name of the donor advised fund
            balance: Current balance in the fund
            growth_rate: Expected annual growth rate percentage (default 7%)
            management_fee: Annual management fee percentage (default 0.6%)
            distribution_rate: Percentage of balance to distribute to charity annually (default 5%)
        """
        super().__init__(person, balance, growth_rate)
        self.fund_name = fund_name
        self.management_fee = management_fee
        self.distribution_rate = distribution_rate

        # Track stats
        self.stat_charitable_donations = 0  # Distributions to charity this year
        self.stat_total_donated = 0  # Total distributed to charity over lifetime
        self.stat_contributions_this_year = 0  # Contributions made this year (for tax deductions)
        self.stat_total_contributions = 0  # Total contributed to DAF over lifetime
        self.stat_management_fees_paid = 0  # Total management fees paid over lifetime

        # Register with the model registry
        model = cast(LifeModel, person.model)
        model.registries.donor_advised_funds.register(person, self)

    def calculate_growth(self) -> float:
        """Calculate investment growth based on growth rate"""
        return self.balance * (self.growth_rate / 100)

    def get_balance(self) -> float:
        return self.balance

    def deposit(self, amount: float) -> bool:
        """Make a contribution to the DAF (tax deductible at contribution time)

        Args:
            amount: Amount to contribute

        Returns:
            True if successful, False otherwise
        """
        if amount <= 0:
            return False
        self.balance += amount
        self.stat_contributions_this_year += amount
        self.stat_total_contributions += amount

        # Log contribution event
        model = cast(LifeModel, self.person.model)
        model.event_log.add(Event(
            f"{self.person.name} contributed ${amount:,.0f} to {self.fund_name} DAF"
        ))
        return True

    def withdraw(self, amount: float) -> float:
        """Withdraw amount from DAF (not typically used - distributions go to charity)

        Args:
            amount: Amount to withdraw

        Returns:
            Actual amount withdrawn
        """
        actual_withdrawal = min(amount, self.balance)
        self.balance -= actual_withdrawal
        return actual_withdrawal

    def contribute(self, amount: float) -> float:
        """Contribute to DAF from person's bank account (creates tax deduction)

        Args:
            amount: Amount to contribute

        Returns:
            Amount actually contributed
        """
        # Withdraw from person's bank account
        # Note: deduct_from_bank_accounts returns amount that COULD NOT be deducted
        remaining_balance = self.person.deduct_from_cashflow_bank_accounts(amount)
        amount_withdrawn = amount - remaining_balance

        if amount_withdrawn > 0:
            self.deposit(amount_withdrawn)

        return amount_withdrawn

    def distribute_to_charity(self, amount: float) -> float:
        """Distribute funds to charity (no additional tax deduction)

        Args:
            amount: Amount to distribute

        Returns:
            Amount actually distributed
        """
        actual_distribution = min(amount, self.balance)
        if actual_distribution <= 0:
            return 0.0

        self.balance -= actual_distribution
        self.stat_charitable_donations += actual_distribution
        self.stat_total_donated += actual_distribution

        # Log distribution event
        model = cast(LifeModel, self.person.model)
        model.event_log.add(Event(
            f"{self.fund_name} DAF distributed ${actual_distribution:,.0f} to charity"
        ))

        return actual_distribution

    def apply_management_fee(self) -> float:
        """Apply annual management fee to the fund

        Returns:
            Fee amount charged
        """
        fee = self.balance * (self.management_fee / 100)
        self.balance -= fee
        self.stat_management_fees_paid += fee
        return fee

    def make_automatic_distribution(self) -> float:
        """Make automatic distribution based on distribution_rate

        Returns:
            Amount distributed
        """
        distribution_amount = self.balance * (self.distribution_rate / 100)
        return self.distribute_to_charity(distribution_amount)

    def _repr_html_(self):
        desc = '<ul>'
        desc += f'<li>Fund Name: {html.escape(self.fund_name)}</li>'
        desc += f'<li>Balance: ${self.balance:,.2f}</li>'
        desc += f'<li>Growth Rate: {self.growth_rate}%</li>'
        desc += f'<li>Management Fee: {self.management_fee}%</li>'
        desc += f'<li>Distribution Rate: {self.distribution_rate}%</li>'
        desc += f'<li>Total Contributed: ${self.stat_total_contributions:,.2f}</li>'
        desc += f'<li>Total Distributed: ${self.stat_total_donated:,.2f}</li>'
        desc += '</ul>'
        return desc

    def pre_step(self):
        """Reset distribution stats before the year begins.

        Contribution tracking must NOT reset here: contribution events fire
        earlier in the same pre-step phase (LifeEvents is created first), and
        the year's settlement reads stat_contributions_this_year for the
        charitable deduction. It resets in post_step instead.
        """
        self.stat_charitable_donations = 0

    def step(self):
        """Execute annual DAF logic: apply growth, fees, and distributions

        We override parent Investment.step() to control the order of operations.
        """
        # Apply growth first (inherited from Investment base class)
        self.apply_growth()

        # Then apply management fee on the grown balance
        self.apply_management_fee()

        # Then make automatic distribution from the fee-adjusted balance
        self.make_automatic_distribution()

        # Track balance history (same as parent Investment.step() does)
        self.stat_balance_history.append(self.balance)

    def post_step(self):
        """Reset annual contribution tracking after taxes have been settled"""
        self.stat_contributions_this_year = 0
