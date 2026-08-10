# Copyright 2023 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

# The Social Security Administration uses a complex formula to calculate your
# Social Security benefits. Here's a simplified version of how it works:

# Calculate Your Average Indexed Monthly Earnings (AIME): The Social Security
# Administration takes your 35 highest-earning years (adjusted for inflation),
# adds them up, and then divides by the total number of months in those years
# to get your AIME.

# Apply the Bend Point Formula: The Social Security Administration then applies
# a formula to your AIME to calculate your Primary Insurance Amount (PIA). As
# of 2021, the formula is:

# 90% of the first $996 of your AIME, plus
# 32% of your AIME over $996 and up to $6,002, plus
# 15% of your AIME over $6,002
# The dollar amounts in the formula (known as "bend points") are adjusted each
# year for inflation.

# Adjust for Early or Late Retirement: If you start taking benefits before your
# full retirement age (which depends on your birth year), your monthly benefit
# will be reduced. If you delay taking benefits past your full retirement age,
# your monthly benefit will be increased.

# Cost-of-Living Adjustments (COLAs): Once you start receiving benefits, they
# will be adjusted each year for inflation based on the Consumer Price Index for
# Urban Wage Earners and Clerical Workers (CPI-W).

# The Social Security Administration has a more detailed explanation of how
# benefits are calculated.

# https://www.ssa.gov/oact/cola/piaformula.html
# https://www.ssa.gov/oact/cola/Benefits.html
# https://www.ssa.gov/oact/cola/latestCOLA.html
# https://www.ssa.gov/OACT/COLA/awiseries.html

from typing import List, Optional, Tuple, Union
from ..people.person import Person
from ..model import LifeModelAgent
from ..tax.fica import get_social_security_max_income
from ..config.config_manager import config


def get_avg_wage_index(year: int) -> float:
    """ Compute the average wage index for a given year

    Args:
        year: The year to compute the average wage index for

    Returns:
        The average wage index for the given year

    Raises:
        ValueError: If the year is before 1951
    """
    if year < 1951:
        raise ValueError("Average wage index is not available before 1951")

    ss_config = config.financial.get('social_security')
    last_year = ss_config['last_avg_wage_index_year']

    if year <= last_year:
        # Years in this range are in the table
        return ss_config['avg_wage_index'][year]
    else:
        # Years after this range are projected
        last_index = ss_config['avg_wage_index'][last_year]
        increase_rate = ss_config['last_avg_wage_index_increase']

        for _ in range(last_year, year):
            last_index *= (1 + increase_rate / 100.0)
        return last_index


def get_social_security_wage_base(year: int) -> float:
    """Approximate the SSA contribution-and-benefit base for a given year.

    The configured ``social_security_max_income`` applies to the year two
    years after the last published average wage index (the SSA derives each
    year's base from the AWI two years prior). Other years are scaled by the
    AWI ratio and rounded to the nearest $300, mirroring the statutory method.

    Args:
        year: The year to compute the wage base for

    Returns:
        The approximate taxable maximum for the given year
    """
    current_base = get_social_security_max_income()
    ss_config = config.financial.get('social_security')
    base_year = ss_config['last_avg_wage_index_year'] + 2

    if year == base_year:
        return current_base

    # AWI data starts in 1951; clamp earlier requests to the earliest ratio
    awi_year = max(year - 2, 1951)
    ratio = get_avg_wage_index(awi_year) / get_avg_wage_index(base_year - 2)
    return round(current_base * ratio / 300) * 300


def get_cost_of_living_adj(year: int) -> float:
    """ Get the cost of living adjustment for a given year

    Args:
        year: The year to get the cost of living adjustment for

    Returns:
        The cost of living adjustment for the given year

    Raises:
        ValueError: If the year is before 1975
    """
    if year < 1975:
        raise ValueError("Cost of living adjustment is not available before 1975")

    ss_config = config.financial.get('social_security')
    last_year = ss_config['last_cost_of_living_adj_year']

    if year <= last_year:
        # Years in this range are in the table
        return ss_config['cost_of_living_adj'][year]
    else:
        # Years after this range use the last available value
        return ss_config['cost_of_living_adj'][last_year]


def get_bend_points(year: int) -> Tuple[float, float]:
    """ Get the bend points for a given year

    Args:
        year: The year to get the bend points for

    Returns:
        A tuple containing the bend points for the given year

    Raises:
        ValueError: If the year is before 1979
    """
    if year < 1979:
        raise ValueError("Bend points are not available before 1979")

    ss_config = config.financial.get('social_security')
    last_year = ss_config['last_bend_points_year']

    if year <= last_year:
        # Years in this range are in the table
        bend_point_data = ss_config['bend_points'][year]
        return (float(bend_point_data[0]), float(bend_point_data[1]))
    else:
        # Years after this range use the last available values
        # TODO - Add some way of estimating bend points after last_year
        bend_point_data = ss_config['bend_points'][last_year]
        return (float(bend_point_data[0]), float(bend_point_data[1]))


