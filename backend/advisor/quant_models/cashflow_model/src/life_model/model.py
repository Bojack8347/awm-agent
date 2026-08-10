# Copyright 2022 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

import mesa
import numpy as np
import pandas as pd
from datetime import date
from typing import Optional, List, Callable, Dict, Iterable
from pandas.io.formats.style import Styler
from math import e as const_e
from .registry import ModelRegistries
from .tax.regime import TaxRegime, coerce_tax_regime


def compound_interest(principal: float, rate: float, num_times_applied: int = 1, elapsed_time_periods: int = 1):
    return principal * pow(1 + ((rate / 100) / num_times_applied), num_times_applied * elapsed_time_periods) - principal


def continous_interest(principal: float, rate: float, elapsed_time_periods: int = 1):
    return principal * pow(const_e, (rate / 100) * elapsed_time_periods) - principal


FMT_MONEY = '${:,.0f}'
RETIREMENT_EARLY_WITHDRAWAL_TAX_RATE = 0.10
RETIREMENT_EARLY_WITHDRAWAL_AGE = 59.5


class Stat:
    def __init__(self, name: str, title: Optional[str] = None, fmt: Optional[str] = None,
                 aggregator: Optional[Callable] = None):
        """Stat

        Args:
            name (str): Name of stat.
            title (str, optional): Title of stat.
            fmt (str, optional): Format string for printing.
            aggregator (Callable, optional): Function to aggregate stat values. Default is sum().
        """
        self.name = name
        self.title = title or name
        self.fmt = fmt
        self.aggregator = aggregator or sum

    def model_reporter(self, model: 'LifeModel'):
        """ Return the value of the stat for the model. """
        return self.aggregator(getattr(agent, self.name) for agent in model.agents)


class MoneyStat(Stat):
    def __init__(self, name: str, title: Optional[str] = None):
        super().__init__(name, title, FMT_MONEY)


class RateStat(Stat):
    def __init__(self, name: str, title: Optional[str] = None):
        super().__init__(name, title, '{:.1f}%', max)


class _MonteCarloArrayCollector:
    """Minimal numeric collector for Monte Carlo model-level reporters."""

    def __init__(self, model_reporters: Dict[str, object], max_records: int):
        self.model_reporters = dict(model_reporters)
        self.agent_reporters: Dict[str, object] = {}
        self.model_vars = {name: [] for name in self.model_reporters}
        self._columns = list(self.model_reporters)
        self._values = np.empty((int(max_records), len(self._columns)), dtype=float)
        self._record_count = 0

    def collect(self, model: "LifeModel") -> None:
        if self._record_count >= len(self._values):
            raise RuntimeError("Monte Carlo array collector capacity exceeded")
        for column_index, reporter in enumerate(self.model_reporters.values()):
            if isinstance(reporter, str):
                value = getattr(model, reporter)
            elif callable(reporter):
                value = reporter(model)
            else:
                raise TypeError(
                    f"Unsupported Monte Carlo reporter type: {type(reporter).__name__}"
                )
            self._values[self._record_count, column_index] = float(value)
        self._record_count += 1

    def get_array_result(self):
        """Return collected values and column order without copying."""

        return self._values[: self._record_count], list(self._columns)

    def get_model_vars_dataframe(self) -> pd.DataFrame:
        values, columns = self.get_array_result()
        return pd.DataFrame(values.copy(), columns=columns)


def _agent_class_name(agent) -> str:
    return agent.__class__.__name__


def _iter_people(model: 'LifeModel'):
    for agent in model.agents:
        if _agent_class_name(agent) == 'Person':
            yield agent


def _iter_families(model: 'LifeModel'):
    for agent in model.agents:
        if _agent_class_name(agent) == 'Family':
            yield agent


def _person_taxable_retirement_balance(model: 'LifeModel', person) -> float:
    pretax_401k = sum(
        max(0.0, getattr(account, 'pretax_balance', 0.0) or 0.0)
        for account in getattr(person, 'all_retirement_accounts', [])
    )
    traditional_ira = sum(
        max(0.0, getattr(account, 'balance', 0.0) or 0.0)
        for account in getattr(person, 'traditional_iras', [])
    )
    return pretax_401k + traditional_ira


def _person_roth_retirement_balance(model: 'LifeModel', person) -> float:
    roth_401k = sum(
        max(0.0, getattr(account, 'roth_balance', 0.0) or 0.0)
        for account in getattr(person, 'all_retirement_accounts', [])
    )
    roth_ira = sum(
        max(0.0, getattr(account, 'balance', 0.0) or 0.0)
        for account in getattr(person, 'roth_iras', [])
    )
    return roth_401k + roth_ira


