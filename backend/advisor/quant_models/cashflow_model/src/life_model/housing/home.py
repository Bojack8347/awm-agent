# Copyright 2022 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

import html
import math
from enum import Enum
from typing import Optional, Union
from ..people.person import Person
from ..model import LifeModelAgent, LifeModel, Event
from ..config.config_manager import config


class MortgageType(Enum):
    """General mortgage payment-structure categories."""

    FIXED_RATE = "Fixed Rate"
    ADJUSTABLE_RATE = "Adjustable Rate"
    INTEREST_ONLY = "Interest Only"
    BALLOON = "Balloon"


def _coerce_mortgage_type(value: Union[MortgageType, str]) -> MortgageType:
    if isinstance(value, MortgageType):
        return value

    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "fixed": MortgageType.FIXED_RATE,
        "fixed_rate": MortgageType.FIXED_RATE,
        "fixed_rate_mortgage": MortgageType.FIXED_RATE,
        "adjustable": MortgageType.ADJUSTABLE_RATE,
        "adjustable_rate": MortgageType.ADJUSTABLE_RATE,
        "adjustable_rate_mortgage": MortgageType.ADJUSTABLE_RATE,
        "arm": MortgageType.ADJUSTABLE_RATE,
        "interest_only": MortgageType.INTEREST_ONLY,
        "interest_only_mortgage": MortgageType.INTEREST_ONLY,
        "balloon": MortgageType.BALLOON,
        "balloon_mortgage": MortgageType.BALLOON,
    }
    if normalized in aliases:
        return aliases[normalized]
    for mortgage_type in MortgageType:
        if normalized == mortgage_type.value.lower().replace(" ", "_"):
            return mortgage_type
    raise ValueError(
        "Unsupported mortgage_type. Expected one of: "
        "fixed_rate, adjustable_rate, interest_only, balloon"
    )


