# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE
from typing import Dict, Optional, TYPE_CHECKING
from ..people.person import Person
from ..base_classes import Investment
from ..config.config_manager import config
from ..limits import required_min_distrib
from ..model import compound_interest
from .asset_allocation import (
    AssetAllocationInput,
    AssetReturnRatesInput,
    calculate_weighted_return_rate,
    coerce_asset_allocation,
    coerce_asset_return_rates,
    validate_asset_allocation,
)

if TYPE_CHECKING:
    from ..montecarlo.market_assumptions import MarketAssumptions


class TraditionalIRA(Investment):
    def __init__(self, person: Person, balance: float = 0, growth_rate: Optional[float] = None,
                 contribution_limit: float = 7500,
                 asset_allocation: AssetAllocationInput = None,
                 asset_return_rates: AssetReturnRatesInput = None,
                 market_assumptions: Optional['MarketAssumptions'] = None):
        """ Models a Traditional IRA account for a person

        Args:
            person: The person to which this IRA belongs
            balance: Current balance in the IRA
            growth_rate: Expected annual growth rate percentage (fallback if no
                        allocation). Defaults to the configured
                        retirement.ira.default_growth_rate, so economic
                        scenarios can override it.
            contribution_limit: Annual contribution limit
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
        if growth_rate is None:
            growth_rate = config.financial.get('retirement.ira.default_growth_rate', 7.0)
        super().__init__(person, balance, growth_rate)
        self.contribution_limit = contribution_limit
        self.contributions_this_year = 0
        asset_allocation = coerce_asset_allocation(asset_allocation)
        asset_return_rates = coerce_asset_return_rates(asset_return_rates)
        self._asset_allocation = asset_allocation
        self._asset_return_rates = asset_return_rates
        self._market_assumptions = market_assumptions
        self._account_id = f"traditional_ira_{id(self)}"
        self._stochastic_growth_applied = False
        
        # Cached derived values
        self._derived_expected_return: Optional[float] = None
        self._derived_volatility: Optional[float] = None
        
        if asset_allocation is not None:
            self._calculate_derived_params()

        self.model.registries.traditional_iras.register(person, self)

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
        """Get effective growth rate (derived from allocation or fixed)."""
        if self._derived_expected_return is not None:
            return self._derived_expected_return * 100
        return self.growth_rate

    def apply_stochastic_return(self, return_rate: float) -> float:
        """Apply a stochastic return rate (used in Monte Carlo mode)."""
        growth = self.balance * return_rate
        self.balance += growth
        self.stat_growth_history.append(growth)
        self.stat_investment_return = growth
        self._stochastic_growth_applied = True
        return growth

    def apply_growth(self):
        """Apply calculated growth to balance."""
        if self._stochastic_growth_applied:
            self._stochastic_growth_applied = False
            return 0
        growth = self.calculate_growth()
        self.balance += growth
        self.stat_growth_history.append(growth)
        self.stat_investment_return = growth
        return growth

    def contribute(self, amount: float) -> float:
        """Make a contribution to the IRA

        Args:
            amount: Amount to contribute

        Returns:
            Amount actually contributed (limited by contribution limit)
        """
        available_limit = self.contribution_limit - self.contributions_this_year
        actual_contribution = min(amount, available_limit)

        if actual_contribution > 0:
            self.balance += actual_contribution
            self.contributions_this_year += actual_contribution

        return actual_contribution

    def get_balance(self) -> float:
        """Get current account balance"""
        return self.balance

    def deposit(self, amount: float) -> bool:
        """Deposit amount into account. Returns success status"""
        if amount <= 0:
            return False
        contribution = self.contribute(amount)
        return contribution > 0

    def withdraw(self, amount: float) -> float:
        """Withdraw amount from account. Returns actual amount withdrawn"""
        if amount <= 0:
            return 0.0
        # Traditional IRA withdrawals may have penalties, but for simplicity
        # we'll just allow withdrawals up to the balance
        amount_withdrawn = min(self.balance, amount)
        self.balance -= amount_withdrawn
        return amount_withdrawn

    def calculate_growth(self) -> float:
        """Calculate investment growth for the period using effective growth rate."""
        return compound_interest(self.balance, self.effective_growth_rate, 1, 1)

    def reset_annual_contributions(self):
        """Reset annual contribution tracking (called at year end)"""
        self.contributions_this_year = 0

    def _repr_html_(self):
        desc = '<ul>'
        desc += f'<li>Balance: ${self.balance:,.2f}</li>'
        desc += f'<li>Growth Rate: {self.growth_rate}%</li>'
        desc += f'<li>Contribution Limit: ${self.contribution_limit:,.2f}</li>'
        desc += f'<li>Contributions This Year: ${self.contributions_this_year:,.2f}</li>'
        desc += '</ul>'
        return desc

    def prepare_start_year_stats(self):
        self.stat_traditional_ira_balance = self.balance
        self.stat_required_min_distrib = 0.0

    def pre_step(self):
        rmd_base_balance = self.balance
        self.apply_growth()
        required_distribution = self.withdraw(
            required_min_distrib(self.person.age, rmd_base_balance)
        )
        if required_distribution > 0:
            self.person.deposit_into_cashflow_bank_account(required_distribution)
            self.person.taxable_income += required_distribution
        self.stat_required_min_distrib = required_distribution
        self.stat_traditional_ira_balance = self.balance

    def step(self):
        self.stat_balance_history.append(self.balance)
        self.stat_traditional_ira_balance = self.balance
