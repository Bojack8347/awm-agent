# Copyright 2022 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

from typing import Optional, Dict, TYPE_CHECKING
from ..limits import federal_retirement_age, required_min_distrib, early_withdrawal_penalty_rate
from ..base_classes import RetirementAccount
from .asset_allocation import (
    AssetAllocationInput,
    AssetReturnRatesInput,
    calculate_weighted_return_rate,
    coerce_asset_allocation,
    coerce_asset_return_rates,
    validate_asset_allocation,
)

if TYPE_CHECKING:
    from ..work.job import Job
    from ..montecarlo.market_assumptions import MarketAssumptions


class Job401kAccount(RetirementAccount):
    def __init__(self, job: 'Job',
                 pretax_balance: float = 0, pretax_contrib_percent: float = 0,
                 roth_balance: float = 0, roth_contrib_percent: float = 0,
                 average_growth: float = 0, company_match_percent: float = 0,
                 asset_allocation: AssetAllocationInput = None,
                 asset_return_rates: AssetReturnRatesInput = None,
                 market_assumptions: Optional['MarketAssumptions'] = None):
        """401k Account

        Args:
            job (Job): Job offering the 401k plan.
            pretax_balance (float, optional): Initial pre-tax balance of account. Defaults to 0.
            pretax_contrib_percent (float, optional): Pre-tax contribution percentage. Defaults to 0.
            roth_balance (float, optional): Initial roth balance of account. Defaults to 0.
            roth_contrib_percent (float, optional): Roth contribution percentage. Defaults to 0.
            average_growth (float, optional): Average account growth every year (fallback). Defaults to 0.
            company_match_percent (float, optional): Percentage that company matches contributions. Defaults to 0.
            asset_allocation: Optional allocation input. Accepts a dict mapping
                            asset class names to weights, an AssetAllocation
                            object, or Agent-style entries. When provided,
                            expected return is derived from allocation.
            asset_return_rates: Optional return-rate input. Accepts a dict
                            mapping asset class names to percentage return
                            rates, an AssetReturnRates object, or Agent-style
                            entries. If provided with asset_allocation,
                            deterministic growth uses the weighted return.
            market_assumptions: Optional MarketAssumptions for deriving return/volatility.
        """
        super().__init__(job.owner, 0)  # Initialize with 0, we'll handle balance ourselves
        self.job: Optional['Job'] = job
        self.pretax_balance = pretax_balance
        self.pretax_contrib_percent = pretax_contrib_percent
        self.roth_balance = roth_balance
        # Opening Roth balances are treated as contributions; growth credited
        # during the simulation accumulates as earnings above this basis.
        self.roth_basis = roth_balance
        self.roth_contrib_percent = roth_contrib_percent
        # Earnings withdrawn from the Roth balance before the federal
        # retirement age are recognized at the next settlement (taxable
        # income plus the early-withdrawal penalty).
        self._pending_roth_taxable = 0.0
        self._pending_roth_penalty = 0.0
        self.average_growth = average_growth
        self.company_match_percent = company_match_percent

        self.stat_required_min_distrib = 0
        self.stat_401k_balance = 0
        
        # Monte Carlo support
        asset_allocation = coerce_asset_allocation(asset_allocation)
        asset_return_rates = coerce_asset_return_rates(asset_return_rates)
        self._asset_allocation = asset_allocation
        self._asset_return_rates = asset_return_rates
        self._market_assumptions = market_assumptions
        self._account_id = f"401k_{id(self)}"
        self._stochastic_growth_applied = False
        self._stochastic_rate = 0.0
        
        # Cached derived values
        self._derived_expected_return: Optional[float] = None
        self._derived_volatility: Optional[float] = None
        
        if asset_allocation is not None:
            self._calculate_derived_params()

        job.retirement_account = self

    def _calculate_derived_params(self):
        """Calculate expected return and volatility from asset allocation."""
        if self._asset_allocation is None:
            self._derived_expected_return = None
            self._derived_volatility = None
            return

        if self._asset_return_rates is not None:
            weighted_return_rate = calculate_weighted_return_rate(
                self._asset_allocation,
                self._asset_return_rates,
            )
            self._derived_expected_return = weighted_return_rate / 100
            self._derived_volatility = None
            return
        
        market = self._market_assumptions
        if market is None:
            from ..montecarlo.market_assumptions import MarketAssumptions
            market = MarketAssumptions.create_default()
            self._market_assumptions = market
        
        from ..montecarlo.account_parameters import AccountParametersCalculator
        calc = AccountParametersCalculator(market)
        params = calc.calculate_account_params(self._account_id, self._asset_allocation)
        
        self._derived_expected_return = params.expected_return
        self._derived_volatility = params.volatility

    @property
    def asset_allocation(self) -> Optional[Dict[str, float]]:
        """Get the asset allocation for this account."""
        return self._asset_allocation
    
    @asset_allocation.setter
    def asset_allocation(self, value: AssetAllocationInput):
        """Set the asset allocation for this account."""
        if value is not None:
            validate_asset_allocation(value)
        self._asset_allocation = coerce_asset_allocation(value)
        self._calculate_derived_params()

    @property
    def asset_return_rates(self) -> Optional[Dict[str, float]]:
        """Get directly supplied asset return rates in percentage form."""
        return self._asset_return_rates

    @asset_return_rates.setter
    def asset_return_rates(self, value: AssetReturnRatesInput):
        """Set directly supplied asset return rates and recalculate return."""
        self._asset_return_rates = coerce_asset_return_rates(value)
        self._calculate_derived_params()
    
    @property
    def account_id(self) -> str:
        """Unique identifier for this account."""
        return self._account_id
    
    @property
    def effective_growth_rate(self) -> float:
        """Get effective growth rate (derived from allocation or fixed average_growth)."""
        if self._derived_expected_return is not None:
            return self._derived_expected_return * 100
        return self.average_growth

    def apply_stochastic_return(self, return_rate: float) -> float:
        """Record a stochastic return rate for this year (Monte Carlo mode).

        The rate is consumed during pre_step at the exact point deterministic
        growth applies, so contribution ordering and the RMD base (the prior
        year-end balance) are identical between simulation modes. Rates are
        floored at -100% because a long-only account cannot lose more than
        its value.

        Returns:
            The projected growth on the current balance, for reporting.
        """
        return_rate = max(return_rate, -1.0)
        self._stochastic_rate = return_rate
        self._stochastic_growth_applied = True
        return self.balance * return_rate

    def pretax_contrib(self, salary: float):
        return salary * (self.pretax_contrib_percent / 100)

    def roth_contrib(self, salary: float):
        return salary * (self.roth_contrib_percent / 100)

    def company_match(self, contribution: float):
        return contribution * (self.company_match_percent / 100)

    @property
    def balance(self):
        return self.pretax_balance + self.roth_balance

    @balance.setter
    def balance(self, value):
        if value < 0:
            raise ValueError('401k balance cannot be negative')
        if not hasattr(self, 'pretax_balance') or not hasattr(self, 'roth_balance'):
            return

        current_balance = self.balance
        if current_balance > 0:
            pretax_share = self.pretax_balance / current_balance
            previous_roth_balance = self.roth_balance
            self.pretax_balance = value * pretax_share
            self.roth_balance = value - self.pretax_balance
            if previous_roth_balance > 0:
                self.roth_basis *= self.roth_balance / previous_roth_balance
        else:
            self.pretax_balance = value
            self.roth_balance = 0.0
            self.roth_basis = 0.0

    def get_balance(self) -> float:
        """Get current account balance"""
        return self.balance

    def deposit(self, amount: float) -> bool:
        """Deposit amount into account. Returns success status"""
        if amount <= 0:
            return False
        # For 401k, deposits go to pretax by default
        self.pretax_balance += amount
        return True

    def withdraw(self, amount: float) -> float:
        """Withdraw amount from account. Returns actual amount withdrawn"""
        if amount <= 0:
            return 0.0
        # Withdraw from pretax first, then roth
        total_withdrawn = self.deduct_pretax(amount)
        if total_withdrawn < amount:
            total_withdrawn += self.deduct_roth(amount - total_withdrawn)
        return total_withdrawn

    def _repr_html_(self):
        company = self.job.company if self.job is not None else "<None>"
        return f"401k at {company} balance: ${self.balance:,}"

    def prepare_start_year_stats(self):
        self.stat_401k_balance = self.balance
        self.stat_useable_balance = self.balance if self.is_useable else 0

    # Using pre_step() so taxable_income will be set before person's step() is called
    def pre_step(self):
        # Recognize Roth earnings withdrawn during last year's bill paying:
        # they are taxed (plus penalty) at this year's settlement, matching
        # when the tax on a late-year distribution would actually be paid.
        if self._pending_roth_taxable > 0:
            self.person.taxable_income += self._pending_roth_taxable
            self._pending_roth_taxable = 0.0
        if self._pending_roth_penalty > 0:
            self.person.early_withdrawal_penalty += self._pending_roth_penalty
            self._pending_roth_penalty = 0.0

        # The RMD base is the prior year-end balance (IRS Pub. 590-B), so it
        # is snapshotted before this year's growth in BOTH simulation modes.
        rmd_base_pretax_balance = self.pretax_balance

        # The growth rate comes from the recorded stochastic draw in Monte
        # Carlo mode and from the configured/derived rate otherwise. Both
        # apply at this same point, so contribution ordering and the RMD
        # base are identical between modes.
        if self._stochastic_growth_applied:
            growth_rate = self._stochastic_rate * 100
            self._stochastic_growth_applied = False
        else:
            growth_rate = self.effective_growth_rate
        pretax_growth = self.pretax_balance * (growth_rate / 100)
        roth_growth = self.roth_balance * (growth_rate / 100)
        self.pretax_balance += pretax_growth
        self.roth_balance += roth_growth
        self.stat_investment_return = pretax_growth + roth_growth

        # Balance is automatically calculated by the property

        # Track balance history
        self.stat_balance_history.append(self.balance)
        if (self.person.age > federal_retirement_age()):
            self.stat_useable_balance = self.balance

        # Required minimum distributions
        # - Based on the owner's age, force withdraw the required minium
        required_min_dist_amount = self.deduct_pretax(
            required_min_distrib(self.person.age, rmd_base_pretax_balance)
        )
        self.person.deposit_into_cashflow_bank_account(required_min_dist_amount)
        self.person.taxable_income += required_min_dist_amount

        self.stat_required_min_distrib = required_min_dist_amount
        self.stat_401k_balance = self.balance

    def deduct_pretax(self, amount: float):
        """Deduct from pre-tax balance

        Args:
            amount (float): Amount to deduct.

        Returns:
            float: Amount deducted. Will not be less than the account balance.
        """
        # TODO - Need to figure out where early penalties and limits are applied
        amount_deducted = min(self.pretax_balance, amount)
        self.pretax_balance -= amount_deducted
        return amount_deducted

    def deduct_roth(self, amount: float) -> float:
        """Deduct from roth balance.

        Designated Roth (401k) distributions before the federal retirement
        age are non-qualified: the withdrawal is split pro-rata between
        contributions (tax- and penalty-free) and earnings, and the earnings
        portion is recognized as ordinary income plus the early-withdrawal
        penalty at the next settlement. Withdrawals at or after the federal
        retirement age are treated as qualified and tax-free.

        Args:
            amount (float): Amount to deduct.

        Returns:
            float: Amount deducted. Will not be less than the account balance.
        """
        amount_deducted = min(self.roth_balance, amount)
        if amount_deducted <= 0:
            return 0.0

        earnings = max(0.0, self.roth_balance - self.roth_basis)
        earnings_fraction = earnings / self.roth_balance
        earnings_portion = amount_deducted * earnings_fraction
        basis_portion = amount_deducted - earnings_portion

        self.roth_balance -= amount_deducted
        self.roth_basis = max(0.0, self.roth_basis - basis_portion)

        if earnings_portion > 0 and self.person.age < federal_retirement_age():
            self._pending_roth_taxable += earnings_portion
            self._pending_roth_penalty += earnings_portion * (early_withdrawal_penalty_rate() / 100)

        return amount_deducted
