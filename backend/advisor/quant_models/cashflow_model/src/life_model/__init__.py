# Copyright 2022 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

"""
Cashflow Analytics Engine

A comprehensive personal finance simulation framework for multi-year
cashflow projections with modeling of income, expenses, loans, accounts,
insurance, taxes, and more.

Example usage:
    from life_model import LifeModel, Family, Person, Spending, BankAccount, Job, Salary
    
    model = LifeModel(start_year=2025, end_year=2050)
    family = Family(model)
    person = Person(family=family, name='John', age=30, retirement_age=65,
                    spending=Spending(model, base=30000), state='NY')
    BankAccount(owner=person, company='Bank', type='Checking', balance=20000)
    Job(owner=person, company='Company', role='Employee',
        salary=Salary(model, base=75000, yearly_increase=3))
    
    model.run()
    df = model.get_yearly_stat_df()
"""

# Core simulation
from .model import LifeModel, LifeModelAgent, Event, EventLog

# People
from .people.family import Family
from .people.person import Person, Spending

# Accounts
from .account.bank import BankAccount
from .account.job401k import Job401kAccount
from .account.brokerage import BrokerageAccount
from .account.investment_return import InvestmentReturn
from .account.asset_allocation import (
    ALLOWED_ASSET_CLASSES,
    AssetAllocation,
    AssetReturnRates,
    get_allowed_asset_classes,
)
from .account.hsa import HealthSavingsAccount
from .account.roth_IRA import RothIRA
from .account.traditional_IRA import TraditionalIRA

# Work
from .work.job import Job, Salary

# Debt
from .debt.student_loan import StudentLoan
from .debt.car_loan import CarLoan
from .debt.credit_card import CreditCard

# Retirement benefits and estate planning
from .account.pension import Pension
from .account.trust import Trust, TrustType

# Non-housing real assets
from .assets.tangible_asset import TangibleAsset, AssetLoan, AssetType

# Healthcare and aging
from .healthcare.healthcare import Healthcare

# Insurance
from .insurance.life_insurance import LifeInsurance, LifeInsuranceType
from .insurance.social_security import SocialSecurity
from .insurance.general_insurance import Insurance, InsuranceType
from .insurance.annuity import Annuity

# Housing
from .housing.home import Home, Mortgage, MortgageType, HomeExpenses
from .housing.apartment import Apartment

# Tax regimes
from .tax.regime import CurrentLawTaxRegime, TaxInput, TaxRegime

# Dependents
from .dependents.child import Child
from .dependents.plan529 import Plan529

# Life Events
from .lifeevents import LifeEvents, LifeEvent, LifeEventName, FinancialDecisionEvent, FIXED_LIFE_EVENT_NAMES

# Monte Carlo Simulation
from .montecarlo import (
    MonteCarloSimulator,
    MonteCarloConfig,
    MonteCarloResults,
    MarketAssumptions,
    AssetClassAssumptions,
    AccountParametersCalculator,
    AccountStochasticParams,
    AccountCorrelatedReturnGenerator,
    YearVaryingMarketAssumptions,
    YearVaryingAccountReturnGenerator,
    InvestmentAccountRegistry,
)

# Version
from .__meta__ import __version__

__all__ = [
    # Core
    'LifeModel', 'LifeModelAgent', 'Event', 'EventLog',
    # People
    'Family', 'Person', 'Spending',
    # Accounts
    'BankAccount', 'Job401kAccount', 'BrokerageAccount', 'InvestmentReturn',
    'ALLOWED_ASSET_CLASSES', 'AssetAllocation', 'AssetReturnRates', 'get_allowed_asset_classes',
    'HealthSavingsAccount', 'RothIRA', 'TraditionalIRA',
    # Work
    'Job', 'Salary',
    # Debt
    'StudentLoan', 'CarLoan', 'CreditCard',
    # Retirement benefits and estate planning
    'Pension', 'Trust', 'TrustType',
    # Non-housing real assets
    'TangibleAsset', 'AssetLoan', 'AssetType',
    # Healthcare and aging
    'Healthcare',
    # Insurance
    'LifeInsurance', 'LifeInsuranceType', 'SocialSecurity', 
    'Insurance', 'InsuranceType', 'Annuity',
    # Housing
    'Home', 'Mortgage', 'MortgageType', 'HomeExpenses', 'Apartment',
    # Tax regimes
    'CurrentLawTaxRegime', 'TaxInput', 'TaxRegime',
    # Dependents
    'Child', 'Plan529',
    # Life Events
    'LifeEvents', 'LifeEvent', 'LifeEventName', 'FinancialDecisionEvent', 'FIXED_LIFE_EVENT_NAMES',
    # Monte Carlo
    'MonteCarloSimulator', 'MonteCarloConfig', 'MonteCarloResults',
    'MarketAssumptions', 'AssetClassAssumptions',
    'AccountParametersCalculator', 'AccountStochasticParams',
    'AccountCorrelatedReturnGenerator', 'InvestmentAccountRegistry',
    'YearVaryingMarketAssumptions', 'YearVaryingAccountReturnGenerator',
    # Version
    '__version__',
]
