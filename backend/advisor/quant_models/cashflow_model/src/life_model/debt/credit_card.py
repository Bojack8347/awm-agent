# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE
import html
from enum import Enum
from ..people.person import Person
from ..base_classes import Loan


class CreditCardType(Enum):
    """Types of credit cards"""
    VISA = "Visa"
    MASTERCARD = "MasterCard"
    AMERICAN_EXPRESS = "American Express"
    DISCOVER = "Discover"
    STORE_CARD = "Store Card"


class CreditCard(Loan):
    def __init__(self, person: Person, card_name: str, credit_limit: float,
                 current_balance: float = 0, yearly_interest_rate: float = 18.0,
                 minimum_payment_percent: float = 2.0):
        """ Models a credit card for a person

        Args:
            person: The person who owns this credit card
            card_name: Card issuing company
            credit_limit: Credit limit on the card
            current_balance: Current balance owed
            yearly_interest_rate: Annual interest rate percentage
            minimum_payment_percent: Minimum payment as percentage of balance
        """
        # Credit cards don't have a fixed term, so we'll use a dummy value and override
        # the monthly_payment calculation to use minimum payment instead
        super().__init__(person, current_balance, yearly_interest_rate,
                         length_years=1, principal=current_balance,
                         monthly_payment=0)  # We'll calculate this after initialization
        self.card_name = card_name
        self.credit_limit = credit_limit
        self.minimum_payment_percent = minimum_payment_percent
        # Override the monthly payment with our minimum payment calculation
        self.monthly_payment = self.get_minimum_payment()

    def _refresh_loan_type_stats(self):
        super()._refresh_loan_type_stats()
        self.stat_credit_cards = 1

    def get_available_credit(self) -> float:
        """Get available credit remaining"""
        return max(0, self.credit_limit - self.principal)

    def charge(self, amount: float) -> bool:
        """Charge amount to credit card. Returns success status"""
        if amount < 0:
            raise ValueError("Cannot charge negative amounts")
        if amount == 0:
            return True  # No-op for zero charges
        if amount <= self.get_available_credit():
            self.principal += amount
            return True
        return False

    def get_minimum_payment(self) -> float:
        """Calculate minimum payment required"""
        if self.principal <= 0:
            return 0.0
        # Minimum payment is typically 2-3% of balance, but at least $25 if there's a balance
        calculated_minimum = self.principal * (self.minimum_payment_percent / 100)
        return max(25.0, calculated_minimum) if self.principal > 0 else 0.0

    def make_payment(self, payment_amount: float, extra_to_principal: float = 0) -> float:
        """Make credit card payment"""
        if payment_amount < 0:
            raise ValueError("Payment amount cannot be negative")
        if extra_to_principal < 0:
            raise ValueError("Extra principal payment cannot be negative")

        if payment_amount == 0 and extra_to_principal == 0:
            return 0.0  # No payment made

        # Calculate monthly interest (credit cards use monthly compounding)
        monthly_interest_rate = self.yearly_interest_rate / (100 * 12)
        interest_amount = self.principal * monthly_interest_rate

        # Total available for principal payment
        total_available = payment_amount + extra_to_principal

        # For credit cards, we pay interest first, then principal
        if total_available <= interest_amount:
            # Payment doesn't cover interest - principal increases
            unpaid_interest = interest_amount - total_available
            self.principal += unpaid_interest
            principal_payment = 0.0
            interest_payment = total_available
        else:
            # Payment covers interest and some principal
            principal_payment = min(total_available - interest_amount, self.principal)
            interest_payment = interest_amount
            self.principal -= principal_payment

        # Ensure principal doesn't go negative
        self.principal = max(0.0, self.principal)

        # Track statistics
        self.stat_principal_payment_history.append(principal_payment)
        self.stat_interest_payment_history.append(interest_payment)

        return interest_payment + principal_payment

    def _scheduled_monthly_payment(self) -> float:
        if self.principal <= 0:
            return 0.0
        monthly_interest = self.principal * (self.yearly_interest_rate / (100 * 12))
        return min(self.get_minimum_payment(), self.principal + monthly_interest)

    def step(self):
        super().step()
        self.monthly_payment = self.get_minimum_payment()

    def _repr_html_(self):
        desc = '<ul>'
        desc += f'<li>Card: {html.escape(self.card_name)}</li>'
        desc += f'<li>Credit Limit: ${self.credit_limit:,.2f}</li>'
        desc += f'<li>Current Balance: ${self.principal:,.2f}</li>'
        desc += f'<li>Available Credit: ${self.get_available_credit():,.2f}</li>'
        desc += f'<li>Interest Rate: {self.yearly_interest_rate}%</li>'
        desc += f'<li>Minimum Payment: ${self.get_minimum_payment():,.2f}</li>'
        desc += '</ul>'
        return desc