def _income_tax_cost_for_liquidation(taxpayer, taxable_retirement_balance: float) -> float:
    if taxable_retirement_balance <= 0:
        return 0.0
    try:
        base_taxes = taxpayer.get_income_taxes_due()
        liquidation_taxes = taxpayer.get_income_taxes_due(taxable_retirement_balance)
    except (AttributeError, NotImplementedError):
        return 0.0
    return max(0.0, liquidation_taxes.total - base_taxes.total)


def _person_early_withdrawal_tax(model: 'LifeModel', person) -> float:
    if getattr(person, 'age', RETIREMENT_EARLY_WITHDRAWAL_AGE) >= RETIREMENT_EARLY_WITHDRAWAL_AGE:
        return 0.0
    return _person_taxable_retirement_balance(model, person) * RETIREMENT_EARLY_WITHDRAWAL_TAX_RATE


def _is_joint_family(family) -> bool:
    filing_status = getattr(family, 'filing_status', None)
    return getattr(filing_status, 'name', None) == 'MARRIED_FILING_JOINTLY'


def _retirement_liquidation_components(model: 'LifeModel') -> Dict[str, float]:
    components = {
        'taxable_balance': 0.0,
        'roth_balance': 0.0,
        'income_tax_cost': 0.0,
        'early_withdrawal_tax': 0.0,
    }
    handled_people = set()

    for family in _iter_families(model):
        members = list(getattr(family, 'members', []))
        if not members or not _is_joint_family(family):
            continue

        taxable_balance = sum(_person_taxable_retirement_balance(model, member) for member in members)
        roth_balance = sum(_person_roth_retirement_balance(model, member) for member in members)
        early_withdrawal_tax = sum(_person_early_withdrawal_tax(model, member) for member in members)

        components['taxable_balance'] += taxable_balance
        components['roth_balance'] += roth_balance
        components['income_tax_cost'] += _income_tax_cost_for_liquidation(family, taxable_balance)
        components['early_withdrawal_tax'] += early_withdrawal_tax
        handled_people.update(id(member) for member in members)

    for person in _iter_people(model):
        if id(person) in handled_people:
            continue

        taxable_balance = _person_taxable_retirement_balance(model, person)
        roth_balance = _person_roth_retirement_balance(model, person)

        components['taxable_balance'] += taxable_balance
        components['roth_balance'] += roth_balance
        components['income_tax_cost'] += _income_tax_cost_for_liquidation(person, taxable_balance)
        components['early_withdrawal_tax'] += _person_early_withdrawal_tax(model, person)

    components['liquidation_tax_cost'] = (
        components['income_tax_cost'] + components['early_withdrawal_tax']
    )
    components['after_tax_retirement_value'] = max(
        0.0,
        components['taxable_balance']
        + components['roth_balance']
        - components['liquidation_tax_cost'],
    )
    return components


class TotalLiabilitiesStat(MoneyStat):
    def __init__(self):
        super().__init__('stat_total_liabilities', 'Total Liabilities')

    def model_reporter(self, model: 'LifeModel'):
        return (
            sum(getattr(agent, 'stat_debt', 0) for agent in model.agents)
            + sum(getattr(agent, 'stat_mortgage_balance', 0) for agent in model.agents)
            + sum(getattr(agent, 'stat_loan_balance', 0) for agent in model.agents)
            + sum(getattr(agent, 'stat_life_insurance_loan_balance', 0) for agent in model.agents)
        )


class TotalAssetsStat(MoneyStat):
    def __init__(self):
        super().__init__('stat_total_assets', 'Total Assets')

    def model_reporter(self, model: 'LifeModel'):
        return (
            sum(getattr(agent, 'stat_bank_balance', 0) for agent in model.agents)
            + sum(getattr(agent, 'stat_401k_balance', 0) for agent in model.agents)
            + sum(getattr(agent, 'stat_investment_balance', 0) for agent in model.agents)
            + sum(getattr(agent, 'stat_brokerage_balance', 0) for agent in model.agents)
            + sum(getattr(agent, 'stat_traditional_ira_balance', 0) for agent in model.agents)
            + sum(getattr(agent, 'stat_roth_ira_balance', 0) for agent in model.agents)
            + sum(getattr(agent, 'stat_hsa_balance', 0) for agent in model.agents)
            + sum(getattr(agent, 'stat_529_balance', 0) for agent in model.agents)
            + sum(getattr(agent, 'stat_home_value', 0) for agent in model.agents)
            + sum(getattr(agent, 'stat_cash_value', 0) for agent in model.agents)
            + sum(getattr(agent, 'stat_balance', 0) for agent in model.agents)
            + sum(getattr(agent, 'stat_trust_balance', 0) for agent in model.agents)
            + sum(getattr(agent, 'stat_real_asset_value', 0) for agent in model.agents)
        )


