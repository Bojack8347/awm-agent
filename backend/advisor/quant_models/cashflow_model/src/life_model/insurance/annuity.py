# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE
from enum import Enum
from typing import Optional, cast
from ..people.person import Person, GenderAtBirth
from ..people.mortality import get_chance_of_mortality
from ..model import LifeModel, LifeModelAgent, Event


class AnnuityType(Enum):
    """ Enum for annuity types """
    FIXED = "Fixed"
    VARIABLE = "Variable"
    IMMEDIATE = "Immediate"
    DEFERRED = "Deferred"


class AnnuityPayoutType(Enum):
    """ Enum for annuity payout types """
    LIFE_ONLY = "Life Only"
    LIFE_WITH_PERIOD_CERTAIN = "Life with Period Certain"
    JOINT_AND_SURVIVOR = "Joint and Survivor"
    LUMP_SUM = "Lump Sum"


def calculate_life_expectancy(age: int, gender: Optional[GenderAtBirth] = None) -> float:
    """Calculate life expectancy using actuarial mortality tables

    Args:
        age: Current age of the person
        gender: Gender for more accurate calculation (optional)

    Returns:
        Expected remaining years of life
    """
    if age >= 119:
        return 0.5  # Minimum life expectancy

    remaining_years = 0.0
    survival_probability = 1.0

    # Calculate expected remaining life using mortality tables
    for future_age in range(age, 120):
        if gender is not None:
            mortality_rate = get_chance_of_mortality(future_age, gender)
        else:
            # Use average of male and female rates if gender not specified
            male_rate = get_chance_of_mortality(future_age, GenderAtBirth.MALE)
            female_rate = get_chance_of_mortality(future_age, GenderAtBirth.FEMALE)
            mortality_rate = (male_rate + female_rate) / 2

        # Calculate probability of surviving this year
        year_survival_prob = 1 - mortality_rate

        # Add expected fraction of year lived
        remaining_years += survival_probability * year_survival_prob

        # Update survival probability for next year
        survival_probability *= year_survival_prob

        # Stop if survival probability becomes negligible
        if survival_probability < 0.001:
            break

    return max(remaining_years, 0.5)  # Minimum 6 months


def calculate_annuity_factor(age: int, interest_rate: float, payout_type: AnnuityPayoutType,
                             period_certain_years: int = 0, gender: Optional[GenderAtBirth] = None) -> float:
    """Calculate annuity factor using actuarial principles

    Args:
        age: Current age of annuitant
        interest_rate: Annual interest rate (as percentage)
        payout_type: Type of annuity payout
        period_certain_years: Years of guaranteed payments for period certain
        gender: Gender for mortality calculations

    Returns:
        Annuity factor (present value of $1 annuity)
    """
    monthly_rate = interest_rate / 100 / 12
    annuity_factor = 0.0

    if payout_type == AnnuityPayoutType.LIFE_ONLY:
        # Pure life annuity - payments until death
        survival_probability = 1.0
        for month in range(12 * 80):  # Up to age 120
            current_age = age + month / 12
            if current_age >= 120:
                break

            # Get mortality rate for this age
            age_int = int(current_age)
            if gender is not None:
                annual_mortality = get_chance_of_mortality(age_int, gender)
            else:
                male_rate = get_chance_of_mortality(age_int, GenderAtBirth.MALE)
                female_rate = get_chance_of_mortality(age_int, GenderAtBirth.FEMALE)
                annual_mortality = (male_rate + female_rate) / 2

            monthly_mortality = annual_mortality / 12
            monthly_survival = 1 - monthly_mortality

            # Present value of payment if alive
            discount_factor = (1 + monthly_rate) ** (-month)
            annuity_factor += survival_probability * discount_factor

            # Update survival probability
            survival_probability *= monthly_survival

            if survival_probability < 0.001:
                break

    elif payout_type == AnnuityPayoutType.LIFE_WITH_PERIOD_CERTAIN:
        # Life annuity with guaranteed period
        guaranteed_months = period_certain_years * 12
        survival_probability = 1.0

        for month in range(12 * 80):
            current_age = age + month / 12
            if current_age >= 120 and month >= guaranteed_months:
                break

            discount_factor = (1 + monthly_rate) ** (-month)

            if month < guaranteed_months:
                # Guaranteed payment regardless of survival
                annuity_factor += discount_factor
            else:
                # Payment only if alive after guaranteed period
                age_int = int(current_age)
                if gender is not None:
                    annual_mortality = get_chance_of_mortality(age_int, gender)
                else:
                    male_rate = get_chance_of_mortality(age_int, GenderAtBirth.MALE)
                    female_rate = get_chance_of_mortality(age_int, GenderAtBirth.FEMALE)
                    annual_mortality = (male_rate + female_rate) / 2

                monthly_mortality = annual_mortality / 12
                monthly_survival = 1 - monthly_mortality

                annuity_factor += survival_probability * discount_factor
                survival_probability *= monthly_survival

                if survival_probability < 0.001:
                    break

    else:
        # For other types, use simplified calculation
        life_expectancy = calculate_life_expectancy(age, gender)
        total_months = life_expectancy * 12

        if monthly_rate > 0:
            annuity_factor = (1 - (1 + monthly_rate) ** (-total_months)) / monthly_rate
        else:
            annuity_factor = total_months

    return annuity_factor