class Home(LifeModelAgent):
    def __init__(self, person: Person, name: str, purchase_price: float, value_yearly_increase: float,
                 down_payment: float, mortgage: 'Mortgage', expenses: 'HomeExpenses',
                 current_value: Optional[float] = None, tax_basis_known: bool = True):
        """Home

        Args:
            person (Person): Primary resident or person that pays the bills.
            name (string): Name of the house or neighborhood.
            purchase_price (float): Purchase price of the home.
            value_yearly_increase (float): Percentage of yearly home value appreciation.
            down_payment (float): Amount of down payment.
            mortgage (Mortgage): Mortgage associated with the home.
            expenses (HomeExpenses): Home expenses associated with the home.
            current_value (float, optional): Opening market value for a home already owned.
                A newly purchased home defaults to ``purchase_price``.
            tax_basis_known (bool): Whether ``purchase_price`` is a confirmed tax basis.
                Existing homes may omit basis when no sale is modeled; a later sale then
                fails closed instead of assuming a zero gain.
        """
        for label, value in (
            ("purchase_price", purchase_price),
            ("value_yearly_increase", value_yearly_increase),
            ("down_payment", down_payment),
            ("current_value", purchase_price if current_value is None else current_value),
        ):
            if isinstance(value, bool) or not math.isfinite(float(value)):
                raise ValueError(f"Home {label} must be a finite number")
        if purchase_price < 0 or down_payment < 0:
            raise ValueError("Home purchase_price and down_payment cannot be negative")
        if current_value is not None and current_value < 0:
            raise ValueError("Home current_value cannot be negative")
        if value_yearly_increase <= -100 or value_yearly_increase > 100:
            raise ValueError(
                "Home value_yearly_increase must be greater than -100 and at most 100"
            )
        super().__init__(person.model)
        self.person = person
        self.name = name
        self.purchase_price = purchase_price
        self.value_yearly_increase = value_yearly_increase
        self.down_payment = down_payment
        self.tax_basis_known = bool(tax_basis_known)
        self.mortgage = mortgage
        self.expenses = expenses
        self.expenses.home = self
        self.home_value: float = (
            self.purchase_price if current_value is None else float(current_value)
        )
        self.is_sold = False
        self._refresh_mortgage_stats()

        # Register with the model registry
        self.model.registries.homes.register(person, self)

    @property
    def yearly_expenses_due(self) -> float:
        if self.is_sold:
            return 0.0
        return self.expenses.get_yearly_spending() + self.mortgage.get_payment_due_for_year()

    def make_yearly_payment(self, yearly_payment: Optional[float] = None, extra_to_principal: float = 0):
        if self.is_sold:
            return 0.0
        if yearly_payment is None:
            yearly_payment = self.yearly_expenses_due
        base_mortgage_payment = max(0.0, yearly_payment - self.expenses.get_yearly_spending())
        actual_mortgage_payment = self.mortgage.make_yearly_payment(base_mortgage_payment, extra_to_principal)
        self._refresh_mortgage_stats()
        return self.expenses.get_yearly_spending() + actual_mortgage_payment

    def sell(self, selling_cost_percent: float = 6.0) -> float:
        """Sell the home at its current value.

        Selling costs come out of the gross price, the mortgage is paid off
        from the proceeds, and the remainder is deposited into the owner's
        cashflow bank account. The gain over the purchase price above the
        IRC §121 exclusion (configured per filing status) is recognized as
        ordinary income, the model's convention for gains. The home stops
        billing expenses and leaves the registry, so the household no longer
        owns it.
        """
        if self.is_sold:
            return 0.0
        if not self.tax_basis_known:
            raise ValueError(
                f"Cannot sell home {self.name}: confirmed acquisition tax basis is required"
            )

        gross_value = self.home_value
        proceeds = gross_value * (1 - selling_cost_percent / 100)

        payoff = min(proceeds, self.mortgage.principal)
        self.mortgage.principal = max(0.0, self.mortgage.principal - payoff)
        proceeds -= payoff
        if self.mortgage.principal > 0:
            # Underwater sale: the deficiency rolls into the year's bills
            self.person.spending.add_expense(self.mortgage.principal, 'asset_sale_shortfalls')
            self.mortgage.principal = 0.0

        gain = max(0.0, gross_value - self.purchase_price)
        filing_key = ('married_filing_jointly'
                      if getattr(self.person.filing_status, 'name', '') == 'MARRIED_FILING_JOINTLY'
                      else 'single')
        exclusion = config.financial.get(f'tax.federal.home_sale_exclusion.{filing_key}',
                                         500000.0 if filing_key == 'married_filing_jointly' else 250000.0)
        taxable_gain = max(0.0, gain - exclusion)
        if taxable_gain > 0:
            self.person.taxable_income += taxable_gain

        if proceeds > 0:
            self.person.deposit_into_cashflow_bank_account(proceeds)

        self.model.event_log.add(Event(
            f"{self.person.name} sold home {self.name} for ${gross_value:,.0f}"))

        self.is_sold = True
        self.home_value = 0.0
        # No further payments happen; clear the last-payment stats so the
        # projection doesn't report phantom mortgage flows after the sale
        self.mortgage.stat_last_payment = 0.0
        self.mortgage.stat_last_principal_paid = 0.0
        self.mortgage.stat_last_interest_paid = 0.0
        self._refresh_mortgage_stats()
        self.model.registries.homes.unregister(self.person, self)
        return proceeds

    def _refresh_mortgage_stats(self):
        self.stat_home_value = self.home_value
        self.stat_mortgage_balance = self.mortgage.principal
        self.stat_mortgage_payments = self.mortgage.stat_last_payment
        self.stat_mortgage_principal_paid = self.mortgage.stat_last_principal_paid
        self.stat_mortgage_interest_paid = self.mortgage.stat_last_interest_paid
        self.stat_fixed_rate_mortgages = 1 if self.mortgage.mortgage_type == MortgageType.FIXED_RATE else 0
        self.stat_adjustable_rate_mortgages = 1 if self.mortgage.mortgage_type == MortgageType.ADJUSTABLE_RATE else 0
        self.stat_interest_only_mortgages = 1 if self.mortgage.mortgage_type == MortgageType.INTEREST_ONLY else 0
        self.stat_balloon_mortgages = 1 if self.mortgage.mortgage_type == MortgageType.BALLOON else 0

    def prepare_start_year_stats(self):
        self._refresh_mortgage_stats()

    def _repr_html_(self):
        return f"{html.escape(self.name)}, current value ${self.home_value:,}, " \
               + f"monthly mortgage ${self.mortgage.monthly_payment:,}"

    def step(self):
        if self.is_sold:
            self.stat_home_value = 0.0
            return
        self.home_value += self.home_value * (self.value_yearly_increase / 100)
        self.stat_home_value = self.home_value


