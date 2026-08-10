# Copyright 2026 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE
import html
from typing import Dict, Optional
from ..model import LifeModelAgent, Event
from ..people.person import Person
from ..config.config_manager import config


class Healthcare(LifeModelAgent):
    def __init__(self, person: Person,
                 pre_medicare_annual_premium: float = 0,
                 annual_out_of_pocket: Optional[float] = None,
                 medicare_start_age: Optional[int] = None,
                 medicare_part_b_monthly: Optional[float] = None,
                 medicare_part_d_monthly: Optional[float] = None,
                 medigap_monthly: Optional[float] = None,
                 healthcare_inflation_percent: Optional[float] = None,
                 age_cost_multipliers: Optional[Dict[int, float]] = None,
                 ltc_start_age: Optional[int] = None,
                 ltc_years: int = 0,
                 ltc_annual_cost: Optional[float] = None):
        """ Models healthcare and aging costs for a person

        Each year the component computes premiums plus age-scaled
        out-of-pocket medical costs, inflated at the healthcare cost trend,
        and routes the total through the person's yearly bills so withdrawal
        planning funds it. Before ``medicare_start_age`` the person pays
        ``pre_medicare_annual_premium`` (employer share or ACA); from that age
        on they pay Medicare Part B + Part D + Medigap premiums instead. An
        optional deterministic long-term care window adds nursing/assisted
        living costs for ``ltc_years`` starting at ``ltc_start_age``.

        IRMAA surcharges are not yet modeled.

        Args:
            person: The person whose healthcare costs are modeled
            pre_medicare_annual_premium: Yearly premium before Medicare age
            annual_out_of_pocket: Base yearly out-of-pocket medical cost at
                the baseline age band. Defaults to the configured
                healthcare.annual_out_of_pocket.
            medicare_start_age: Age Medicare coverage begins (default 65)
            medicare_part_b_monthly: Part B premium (configured default)
            medicare_part_d_monthly: Part D premium (configured default)
            medigap_monthly: Medigap/supplement premium (configured default)
            healthcare_inflation_percent: Healthcare cost trend (configured
                default, typically above general inflation)
            age_cost_multipliers: Mapping of age-decade floor to out-of-pocket
                multiplier, e.g. {50: 1.2, 60: 1.5, 70: 2.0, 80: 2.6}
            ltc_start_age: Optional age at which long-term care begins
            ltc_years: Years of long-term care
            ltc_annual_cost: Yearly LTC cost (configured default)
        """
        super().__init__(person.model)
        self.person = person
        self.pre_medicare_annual_premium = pre_medicare_annual_premium

        get = config.financial.get
        self.annual_out_of_pocket = (annual_out_of_pocket if annual_out_of_pocket is not None
                                     else get('healthcare.annual_out_of_pocket', 5500.0))
        self.medicare_start_age = (medicare_start_age if medicare_start_age is not None
                                   else get('healthcare.medicare_start_age', 65))
        self.medicare_part_b_monthly = (medicare_part_b_monthly if medicare_part_b_monthly is not None
                                        else get('healthcare.medicare_part_b_monthly_premium', 206.50))
        self.medicare_part_d_monthly = (medicare_part_d_monthly if medicare_part_d_monthly is not None
                                        else get('healthcare.medicare_part_d_monthly_premium', 46.50))
        self.medigap_monthly = (medigap_monthly if medigap_monthly is not None
                                else get('healthcare.medigap_monthly_premium', 155.0))
        self.healthcare_inflation_percent = (
            healthcare_inflation_percent if healthcare_inflation_percent is not None
            else get('healthcare.cost_inflation_rate', 5.0))
        raw_multipliers = (age_cost_multipliers if age_cost_multipliers is not None
                           else get('healthcare.age_cost_multipliers',
                                    {50: 1.2, 60: 1.5, 70: 2.0, 80: 2.6}))
        self.age_cost_multipliers = {int(age): float(mult) for age, mult in raw_multipliers.items()}
        self.ltc_start_age = ltc_start_age
        self.ltc_years = ltc_years
        self.ltc_annual_cost = (ltc_annual_cost if ltc_annual_cost is not None
                                else get('healthcare.long_term_care_annual_cost', 130000.0))

        self.anchor_year = self.model.component_anchor_year
        self._ltc_announced = False

        self.stat_healthcare_costs = 0.0
        self.stat_medicare_premiums = 0.0
        self.stat_ltc_costs = 0.0

        # Register with the model registry
        self.model.registries.healthcare.register(person, self)

    @property
    def on_medicare(self) -> bool:
        return self.person.age >= self.medicare_start_age

    @property
    def in_ltc(self) -> bool:
        if self.ltc_start_age is None or self.ltc_years <= 0:
            return False
        return self.ltc_start_age <= self.person.age < self.ltc_start_age + self.ltc_years

    def _age_multiplier(self) -> float:
        multiplier = 1.0
        for age_floor in sorted(self.age_cost_multipliers):
            if self.person.age >= age_floor:
                multiplier = self.age_cost_multipliers[age_floor]
        return multiplier

    def _inflation_factor(self) -> float:
        years_elapsed = max(0, self.model.year - self.anchor_year)
        return (1 + self.healthcare_inflation_percent / 100) ** years_elapsed

    def pre_step(self):
        """Compute the year's healthcare costs and route them through bills."""
        if getattr(self.person, 'is_deceased', False):
            self.stat_healthcare_costs = 0.0
            self.stat_medicare_premiums = 0.0
            self.stat_ltc_costs = 0.0
            return

        inflation = self._inflation_factor()

        if self.on_medicare:
            premiums = 12 * (self.medicare_part_b_monthly + self.medicare_part_d_monthly
                             + self.medigap_monthly)
        else:
            premiums = self.pre_medicare_annual_premium
        premiums *= inflation

        out_of_pocket = self.annual_out_of_pocket * self._age_multiplier() * inflation

        ltc_cost = 0.0
        if self.in_ltc:
            ltc_cost = self.ltc_annual_cost * inflation
            if not self._ltc_announced:
                self.model.event_log.add(Event(
                    f"{self.person.name} began long-term care at age {self.person.age}"))
                self._ltc_announced = True

        total = premiums + out_of_pocket + ltc_cost
        hsa_paid = 0.0
        if total > 0 and hasattr(self.person, 'deduct_from_hsa_medical'):
            remaining = self.person.deduct_from_hsa_medical(total)
            hsa_paid = total - remaining
            total = remaining

        if total > 0:
            # Flows through the year's bills so settlement plans for it
            self.person.spending.add_expense(total, 'healthcare_costs')

        self.stat_healthcare_costs = total
        self.stat_medicare_premiums = premiums if self.on_medicare else 0.0
        self.stat_ltc_costs = ltc_cost
        self.stat_hsa_medical_withdrawals = hsa_paid

    def _repr_html_(self):
        desc = '<ul>'
        desc += f'<li>Person: {html.escape(self.person.name)}</li>'
        desc += f'<li>Medicare Start Age: {self.medicare_start_age}</li>'
        desc += f'<li>Annual Out-of-Pocket (base): ${self.annual_out_of_pocket:,.2f}</li>'
        if self.ltc_start_age is not None:
            desc += f'<li>LTC: age {self.ltc_start_age} for {self.ltc_years} years</li>'
        desc += '</ul>'
        return desc
