# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE
import html
from enum import Enum
from typing import Optional
from ..people.person import Person
from ..base_classes import Loan


class StudentLoanType(Enum):
    """ Enum for student loan types """
    FEDERAL_SUBSIDIZED = "Federal Subsidized"
    FEDERAL_UNSUBSIDIZED = "Federal Unsubsidized"
    PRIVATE = "Private"
    PLUS = "PLUS"


class StudentLoan(Loan):
    def __init__(self, person: Person, loan_type: StudentLoanType, loan_amount: float,
                 yearly_interest_rate: float, length_years: int,
                 school_name: str, principal: Optional[float] = None,
                 monthly_payment: Optional[float] = None,
                 deferment_end_year: Optional[int] = None):
        """ Models a student loan for a person

        Args:
            person: The person to which this loan belongs
            loan_type: Type of student loan
            loan_amount: Original amount of the loan
            yearly_interest_rate: Annual interest rate percentage
            length_years: Length of repayment in years (term starts when
                deferment ends)
            school_name: Name of the educational institution
            principal: Current principal balance (defaults to loan_amount)
            monthly_payment: Monthly payment amount (calculated if not provided)
            deferment_end_year: Last year of in-school deferment. While in
                deferment no payments are due; interest accrues and
                capitalizes except for federal subsidized loans. Repayment is
                amortized over length_years from the post-deferment balance.
        """
        super().__init__(person, loan_amount, yearly_interest_rate, length_years,
                         principal, monthly_payment)
        self.loan_type = loan_type
        self.school_name = school_name
        self.deferment_end_year = deferment_end_year
        self._user_set_payment = monthly_payment is not None
        self._pending_reamortization = deferment_end_year is not None and not self._user_set_payment

    @property
    def in_deferment(self) -> bool:
        """Whether the loan is in its in-school deferment period."""
        return self.deferment_end_year is not None and self.model.year <= self.deferment_end_year

    def borrow(self, amount: float):
        """Disburse an additional amount onto the loan (a later school year)."""
        if amount <= 0:
            return
        self.loan_amount += amount
        self.principal += amount
        if not self._user_set_payment:
            self._pending_reamortization = True
        self._refresh_loan_stats()

    def _amortized_payment_for(self, principal: float) -> float:
        i = self.yearly_interest_rate / (100 * 12)
        n = self.length_years * 12
        if n <= 0:
            return principal
        if i == 0:
            return principal / n
        return principal * (i * ((1 + i) ** n)) / (((1 + i) ** n) - 1)

    def step(self):
        if self.in_deferment:
            # No payments are due while in school. Interest accrues and
            # capitalizes for every type except federal subsidized loans.
            if self.loan_type != StudentLoanType.FEDERAL_SUBSIDIZED:
                self.principal += self.get_interest_amount()
            self.stat_balance_history.append(self.principal)
            self._refresh_loan_stats()
            return

        if self._pending_reamortization:
            # Repayment begins now: amortize the post-deferment balance
            # (including any capitalized interest) over the loan term.
            self.monthly_payment = self._amortized_payment_for(self.principal)
            self._pending_reamortization = False

        super().step()

    def _refresh_loan_type_stats(self):
        super()._refresh_loan_type_stats()
        self.stat_student_loans = 1
        if self.loan_type == StudentLoanType.FEDERAL_SUBSIDIZED:
            self.stat_federal_subsidized_student_loans = 1
        elif self.loan_type == StudentLoanType.FEDERAL_UNSUBSIDIZED:
            self.stat_federal_unsubsidized_student_loans = 1
        elif self.loan_type == StudentLoanType.PRIVATE:
            self.stat_private_student_loans = 1
        elif self.loan_type == StudentLoanType.PLUS:
            self.stat_plus_student_loans = 1

    def get_monthly_payment(self) -> float:
        """ Calculate monthly payment using standard loan formula """
        return self.calculate_monthly_payment()

    def make_payment(self, payment_amount: float, extra_to_principal: float = 0) -> float:
        """Make student loan payment"""
        # Validate inputs
        if payment_amount < 0:
            raise ValueError("Payment amount cannot be negative")
        if extra_to_principal < 0:
            raise ValueError("Extra principal payment cannot be negative")

        if payment_amount == 0 and extra_to_principal == 0:
            # No payment made, but interest still accrues (negative amortization)
            monthly_interest = self.get_interest_amount() / 12
            self.principal += monthly_interest

            # Track statistics
            self.stat_principal_payment_history.append(0.0)
            self.stat_interest_payment_history.append(0.0)

            return 0.0

        # Calculate monthly interest
        monthly_interest = self.get_interest_amount() / 12

        # Calculate how much goes to principal from the regular payment
        available_for_principal = payment_amount - monthly_interest

        # Principal payment cannot be negative and cannot exceed current principal balance
        # Include extra principal in the total principal payment
        total_principal_payment = max(0, available_for_principal) + extra_to_principal
        principal_payment = min(total_principal_payment, self.principal)

        # Calculate actual total payment made
        # If payment_amount < monthly_interest, we only get partial interest payment
        actual_interest_payment = min(payment_amount, monthly_interest)
        total_payment = actual_interest_payment + principal_payment

        # Update principal balance
        # If payment doesn't cover interest, principal grows by unpaid interest
        unpaid_interest = monthly_interest - actual_interest_payment
        self.principal = self.principal - principal_payment + unpaid_interest

        # Ensure principal doesn't go negative
        self.principal = max(0.0, self.principal)

        # Track statistics
        self.stat_principal_payment_history.append(principal_payment)
        self.stat_interest_payment_history.append(actual_interest_payment)

        return total_payment

    def _repr_html_(self):
        desc = '<ul>'
        desc += f'<li>Loan Type: {self.loan_type.value}</li>'
        desc += f'<li>School: {html.escape(self.school_name)}</li>'
        desc += f'<li>Loan Amount: ${self.loan_amount:,.2f}</li>'
        desc += f'<li>Principal Balance: ${self.principal:,.2f}</li>'
        desc += f'<li>Monthly Payment: ${self.monthly_payment:,.2f}</li>'
        desc += f'<li>Interest Rate: {self.yearly_interest_rate}%</li>'
        desc += '</ul>'
        return desc