class HomeExpenses(LifeModelAgent):
    def __init__(self, model: LifeModel,
                 property_tax_percent: float, home_insurance_percent: float,
                 maintenance_amount: float, maintenance_increase: float,
                 improvement_amount: float, improvement_increase: float,
                 hoa_amount: float, hoa_increase: float):
        """Home Expenses

        Args:
            property_tax_percent (float): Property tax percentage paid yearly based on home value.
            home_insurance_percent (float): Yearly home insurance cost as percentage of home value.
            maintenance_amount (float): Yearly cost of home maintenance.
            maintenance_increase (float): Yearly percentage increase of maintenance costs.
            improvement_amount (float): Yearly cost of improvements.
            improvement_increase (float): Yearly percentage increase of improvment costs.
            hoa_amount (float): Yearly HOA dues.
            hoa_increase (float): Yearly percentage incresae of HOA dues.
        """
        super().__init__(model)
        self.property_tax_percent = property_tax_percent
        self.home_insurance_percent = home_insurance_percent
        self.maintenance_amount = maintenance_amount
        self.maintenance_increase = maintenance_increase
        self.improvement_amount = improvement_amount
        self.improvement_increase = improvement_increase
        self.hoa_amount = hoa_amount
        self.hoa_increase = hoa_increase
        self.home: Optional[Home] = None

    def get_yearly_spending(self):
        spending_amount = 0
        if self.home is not None:
            spending_amount += self.home.home_value * (self.property_tax_percent / 100)
            spending_amount += self.home.home_value * (self.home_insurance_percent / 100)
        spending_amount += self.maintenance_amount + self.improvement_amount + self.hoa_amount
        return spending_amount

    def step(self):
        self.maintenance_amount += self.maintenance_amount * (self.maintenance_increase / 100)
        self.improvement_amount += self.improvement_amount * (self.improvement_increase / 100)
        self.hoa_amount += self.hoa_amount * (self.hoa_increase / 100)