class NetWorthStat(MoneyStat):
    def __init__(self):
        super().__init__('stat_net_worth', 'Net Worth')

    def model_reporter(self, model: 'LifeModel'):
        return TotalAssetsStat().model_reporter(model) - TotalLiabilitiesStat().model_reporter(model)


class TaxableRetirementBalanceStat(MoneyStat):
    def __init__(self):
        super().__init__('stat_taxable_retirement_balance', 'Taxable Retirement Balance')

    def model_reporter(self, model: 'LifeModel'):
        return _retirement_liquidation_components(model)['taxable_balance']


class RothRetirementBalanceStat(MoneyStat):
    def __init__(self):
        super().__init__('stat_roth_retirement_balance', 'Roth Retirement Balance')

    def model_reporter(self, model: 'LifeModel'):
        return _retirement_liquidation_components(model)['roth_balance']


class RetirementLiquidationIncomeTaxStat(MoneyStat):
    def __init__(self):
        super().__init__('stat_retirement_liquidation_income_tax', 'Retirement Liquidation Income Tax')

    def model_reporter(self, model: 'LifeModel'):
        return _retirement_liquidation_components(model)['income_tax_cost']


class RetirementEarlyWithdrawalTaxStat(MoneyStat):
    def __init__(self):
        super().__init__('stat_retirement_early_withdrawal_tax', 'Retirement Early Withdrawal Tax')

    def model_reporter(self, model: 'LifeModel'):
        return _retirement_liquidation_components(model)['early_withdrawal_tax']


class RetirementLiquidationTaxCostStat(MoneyStat):
    def __init__(self):
        super().__init__('stat_retirement_liquidation_tax_cost', 'Retirement Liquidation Tax Cost')

    def model_reporter(self, model: 'LifeModel'):
        return _retirement_liquidation_components(model)['liquidation_tax_cost']


class AfterTaxRetirementValueStat(MoneyStat):
    def __init__(self):
        super().__init__('stat_after_tax_retirement_value', 'After-Tax Retirement Value')

    def model_reporter(self, model: 'LifeModel'):
        return _retirement_liquidation_components(model)['after_tax_retirement_value']


class TaxAdjustedNetWorthStat(MoneyStat):
    def __init__(self):
        super().__init__('stat_tax_adjusted_net_worth', 'Tax-Adjusted Net Worth')

    def model_reporter(self, model: 'LifeModel'):
        return (
            NetWorthStat().model_reporter(model)
            - _retirement_liquidation_components(model)['liquidation_tax_cost']
        )


class Event:
    def __init__(self, message: str):
        """Event

        Args:
            message (str): Event description.
        """
        self.message = message
        self.year = 0

    def _repr_html_(self):
        return f"<tr><td>{self.year}</td><td>{self.message}</td></tr>\n"


class EventLog:
    def __init__(self, model: 'LifeModel'):
        """Event Log

        Args:
            model (LifeModel): LifeModel.
        """
        self.model = model
        self.list = []

    def _repr_html_(self):
        table = "<table>"
        table += "<tr><th>Year:</th><th>Event:</th></tr>\n"
        table += "".join(x._repr_html_() for x in self.list)
        table += "</table>"
        return table

    def add(self, event: Event):
        event.year = self.model.year
        self.list.append(event)


