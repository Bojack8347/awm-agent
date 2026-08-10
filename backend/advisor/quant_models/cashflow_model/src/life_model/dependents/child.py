# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE
import html
from typing import Optional, TYPE_CHECKING

from ..config.config_manager import config
from ..model import LifeModelAgent

if TYPE_CHECKING:
    from ..people.person import Person
    from .plan529 import Plan529


class Child(LifeModelAgent):
    def __init__(
        self,
        person: 'Person',
        name: str,
        birth_year: int,
        adoption_year: Optional[int] = None,
        birth_adoption_cost: float = 0,
        childcare_annual_cost: float = 0,
        school_activity_annual_cost: float = 0,
        college_annual_cost: float = 0,
        annual_529_contribution: float = 0,
        plan_529: Optional['Plan529'] = None,
        childcare_start_age: int = 0,
        childcare_end_age: int = 5,
        school_start_age: int = 6,
        school_end_age: int = 17,
        college_start_age: int = 18,
        college_end_age: int = 21,
        contribution_start_age: int = 0,
        contribution_end_age: int = 17,
        independence_age: int = 22,
        yearly_increase: Optional[float] = None,
        child_income: float = 0,
        child_income_start_age: Optional[int] = None,
        child_income_end_age: Optional[int] = None,
        child_income_yearly_increase: Optional[float] = None,
        family_contribution_percent: float = 0,
    ):
        """Models a child dependent for a person.

        The child model exposes five common event indicators and adds the
        resulting cash costs to household cashflow. Cost inputs are annual
        starting amounts in nominal dollars. If ``yearly_increase`` is None,
        configured inflation is used.

        Args:
            person: The parent or guardian to which this child belongs.
            name: The child's name.
            birth_year: The year the child was born.
            adoption_year: Optional adoption year. If supplied, the
                birth/adoption indicator and one-time cost occur in this year.
            birth_adoption_cost: One-time birth/adoption cash cost.
            childcare_annual_cost: Recurring childcare cost during the
                childcare age range.
            school_activity_annual_cost: Recurring school/activity cost during
                the school age range.
            college_annual_cost: Gross recurring college/education cost during
                the college age range. Matching 529 withdrawals reduce the
                cash amount paid by the household.
            annual_529_contribution: Planned recurring contribution to a
                matching 529 plan during the contribution age range.
            plan_529: Optional 529 plan to use for contributions/withdrawals.
                If omitted, plans owned by ``person`` with this child as
                beneficiary are used.
            childcare_start_age: First age with childcare costs.
            childcare_end_age: Last age with childcare costs.
            school_start_age: First age with school/activity costs.
            school_end_age: Last age with school/activity costs.
            college_start_age: First age with college/education costs.
            college_end_age: Last age with college/education costs.
            contribution_start_age: First age with planned 529 contributions.
            contribution_end_age: Last age with planned 529 contributions.
            independence_age: Age at which child independence is reported.
            yearly_increase: Annual cost growth percentage.
            child_income: Optional gross annual income earned by the child in
                the work/contribution phase. This income is not counted as
                parent taxable income.
            child_income_start_age: First age at which child income can be
                contributed. If omitted and ``child_income`` is positive,
                ``independence_age`` is used.
            child_income_end_age: Optional last age at which child income can
                be contributed.
            child_income_yearly_increase: Annual growth percentage for child
                income. If None, the configured inflation rate is used.
            family_contribution_percent: Percentage of child income paid into
                the family bank pool as an after-tax transfer.
        """
        super().__init__(person.model)
        if family_contribution_percent < 0 or family_contribution_percent > 100:
            raise ValueError('family_contribution_percent must be between 0 and 100')

        self.person = person
        self.name = name
        self.birth_year = birth_year
        self.adoption_year = adoption_year

        self.birth_adoption_cost = birth_adoption_cost
        self.childcare_annual_cost = childcare_annual_cost
        self.school_activity_annual_cost = school_activity_annual_cost
        self.college_annual_cost = college_annual_cost
        self.annual_529_contribution = annual_529_contribution
        self.plan_529 = plan_529

        self.childcare_start_age = childcare_start_age
        self.childcare_end_age = childcare_end_age
        self.school_start_age = school_start_age
        self.school_end_age = school_end_age
        self.college_start_age = college_start_age
        self.college_end_age = college_end_age
        self.contribution_start_age = contribution_start_age
        self.contribution_end_age = contribution_end_age
        self.independence_age = independence_age
        self.yearly_increase = (
            config.financial.get_inflation_rate()
            if yearly_increase is None else yearly_increase
        )
        self.child_income = child_income
        self.child_income_start_age = (
            independence_age
            if child_income > 0 and child_income_start_age is None
            else child_income_start_age
        )
        self.child_income_end_age = child_income_end_age
        self.child_income_yearly_increase = (
            config.financial.get_inflation_rate()
            if child_income_yearly_increase is None else child_income_yearly_increase
        )
        self.family_contribution_percent = family_contribution_percent

        self.stat_child_cash_outflow = 0.0
        self.stat_child_family_contribution = 0.0
        self._pre_step_applied_year: Optional[int] = None

        self.model.registries.children.register(person, self)

    @property
    def age(self) -> int:
        """Calculate child's current age based on model year."""
        return self.model.year - self.birth_year

    @property
    def event_year(self) -> int:
        """Year used for the birth/adoption indicator and one-time cost."""
        return self.adoption_year if self.adoption_year is not None else self.birth_year

    @property
    def matching_529_plans(self):
        if self.plan_529 is not None:
            return [self.plan_529]
        return [plan for plan in self.person.plan_529s if plan.beneficiary is self]

    def _is_age_in_range(self, start_age: int, end_age: int) -> bool:
        return self.age >= start_age and self.age <= end_age

    def _inflated_amount(self, amount: float) -> float:
        if amount <= 0:
            return 0.0
        years_elapsed = max(0, self.model.year - self.model.start_year)
        return amount * ((1 + self.yearly_increase / 100) ** years_elapsed)

    def _inflated_child_income(self) -> float:
        if self.child_income <= 0:
            return 0.0
        years_elapsed = max(0, self.model.year - self.model.start_year)
        return self.child_income * ((1 + self.child_income_yearly_increase / 100) ** years_elapsed)

    def _is_child_work_active(self) -> bool:
        if self.child_income <= 0 or self.family_contribution_percent <= 0:
            return False
        if self.child_income_start_age is None:
            return False
        if self.age < self.child_income_start_age:
            return False
        if self.child_income_end_age is not None and self.age > self.child_income_end_age:
            return False
        return True

    def _withdraw_from_529s(self, amount: float) -> float:
        remaining = amount
        total_withdrawn = 0.0
        for plan in self.matching_529_plans:
            if remaining <= 0:
                break
            withdrawn = plan.withdraw_qualified(remaining)
            total_withdrawn += withdrawn
            remaining -= withdrawn
        return total_withdrawn

    def _contribute_to_529s(self, amount: float) -> float:
        remaining = amount
        total_contributed = 0.0
        for plan in self.matching_529_plans:
            if remaining <= 0:
                break
            contributed = plan.contribute(remaining)
            total_contributed += contributed
            remaining -= contributed
        return total_contributed

    def _calculate_year(self):
        if self.age < 0:
            return {
                'birth_event': 0,
                'childcare_event': 0,
                'school_event': 0,
                'college_event': 0,
                'independence_event': 0,
                'child_work_event': 0,
                'birth_cost': 0.0,
                'childcare_cost': 0.0,
                'school_cost': 0.0,
                'gross_college_cost': 0.0,
                'requested_529_contribution': 0.0,
                'child_family_contribution': 0.0,
            }

        birth_event = 1 if self.model.year == self.event_year else 0
        childcare_event = 1 if self._is_age_in_range(self.childcare_start_age, self.childcare_end_age) else 0
        school_event = 1 if self._is_age_in_range(self.school_start_age, self.school_end_age) else 0
        college_age_active = self._is_age_in_range(self.college_start_age, self.college_end_age)
        contribution_age_active = self._is_age_in_range(self.contribution_start_age, self.contribution_end_age)
        college_event = 1 if college_age_active or (
            contribution_age_active and self.annual_529_contribution > 0
        ) else 0
        independence_event = 1 if self.age == self.independence_age else 0
        child_work_event = 1 if self._is_child_work_active() else 0
        child_family_contribution = (
            self._inflated_child_income() * (self.family_contribution_percent / 100)
            if child_work_event else 0.0
        )

        return {
            'birth_event': birth_event,
            'childcare_event': childcare_event,
            'school_event': school_event,
            'college_event': college_event,
            'independence_event': independence_event,
            'child_work_event': child_work_event,
            'birth_cost': self._inflated_amount(self.birth_adoption_cost) if birth_event else 0.0,
            'childcare_cost': self._inflated_amount(self.childcare_annual_cost) if childcare_event else 0.0,
            'school_cost': self._inflated_amount(self.school_activity_annual_cost) if school_event else 0.0,
            'gross_college_cost': self._inflated_amount(self.college_annual_cost) if college_age_active else 0.0,
            'requested_529_contribution': (
                self._inflated_amount(self.annual_529_contribution)
                if contribution_age_active else 0.0
            ),
            'child_family_contribution': child_family_contribution,
        }

    def _set_current_year_stats(self, apply_529_transactions: bool):
        values = self._calculate_year()
        contribution = 0.0
        withdrawal = 0.0

        if apply_529_transactions:
            contribution = self._contribute_to_529s(values['requested_529_contribution'])
            withdrawal = self._withdraw_from_529s(values['gross_college_cost'])
            self.person.family.deposit_into_bank_account(values['child_family_contribution'])

        direct_college_cost = max(0.0, values['gross_college_cost'] - withdrawal)
        child_costs = (
            values['birth_cost']
            + values['childcare_cost']
            + values['school_cost']
            + direct_college_cost
        )

        self.stat_child_birth_events = values['birth_event']
        self.stat_childcare_events = values['childcare_event']
        self.stat_school_activity_events = values['school_event']
        self.stat_college_education_events = values['college_event']
        self.stat_child_independence_events = values['independence_event']
        self.stat_child_work_events = values['child_work_event']

        self.stat_child_costs = child_costs
        self.stat_money_spent = child_costs
        self.stat_529_contributions = contribution
        self.stat_529_withdrawals = withdrawal
        self.stat_child_family_contribution = values['child_family_contribution']
        self.stat_child_cash_outflow = child_costs + contribution

    def prepare_start_year_stats(self):
        self._set_current_year_stats(apply_529_transactions=False)
        self.stat_child_costs = 0.0
        self.stat_money_spent = 0.0
        self.stat_529_contributions = 0.0
        self.stat_529_withdrawals = 0.0
        self.stat_child_family_contribution = 0.0
        self.stat_child_cash_outflow = 0.0

    def pre_step(self):
        if self._pre_step_applied_year == self.model.year:
            return
        self._set_current_year_stats(apply_529_transactions=True)
        self._pre_step_applied_year = self.model.year

    def apply_current_year_transactions(self):
        """Apply this child's current-year costs when created by a life event."""
        self._set_current_year_stats(apply_529_transactions=True)
        self._pre_step_applied_year = self.model.year

    def _repr_html_(self):
        desc = '<ul>'
        desc += f'<li>Name: {html.escape(self.name)}</li>'
        desc += f'<li>Birth Year: {self.birth_year}</li>'
        if self.adoption_year is not None:
            desc += f'<li>Adoption Year: {self.adoption_year}</li>'
        desc += f'<li>Age: {self.age}</li>'
        desc += '</ul>'
        return desc