# https://www.nerdwallet.com/mortgages/mortgage-calculator/calculate-mortgage-payment
# https://www.valuepenguin.com/mortgages/mortgage-payments-calculator
# https://www.investopedia.com/calculate-principal-and-interest-5211981
class Mortgage:
    def __init__(self, loan_amount: float, start_date: float, length_years: int, yearly_interest_rate: float,
                 principal: Optional[float] = None, monthly_payment: Optional[float] = None,
                 mortgage_type: Union[MortgageType, str] = MortgageType.FIXED_RATE):
        """Mortgage

        Args:
            loan_amount (float): Amount of loan.
            start_date (float): Starting year of loan.
            length_years (int): Length of years of loan (e.g. 30, 15)
            yearly_interest_rate (float): Yearly interest rate
            principal (float, optional): Initial principal. Defaults to None.
            monthly_payment (float, optional): Monthly payment. Defaults to None.
            mortgage_type: General mortgage category. Defaults to fixed-rate.
        """
        # TODO - Need to add PMI
        numeric_values = {
            "loan_amount": loan_amount,
            "start_date": start_date,
            "length_years": length_years,
            "yearly_interest_rate": yearly_interest_rate,
            "principal": loan_amount if principal is None else principal,
        }
        if monthly_payment is not None:
            numeric_values["monthly_payment"] = monthly_payment
        for label, value in numeric_values.items():
            if isinstance(value, bool) or not math.isfinite(float(value)):
                raise ValueError(f"Mortgage {label} must be a finite number")
        if float(loan_amount) < 0 or float(numeric_values["principal"]) < 0:
            raise ValueError("Mortgage loan_amount and principal cannot be negative")
        if int(length_years) <= 0 or not float(length_years).is_integer():
            raise ValueError("Mortgage length_years must be a positive whole number")
        if float(yearly_interest_rate) < 0:
            raise ValueError("Mortgage yearly_interest_rate cannot be negative")
        if monthly_payment is not None and float(monthly_payment) <= 0 and float(numeric_values["principal"]) > 0:
            raise ValueError("Mortgage monthly_payment must be positive while principal remains")
        self.loan_amount = float(loan_amount)
        self.start_date = float(start_date)
        self.length_years = int(length_years)
        self.yearly_interest_rate = float(yearly_interest_rate)
        self.mortgage_type = _coerce_mortgage_type(mortgage_type)
        self.principal = self.loan_amount if principal is None else float(principal)
        self.monthly_payment = (
            self.get_monthly_payment()
            if monthly_payment is None
            else float(monthly_payment)
        )
        self.yearly_payment = self.monthly_payment * 12

        self.stat_principal_payment_history = []
        self.stat_interest_payment_history = []
        self.stat_principal_balance_history = []
        self.stat_last_payment = 0.0
        self.stat_last_principal_paid = 0.0
        self.stat_last_interest_paid = 0.0

    def get_monthly_payment(self) -> float:
        p = self.loan_amount
        i = self.yearly_interest_rate / (100 * 12)
        n = self.length_years * 12
        if i == 0:
            return p / n
        return p * (i * ((1 + i) ** n)) / (((1 + i) ** n) - 1)

    def get_payment_due_for_year(self) -> float:
        payment, _principal_paid, _interest_paid, _ending_principal = self._simulate_yearly_payment(
            self.yearly_payment,
            0.0,
            mutate=False,
        )
        return payment

    def get_interest_for_year(self) -> float:
        _payment, _principal_paid, interest_paid, _ending_principal = self._simulate_yearly_payment(
            self.yearly_payment,
            0.0,
            mutate=False,
        )
        return interest_paid

    def _simulate_yearly_payment(
        self,
        yearly_payment: float,
        extra_to_principal: float = 0,
        mutate: bool = False,
    ) -> tuple[float, float, float, float]:
        principal = self.principal
        cash_remaining = yearly_payment
        principal_paid = 0.0
        interest_paid = 0.0

        for _month in range(12):
            if principal <= 0 or cash_remaining <= 0:
                break
            monthly_interest = principal * (self.yearly_interest_rate / 100) / 12
            scheduled_due = min(self.monthly_payment, principal + monthly_interest)
            payment = min(cash_remaining, scheduled_due)
            month_interest_paid = min(payment, monthly_interest)
            month_principal_paid = min(principal, max(0.0, payment - month_interest_paid))
            unpaid_interest = monthly_interest - month_interest_paid

            principal = max(0.0, principal - month_principal_paid + unpaid_interest)
            cash_remaining -= payment
            principal_paid += month_principal_paid
            interest_paid += month_interest_paid

        extra_principal_paid = min(principal, extra_to_principal)
        principal -= extra_principal_paid
        principal_paid += extra_principal_paid

        total_paid = principal_paid + interest_paid
        if mutate:
            self.principal = max(0.0, principal)
        return total_paid, principal_paid, interest_paid, max(0.0, principal)

    def make_yearly_payment(self, yearly_payment: float, extra_to_principal: float = 0) -> float:
        if yearly_payment < 0:
            raise ValueError("Yearly payment cannot be negative")
        if extra_to_principal < 0:
            raise ValueError("Extra principal payment cannot be negative")
        if self.principal <= 0:
            self.principal = 0.0
            self.stat_last_payment = 0.0
            self.stat_last_principal_paid = 0.0
            self.stat_last_interest_paid = 0.0
            self.stat_principal_payment_history.append(0.0)
            self.stat_interest_payment_history.append(0.0)
            self.stat_principal_balance_history.append(self.principal)
            return 0.0

        total_paid, principal_amount, interest_paid, _ending_principal = self._simulate_yearly_payment(
            yearly_payment,
            extra_to_principal,
            mutate=True,
        )

        self.stat_principal_payment_history.append(principal_amount)
        self.stat_interest_payment_history.append(interest_paid)
        self.stat_principal_balance_history.append(self.principal)
        self.stat_last_payment = total_paid
        self.stat_last_principal_paid = principal_amount
        self.stat_last_interest_paid = interest_paid
        return total_paid
