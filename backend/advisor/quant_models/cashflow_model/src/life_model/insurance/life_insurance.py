# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE
from enum import Enum
from typing import Optional, cast, Union
from ..people.person import Person
from ..model import LifeModel, LifeModelAgent, Event, compound_interest
from ..config.config_manager import config


class LifeInsuranceType(Enum):
    """ Enum for life insurance types """
    TERM = "Term"
    WHOLE = "Whole"


class PremiumIncreaseType(Enum):
    """ Enum for premium increase calculation types """
    AGE_BASED = "age_based"
    YEARLY = "yearly"


class LifeInsurance(LifeModelAgent):
    def __init__(self, person: Person, policy_type: LifeInsuranceType,
                 death_benefit: float, monthly_premium: float,
                 term_years: Optional[int] = None,
                 premium_increase_rate: Union[float, dict, None] = None,
                 cash_value_growth_rate: Optional[float] = None,
                 loan_interest_rate: Optional[float] = None,
                 max_missed_payments: int = 3):
        """ Models life insurance policy for a person

        Args:
            person: The person to which this policy belongs
            policy_type: The type of life insurance policy
            death_benefit: Amount paid out on death
            monthly_premium: Monthly premium cost
            term_years: Number of years for term life (None for whole life)
            premium_increase_rate: Either a yearly percentage increase (float) or
                                 age-based multipliers dict (dict), or None for default age-based
            cash_value_growth_rate: Yearly growth rate for whole life cash value.
                                  Defaults to the configured
                                  insurance.life.default_cash_value_growth_rate,
                                  so economic scenarios can override it.
            loan_interest_rate: Interest rate for loans against cash value.
                              Defaults to the configured
                              insurance.life.default_loan_interest_rate.
            max_missed_payments: Maximum consecutive missed payments before lapse
        """
        super().__init__(cast(LifeModel, person.model))
        self.model: 'LifeModel' = cast('LifeModel', self.model)  # Type override for better intellisense
        self.person = person
        self.policy_type = policy_type
        self.death_benefit = death_benefit
        self.monthly_premium = monthly_premium
        self.base_monthly_premium = monthly_premium
        self.term_years = term_years
        if cash_value_growth_rate is None:
            cash_value_growth_rate = config.financial.get('insurance.life.default_cash_value_growth_rate', 0.0)
        if loan_interest_rate is None:
            loan_interest_rate = config.financial.get('insurance.life.default_loan_interest_rate', 6.0)
        self.cash_value_growth_rate = cash_value_growth_rate
        self.loan_interest_rate = loan_interest_rate
        self.max_missed_payments = max_missed_payments

        # Handle premium_increase_rate parameter - can be float, dict, or None
        if isinstance(premium_increase_rate, dict):
            # Age-based multipliers provided
            self.premium_increase_type = PremiumIncreaseType.AGE_BASED
            self.yearly_increase_rate = 0.0
            self.age_multipliers = premium_increase_rate
        elif isinstance(premium_increase_rate, (int, float)) and premium_increase_rate > 0:
            # Yearly percentage increase provided
            self.premium_increase_type = PremiumIncreaseType.YEARLY
            self.yearly_increase_rate = float(premium_increase_rate)
            self.age_multipliers = {}
        else:
            # Default age-based multipliers for term life
            self.premium_increase_type = PremiumIncreaseType.AGE_BASED
            self.yearly_increase_rate = 0.0
            self.age_multipliers = {
                20: 1.0, 25: 1.1, 30: 1.3, 35: 1.6, 40: 2.1,
                45: 2.8, 50: 3.8, 55: 5.2, 60: 7.1, 65: 10.0,
                70: 15.0, 75: 23.0, 80: 35.0, 85: 55.0
            }

        # Policy state
        self.is_active = True
        self.is_lapsed = False
        self.policy_start_year = self.model.component_anchor_year
        self.consecutive_missed_payments = 0
        self.total_premiums_paid = 0.0

        # Whole life specific
        self.cash_value = 0.0
        self.outstanding_loan_balance = 0.0

        # Statistics tracking
        self.stat_premium_payments = 0.0
        self.stat_cash_value = 0.0
        self.stat_life_insurance_loan_balance = 0.0
        self.stat_death_benefit_paid = 0.0
        self.stat_policy_active = 1
        self._stat_year = self.model.year

        # Register with the model registry
        self.model.registries.life_insurance_policies.register(person, self)

    def _reset_yearly_stats_if_needed(self):
        if self._stat_year == self.model.year:
            return
        self.stat_premium_payments = 0.0
        self.stat_death_benefit_paid = 0.0
        self._stat_year = self.model.year

    def prepare_start_year_stats(self):
        self.stat_cash_value = self.cash_value
        self.stat_life_insurance_loan_balance = self.outstanding_loan_balance

    @property
    def yearly_premium(self) -> float:
        """Calculate yearly premium cost"""
        return self.monthly_premium * 12

    @property
    def available_cash_value(self) -> float:
        """Cash value available for loans (cash value minus outstanding loans)"""
        return max(0, self.cash_value - self.outstanding_loan_balance)

    @property
    def net_death_benefit(self) -> float:
        """Death benefit minus any outstanding loans"""
        return max(0, self.death_benefit - self.outstanding_loan_balance)

    @property
    def policy_years_active(self) -> int:
        """Number of years the policy has been active"""
        return self.model.year - self.policy_start_year

    @property
    def is_term_expired(self) -> bool:
        """Check if term life policy has expired"""
        if self.policy_type == LifeInsuranceType.TERM and self.term_years:
            return self.policy_years_active >= self.term_years
        return False

    def make_premium_payment(self) -> bool:
        """Attempt to pay the yearly premium. Returns True if successful."""
        self._reset_yearly_stats_if_needed()
        if not self.is_active or self.is_lapsed or self.is_term_expired:
            return False

        yearly_cost = self.yearly_premium

        # Try to pay from bank accounts first
        remaining_balance = self.person.deduct_from_cashflow_bank_accounts(yearly_cost)
        amount_paid = yearly_cost - remaining_balance

        if amount_paid >= yearly_cost:
            self.total_premiums_paid += yearly_cost
            self.consecutive_missed_payments = 0
            self.stat_premium_payments = yearly_cost

            # For whole life, part of premium goes to cash value
            if self.policy_type == LifeInsuranceType.WHOLE:
                # Typically 50-80% of premium goes to cash value after first year
                cash_value_contribution = yearly_cost * 0.6 if self.policy_years_active > 0 else yearly_cost * 0.3
                self.cash_value += cash_value_contribution

            return True
        else:
            # Partial payment or no payment from bank
            if amount_paid > 0:
                self.total_premiums_paid += amount_paid

            # For whole life, can use cash value to pay premiums
            if self.policy_type == LifeInsuranceType.WHOLE and self.available_cash_value > 0:
                remaining_premium = yearly_cost - amount_paid
                cash_value_used = min(remaining_premium, self.available_cash_value)
                self.cash_value -= cash_value_used
                amount_paid += cash_value_used

                # If we used any cash value, consider the payment successful
                # (This is common in real life insurance - cash value keeps policy in force)
                if cash_value_used > 0:
                    self.consecutive_missed_payments = 0
                    self.stat_premium_payments = amount_paid
                    return True

            # Payment failed
            self.consecutive_missed_payments += 1
            self.stat_premium_payments = amount_paid

            # Check if policy should lapse
            if self.consecutive_missed_payments >= self.max_missed_payments:
                self.lapse_policy()

            return False

    def lapse_policy(self):
        """Lapse the policy due to non-payment"""
        self.is_lapsed = True
        self.is_active = False
        self.model.event_log.add(Event(f"{self.person.name}'s life insurance policy lapsed due to non-payment"))

    def drop_policy(self):
        """Voluntarily drop the policy (e.g., when no longer needed)"""
        self.is_active = False

        # For whole life policies, person gets the cash surrender value
        if self.policy_type == LifeInsuranceType.WHOLE and self.cash_value > 0:
            # Surrender value is typically 70-90% of cash value after first few years
            surrender_percentage = 0.8 if self.policy_years_active >= 3 else 0.5
            surrender_value = self.available_cash_value * surrender_percentage

            if surrender_value > 0:
                self.person.deposit_into_cashflow_bank_account(surrender_value)
                self.model.event_log.add(
                    Event(f"{self.person.name} surrendered life insurance policy for ${surrender_value:,.0f}"))
                self.cash_value = 0.0

        self.model.event_log.add(Event(f"{self.person.name} voluntarily dropped their life insurance policy"))

    def take_loan(self, loan_amount: float) -> float:
        """Take a loan against cash value (whole life only)"""
        if self.policy_type != LifeInsuranceType.WHOLE or not self.is_active:
            return 0.0

        # Calculate what portion of requested amount can actually be borrowed
        # Typically can borrow 90% of the requested amount up to the available cash value
        potential_loan = min(loan_amount, self.available_cash_value)
        actual_loan = potential_loan * 0.9  # 90% loan-to-value ratio

        if actual_loan > 0:
            self.outstanding_loan_balance += actual_loan
            self.person.deposit_into_cashflow_bank_account(actual_loan)
            self.model.event_log.add(Event(f"{self.person.name} took ${actual_loan:,.0f} loan against life insurance"))

        return actual_loan

    def repay_loan(self, repayment_amount: float) -> float:
        """Repay loan against the policy"""
        if self.outstanding_loan_balance <= 0:
            return 0.0

        actual_repayment = min(repayment_amount, self.outstanding_loan_balance)
        remaining_balance = self.person.deduct_from_cashflow_bank_accounts(actual_repayment)
        amount_paid = actual_repayment - remaining_balance

        self.outstanding_loan_balance -= amount_paid
        return amount_paid

    def process_death_benefit(self):
        """Process death benefit payout"""
        self._reset_yearly_stats_if_needed()
        if not self.is_active or self.is_lapsed:
            return 0.0

        benefit_amount = self.net_death_benefit
        if benefit_amount > 0:
            # For simplicity, add to spouse's bank account if married, otherwise to family
            if self.person.spouse and hasattr(self.person.spouse, 'deposit_into_cashflow_bank_account'):
                self.person.spouse.deposit_into_cashflow_bank_account(benefit_amount)
                recipient = self.person.spouse.name
            else:
                # Add to family's primary account (first family member with bank account)
                for member in self.person.family.members:
                    if member != self.person and hasattr(member, 'deposit_into_cashflow_bank_account'):
                        member.deposit_into_cashflow_bank_account(benefit_amount)
                        recipient = member.name
                        break
                else:
                    recipient = "estate"

            self.stat_death_benefit_paid = benefit_amount
            self.model.event_log.add(
                Event(f"Life insurance death benefit of ${benefit_amount:,.0f} paid to {recipient}"))

        return benefit_amount

    def pre_step(self):
        """Pre-step phase: Pay premiums before other expenses"""
        self._reset_yearly_stats_if_needed()
        # Only pay premiums in pre_step to ensure they happen before other expenses
        if self.is_active and not self.is_term_expired:
            self.make_premium_payment()

    def step(self):
        """Yearly step function"""
        self._reset_yearly_stats_if_needed()
        if not self.is_active:
            self.stat_policy_active = 0
            return

        # Check if term policy has expired
        if self.is_term_expired:
            self.is_active = False
            self.model.event_log.add(Event(f"{self.person.name}'s term life insurance policy expired"))
            self.stat_policy_active = 0
            return

        # Update premiums based on the premium increase type
        if self.policy_type == LifeInsuranceType.TERM:
            if self.premium_increase_type == PremiumIncreaseType.YEARLY and self.yearly_increase_rate > 0:
                # Use yearly percentage increase
                self.monthly_premium += self.monthly_premium * (self.yearly_increase_rate / 100)
            else:
                # Use age-based increases (default or custom multipliers)
                self.update_term_life_premiums()
        elif self.premium_increase_type == PremiumIncreaseType.YEARLY and self.yearly_increase_rate > 0:
            # Whole life policies with yearly percentage increases
            self.monthly_premium += self.monthly_premium * (self.yearly_increase_rate / 100)

        # Grow cash value for whole life policies
        if self.policy_type == LifeInsuranceType.WHOLE and self.cash_value > 0:
            growth = compound_interest(self.cash_value, self.cash_value_growth_rate)
            self.cash_value += growth

        # Compound interest on outstanding loans
        if self.outstanding_loan_balance > 0:
            loan_interest = self.outstanding_loan_balance * (self.loan_interest_rate / 100)
            self.outstanding_loan_balance += loan_interest

        # Update stats
        self.stat_cash_value = self.cash_value
        self.stat_life_insurance_loan_balance = self.outstanding_loan_balance
        self.stat_policy_active = 1 if self.is_active else 0

    def calculate_age_based_premium_increase(self) -> float:
        """Calculate realistic age-based premium increase for term life insurance

        Returns the new monthly premium based on current age.
        Term life premiums typically increase exponentially with age.
        """
        if self.policy_type != LifeInsuranceType.TERM:
            return self.monthly_premium

        current_age = self.person.age

        # Find the appropriate multiplier for current age
        multiplier = 1.0
        for age_threshold in sorted(self.age_multipliers.keys()):
            if current_age >= age_threshold:
                multiplier = self.age_multipliers[age_threshold]
            else:
                break

        # Interpolate between age brackets for more realistic pricing
        if current_age not in self.age_multipliers:
            age_brackets = sorted(self.age_multipliers.keys())
            for i in range(len(age_brackets) - 1):
                if age_brackets[i] <= current_age < age_brackets[i + 1]:
                    lower_age = age_brackets[i]
                    upper_age = age_brackets[i + 1]
                    lower_mult = self.age_multipliers[lower_age]
                    upper_mult = self.age_multipliers[upper_age]

                    # Linear interpolation
                    age_progress = (current_age - lower_age) / (upper_age - lower_age)
                    multiplier = lower_mult + (upper_mult - lower_mult) * age_progress
                    break

        return self.base_monthly_premium * multiplier

    def update_term_life_premiums(self):
        """Update term life insurance premiums based on current age"""
        if self.policy_type == LifeInsuranceType.TERM:
            old_premium = self.monthly_premium
            self.monthly_premium = self.calculate_age_based_premium_increase()

            # Log significant premium increases (>20%)
            if self.monthly_premium > old_premium * 1.2:
                increase_pct = ((self.monthly_premium - old_premium) / old_premium) * 100
                self.model.event_log.add(Event(
                    f"{self.person.name}'s term life premium increased {increase_pct:.0f}% "
                    f"(${old_premium:.0f} -> ${self.monthly_premium:.0f}/month) due to age"
                ))

    def _repr_html_(self):
        desc = '<ul>'
        desc += f'<li>Policy Type: {self.policy_type.value}</li>'
        desc += f'<li>Death Benefit: ${self.death_benefit:,}</li>'
        desc += f'<li>Monthly Premium: ${self.monthly_premium:,.2f}</li>'
        if self.policy_type == LifeInsuranceType.TERM and self.term_years:
            desc += f'<li>Term: {self.term_years} years</li>'
        if self.policy_type == LifeInsuranceType.WHOLE:
            desc += f'<li>Cash Value: ${self.cash_value:,.2f}</li>'
            if self.outstanding_loan_balance > 0:
                desc += f'<li>Outstanding Loan: ${self.outstanding_loan_balance:,.2f}</li>'
        desc += f'<li>Status: {"Active" if self.is_active else "Lapsed"}</li>'
        desc += '</ul>'
        return desc
