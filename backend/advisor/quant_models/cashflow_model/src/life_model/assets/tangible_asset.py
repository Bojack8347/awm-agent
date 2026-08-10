# Copyright 2026 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE
import html
from enum import Enum
from typing import Optional
from ..model import LifeModelAgent, Event
from ..people.person import Person
from ..base_classes import Loan
from ..config.config_manager import config


class AssetType(Enum):
    """Types of non-housing tangible assets"""
    VEHICLE = "Vehicle"
    BOAT = "Boat"
    RV = "RV"
    COLLECTIBLE = "Collectible"
    ART = "Art"
    JEWELRY = "Jewelry"
    EQUIPMENT = "Equipment"
    OTHER = "Other"


class AssetLoan(Loan):
    """Standard amortizing loan attached to a tangible asset.

    Payments are driven monthly from the owner's cashflow by the Loan base
    class, with unpaid interest capitalizing onto the principal.
    """

    def make_payment(self, payment_amount: float, extra_to_principal: float = 0) -> float:
        if payment_amount < 0 or extra_to_principal < 0:
            raise ValueError("Payment amounts cannot be negative")

        monthly_interest = self.get_interest_amount() / 12
        interest_paid = min(payment_amount, monthly_interest)
        principal_cash = max(0.0, payment_amount - interest_paid) + extra_to_principal
        principal_paid = min(self.principal, principal_cash)
        unpaid_interest = monthly_interest - interest_paid
        self.principal = max(0.0, self.principal - principal_paid + unpaid_interest)

        self.stat_principal_payment_history.append(principal_paid)
        self.stat_interest_payment_history.append(interest_paid)
        return interest_paid + principal_paid


