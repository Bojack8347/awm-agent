# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

from pydantic import BaseModel, Field, ConfigDict
from typing import List, Union, Dict, Optional


class StandardDeductionConfig(BaseModel):
    single: int
    married_filing_jointly: int


class TaxBracketsConfig(BaseModel):
    single: List[List[Union[int, float]]]
    married_filing_jointly: List[List[Union[int, float]]]


class SSTaxationThresholdConfig(BaseModel):
    base: int
    second: int


class SSTaxationThresholdsConfig(BaseModel):
    single: SSTaxationThresholdConfig
    married_filing_jointly: SSTaxationThresholdConfig


class HomeSaleExclusionConfig(BaseModel):
    single: float = Field(ge=0)
    married_filing_jointly: float = Field(ge=0)


class FederalTaxConfig(BaseModel):
    standard_deduction: StandardDeductionConfig
    tax_brackets: TaxBracketsConfig
    # IRC §86 provisional-income thresholds for taxing Social Security
    social_security_taxation_thresholds: Optional[SSTaxationThresholdsConfig] = None
    # IRC §121 exclusion of gain on the sale of a primary residence
    home_sale_exclusion: Optional[HomeSaleExclusionConfig] = None


class LowIncomeRecaptureConfig(BaseModel):
    taxable_income_not_over: float
    rate: float = Field(ge=0, le=100)


class FinalFlatRateConfig(BaseModel):
    agi_over: float
    rate: float = Field(ge=0, le=100)


class StateHighIncomeTaxComputationConfig(BaseModel):
    agi_threshold: float
    phase_in_end: float
    low_income_recapture: LowIncomeRecaptureConfig
    recapture_segments: List[List[float]]
    final_flat_rate: FinalFlatRateConfig


class StateTaxTableConfig(BaseModel):
    agi_not_over: float
    taxable_income_less_than: float


class StateTaxConfig(BaseModel):
    jurisdiction: str = 'flat'
    tax_year: Optional[int] = None
    tax_rate: Optional[float] = Field(default=None, ge=0, le=100)
    standard_deduction: Optional[StandardDeductionConfig] = None
    tax_table: Optional[StateTaxTableConfig] = None
    tax_brackets: Optional[TaxBracketsConfig] = None
    high_income_tax_computation: Optional[Dict[str, StateHighIncomeTaxComputationConfig]] = None


class MedicareThresholdConfig(BaseModel):
    single: int
    married_filing_jointly: int


class FICATaxConfig(BaseModel):
    social_security_rate: float = Field(ge=0, le=100)
    social_security_max_income: int
    medicare_rate: float = Field(ge=0, le=100)
    medicare_additional_rate: float = Field(ge=0, le=100)
    medicare_additional_rate_threshold: MedicareThresholdConfig


class TrustTaxConfig(BaseModel):
    flat_tax_rate: float = Field(ge=0, le=100)


class TaxConfig(BaseModel):
    federal: FederalTaxConfig
    state: StateTaxConfig
    fica: FICATaxConfig
    trust: Optional[TrustTaxConfig] = None


class EconomyConfig(BaseModel):
    inflation_rate: float = Field(ge=-100)


class SalaryConfig(BaseModel):
    default_yearly_increase: float = Field(ge=-100)


class EmploymentConfig(BaseModel):
    salary: SalaryConfig


class Job401kContribLimitConfig(BaseModel):
    base: int
    catch_up_age: int
    catch_up_amount: int
    super_catch_up_start_age: int
    super_catch_up_end_age: int
    super_catch_up_amount: int


class IRAConfig(BaseModel):
    contribution_limit: int
    catch_up_age: int
    catch_up_amount: int
    default_growth_rate: float = Field(ge=0)


class RetirementConfig(BaseModel):
    federal_retirement_age: float
    # IRC §72(t) additional tax on early retirement withdrawals
    early_withdrawal_penalty_rate: float = Field(default=10.0, ge=0, le=100)
    job_401k_contrib_limit: Job401kContribLimitConfig
    ira: IRAConfig
    rmd_distribution_periods: List[List[float]]


