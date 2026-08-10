# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE
import html
from typing import Optional
from ..people.person import Person
from ..base_classes import Loan


class CarLoan(Loan):
    def __init__(self, person: Person, loan_amount: float, length_years: int, yearly_interest_rate: float,
                 name: str, principal: Optional[float] = None, monthly_payment: Optional[float] = None):
        """ Models a car loan for a person

        Args:
            person: The person to which this loan belongs
            loan_amount: Amount of the loan
            length_years: Length of loan in years
            yearly_interest_rate: Annual interest rate percentage
            name: Name of this vehicle (Make/Model)
            principal: Current principal balance (defaults to loan_amount)
            monthly_payment: Monthly payment amount (calculated if not provided)
        """
        super().__init__(person, loan_amount, yearly_interest_rate, length_years, principal, monthly_payment)
        self.name = name

    def _refresh_loan_type_stats(self):
        super()._refresh_loan_type_stats()
        self.stat_car_loans = 1

    def make_payment(self, payment_amount: float, extra_to_principal: float = 0) -> float:
        """Make loan payment"""
        # Validate inputs
        if payment_amount < 0:
            raise ValueError("Payment amount cannot be negative")
        if extra_to_principal < 0:
            raise ValueError("Extra principal payment cannot be negative")

        # Calculate monthly interest
        interest_amount = self.get_interest_amount() / 12  # Monthly interest

        # Calculate how much goes to principal from the regular payment
        available_for_principal = payment_amount - interest_amount

        # Principal payment cannot be negative and cannot exceed current principal balance
        # Include extra principal in the total principal payment
        total_principal_payment = max(0, available_for_principal) + extra_to_principal
        principal_payment = min(total_principal_payment, self.principal)

        # Calculate actual total payment made
        # If payment_amount < interest_amount, we only get partial interest payment
        actual_interest_payment = min(payment_amount, interest_amount)
        total_payment = actual_interest_payment + principal_payment

        # Update principal balance
        # If payment doesn't cover interest, principal grows by unpaid interest
        unpaid_interest = interest_amount - actual_interest_payment
        self.principal = self.principal - principal_payment + unpaid_interest

        # Track statistics
        self.stat_principal_payment_history.append(principal_payment)
        self.stat_interest_payment_history.append(actual_interest_payment)

        return total_payment

    def get_monthly_payment(self) -> float:
        """ Calculate monthly payment using standard loan formula """
        return self.calculate_monthly_payment()

    def _repr_html_(self):
        desc = '<ul>'
        desc += f'<li>Vehicle: {html.escape(self.name)}</li>'
        desc += f'<li>Loan Amount: ${self.loan_amount:,.2f}</li>'
        desc += f'<li>Principal Balance: ${self.principal:,.2f}</li>'
        desc += f'<li>Monthly Payment: ${self.monthly_payment:,.2f}</li>'
        desc += f'<li>Interest Rate: {self.yearly_interest_rate}%</li>'
        desc += '</ul>'
        return desc