class LifeModel(mesa.Model):

    STATS = [
        MoneyStat('stat_gross_income',         'Income'),           # Gross income made in a year
        MoneyStat('stat_one_time_income',      'One-time Income'),  # One-off cash income received in a year
        MoneyStat('stat_bank_balance',         'Bank Balance'),     # Bank account balance at the end of each year
        MoneyStat('stat_401k_balance',         '401k Balance'),     # Total 401k balance at the end of each year
        MoneyStat('stat_brokerage_balance',    'Brokerage Balance'),  # Brokerage/investment account balance
        MoneyStat('stat_traditional_ira_balance', 'Traditional IRA Balance'),  # Traditional IRA balance
        MoneyStat('stat_roth_ira_balance',     'Roth IRA Balance'),  # Roth IRA balance
        MoneyStat('stat_hsa_balance',          'HSA Balance'),       # Health savings account balance
        MoneyStat('stat_home_value',           'Home Value'),        # Market value of owned homes
        MoneyStat('stat_useable_balance',      'Useable Balance'),  # Balance available for use in a year
        MoneyStat('stat_debt',                 'Cashflow Shortfall Debt'),  # Unpaid cashflow shortfall
        MoneyStat('stat_taxes_paid',           'Taxes'),            # Taxes paid in a year
        MoneyStat('stat_money_spent',          'Spending'),         # Total spending from mutually exclusive subcategories
        MoneyStat('stat_base_living_spending', 'Base Living Spending'),  # Recurring living/discretionary spending
        MoneyStat('stat_one_time_expenses',    'One-time Expenses'),  # Generic one-off spending events
        MoneyStat('stat_education_costs',      'Education Costs'),   # Adult education costs paid from cash
        MoneyStat('stat_home_purchase_costs',  'Home Purchase Costs'),  # Down payment and closing costs
        MoneyStat('stat_asset_sale_shortfalls', 'Asset Sale Shortfalls'),  # Debt deficiencies on asset sales
        MoneyStat('stat_retirement_contrib',   '401k Contrib'),     # Money contributed to retirement in a given year
        MoneyStat('stat_retirement_match',     '401k Match'),       # Money matched by company in 401k in a given year
        MoneyStat('stat_retirement_withdrawals', '401k Withdrawals'),  # Planned retirement withdrawals paid to cashflow
        MoneyStat('stat_investment_return',    'Investment Return'),  # Investment growth credited in a year
        MoneyStat('stat_investment_balance',   'Investment Balance'),  # Family-level investment ending balance
        MoneyStat('stat_cash_investment_return', 'Cash Investment Return'),  # Investment return paid into family cashflow
        MoneyStat('stat_bank_interest',        'Bank Interest'),     # Bank interest credited in a year
        MoneyStat('stat_required_min_distrib', 'RMDs'),             # Money taken out from required minimum distrib.
        MoneyStat('stat_housing_costs',        'Housing'),          # Money paid towards mortgage or rent
        MoneyStat('stat_mortgage_balance',     'Mortgage Balance'),  # Remaining mortgage principal balance
        MoneyStat('stat_mortgage_payments',    'Mortgage Payments'),  # Mortgage principal and interest paid
        MoneyStat('stat_mortgage_principal_paid', 'Mortgage Principal Paid'),  # Mortgage principal paid
        MoneyStat('stat_mortgage_interest_paid', 'Mortgage Interest Paid'),  # Mortgage interest paid
        Stat('stat_fixed_rate_mortgages',      'Fixed Rate Mortgages'),  # Count of fixed-rate mortgages
        Stat('stat_adjustable_rate_mortgages', 'Adjustable Rate Mortgages'),  # Count of ARMs
        Stat('stat_interest_only_mortgages',   'Interest Only Mortgages'),  # Count of interest-only mortgages
        Stat('stat_balloon_mortgages',         'Balloon Mortgages'),  # Count of balloon mortgages
        MoneyStat('stat_loan_balance',         'Loan Balance'),       # Remaining non-mortgage loan principal
        MoneyStat('stat_loan_payments',        'Loan Payments'),      # Non-mortgage loan principal and interest paid
        MoneyStat('stat_loan_principal_paid',  'Loan Principal Paid'),  # Non-mortgage loan principal paid
        MoneyStat('stat_loan_interest_paid',   'Loan Interest Paid'),  # Non-mortgage loan interest paid
        MoneyStat('stat_life_insurance_loan_balance', 'Life Ins Loan Balance'),  # Outstanding policy loans
        TotalAssetsStat(),                                           # Modeled balance-sheet assets
        TotalLiabilitiesStat(),                                      # Mortgage + loan + cashflow shortfall
        NetWorthStat(),                                              # Total Assets - Total Liabilities
        TaxableRetirementBalanceStat(),                              # Pre-tax 401k + traditional IRA
        RothRetirementBalanceStat(),                                 # Roth 401k + Roth IRA
        RetirementLiquidationIncomeTaxStat(),                        # Incremental income tax on liquidation
        RetirementEarlyWithdrawalTaxStat(),                          # Modeled 10% additional tax before age 59.5
        RetirementLiquidationTaxCostStat(),                          # Total tax cost of liquidation
        AfterTaxRetirementValueStat(),                               # Retirement balance after modeled tax cost
        TaxAdjustedNetWorthStat(),                                   # Net worth less modeled retirement tax cost
        Stat('stat_car_loans',                 'Car Loans'),          # Count of car loans
        Stat('stat_student_loans',             'Student Loans'),      # Count of student loans
        Stat('stat_credit_cards',              'Credit Cards'),       # Count of credit-card accounts
        Stat('stat_federal_subsidized_student_loans', 'Federal Subsidized Student Loans'),
        Stat('stat_federal_unsubsidized_student_loans', 'Federal Unsubsidized Student Loans'),
        Stat('stat_private_student_loans',     'Private Student Loans'),
        Stat('stat_plus_student_loans',        'PLUS Student Loans'),
        MoneyStat('stat_interest_paid',        'Interest Paid'),    # Money paid in interest for loans
        MoneyStat('stat_ss_income',            'SS Income'),        # Income from social security
        MoneyStat('stat_charitable_donations', 'Charity'),          # Total charitable donations in a year
        MoneyStat('stat_child_costs',          'Child Costs'),      # Direct child/dependent costs paid from cash
        MoneyStat('stat_child_family_contribution', 'Child Family Contributions'),  # Child income contributed to family cashflow
        MoneyStat('stat_529_contributions',    '529 Contributions'),  # 529 savings contributions paid from cash
        MoneyStat('stat_529_withdrawals',      '529 Withdrawals'),  # Qualified 529 withdrawals used for education
        MoneyStat('stat_529_balance',          '529 Balance'),      # 529 plan ending balance
        Stat('stat_owns_home',                 'Owns Home'),        # Household owns at least one home
        Stat('stat_rents_apartment',           'Rents Apartment'),  # Household rents at least one apartment
        Stat('stat_child_birth_events',        'Child Birth/Adoption Events'),  # Birth/adoption event count
        Stat('stat_childcare_events',          'Childcare Events'),  # Childcare phase active count
        Stat('stat_school_activity_events',    'School Activity Events'),  # School/activity phase active count
        Stat('stat_college_education_events',  'College Savings/Education Events'),  # College/savings phase active count
        Stat('stat_child_independence_events', 'Child Independence Events'),  # Independence event count
        Stat('stat_child_work_events',         'Child Work Contribution Events'),  # Child contribution phase active count
    ]

    EXTRA_STATS = [
        MoneyStat('stat_taxes_paid_federal',   'Federal Taxes'),    # Federal income taxes paid in a year
        MoneyStat('stat_taxes_paid_state',     'State Taxes'),      # State income taxes paid in a year
        MoneyStat('stat_taxes_paid_ss',        'SS Taxes'),         # Social security taxes paid in a year
        MoneyStat('stat_taxes_paid_medicare',  'Medicare Taxes'),   # Medicare taxes paid in a year
        MoneyStat('stat_taxes_paid_penalties', 'Early Withdrawal Penalties'),  # Pre-59.5 retirement withdrawal penalty
        MoneyStat('stat_pension_income',       'Pension Income'),   # Defined-benefit pension payments received
        MoneyStat('stat_trust_balance',        'Trust Balance'),    # Trust assets at the end of each year
        MoneyStat('stat_trust_distributions',  'Trust Distributions'),  # Trust payouts to beneficiaries
        MoneyStat('stat_real_asset_value',     'Real Asset Value'),  # Vehicles, collectibles, equipment value
        MoneyStat('stat_real_asset_costs',     'Real Asset Costs'),  # Maintenance and insurance on real assets
        MoneyStat('stat_healthcare_costs',     'Healthcare Costs'),  # Premiums, out-of-pocket, and LTC costs
        MoneyStat('stat_ltc_costs',            'Long-Term Care Costs'),  # Long-term care portion of healthcare
        MoneyStat('stat_tax_agi',              'AGI'),              # Adjusted gross income used for tax planning
        MoneyStat('stat_taxable_income',       'Taxable Income'),   # Federal taxable income after deductions
        MoneyStat('stat_tax_deductions',       'Tax Deductions'),   # Federal standard/itemized deductions used
        RateStat('stat_federal_marginal_rate', 'Federal Marginal Rate'),  # Ordinary federal marginal bracket
        RateStat('stat_effective_tax_rate',    'Effective Tax Rate'),     # Total tax divided by AGI
        MoneyStat('stat_premium_payments',     'Life Ins Premiums'),  # Life insurance premiums paid in a year
        MoneyStat('stat_cash_value',           'Life Ins Cash Value'),  # Life insurance cash value
        MoneyStat('stat_death_benefit_paid',   'Death Benefits'),   # Death benefits paid out
        MoneyStat('stat_premiums_paid',        'Insurance Premiums'),  # General insurance premiums paid
        Stat('stat_claims_filed',              'Insurance Claims Filed'),  # General insurance claim count
        MoneyStat('stat_claims_paid_out',      'Insurance Claim Payouts'),  # General insurance payouts received
        MoneyStat('stat_deductibles_paid',     'Insurance Deductibles'),  # General insurance deductibles paid
        MoneyStat('stat_balance',              'Annuity Balance'),    # Annuity ending balance
        MoneyStat('stat_interest_earned',      'Annuity Interest'),   # Annuity interest credited
        MoneyStat('stat_payouts_received',     'Annuity Payouts'),    # Annuity payouts received
        MoneyStat('stat_surrender_charges_paid', 'Annuity Surrender Charges'),  # Surrender charges paid
    ]

    def __init__(
        self,
        end_year: Optional[int] = None,
        start_year: Optional[int] = None,
        seed: Optional[int] = None,
        tax_regime: Optional[TaxRegime] = None,
    ):
        """LifeModel Helper Class

        Args:
            end_year (int, optional): End date of the model. Defaults to None.
            start_year (int, optional): Start date of the model. Defaults to None.
            seed (int, optional): Random seed. Defaults to None.
            tax_regime (TaxRegime, optional): Tax-law implementation used for
                filing-unit calculations. Defaults to the current-law US
                federal/configured-state approximation.
        """
        super().__init__(seed=seed)  # Required in Mesa 3.0
        if start_year is None:
            start_year = date.today().year

        # Initialize registries
        self.registries = ModelRegistries()
        if end_year is None:
            end_year = start_year + 50
        self.start_year = start_year
        self.end_year = end_year
        self.year = start_year
        self.tax_regime = coerce_tax_regime(tax_regime)
        self.event_log = EventLog(self)
        self.simulated_years = []
        self._baseline_collected = False
        self._stages = ["decision_step", "pre_step", "step", "post_step"]
        
        # Monte Carlo simulation mode support
        self._simulation_mode = 'deterministic'
        self._return_generator = None
        self._investment_registry = None
        
        self.datacollector = mesa.DataCollector(
            model_reporters={
                **{"Year": "year"},
                **{x.title: lambda model, x=x: x.model_reporter(model) for x in self.STATS},
                **{x.title: lambda model, x=x: x.model_reporter(model) for x in self.EXTRA_STATS}
            },
            agent_reporters={
                **{x.title: x.name for x in self.STATS},
                **{x.title: x.name for x in self.EXTRA_STATS},
            }
        )

    def configure_monte_carlo_reporting(
        self,
        columns: Optional[Iterable[str]] = None,
        *,
        array_backed: bool = False,
    ) -> None:
        """Use an output-only data collector for a Monte Carlo path.

        Monte Carlo aggregation consumes model-level yearly series and never
        consumes Mesa's per-agent history.  Replacing the collector before the
        opening row is recorded avoids evaluating and copying unused reporters
        on every year of every path.  ``columns=None`` retains every model
        reporter for callers that explicitly request a full projection.

        Args:
            columns: Model reporter titles required by the caller. ``Year`` is
                always retained.

        Raises:
            RuntimeError: If collection has already started.
            ValueError: If a requested reporter is not configured on the model.
        """
        if self._baseline_collected or any(self.datacollector.model_vars.values()):
            raise RuntimeError(
                "Monte Carlo reporting must be configured before data collection starts"
            )

        available_reporters = self.datacollector.model_reporters
        if columns is None:
            selected_reporters = dict(available_reporters)
        else:
            requested = list(dict.fromkeys(["Year", *[str(column) for column in columns]]))
            missing = [column for column in requested if column not in available_reporters]
            if missing:
                raise ValueError(
                    "Unknown Monte Carlo report columns: " + ", ".join(missing)
                )
            selected_reporters = {
                column: available_reporters[column]
                for column in requested
            }

        if array_backed:
            self.datacollector = _MonteCarloArrayCollector(
                selected_reporters,
                max_records=self.end_year - self.start_year + 1,
            )
        else:
            # A fresh collector is safer than mutating Mesa's private record maps
            # and guarantees that per-agent reporters stay disabled for this path.
            self.datacollector = mesa.DataCollector(model_reporters=selected_reporters)

    @classmethod
    def get_stat_by_name(cls, stat_name: str) -> Optional[Stat]:
        """Returns a stat by name.

        Args:
            stat_name (str): Name of stat.

        Returns:
            Optional[Stat]: Stat.
        """
        for stat in cls.STATS:
            if stat.name == stat_name:
                return stat
        for stat in cls.EXTRA_STATS:
            if stat.name == stat_name:
                return stat
        return None

    @classmethod
    def get_stat_by_title(cls, stat_title: str) -> Optional[Stat]:
        """Returns a stat by title.

        Args:
            stat_title (str): Title of stat.

        Returns:
            Optional[Stat]: Stat.
        """
        for stat in cls.STATS:
            if stat.title == stat_title:
                return stat
        for stat in cls.EXTRA_STATS:
            if stat.title == stat_title:
                return stat
        return None

    def set_simulation_mode(self, mode: str, return_generator=None, investment_registry=None):
        """Set simulation mode: 'deterministic' or 'probabilistic'.
        
        Args:
            mode: Either 'deterministic' (default) or 'probabilistic' for Monte Carlo.
            return_generator: For probabilistic mode, the AccountCorrelatedReturnGenerator
                            that produces correlated returns each year.
            investment_registry: For probabilistic mode, the InvestmentAccountRegistry
                               containing accounts to apply stochastic returns to.
        """
        if mode not in ('deterministic', 'probabilistic'):
            raise ValueError(f"Invalid simulation mode: {mode}")
        
        self._simulation_mode = mode
        self._return_generator = return_generator
        self._investment_registry = investment_registry
    
    @property
    def simulation_mode(self) -> str:
        """Get current simulation mode."""
        return self._simulation_mode
    
    @property
    def is_probabilistic(self) -> bool:
        """Check if running in probabilistic (Monte Carlo) mode."""
        return self._simulation_mode == 'probabilistic'

    @property
    def component_anchor_year(self) -> int:
        """Anchor year for newly created time-based components.

        Components created before the first simulated year (policies,
        annuities, recurring donations) belong to the first simulated year,
        start_year + 1; components created mid-simulation anchor to the year
        currently being simulated.
        """
        return self.year if self.simulated_years else self.year + 1

    def _ensure_baseline_row(self):
        """Collect the opening-balance row for start_year exactly once."""
        if self._baseline_collected:
            return
        self._baseline_collected = True
        self.agents.do("prepare_start_year_stats")
        self.datacollector.collect(self)

    def step(self):
        """Simulate one year of activity.

        The first call records the baseline row for start_year (opening
        balances, no activity). Every call then advances into the next
        calendar year, runs that year's stages, and collects its results,
        so the row labeled Year == N holds the activity that happened
        during year N. A person entered at age A is age A in the baseline
        row and age A + 1 during the first simulated year.
        """
        self._ensure_baseline_row()

        self.year += 1
        self.simulated_years.append(self.year)

        # In probabilistic mode, growth is applied before the stages so it
        # is included in this year's calculations.
        self._apply_probabilistic_returns()

        # Execute each stage using AgentSet functionality
        for stage in self._stages:
            self.agents.do(stage)

        self.datacollector.collect(self)

    def _apply_probabilistic_returns(self):
        # In probabilistic mode, generate and apply correlated returns before
        # regular step phases so growth is included in that year's calculations.
        if self._simulation_mode == 'probabilistic' and self._return_generator is not None:
            try:
                # Year-aware generators use this to apply per-year assumptions
                yearly_returns = self._return_generator.generate_yearly_returns(self.year)
            except TypeError:
                # Custom generators that predate the year parameter
                yearly_returns = self._return_generator.generate_yearly_returns()
            if self._investment_registry is not None:
                self._investment_registry.apply_returns(yearly_returns)

    def get_year_range(self) -> range:
        """ Get the range of years in the model """
        return range(self.start_year, self.end_year + 1)

    def run(self):
        """Run the simulation, simulating each year through end_year."""
        self._ensure_baseline_row()
        while self.year < self.end_year:
            self.step()

    def add_agent_stat(self, title: str, attr_name: str):
        """Add an agent stat to the model

        Args:
            title (str): Title of the stat
            attr_name (str): Name of the attribute
        """
        self.datacollector._new_agent_reporter(title, attr_name)

        # Set stat value to 0 for agents that don't have that attribute
        for agent in self.agents:
            if not hasattr(agent, attr_name):
                setattr(agent, attr_name, 0)

    def get_yearly_stat_df(self, columns: Optional[List[str]] = None, extra_columns: Optional[List[str]] = None,
                           aggregate: Optional[Dict[str, Callable]] = None,
                           column_formats: Optional[Dict[str, str]] = None) -> Styler:
        """Get a DataFrame of the yearly stats

        Args:
            columns (List[str], optional): Optional list of columns to include. Defaults to None.
            extra_columns (List[str], optional): Optional list of extra columns to include. Defaults to None.
            aggregate (Dict[str, Callable], optional): Dictionary of aggregators to use. Defaults to None.
            column_formats (Dict[str, str], optional): Dictionary of column formats to use. Defaults to None.

        Returns:
            pd.DataFrame: DataFrame of the yearly stats
        """
        # Get the list of columns to use
        if columns is None:
            columns = ['Year'] + [x.title for x in self.STATS]
        if extra_columns is not None:
            for i, column in enumerate(extra_columns):
                columns.insert(i+1, column)
        # Get the list of stats to use
        stats = []
        for column_name in columns:
            stat = self.get_stat_by_title(column_name)
            if stat is not None:
                stats.append(stat)
        # Create a dataframe from the data
        df = self.datacollector.get_model_vars_dataframe()
        # Only keep certain columns in the data frame
        df = df[columns]
        if aggregate is not None:
            # Aggregate the data if desired
            aggregators = {**{'Year': 'max'}, **aggregate, **{x.title: x.aggregator.__name__ for x in stats}}
            df = df.aggregate(aggregators).reset_index().transpose()
            df.columns = df.iloc[0]
            df = df.drop(df.index[0])
        formats = {x.title: x.fmt for x in stats if x.fmt is not None}
        if column_formats is not None:
            formats.update(column_formats)
        return df.style.format(precision=0, na_rep='MISSING', formatter=formats).hide()

    def format_dataframe(self, df: pd.DataFrame, extra_formats: Optional[Dict[str, str]] = None) -> Styler:
        """Format a dataframe

        Args:
            df (pd.DataFrame): DataFrame to format
            extra_formats (Dict[str, str], optional): Dictionary of formats to use. Defaults to None.

        Returns:
            Styler: Formatted DataFrame
        """
        stats = [self.get_stat_by_title(str(x)) for x in df.columns]
        stats = [x for x in stats if x is not None]
        formats = {x.title: x.fmt for x in stats if x.fmt is not None}
        formats = {**formats, **extra_formats} if extra_formats is not None else formats
        return df.style.format(precision=0, na_rep='MISSING', formatter=formats).hide()

    def aggregate_dataframe(self, df: pd.DataFrame, aggregate: Optional[Dict[str, Callable]] = None) -> pd.DataFrame:
        """Aggregate a dataframe

        Args:
            df (pd.DataFrame): DataFrame to aggregate
            aggregate (Dict[str, Callable], optional): Dictionary of aggregators to use. Defaults to None.

        Returns:
            pd.DataFrame: Aggregated DataFrame
        """
        # Aggregate the data
        stats = [self.get_stat_by_title(str(x)) for x in df.columns]
        stats = [x for x in stats if x is not None]
        aggregators = {**{'Year': 'max'}, **{x.title: x.aggregator.__name__ for x in stats}}
        df = df.aggregate(aggregators).reset_index().transpose()
        df.columns = df.iloc[0]
        return df.drop(df.index[0])


class LifeModelAgent(mesa.Agent):
    def __init__(self, model: LifeModel):
        """LifeModelAgent

        Args:
            model (LifeModel): LifeModel.
        """
        super().__init__(model)  # unique_id is now automatically assigned

        # Initialize the stats
        for stat in LifeModel.STATS:
            setattr(self, stat.name, 0)
        for stat in LifeModel.EXTRA_STATS:
            setattr(self, stat.name, 0)

    def pre_step(self):
        """ Pre-step phase. Called for all agents before step phase. """
        pass

    def decision_step(self):
        """Decision phase. Called before pre-step for scenario decision agents."""
        pass

    def prepare_start_year_stats(self):
        """Prepare stats for the initial collection row."""
        pass

    def step(self):
        """ Step phase. Called for all agents after pre-step phase. """
        pass

    def post_step(self):
        """ Post-step phase. Called for all agents after post-step phase. """
        pass


class ModelSetupException(Exception):
    """Exception raised when there is an error setting up the model."""
    pass
