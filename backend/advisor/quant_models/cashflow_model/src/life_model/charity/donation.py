# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE
import html
from enum import Enum
from typing import Optional, TYPE_CHECKING, cast
from ..model import LifeModel, LifeModelAgent, Event

if TYPE_CHECKING:
    from ..people.person import Person


class DonationType(Enum):
    """ Enum for donation types """
    CASH = "Cash"
    STOCK = "Stock"
    PROPERTY = "Property"
    OTHER = "Other"


class Donation(LifeModelAgent):
    def __init__(self, person: 'Person', charity_name: str, annual_amount: float,
                 donation_type: DonationType = DonationType.CASH,
                 tax_deductible: bool = True,
                 frequency_years: int = 1,
                 start_year: Optional[int] = None,
                 end_year: Optional[int] = None):
        """ Models a charitable donation for a person

        Args:
            person: The person making the donation
            charity_name: Name of the charity receiving the donation
            annual_amount: Amount to donate (per frequency_years)
            donation_type: Type of donation (cash, stock, property, etc.)
            tax_deductible: Whether the donation is tax deductible
            frequency_years: How often to make donation (1=yearly, 2=every 2 years, etc.)
            start_year: First year to make donation (defaults to current year)
            end_year: Last year to make donation (defaults to model end)
        """
        super().__init__(person.model)
        self.person = person
        self.charity_name = charity_name
        self.annual_amount = annual_amount
        self.donation_type = donation_type
        self.tax_deductible = tax_deductible
        self.frequency_years = frequency_years
        model = cast(LifeModel, person.model)
        self.start_year = start_year if start_year is not None else model.component_anchor_year
        self.end_year = end_year if end_year is not None else model.end_year

        # Track donation stats
        self.stat_charitable_donations = 0  # Donations made this year
        self.stat_total_donated = 0  # Total donated over lifetime

        # Register with the model registry
        model.registries.donations.register(person, self)

    def should_donate_this_year(self) -> bool:
        """Check if a donation should be made this year"""
        model = cast(LifeModel, self.person.model)
        current_year = model.year
        if current_year < self.start_year or current_year > self.end_year:
            return False
        years_since_start = current_year - self.start_year
        return years_since_start % self.frequency_years == 0

    def make_donation(self) -> float:
        """Process donation payment from person's bank account

        Returns:
            Amount actually donated (may be less than requested if insufficient funds)
        """
        if not self.should_donate_this_year():
            return 0.0

        # Attempt to withdraw from bank account
        # Note: deduct_from_bank_accounts returns amount that COULD NOT be deducted
        remaining_balance = self.person.deduct_from_cashflow_bank_accounts(self.annual_amount)
        amount_withdrawn = self.annual_amount - remaining_balance

        if amount_withdrawn > 0:
            self.stat_charitable_donations = amount_withdrawn
            self.stat_total_donated += amount_withdrawn
            self._refresh_cashflow_stats()

            # Log the donation event
            model = cast(LifeModel, self.person.model)
            model.event_log.add(Event(
                f"{self.person.name} donated ${amount_withdrawn:,.0f} to {self.charity_name}"
            ))

        return amount_withdrawn

    def _refresh_cashflow_stats(self):
        """Refresh bank stats after post-step donations change cash balances."""
        if self.person.uses_joint_cashflow:
            self.person.family._refresh_member_balance_stats(zero_member_debt=True)
            return

        self.person.stat_bank_balance = self.person.bank_account_balance
        for bank_account in self.person.bank_accounts:
            bank_account.stat_useable_balance = bank_account.balance

    def get_tax_deduction_amount(self) -> float:
        """Get the tax deductible amount for this year's donation"""
        if not self.tax_deductible:
            return 0.0
        return self.stat_charitable_donations

    def get_planned_tax_deduction_amount(self) -> float:
        """Get the scheduled deductible amount for same-year tax planning."""
        if not self.tax_deductible or not self.should_donate_this_year():
            return 0.0
        return self.annual_amount

    def _repr_html_(self):
        desc = '<ul>'
        desc += f'<li>Charity: {html.escape(self.charity_name)}</li>'
        desc += f'<li>Annual Amount: ${self.annual_amount:,.2f}</li>'
        desc += f'<li>Type: {self.donation_type.value}</li>'
        desc += f'<li>Tax Deductible: {self.tax_deductible}</li>'
        desc += f'<li>Frequency: Every {self.frequency_years} year(s)</li>'
        desc += f'<li>Total Donated: ${self.stat_total_donated:,.2f}</li>'
        desc += '</ul>'
        return desc

    def pre_step(self):
        """Reset stats before the year begins"""
        # Reset this year's donation stat
        self.stat_charitable_donations = 0

    def step(self):
        """Step phase - wait for post_step to make donations"""
        pass

    def post_step(self):
        """Make donation after person pays bills and expenses

        Donations execute in post_step (lowest priority) so essential bills
        and spending in person.step() are paid first. Only leftover funds
        are available for charitable giving.
        Donations are tracked separately from expenses in stat_charitable_donations.
        """
        self.make_donation()