def get_qc_earnings_for_year(year: int) -> int:
    """ Compute the number of credits earned for a given year

    Args:
        year: The year to compute the number of credits earned for

    Returns:
        The number of credits earned for the given year

    Raises:
        ValueError: If the year is before 1978
    """
    if year < 1978:
        raise ValueError("QC earnings are not available before 1978")

    ss_config = config.financial.get('social_security')
    credit_amt_1978 = ss_config['qc_credit_amount_1978']
    avg_wage_idx_1976 = ss_config['qc_avg_wage_index_1976']

    # Calculate previous year's QC amount (rounded to nearest 10 dollars)
    prev_year_amount = credit_amt_1978 * get_avg_wage_index(year-3) / avg_wage_idx_1976
    prev_year_amount = int(round(prev_year_amount / 10.0) * 10.0)

    # Calculate current year's QC amount (rounded to nearest 10 dollars)
    curr_year_amount = credit_amt_1978 * get_avg_wage_index(year-2) / avg_wage_idx_1976
    curr_year_amount = int(round(curr_year_amount / 10.0) * 10.0)

    # Pick the larger of the two
    return max(prev_year_amount, curr_year_amount)


def get_credits_for_year(year: int, earnings: float) -> int:
    """ Compute the number of credits earned for a given year """
    qc_earnings = get_qc_earnings_for_year(year)
    ss_config = config.financial.get('social_security')
    max_credits = ss_config['max_credits_per_year']
    return min(max_credits, int(earnings / qc_earnings))


# Property accessors for configuration values
def get_min_eligible_credits() -> int:
    """Get minimum eligible credits from configuration"""
    return config.financial.get('social_security.min_eligible_credits')


def get_max_credits_per_year() -> int:
    """Get maximum credits per year from configuration"""
    return config.financial.get('social_security.max_credits_per_year')


def get_max_years_of_income() -> int:
    """Get maximum years of income from configuration"""
    return config.financial.get('social_security.max_years_of_income')


def get_min_early_retirement_age() -> int:
    """Get minimum early retirement age from configuration"""
    return config.financial.get('social_security.min_early_retirement_age')


def get_normal_retirement_age() -> int:
    """Get normal retirement age from configuration"""
    return config.financial.get('social_security.normal_retirement_age')


def get_delayed_retirement_credit() -> float:
    """Get delayed retirement credit from configuration"""
    return config.financial.get('social_security.delayed_retirement_credit')


def get_max_delayed_retirement_credit_age() -> int:
    """Get maximum delayed retirement credit age from configuration"""
    return config.financial.get('social_security.max_delayed_retirement_credit_age')


class Income:
    def __init__(self, year: int, amount: float):
        """ Create an income record for a given year

        Args:
            year: The year of the income record
            amount: The amount of income for the given year
        """
        self.year = year
        self.amount = amount

    def _repr_html_(self):
        return f'<p>{self.year}: ${self.amount}</p>'

    def get_credits(self) -> int:
        """ Compute the number of credits earned for a given year """
        return get_credits_for_year(self.year, self.amount)

    def get_indexed_amount(self, person_age_60_year: int) -> float:
        """ Compute the indexed amount for a given year

        Args:
            person_age_60_year: The year in which the person attains age 60

        Returns:
            The indexed amount for the given year
        """
        if self.year >= person_age_60_year:
            return self.amount
        else:
            return self.amount * get_avg_wage_index(person_age_60_year) / get_avg_wage_index(self.year)


