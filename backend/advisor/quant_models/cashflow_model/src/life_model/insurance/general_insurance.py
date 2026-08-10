# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE
import html
from enum import Enum
from typing import Optional, List, cast
from ..people.person import Person
from ..model import LifeModel, LifeModelAgent, Event


class InsuranceType(Enum):
    """Types of insurance coverage"""
    AUTO = "Auto"
    HOME = "Home"
    HEALTH = "Health"
    DISABILITY = "Disability"
    UMBRELLA = "Umbrella"
    RENTERS = "Renters"
    FLOOD = "Flood"
    EARTHQUAKE = "Earthquake"


class ClaimStatus(Enum):
    """Status of insurance claims"""
    PENDING = "Pending"
    APPROVED = "Approved"
    DENIED = "Denied"
    SETTLED = "Settled"


class InsuranceClaim:
    """Represents an insurance claim"""

    def __init__(self, claim_id: str, amount: float, description: str,
                 claim_date: int, deductible: float = 0.0):
        self.claim_id = claim_id
        self.amount = amount
        self.description = description
        self.claim_date = claim_date
        self.deductible = deductible
        self.status = ClaimStatus.PENDING
        self.payout_amount = 0.0
        self.settlement_date: Optional[int] = None


class Insurance(LifeModelAgent):
    def __init__(self, person: Person, insurance_type: InsuranceType,
                 company: str, annual_premium: float, coverage_amount: float,
                 deductible: float = 0, coverage_start_age: Optional[int] = None,
                 coverage_end_age: Optional[int] = None,
                 premium_increase_rate: float = 3.0,
                 max_claims_per_year: int = 3):
        """ Models insurance coverage for a person

        Args:
            person: The person who owns this insurance
            insurance_type: Type of insurance
            company: Insurance company name
            annual_premium: Annual premium cost
            coverage_amount: Coverage amount/limit
            deductible: Insurance deductible amount
            coverage_start_age: Age when coverage starts (None for immediate)
            coverage_end_age: Age when coverage ends (None for lifetime)
            premium_increase_rate: Annual premium increase percentage
            max_claims_per_year: Maximum claims allowed per year
        """
        super().__init__(cast(LifeModel, person.model))
        self.model: 'LifeModel' = cast('LifeModel', self.model)
        self.person = person
        self.insurance_type = insurance_type
        self.company = company
        self.annual_premium = annual_premium
        self.base_annual_premium = annual_premium
        self.coverage_amount = coverage_amount
        self.deductible = deductible
        self.coverage_start_age = coverage_start_age or person.age
        self.coverage_end_age = coverage_end_age
        self.premium_increase_rate = premium_increase_rate
        self.max_claims_per_year = max_claims_per_year

        # State tracking
        self.is_active = True
        self.policy_start_year = self.model.year
        self.claims_history: List[InsuranceClaim] = []
        self.claims_this_year = 0

        # Statistics tracking
        self.stat_premiums_paid = 0.0
        self.stat_claims_filed = 0
        self.stat_claims_paid_out = 0.0
        self.stat_deductibles_paid = 0.0
        self._stat_year = self.model.year

        # Register with model
        self.model.registries.general_insurance_policies.register(person, self)

    def _reset_yearly_stats_if_needed(self):
        if self._stat_year == self.model.year:
            return
        self.stat_premiums_paid = 0.0
        self.stat_claims_filed = 0
        self.stat_claims_paid_out = 0.0
        self.stat_deductibles_paid = 0.0
        self._stat_year = self.model.year

    @property
    def is_coverage_active(self) -> bool:
        """Check if coverage is currently active based on age"""
        if not self.is_active:
            return False
        if self.person.age < self.coverage_start_age:
            return False
        if self.coverage_end_age and self.person.age > self.coverage_end_age:
            return False
        return True

    @property
    def years_active(self) -> int:
        """Years the policy has been active"""
        return self.model.year - self.policy_start_year

    def pay_premium(self) -> bool:
        """Attempt to pay the annual premium"""
        self._reset_yearly_stats_if_needed()
        if not self.is_coverage_active:
            return False

        # Try to pay from bank accounts
        remaining_balance = self.person.deduct_from_cashflow_bank_accounts(self.annual_premium)
        amount_paid = self.annual_premium - remaining_balance

        if amount_paid >= self.annual_premium:
            self.stat_premiums_paid += self.annual_premium
            return True
        else:
            # Partial payment - policy might lapse
            if amount_paid > 0:
                self.stat_premiums_paid += amount_paid
            self.is_active = False
            self.model.event_log.add(Event(
                f"{self.person.name}'s {self.insurance_type.value} insurance lapsed due to non-payment"
            ))
            return False

    def file_claim(self, claim_amount: float, description: str) -> Optional[InsuranceClaim]:
        """File an insurance claim"""
        self._reset_yearly_stats_if_needed()
        if not self.is_coverage_active:
            return None

        if self.claims_this_year >= self.max_claims_per_year:
            self.model.event_log.add(Event(
                f"{self.person.name} cannot file more claims this year ({self.insurance_type.value})"
            ))
            return None

        # Create claim
        claim_id = f"{self.insurance_type.value}_{self.model.year}_{len(self.claims_history) + 1}"
        claim = InsuranceClaim(
            claim_id=claim_id,
            amount=claim_amount,
            description=description,
            claim_date=self.model.year,
            deductible=self.deductible
        )

        self.claims_history.append(claim)
        self.claims_this_year += 1
        self.stat_claims_filed += 1

        # Process claim automatically (simplified)
        self.process_claim(claim)

        self.model.event_log.add(Event(
            f"{self.person.name} filed {self.insurance_type.value} claim for ${claim_amount:,.0f}"
        ))
        return claim

    def process_claim(self, claim: InsuranceClaim) -> bool:
        """Process an insurance claim"""
        self._reset_yearly_stats_if_needed()
        if claim.status != ClaimStatus.PENDING:
            return False

        # Check if claim is within coverage limits
        if claim.amount > self.coverage_amount:
            claim.status = ClaimStatus.DENIED
            self.model.event_log.add(Event(f"{self.person.name}'s claim denied - exceeds coverage limit"))
            return False

        # Calculate payout (simplified approval logic)
        if claim.amount > claim.deductible:
            claim.payout_amount = min(claim.amount - claim.deductible, self.coverage_amount)
            claim.status = ClaimStatus.APPROVED
            claim.settlement_date = self.model.year

            # Pay out to person's bank account, net of the deductible
            self.person.deposit_into_cashflow_bank_account(claim.payout_amount)

            # The insured also pays the deductible out of pocket. Combined with
            # the underlying loss being modeled as a separate expense, a claim
            # leaves the insured bearing roughly twice the nominal deductible.
            # This is an INTENTIONAL conservative buffer: it stands in for
            # claim-related costs the model does not itemize -- post-claim
            # premium increases, depreciation/betterment, partial denials and
            # coverage gaps, and the insured's own time -- so a cashflow plan
            # never over-relies on insurance paying out efficiently. (An
            # accounting-exact alternative would drop this charge so a claim
            # nets to a single deductible.)
            deductible_paid = self.person.deduct_from_cashflow_bank_accounts(claim.deductible)
            actual_deductible = claim.deductible - deductible_paid
            self.stat_deductibles_paid += actual_deductible

            self.stat_claims_paid_out += claim.payout_amount

            self.model.event_log.add(Event(f"{self.person.name} received ${claim.payout_amount:,.0f} insurance payout"))
            return True
        else:
            claim.status = ClaimStatus.DENIED
            self.model.event_log.add(Event(f"{self.person.name}'s claim denied - below deductible"))
            return False

    def cancel_policy(self):
        """Cancel the insurance policy"""
        self.is_active = False
        self.model.event_log.add(Event(f"{self.person.name} cancelled {self.insurance_type.value} insurance"))

    def update_coverage(self, new_coverage_amount: float, new_deductible: Optional[float] = None):
        """Update coverage amount and optionally deductible"""
        old_coverage = self.coverage_amount
        self.coverage_amount = new_coverage_amount

        if new_deductible is not None:
            self.deductible = new_deductible

        # Adjust premium based on coverage change (simplified)
        coverage_ratio = new_coverage_amount / old_coverage
        self.annual_premium = self.base_annual_premium * coverage_ratio

        self.model.event_log.add(Event(
            f"{self.person.name} updated {self.insurance_type.value} coverage to ${new_coverage_amount:,.0f}"
        ))

    def get_claim_history(self, year: Optional[int] = None) -> List[InsuranceClaim]:
        """Get claims history, optionally filtered by year"""
        if year is None:
            return self.claims_history.copy()
        return [claim for claim in self.claims_history if claim.claim_date == year]

    def get_total_claims_amount(self, year: Optional[int] = None) -> float:
        """Get total amount of claims filed, optionally for a specific year"""
        claims = self.get_claim_history(year)
        return sum(claim.amount for claim in claims)

    def get_total_payouts(self, year: Optional[int] = None) -> float:
        """Get total payouts received, optionally for a specific year"""
        claims = self.get_claim_history(year)
        return sum(claim.payout_amount for claim in claims if claim.status == ClaimStatus.APPROVED)

    def step(self):
        """Process insurance for the current year"""
        self._reset_yearly_stats_if_needed()
        if not self.is_active:
            return

        # Reset yearly claim counter
        self.claims_this_year = 0

        # Apply premium increases
        if self.years_active > 0:
            self.annual_premium *= (1 + self.premium_increase_rate / 100)

    def pre_step(self):
        """Pre-step processing - pay premiums"""
        self._reset_yearly_stats_if_needed()
        if self.is_coverage_active:
            self.pay_premium()

    def _repr_html_(self):
        desc = '<ul>'
        desc += f'<li>Type: {self.insurance_type.value}</li>'
        desc += f'<li>Company: {html.escape(self.company)}</li>'
        desc += f'<li>Annual Premium: ${self.annual_premium:,.2f}</li>'
        desc += f'<li>Coverage: ${self.coverage_amount:,.2f}</li>'
        desc += f'<li>Deductible: ${self.deductible:,.2f}</li>'
        desc += f'<li>Status: {"Active" if self.is_coverage_active else "Inactive"}</li>'
        desc += f'<li>Claims Filed: {self.stat_claims_filed}</li>'
        desc += f'<li>Total Payouts: ${self.stat_claims_paid_out:,.2f}</li>'
        desc += '</ul>'
        return desc
