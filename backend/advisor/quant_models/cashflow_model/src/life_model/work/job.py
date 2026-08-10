# Copyright 2022 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

import html
from typing import Optional, TYPE_CHECKING
from ..config.config_manager import config
from ..model import LifeModelAgent, LifeModel, Event
from ..limits import job_401k_contrib_limit
from ..people.person import Person

if TYPE_CHECKING:
    from ..account.job401k import Job401kAccount


class Job(LifeModelAgent):
    def __init__(self, owner: Person, company: str, role: str, salary: 'Salary'):
        """Job

        Args:
            owner (Person): Person who owns the job.
            company (str): Company name.
            role (str): Job title.
            salary (Salary): Salary of the job.
        """
        super().__init__(owner.model)
        self.owner = owner
        self.company = company
        self.role = role
        self.salary = salary
        self.retirement_account: Optional['Job401kAccount'] = None
        self.retired = False
        self.income_multiplier = 1.0

        self.stat_gross_income = 0
        self.stat_retirement_contrib = 0
        self.stat_retirement_match = 0

        # Register with the model registry
        self.model.registries.jobs.register(self.owner, self)

    def _repr_html_(self):
        return f"{html.escape(self.role)} at {html.escape(self.company)}"

    def prepare_start_year_stats(self):
        self.stat_gross_income = 0
        self.stat_retirement_contrib = 0
        self.stat_retirement_match = 0

    # Using pre_step() so taxable_income will be set before person's step() is called
    def pre_step(self):

        # If retired, don't do anything
        if self.retired or self.owner.is_retired:
            if self.owner.is_retired:
                self.retire()
            self.stat_gross_income = 0
            self.stat_retirement_contrib = 0
            self.stat_retirement_match = 0
            return

        effective_salary = self.salary.base * max(0.0, self.income_multiplier)
        remaining_401k_contrib = min(
            effective_salary,
            job_401k_contrib_limit(self.owner.age),
        )
        # Deduct pre-tax contribution from income
        if self.retirement_account is not None:
            yearly_pretax_contrib = min(remaining_401k_contrib,
                                        self.retirement_account.pretax_contrib(effective_salary))
            remaining_401k_contrib -= yearly_pretax_contrib
        else:
            yearly_pretax_contrib = 0

        # Deduct roth contribution from income
        if self.retirement_account is not None:
            yearly_roth_contrib = min(
                remaining_401k_contrib,
                self.retirement_account.roth_contrib(effective_salary),
            )
            remaining_401k_contrib -= yearly_roth_contrib
        else:
            yearly_roth_contrib = 0

        # Note: Contributions are handled here, after 401k growth is calculated
        # This isn't 100% accurate since contributions aren't included in the
        # growth, which is a little pessimistic but that should be fine
        if self.retirement_account is not None:
            self.retirement_account.pretax_balance += yearly_pretax_contrib
            self.retirement_account.roth_balance += yearly_roth_contrib
            self.retirement_account.roth_basis += yearly_roth_contrib
            yearly_401k_contrib = yearly_pretax_contrib + yearly_roth_contrib
            company_match = self.retirement_account.company_match(yearly_401k_contrib)
            self.retirement_account.pretax_balance += company_match
        else:
            yearly_401k_contrib = 0
            company_match = 0

        gross_income = effective_salary + self.salary.bonus * max(
            0.0,
            self.income_multiplier,
        )

        # Deposit take-home pay into bank account
        self.owner.deposit_into_cashflow_bank_account(gross_income - yearly_401k_contrib)

        # Add to taxable income for the person
        # - Taxes are dedudcted in the person class
        # - Social security limits are handled in the SS class
        self.owner.earned_income += gross_income
        self.owner.taxable_income += gross_income - yearly_pretax_contrib
        if self.owner.social_security is not None:
            self.owner.social_security.add_income_for_year(gross_income)

        self.stat_gross_income = gross_income
        self.stat_retirement_contrib = yearly_401k_contrib
        self.stat_retirement_match = company_match

    def retire(self):
        """Retire from the job """
        if self.retired:
            return
        self.retired = True
        self.model.event_log.add(Event(f"{self.owner.name} retired from {self.company}"))


class Salary(LifeModelAgent):
    def __init__(
        self,
        model: LifeModel,
        base: float,
        yearly_increase: Optional[float] = None,
        yearly_bonus: float = 0,
    ):
        """Salary

        Args:
            base (float): Base salary.
            yearly_increase (float): Yearly percentage increase. When omitted,
                uses employment.salary.default_yearly_increase from the
                validated LifeModel financial configuration.
            yearly_bonus (float): Yearly percentage bonus.
        """
        super().__init__(model)
        self.base = base
        self.yearly_increase = (
            float(config.financial.get('employment.salary.default_yearly_increase', 3.0))
            if yearly_increase is None
            else float(yearly_increase)
        )
        self.yearly_bonus = yearly_bonus

    @property
    def bonus(self) -> float:
        return self.base * (self.yearly_bonus / 100)

    def step(self):
        self.base += self.base * (self.yearly_increase / 100)