class SocialSecurity(LifeModelAgent):
    def __init__(self, person: Person, withdrawal_start_age: Optional[float] = None,
                 income_history: Optional[Union[List[Income], List[Tuple[int, float]]]] = None):
        """ Models Social Security benefits for a person

        Args:
            person: The person for whom to model Social Security benefits
            withdrawal_start_age: The age at which the person starts withdrawing Social Security benefits
            income_history: The person's income history

        Raises:
            ValueError: If the withdrawal start age is before the earliest age for early withdrawal
        """
        super().__init__(person.model)
        self.person = person
        self.withdrawal_start_age = withdrawal_start_age or person.retirement_age

        self.income_history = []
        if income_history is not None and len(income_history):
            for income in income_history:
                if isinstance(income, Income):
                    self.income_history.append(income)
                else:
                    self.income_history.append(Income(income[0], income[1]))

        if self.withdrawal_start_age < get_min_early_retirement_age():
            raise ValueError("Withdrawal start age cannot be before early retirement age")

        # Register social security
        self.person.social_security = self

    def _repr_html_(self):
        desc = '<ul>'
        desc += f'<li>Withdrawal Start Age: {self.withdrawal_start_age}</li>'
        desc += f'<li>Years of Work: {len(self.income_history)}</li>'
        desc += '</ul>'
        return desc

    def add_income_for_year(self, amount: float, year: Optional[int] = None):
        """ Add income to the person's income history

        Args:
            amount: The amount of income to add
            year: The year to add the income for. If not specified, the current year is used
        """
        year = year or self.model.year
        matching_years = [x for x in self.income_history if x.year == year]
        if matching_years:
            income_obj = matching_years[0]
            income_obj.amount += amount
        else:
            income_obj = Income(year, amount)
            self.income_history.append(income_obj)

        # Cap income at the taxable maximum for that specific year. The wage
        # base grows with the average wage index, so a single fixed cap would
        # distort indexed earnings for both historical and future years.
        wage_base = get_social_security_wage_base(year)
        if income_obj.amount > wage_base:
            income_obj.amount = wage_base

    @property
    def withdrawal_start_year(self) -> int:
        """ Returns the year in which the person starts withdrawing Social Security benefits """
        return self.person.get_year_at_age(int(self.withdrawal_start_age))

    def get_indexed_income_history(self) -> List[float]:
        """ Computes indexed earnings for a person """
        return [x.get_indexed_amount(self.person.get_year_at_age(60)) for x in self.income_history]

    def get_aime(self) -> float:
        """ Computes Average Indexed Monthly Earnings (AIME) for a person """

        # Make sure the person has enough credits to be eligible
        credits_earned = sum(x.get_credits() for x in self.income_history)
        if credits_earned < get_min_eligible_credits():
            return 0

        # Pick the highest 35 values from the list
        indexed_earnings = self.get_indexed_income_history()
        max_years = get_max_years_of_income()
        highest_earnings = sorted(indexed_earnings, reverse=True)[:max_years]

        return round(sum(highest_earnings) / (max_years * 12))

    def get_early_delayed_pia(self, pia: float) -> float:
        """ Computes the early or delayed PIA for a person

        Args:
            pia: The person's primary insurance amount

        Returns:
            PIA adjusted for early or delayed retirement
        """
        # Apply early/delayed retirement credits
        # https://www.ssa.gov/oact/quickcalc/early_late.html
        normal_ret_age = get_normal_retirement_age()
        if self.withdrawal_start_age < normal_ret_age:
            # Reduce for early withdrawal
            months_early = (normal_ret_age - self.withdrawal_start_age) * 12
            reduction_pct = min(months_early, 36) * 0.01 * (5/9)
            reduction_pct += max(0, months_early - 36) * 0.01 * (5/12)
            pia *= (1 - reduction_pct)
        elif self.withdrawal_start_age > normal_ret_age:
            # Increase for delayed withdrawal
            max_del_age = get_max_delayed_retirement_credit_age()
            max_months_delayed = (max_del_age - normal_ret_age) * 12
            months_delayed = (self.withdrawal_start_age - normal_ret_age) * 12
            months_delayed = min(months_delayed, max_months_delayed)
            del_credit = get_delayed_retirement_credit()
            increase_pct = months_delayed * (del_credit / (100 * 12))
            pia *= (1 + increase_pct)
        return pia

    def get_pia(self, current_year: Optional[int] = None) -> float:
        """ Computes Primary Insurance Amount (PIA) for a person
            Note: PIA is a monthly amount, so should be multiplied by 12 to get annual amount

        Args:
            current_year: The current year

        Returns:
            The person's PIA
        """
        current_year = current_year or self.model.year

        # Get AIME
        aime = self.get_aime()
        pia = 0.0

        # Apply bend points
        year_of_age_62 = self.person.get_year_at_age(62)
        bend_points = get_bend_points(year_of_age_62)
        pia += min(aime, bend_points[0]) * 0.9
        pia += min(max(0, aime - bend_points[0]), bend_points[1] - bend_points[0]) * 0.32
        pia += max(0, aime - bend_points[1]) * 0.15

        # Round to the nearest lowest dime
        pia = int(pia * 10) / 10.0

        # Apply cost of living adjustment
        for cola_year in range(year_of_age_62, current_year):
            # This rounding seems to match the SSA's rounding (truncating to nearest lower dime)
            pia = int(pia * (1 + get_cost_of_living_adj(cola_year) / 100.0) * 10) / 10.0

        # Apply early/delayed retirement credits
        pia = self.get_early_delayed_pia(pia)

        # Round to the nearest cent
        return round(pia, 2)

    def pre_step(self):

        # Benefits stop at death (survivor benefits are not modeled)
        if getattr(self.person, 'is_deceased', False):
            self.person.stat_ss_income = 0.0
            return

        # Add social security income to the person's income. The benefit is
        # tracked separately from taxable_income so the IRC §86 provisional
        # income worksheet can determine the taxable portion at settlement.
        if self.model.year >= self.withdrawal_start_year:
            yearly_pia = self.get_pia() * 12
            self.person.deposit_into_cashflow_bank_account(yearly_pia)
            self.person.social_security_income += yearly_pia
            self.person.stat_ss_income = yearly_pia

    @property
    def html_report(self) -> str:
        """ Returns a detailed report of the person's social security benefits """
        desc = '<ul>'
        desc += f'<li>Withdrawal Start Age: {self.withdrawal_start_age}</li>'
        desc += f'<li>Years of Work: {len(self.income_history)}</li>'
        desc += '<li><ul>'
        desc += ''.join(f'<li>{x._repr_html_()}</li>' for x in self.income_history)
        desc += '</ul></li>'
        desc += f'<li>AIME: {self.get_aime()}</li>'
        desc += f'<li>PIA: {self.get_pia()}</li>'
        desc += '</ul>'
        return desc