class SocialSecurityConfig(BaseModel):
    min_eligible_credits: int
    max_credits_per_year: int
    max_years_of_income: int
    min_early_retirement_age: int
    normal_retirement_age: int
    max_delayed_retirement_credit_age: int
    delayed_retirement_credit: float = Field(ge=0)

    # QC amount calculation base values
    qc_credit_amount_1978: float
    qc_avg_wage_index_1976: float

    # Configuration for extrapolation beyond available data
    last_avg_wage_index_year: int
    last_avg_wage_index_increase: float
    last_cost_of_living_adj_year: int
    last_bend_points_year: int

    # Historical data tables
    avg_wage_index: Dict[int, float]
    cost_of_living_adj: Dict[int, float]
    bend_points: Dict[int, List[int]]


class BankAccountConfig(BaseModel):
    default_interest_rate: float = Field(ge=0)
    compound_rate: int = Field(ge=1)


class BrokerageAccountConfig(BaseModel):
    default_growth_rate: float


class HSAAccountConfig(BaseModel):
    contribution_limit: int
    default_employer_contribution: int = Field(ge=0)


class Plan529Config(BaseModel):
    annual_contribution_limit: int
    lifetime_contribution_limit: int
    default_growth_rate: float = Field(ge=0)
    qualified_expense_penalty: float = Field(ge=0, le=100)


class AccountsConfig(BaseModel):
    bank: BankAccountConfig
    brokerage: BrokerageAccountConfig
    hsa: HSAAccountConfig
    plan_529: Optional[Plan529Config] = None


class OpeningMortgageDefaultsConfig(BaseModel):
    home_value_to_mortgage_balance_multiple: float = Field(gt=0)
    home_appreciation_rate: float = Field(gt=-100, le=100)
    interest_rate: float = Field(ge=0, le=100)
    remaining_term_years: int = Field(gt=0, le=100)
    mortgage_type: str
    annual_spending_includes_mortgage: bool


class HousingConfig(BaseModel):
    opening_mortgage_defaults: OpeningMortgageDefaultsConfig


class SurrenderPercentagesConfig(BaseModel):
    early: float = Field(ge=0, le=1)
    standard: float = Field(ge=0, le=1)


class LifeInsuranceConfig(BaseModel):
    default_loan_interest_rate: float = Field(ge=0)
    default_cash_value_growth_rate: float = Field(ge=0)
    default_max_missed_payments: int = Field(ge=0)
    surrender_percentages: SurrenderPercentagesConfig


class InsuranceConfig(BaseModel):
    life: LifeInsuranceConfig


class CreditCardConfig(BaseModel):
    default_interest_rate: float = Field(ge=0)
    default_minimum_payment_percent: float = Field(ge=0, le=100)


class DebtConfig(BaseModel):
    credit_card: CreditCardConfig


class HealthcareConfig(BaseModel):
    cost_inflation_rate: float = Field(ge=-100)
    annual_out_of_pocket: float = Field(ge=0)
    medicare_start_age: int
    medicare_part_b_monthly_premium: float = Field(ge=0)
    medicare_part_d_monthly_premium: float = Field(ge=0)
    medigap_monthly_premium: float = Field(ge=0)
    long_term_care_annual_cost: float = Field(ge=0)
    age_cost_multipliers: Dict[int, float]


class AssetClassAssumptionConfig(BaseModel):
    """Per-asset Monte Carlo assumptions. Returns and volatilities are annual
    percentages; market_beta drives the cross-asset correlation matrix
    (corr[i][j] = beta_i * beta_j). Fields are optional so scenario files can
    override any subset."""
    expected_return: Optional[float] = None
    volatility: Optional[float] = Field(default=None, ge=0)
    market_beta: Optional[float] = Field(default=None, ge=-1, le=1)


class MarketAssumptionsConfig(BaseModel):
    asset_classes: Dict[str, AssetClassAssumptionConfig]


class FinancialConfigModel(BaseModel):
    """Complete financial configuration model with validation"""
    model_config = ConfigDict(extra='forbid')

    economy: EconomyConfig
    employment: EmploymentConfig
    tax: TaxConfig
    retirement: RetirementConfig
    social_security: SocialSecurityConfig
    accounts: AccountsConfig
    housing: HousingConfig
    insurance: InsuranceConfig
    debt: DebtConfig
    healthcare: Optional[HealthcareConfig] = None
    market_assumptions: Optional[MarketAssumptionsConfig] = None
