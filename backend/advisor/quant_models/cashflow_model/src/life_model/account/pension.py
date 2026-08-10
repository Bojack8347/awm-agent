# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE
import html
from typing import Optional
from ..people.person import Person
from ..base_classes import Benefit


class Pension(Benefit):
    def __init__(self, person: Person, company: str, vesting_years: int = 5,
                 benefit_amount: Optional[float] = None,
                 years_of_service: float = 0,
                 final_average_salary: Optional[float] = None,
                 benefit_multiplier_percent: float = 1.5,
                 payout_start_age: float = 65,
                 cola_percent: float = 0.0):
        """ Models a defined-benefit pension plan for a person

        The annual benefit uses the standard defined-benefit formula
        ``final_average_salary x years_of_service x multiplier`` unless a flat
        ``benefit_amount`` is supplied. Service accrues one year per simulated
        working year, and the final average salary tracks the highest salary
        observed while working when not supplied explicitly. Benefits are paid
        once eligible (vested and at or past ``payout_start_age``), deposited
        into cashflow, and taxed as ordinary income.

        Args:
            person: The person to which this pension belongs
            company: The company providing the pension
            vesting_years: Years of service required before any benefit is owed
            benefit_amount: Optional flat annual benefit; overrides the formula
            years_of_service: Years of service already accrued at the baseline
            final_average_salary: Salary base for the formula. If None, the
                highest salary observed while working is used.
            benefit_multiplier_percent: Percent of salary earned per service
                year (typical plans use 1.0 - 2.0)
            payout_start_age: Age at which benefit payments begin
            cola_percent: Yearly cost-of-living increase applied to payments
        """
        super().__init__(person, company)
        self.vesting_years = vesting_years
        self.benefit_amount = benefit_amount
        self.years_of_service = years_of_service
        self.final_average_salary = final_average_salary
        self._tracked_salary = final_average_salary or 0.0
        self.benefit_multiplier_percent = benefit_multiplier_percent
        self.payout_start_age = payout_start_age
        self.cola_percent = cola_percent
        self._payout_years = 0

        self.stat_pension_income = 0.0

        # Register with the model registry
        self.person.model.registries.pensions.register(person, self)

    @property
    def is_vested(self) -> bool:
        """Whether the person has met the vesting requirement"""
        return self.years_of_service >= self.vesting_years

    def get_annual_benefit(self) -> float:
        """Calculate the annual benefit amount before any COLA"""
        if not self.is_vested:
            return 0.0
        if self.benefit_amount is not None:
            return self.benefit_amount
        salary_base = (self.final_average_salary
                       if self.final_average_salary is not None else self._tracked_salary)
        return salary_base * self.years_of_service * (self.benefit_multiplier_percent / 100)

    def is_eligible(self) -> bool:
        """Check if person is eligible to receive benefits"""
        return self.is_vested and self.person.age >= self.payout_start_age

    def _accrue_service(self):
        working_jobs = [job for job in self.person.jobs if not job.retired]
        if not working_jobs or self.person.is_retired:
            return
        self.years_of_service += 1
        if self.final_average_salary is None:
            top_salary = max(job.salary.base + job.salary.bonus for job in working_jobs)
            self._tracked_salary = max(self._tracked_salary, top_salary)

    def pre_step(self):
        """Accrue service while working; pay the benefit once eligible.

        Payments happen in pre_step so the cash is available to the same
        year's settlement, mirroring Social Security and annuity payouts.
        """
        # Single-life pension: payments stop at death (survivor options
        # are not modeled)
        if getattr(self.person, 'is_deceased', False):
            self.stat_pension_income = 0.0
            return

        self._accrue_service()

        self.stat_pension_income = 0.0
        if not self.is_eligible():
            return

        annual_benefit = self.get_annual_benefit()
        if annual_benefit <= 0:
            return

        benefit = annual_benefit * ((1 + self.cola_percent / 100) ** self._payout_years)
        self._payout_years += 1

        self.person.deposit_into_cashflow_bank_account(benefit)
        self.person.taxable_income += benefit
        self.stat_pension_income = benefit

    def _repr_html_(self):
        desc = '<ul>'
        desc += f'<li>Company: {html.escape(self.company)}</li>'
        desc += f'<li>Vesting Years: {self.vesting_years}</li>'
        desc += f'<li>Years of Service: {self.years_of_service}</li>'
        desc += f'<li>Annual Benefit: ${self.get_annual_benefit():,.2f}</li>'
        desc += '</ul>'
        return desc