class Annuity(LifeModelAgent):
    def __init__(self, person: Person, annuity_type: AnnuityType,
                 initial_balance: float = 0.0, interest_rate: float = 3.0,
                 payout_type: AnnuityPayoutType = AnnuityPayoutType.LIFE_ONLY,
                 payout_start_age: Optional[int] = None,
                 monthly_payout: Optional[float] = None,
                 period_certain_years: int = 10,
                 surrender_charge_years: int = 7,
                 surrender_charge_rate: float = 7.0,
                 survivor_benefit_percent: float = 100.0):
        """ Models an annuity for a person

        Args:
            person: The person to which this annuity belongs
            annuity_type: The type of annuity
            initial_balance: Starting balance in the annuity
            interest_rate: Annual interest rate percentage
            payout_type: How the annuity pays out
            payout_start_age: Age when payouts begin (None for immediate)
            monthly_payout: Fixed monthly payout amount (calculated if None)
            period_certain_years: Years of guaranteed payments for period certain
            surrender_charge_years: Years during which surrender charges apply
            surrender_charge_rate: Annual surrender charge rate percentage
            survivor_benefit_percent: For a JOINT_AND_SURVIVOR annuity, the
                percentage of the monthly payout that continues to the
                surviving spouse after the owner's death (e.g. 100 for full,
                50 for a half survivor benefit). Ignored for other payout types.
        """
        super().__init__(cast(LifeModel, person.model))
        self.model: 'LifeModel' = cast('LifeModel', self.model)
        self.person = person
        self.annuity_type = annuity_type
        self.balance = initial_balance
        self.interest_rate = interest_rate
        self.payout_type = payout_type
        self.payout_start_age = payout_start_age or (65 if annuity_type == AnnuityType.DEFERRED else person.age)
        self.monthly_payout = monthly_payout
        self.period_certain_years = period_certain_years
        self.surrender_charge_years = surrender_charge_years
        self.surrender_charge_rate = surrender_charge_rate
        self.survivor_benefit_percent = survivor_benefit_percent

        # State tracking
        self.is_active = True
        self.is_annuitized = False
        self.annuitization_year = None
        self.purchase_year = self.model.component_anchor_year
        self.remaining_period_certain_payments = 0

        # Tax tracking. Modeled as a non-qualified annuity: premiums are
        # after-tax basis, pre-annuitization withdrawals are gains-first
        # (IRC §72(e)), and annuitized payouts use the exclusion ratio
        # (IRC §72(b)) until basis is fully recovered.
        self.cost_basis = initial_balance
        self.exclusion_ratio = 0.0
        self.unrecovered_basis = 0.0

        # Statistics tracking
        self.stat_balance = initial_balance
        self.stat_interest_earned = 0.0
        self.stat_payouts_received = 0.0
        self.stat_surrender_charges_paid = 0.0
        self._stat_year = self.model.year
        self._payout_applied_year = None

        # Register with model
        self.model.registries.annuities.register(person, self)

    def _reset_yearly_stats_if_needed(self):
        if self._stat_year == self.model.year:
            return
        self.stat_interest_earned = 0.0
        self.stat_payouts_received = 0.0
        self.stat_surrender_charges_paid = 0.0
        self._stat_year = self.model.year

    @property
    def years_since_purchase(self) -> int:
        """Years since the annuity was purchased"""
        return self.model.year - self.purchase_year

    @property
    def is_in_surrender_period(self) -> bool:
        """Whether surrender charges apply"""
        return self.years_since_purchase < self.surrender_charge_years

    @property
    def is_payout_eligible(self) -> bool:
        """Whether the person is eligible to start receiving payouts"""
        return self.person.age >= self.payout_start_age

    @property
    def surrender_charge_amount(self) -> float:
        """Calculate current surrender charge if annuity is surrendered"""
        if not self.is_in_surrender_period:
            return 0.0

        # Surrender charge typically decreases each year
        years_remaining = self.surrender_charge_years - self.years_since_purchase
        charge_rate = (years_remaining / self.surrender_charge_years) * self.surrender_charge_rate
        return self.balance * (charge_rate / 100)

    def deposit(self, amount: float) -> bool:
        """Deposit additional funds into the annuity (if not annuitized)"""
        if self.is_annuitized or not self.is_active:
            return False

        # Try to deduct from bank accounts
        remaining_balance = self.person.deduct_from_cashflow_bank_accounts(amount)
        amount_deposited = amount - remaining_balance

        if amount_deposited > 0:
            self.balance += amount_deposited
            self.cost_basis += amount_deposited
            self.model.event_log.add(Event(f"{self.person.name} deposited ${amount_deposited:,.0f} into annuity"))
            return True
        return False

    def withdraw(self, amount: float) -> float:
        """Withdraw funds from the annuity (with potential surrender charges)"""
        self._reset_yearly_stats_if_needed()
        if self.is_annuitized or not self.is_active or amount <= 0:
            return 0.0

        # Calculate available amount after surrender charges
        withdrawal_amount = min(amount, self.balance)

        # Non-qualified annuity withdrawals are gains-first (IRC §72(e)):
        # earnings come out before the after-tax basis and are ordinary income.
        taxable_gain = min(withdrawal_amount, max(0.0, self.balance - self.cost_basis))
        if taxable_gain > 0:
            self.person.taxable_income += taxable_gain
        self.cost_basis = max(0.0, self.cost_basis - (withdrawal_amount - taxable_gain))

        surrender_charge = 0.0

        if self.is_in_surrender_period:
            surrender_charge = withdrawal_amount * (self.surrender_charge_rate / 100)
            surrender_charge *= (self.surrender_charge_years - self.years_since_purchase) / self.surrender_charge_years
            self.stat_surrender_charges_paid += surrender_charge

        net_withdrawal = withdrawal_amount - surrender_charge
        self.balance -= withdrawal_amount

        # Add to person's bank account
        self.person.deposit_into_cashflow_bank_account(net_withdrawal)

        if surrender_charge > 0:
            self.model.event_log.add(Event(
                f"{self.person.name} withdrew ${net_withdrawal:,.0f} from annuity "
                f"(${surrender_charge:,.0f} surrender charge)"
            ))
        else:
            self.model.event_log.add(Event(f"{self.person.name} withdrew ${net_withdrawal:,.0f} from annuity"))

        return net_withdrawal

    def annuitize(self) -> bool:
        """Convert the annuity to income payments"""
        if self.is_annuitized or not self.is_active or self.balance <= 0:
            return False

        if not self.is_payout_eligible:
            return False

        self.is_annuitized = True
        self.annuitization_year = self.model.year

        # The guaranteed-payment counter applies regardless of whether the
        # payout was supplied explicitly or derived from the annuity factor;
        # it drives the period-certain continuation to survivors at death.
        if self.payout_type == AnnuityPayoutType.LIFE_WITH_PERIOD_CERTAIN:
            self.remaining_period_certain_payments = self.period_certain_years * 12

        # Calculate monthly payout if not specified
        if self.monthly_payout is None:
            # Use actuarial tables to calculate proper annuity payment
            # Determine gender if available (default to None for average calculation)
            gender = getattr(self.person, 'gender', None)

            # Calculate annuity factor using mortality tables
            annuity_factor = calculate_annuity_factor(
                age=self.person.age,
                interest_rate=self.interest_rate,
                payout_type=self.payout_type,
                period_certain_years=self.period_certain_years,
                gender=gender
            )

            # Calculate monthly payment: balance divided by annuity factor
            if annuity_factor > 0:
                self.monthly_payout = self.balance / annuity_factor
            else:
                # Fallback to simple calculation if factor is zero
                life_expectancy = calculate_life_expectancy(self.person.age, gender)
                self.monthly_payout = self.balance / (life_expectancy * 12)

        # Exclusion ratio in the spirit of IRC §72(b): the fraction of each
        # payout that is a tax-free return of after-tax basis. The statute
        # divides basis by the actuarially expected return; in this model the
        # balance stops accruing interest at annuitization and payouts stop
        # when it is exhausted, so total payouts equal the balance at
        # annuitization exactly. Dividing by that balance taxes precisely the
        # interest credited and never the principal.
        self.exclusion_ratio = min(1.0, self.cost_basis / self.balance) if self.balance > 0 else 0.0
        self.unrecovered_basis = self.cost_basis

        self.model.event_log.add(Event(
            f"{self.person.name} annuitized with ${self.monthly_payout:,.0f}/month payments"
        ))
        return True

    def _taxable_payout_portion(self, payout: float) -> float:
        """Split a payout into excluded basis and taxable income."""
        excluded = min(payout * self.exclusion_ratio, self.unrecovered_basis)
        self.unrecovered_basis -= excluded
        return payout - excluded

    def _surviving_recipient(self):
        """First surviving family member other than the (deceased) owner.

        Used to continue survivor-eligible payouts after the owner dies. The
        owner's ``spouse`` link is cleared during ``Person.die``, so this walks
        the family roster rather than relying on ``spouse``.
        """
        family = getattr(self.person, 'family', None)
        if family is None:
            return None
        for member in family.members:
            if member is self.person:
                continue
            if getattr(member, 'is_deceased', False):
                continue
            if hasattr(member, 'deposit_into_cashflow_bank_account'):
                return member
        return None

    def make_payout(self) -> float:
        """Make monthly annuity payment if eligible"""
        self._reset_yearly_stats_if_needed()
        if not self.is_annuitized or not self.is_active:
            return 0.0

        person_deceased = getattr(self.person, 'is_deceased', False) or not getattr(self.person, 'is_alive', True)
        payout_due = self.monthly_payout or 0.0
        if payout_due <= 0:
            return 0.0

        if person_deceased:
            # Handle period certain payments to beneficiaries
            if self.payout_type == AnnuityPayoutType.LIFE_WITH_PERIOD_CERTAIN and \
               self.remaining_period_certain_payments > 0:
                payout = min(payout_due, self.balance)
                if payout <= 0:
                    self.is_active = False
                    return 0.0
                self.balance -= payout
                self.remaining_period_certain_payments -= 1
                self.stat_payouts_received += payout
                taxable_portion = self._taxable_payout_portion(payout)

                # Add to family income or first surviving family member
                if hasattr(self.person, 'family') and self.person.family.members:
                    for family_member in self.person.family.members:
                        if family_member != self.person:  # Simplified survivor check
                            if hasattr(family_member, 'deposit_into_cashflow_bank_account'):
                                family_member.deposit_into_cashflow_bank_account(payout)
                                family_member.taxable_income += taxable_portion
                            break

                return payout
            if self.payout_type == AnnuityPayoutType.JOINT_AND_SURVIVOR:
                # Joint-and-survivor: payouts continue to the surviving spouse
                # (scaled by survivor_benefit_percent) until the balance is
                # exhausted. If nobody survives to receive them, the remaining
                # balance is forfeited to the insurer rather than stranded as a
                # phantom asset on the balance sheet.
                survivor = self._surviving_recipient()
                survivor_payout = min(payout_due * (self.survivor_benefit_percent / 100),
                                      self.balance)
                if survivor is None or survivor_payout <= 0:
                    self.balance = 0.0
                    self.is_active = False
                    self.stat_balance = 0.0
                    return 0.0
                self.balance -= survivor_payout
                self.stat_payouts_received += survivor_payout
                taxable_portion = self._taxable_payout_portion(survivor_payout)
                survivor.deposit_into_cashflow_bank_account(survivor_payout)
                survivor.taxable_income += taxable_portion
                if self.balance <= 0:
                    self.balance = 0.0
                    self.is_active = False
                return survivor_payout
            else:
                # No more payments
                self.is_active = False
                return 0.0

        # Person is alive, make normal payment
        payout = min(payout_due, self.balance)
        if payout <= 0:
            self.is_active = False
            return 0.0
        self.balance -= payout
        self.stat_payouts_received += payout
        self.person.taxable_income += self._taxable_payout_portion(payout)

        # Add to person's bank account
        self.person.deposit_into_cashflow_bank_account(payout)

        # Reduce period certain payments if applicable
        if self.payout_type == AnnuityPayoutType.LIFE_WITH_PERIOD_CERTAIN and \
           self.remaining_period_certain_payments > 0:
            self.remaining_period_certain_payments -= 1

        if self.balance <= 0:
            self.balance = 0.0
            self.is_active = False

        return payout

    def handle_owner_death(self):
        """Apply payout-type death rules when the owner dies.

        - Annuitized LIFE_ONLY: the remaining balance is forfeited to the
          insurer; payments stop immediately.
        - Annuitized LIFE_WITH_PERIOD_CERTAIN: stays active; make_payout's
          deceased branch pays the remaining guaranteed payments to the
          surviving family member.
        - Annuitized JOINT_AND_SURVIVOR: stays active; make_payout's deceased
          branch continues payouts (scaled by survivor_benefit_percent) to the
          surviving spouse until the balance is exhausted. If no one survives,
          make_payout forfeits the remaining balance to the insurer.
        - Not yet annuitized (accumulation phase): the accumulated value is
          the death benefit. It pays to the surviving spouse or first other
          family member; gains over the after-tax basis are income in respect
          of a decedent, taxed as ordinary income to the recipient.
        """
        self._reset_yearly_stats_if_needed()
        if not self.is_active:
            return

        if self.is_annuitized:
            if self.payout_type == AnnuityPayoutType.LIFE_ONLY:
                self.balance = 0.0
                self.is_active = False
                self.stat_balance = 0.0
            return

        death_benefit = self.balance
        if death_benefit <= 0:
            self.is_active = False
            return

        recipient = self.person.spouse
        if recipient is None:
            recipient = next(
                (member for member in self.person.family.members if member is not self.person),
                None,
            )

        taxable_gain = max(0.0, death_benefit - self.cost_basis)
        if recipient is not None:
            recipient.deposit_into_cashflow_bank_account(death_benefit)
            if taxable_gain > 0:
                recipient.taxable_income += taxable_gain
            self.model.event_log.add(Event(
                f"Annuity death benefit of ${death_benefit:,.0f} paid to {recipient.name}"))

        self.balance = 0.0
        self.cost_basis = 0.0
        self.is_active = False
        self.stat_balance = 0.0

    def surrender(self) -> float:
        """Surrender the annuity for cash value"""
        self._reset_yearly_stats_if_needed()
        if not self.is_active or self.balance <= 0:
            return 0.0

        surrender_charge = self.surrender_charge_amount
        net_value = self.balance - surrender_charge

        self.stat_surrender_charges_paid += surrender_charge

        # Surrender proceeds above the after-tax basis are ordinary income
        taxable_gain = max(0.0, net_value - self.cost_basis)
        if taxable_gain > 0:
            self.person.taxable_income += taxable_gain
        self.cost_basis = 0.0

        # Add to person's bank account
        self.person.deposit_into_cashflow_bank_account(net_value)

        self.model.event_log.add(Event(
            f"{self.person.name} surrendered annuity for ${net_value:,.0f} "
            f"(${surrender_charge:,.0f} charge)"
        ))

        self.balance = 0.0
        self.is_active = False
        self.stat_balance = 0.0

        return net_value

    def _make_annual_payouts_once(self):
        if self._payout_applied_year == self.model.year:
            return
        self._payout_applied_year = self.model.year
        if not self.is_annuitized:
            return
        for _month in range(12):
            self.make_payout()
        self.stat_balance = self.balance

    def step(self):
        """Process annuity for the current year"""
        self._reset_yearly_stats_if_needed()
        if not self.is_active:
            return

        # Apply interest growth to non-annuitized balance
        if not self.is_annuitized and self.balance > 0:
            interest_earned = self.balance * (self.interest_rate / 100)
            self.balance += interest_earned
            self.stat_interest_earned += interest_earned

        # Payouts normally happen in pre_step so cash is available to the
        # person's/family's same-year settlement. Keep this guarded fallback
        # for direct method callers.
        self._make_annual_payouts_once()
        self.stat_balance = self.balance

    def pre_step(self):
        """Pre-step processing"""
        self._reset_yearly_stats_if_needed()
        # Auto-annuitize immediate annuities or when payout age is reached
        if not self.is_annuitized and (
            self.annuity_type == AnnuityType.IMMEDIATE or
            (self.annuity_type == AnnuityType.DEFERRED and self.is_payout_eligible)
        ):
            self.annuitize()
        self._make_annual_payouts_once()

    def _repr_html_(self):
        desc = '<ul>'
        desc += f'<li>Type: {self.annuity_type.value}</li>'
        desc += f'<li>Balance: ${self.balance:,.2f}</li>'
        desc += f'<li>Interest Rate: {self.interest_rate}%</li>'
        desc += f'<li>Payout Type: {self.payout_type.value}</li>'
        if self.is_annuitized:
            desc += f'<li>Monthly Payout: ${self.monthly_payout:,.2f}</li>'
        else:
            desc += f'<li>Payout Start Age: {self.payout_start_age}</li>'
        if self.is_in_surrender_period:
            desc += f'<li>Surrender Charge: ${self.surrender_charge_amount:,.2f}</li>'
        desc += f'<li>Status: {"Annuitized" if self.is_annuitized else "Accumulation Phase"}</li>'
        desc += '</ul>'
        return desc