class TangibleAsset(LifeModelAgent):
    def __init__(self, person: Person, name: str, asset_type: AssetType,
                 value: float, value_yearly_change_percent: float,
                 maintenance_annual: float = 0,
                 maintenance_increase_percent: Optional[float] = None,
                 insurance_annual: float = 0,
                 insurance_increase_percent: Optional[float] = None,
                 rental_income_annual: float = 0,
                 loan_amount: float = 0,
                 loan_interest_rate: float = 7.0,
                 loan_term_years: int = 5,
                 sell_year: Optional[int] = None,
                 selling_cost_percent: float = 5.0):
        """ Models a non-housing real asset (vehicle, collectible, equipment)

        The asset's value drifts each year by ``value_yearly_change_percent``
        (negative for depreciating vehicles, positive for appreciating
        collectibles). Maintenance and insurance are paid from cashflow,
        rental income is deposited and taxed as ordinary income, and an
        optional loan finances the purchase through the standard amortizing
        loan machinery. Selling in ``sell_year`` nets out selling costs, pays
        off any remaining loan, recognizes gains above the cost basis as
        ordinary income (personal-use losses are not deductible), and
        deposits the remaining proceeds.

        Args:
            person: Owner who pays the costs and receives proceeds
            name: Name of the asset
            asset_type: Category of asset
            value: Current market value
            value_yearly_change_percent: Yearly value drift (e.g. -15 for a
                car, +4 for art)
            maintenance_annual: Yearly upkeep cost
            maintenance_increase_percent: Yearly upkeep inflation. Defaults to
                the configured economy.inflation_rate.
            insurance_annual: Yearly insurance premium
            insurance_increase_percent: Yearly insurance premium inflation.
                Defaults to the resolved ``maintenance_increase_percent`` so
                insurance escalates in step with maintenance (which itself
                defaults to the configured economy.inflation_rate). Pinning
                maintenance flat therefore keeps insurance flat too, unless an
                explicit value is supplied here.
            rental_income_annual: Yearly rental income the asset produces
            loan_amount: Outstanding financing balance (0 for none)
            loan_interest_rate: Financing interest rate percentage
            loan_term_years: Financing term in years
            sell_year: Optional year in which the asset is sold
            selling_cost_percent: Transaction costs at sale
        """
        super().__init__(person.model)
        self.person = person
        self.name = name
        self.asset_type = asset_type
        self.value = value
        self.cost_basis = value
        self.value_yearly_change_percent = value_yearly_change_percent
        self.maintenance_annual = maintenance_annual
        if maintenance_increase_percent is None:
            maintenance_increase_percent = config.financial.get_inflation_rate()
        self.maintenance_increase_percent = maintenance_increase_percent
        self.insurance_annual = insurance_annual
        # Insurance escalates in step with maintenance by default (symmetric
        # carrying-cost treatment), so an explicitly-flat maintenance keeps
        # insurance flat too.
        if insurance_increase_percent is None:
            insurance_increase_percent = maintenance_increase_percent
        self.insurance_increase_percent = insurance_increase_percent
        self.rental_income_annual = rental_income_annual
        self.sell_year = sell_year
        self.selling_cost_percent = selling_cost_percent
        self.is_owned = True

        self.loan: Optional[AssetLoan] = None
        if loan_amount > 0:
            self.loan = AssetLoan(person, loan_amount, loan_interest_rate, loan_term_years)

        self.stat_real_asset_value = value
        self.stat_real_asset_costs = 0.0
        self.stat_real_asset_income = 0.0

        # Register with the model registry
        self.model.registries.tangible_assets.register(person, self)

    def prepare_start_year_stats(self):
        self.stat_real_asset_value = self.value if self.is_owned else 0.0

    def sell(self):
        """Sell the asset: net selling costs, pay off the loan, recognize
        gains over basis, and deposit the remaining proceeds."""
        if not self.is_owned:
            return 0.0

        gross_value = self.value
        proceeds = gross_value * (1 - self.selling_cost_percent / 100)

        if self.loan is not None and self.loan.principal > 0:
            payoff = min(proceeds, self.loan.principal)
            self.loan.principal -= payoff
            proceeds -= payoff
            if self.loan.principal > 0:
                # Deficiency rolls into the year's bills
                self.person.spending.add_expense(self.loan.principal, 'asset_sale_shortfalls')
                self.loan.principal = 0.0

        gain = max(0.0, gross_value - self.cost_basis)
        if gain > 0:
            self.person.taxable_income += gain

        if proceeds > 0:
            self.person.deposit_into_cashflow_bank_account(proceeds)

        self.model.event_log.add(Event(
            f"{self.person.name} sold {self.name} for ${gross_value:,.0f}"))
        self.is_owned = False
        self.value = 0.0
        self.stat_real_asset_value = 0.0
        return proceeds

    def pre_step(self):
        """Drift value, pay carrying costs, collect rent, and handle sale."""
        self.stat_real_asset_costs = 0.0
        self.stat_real_asset_income = 0.0
        if not self.is_owned:
            self.stat_real_asset_value = 0.0
            return

        self.value = max(0.0, self.value * (1 + self.value_yearly_change_percent / 100))

        # The asset is held for the full year (value drifts a full year and any
        # sale gain is recognized on the drifted value), so it incurs a full
        # year of carrying costs and produces a full year of rent even in the
        # year it is ultimately sold. These are applied before the sale so the
        # sale year is not silently expense- and income-free.
        carrying_costs = self.maintenance_annual + self.insurance_annual
        if carrying_costs > 0:
            # Flows through the year's bills so settlement plans for it
            self.person.spending.add_expense(carrying_costs, 'real_asset_costs')

        if self.rental_income_annual > 0:
            self.person.deposit_into_cashflow_bank_account(self.rental_income_annual)
            self.person.taxable_income += self.rental_income_annual

        self.stat_real_asset_costs = carrying_costs
        self.stat_real_asset_income = self.rental_income_annual

        if self.sell_year is not None and self.model.year >= self.sell_year:
            self.sell()
            return

        # Escalate carrying costs for subsequent years (only while still owned).
        self.maintenance_annual *= (1 + self.maintenance_increase_percent / 100)
        self.insurance_annual *= (1 + self.insurance_increase_percent / 100)

        self.stat_real_asset_value = self.value

    def _repr_html_(self):
        desc = '<ul>'
        desc += f'<li>Name: {html.escape(self.name)}</li>'
        desc += f'<li>Type: {self.asset_type.value}</li>'
        desc += f'<li>Value: ${self.value:,.2f}</li>'
        if self.loan is not None:
            desc += f'<li>Loan Balance: ${self.loan.principal:,.2f}</li>'
        desc += '</ul>'
        return desc
