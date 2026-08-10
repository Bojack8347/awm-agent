# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE
import html
from typing import Dict, Optional, TYPE_CHECKING
from ..people.person import Person
from ..base_classes import Investment
from ..config.config_manager import config
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


class BrokerageAccount(Investment):
    def __init__(self, person: Person, company: str,
                 balance: float = 0, growth_rate: Optional[float] = None,
                 asset_allocation: AssetAllocationInput = None,
                 asset_return_rates: AssetReturnRatesInput = None,
                 market_assumptions: Optional['MarketAssumptions'] = None):
        """ Models a brokerage/investment account

        Args:
            person: The person who owns this account
            company: Brokerage company name
            balance: Current account balance
            growth_rate: Expected annual growth rate percentage (fallback if no
                        allocation). Defaults to the configured
                        accounts.brokerage.default_growth_rate, so economic
                        scenarios can override it.
            asset_allocation: Optional asset allocation input. Accepts a dict
                            mapping asset class names to weights, an
                            AssetAllocation object, or Agent-style entries such
                            as [{"asset": "US Equity", "weight": 0.6}].
                            When provided with market_assumptions, the expected
                            return and volatility are derived from the
                            allocation. Raw weights should sum to 1.0.
            asset_return_rates: Optional return-rate input. Accepts a dict
                              mapping asset class names to percentage return
                              rates, an AssetReturnRates object, or Agent-style
                              entries such as {"asset": "US Equity",
                              "return_rate": 10.0}. If provided with
                              asset_allocation, deterministic growth uses the
                              weighted return.
            market_assumptions: Optional MarketAssumptions for deriving return/volatility
                              from asset_allocation. If None but allocation is provided,
                              will use default assumptions.
        """
        if growth_rate is None:
            growth_rate = config.financial.get('accounts.brokerage.default_growth_rate', 7.0)
        super().__init__(person, balance, growth_rate)
        self.company = company
        self.investments = []  # List of individual investments
        asset_allocation = coerce_asset_allocation(asset_allocation)
        asset_return_rates = coerce_asset_return_rates(asset_return_rates)
        self._asset_allocation = asset_allocation
        self._asset_return_rates = asset_return_rates
        self._market_assumptions = market_assumptions
        self._account_id = f"brokerage_{id(self)}"
        self._stochastic_growth_applied = False  # Track if MC growth was applied this step

        # After-tax cost basis. Growth (deterministic or stochastic) is
        # unrealized appreciation and only becomes taxable when withdrawn.
        # Realized gains are recognized in the following year's settlement,
        # matching InvestmentReturn and the codebase's deferred-recognition
        # pattern for late-year liquidations.
        self.cost_basis = balance
        self._pending_realized_gains = 0.0
        
        # Cached derived values from asset allocation
        self._derived_expected_return: Optional[float] = None
        self._derived_volatility: Optional[float] = None
        
        # Calculate derived values if allocation is provided
        if asset_allocation is not None:
            self._calculate_derived_params()

        self.model.registries.brokerage_accounts.register(person, self)

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
        
        # Get or create market assumptions
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
        """Set the asset allocation for this account.
        
        Args:
            value: Allocation input accepted by coerce_asset_allocation, or None
        
        Raises:
            ValueError: If weights don't sum to approximately 1.0
        """
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
    def market_assumptions(self) -> Optional['MarketAssumptions']:
        """Get the market assumptions used for this account."""
        return self._market_assumptions
    
    @market_assumptions.setter
    def market_assumptions(self, value: Optional['MarketAssumptions']):
        """Set market assumptions and recalculate derived params."""
        self._market_assumptions = value
        if self._asset_allocation is not None:
            self._calculate_derived_params()
    
    @property
    def derived_expected_return(self) -> Optional[float]:
        """Get expected return derived from asset allocation (as decimal, e.g., 0.08 for 8%)."""
        return self._derived_expected_return
    
    @property
    def derived_volatility(self) -> Optional[float]:
        """Get volatility derived from asset allocation (as decimal, e.g., 0.15 for 15%)."""
        return self._derived_volatility
    
    @property
    def effective_growth_rate(self) -> float:
        """Get the effective growth rate used for deterministic calculations.
        
        If asset allocation is provided, uses derived expected return.
        Otherwise, falls back to the fixed growth_rate parameter.
        
        Returns:
            Growth rate as percentage (e.g., 7.0 for 7%)
        """
        if self._derived_expected_return is not None:
            return self._derived_expected_return * 100  # Convert to percentage
        return self.growth_rate
    
    @property
    def account_id(self) -> str:
        """Unique identifier for this account (used in Monte Carlo simulation)."""
        return self._account_id

    def apply_stochastic_return(self, return_rate: float) -> float:
        """Apply a stochastic return rate (used in Monte Carlo mode).
        
        This method is called by the InvestmentAccountRegistry during
        probabilistic simulation to apply correlated returns.
        
        Args:
            return_rate: Annual return as decimal (e.g., 0.08 for 8%)
        
        Returns:
            The growth amount applied
        """
        growth = self.balance * return_rate
        self.balance += growth
        self.stat_growth_history.append(growth)
        self.stat_investment_return = growth
        self._stochastic_growth_applied = True
        return growth

    def calculate_growth(self) -> float:
        """Calculate investment growth based on effective growth rate.
        
        Uses derived expected return from asset allocation if available,
        otherwise falls back to the fixed growth_rate parameter.
        """
        return self.balance * (self.effective_growth_rate / 100)

    def apply_growth(self):
        """Apply calculated growth to balance.
        
        In Monte Carlo mode, if stochastic return was already applied,
        skip deterministic growth to avoid double-counting.
        """
        if self._stochastic_growth_applied:
            # Reset flag for next step; stochastic growth already applied
            self._stochastic_growth_applied = False
            return 0
        
        # Deterministic mode: apply normal growth
        growth = self.calculate_growth()
        self.balance += growth
        self.stat_growth_history.append(growth)
        self.stat_investment_return = growth
        return growth

    def get_balance(self) -> float:
        return self.balance

    def deposit(self, amount: float) -> bool:
        if amount < 0:
            raise ValueError("Deposit amount cannot be negative")
        self.balance += amount
        # Contributions are after-tax dollars, so they add to cost basis.
        self.cost_basis += amount
        return True

    def withdraw(self, amount: float) -> float:
        if amount < 0:
            return 0.0  # Cannot withdraw negative amounts
        if self.balance <= 0:
            return 0.0
        actual_withdrawal = min(amount, self.balance)

        # Liquidating realizes the proportional share of unrealized gains
        # (balance above cost basis). The realized gain is recognized as
        # ordinary income at the next settlement (deferred one year, matching
        # InvestmentReturn), so gap-plugging brokerage draws are no longer
        # silently tax-free.
        gain_fraction = max(0.0, self.balance - self.cost_basis) / self.balance
        realized_gain = actual_withdrawal * gain_fraction
        if realized_gain > 0:
            self._pending_realized_gains += realized_gain
        self.cost_basis = max(0.0, self.cost_basis - actual_withdrawal * (1 - gain_fraction))

        self.balance -= actual_withdrawal
        return actual_withdrawal

    def flush_realized_gains(self) -> float:
        """Return and clear realized gains pending recognition.

        Called at settlement time (before taxes are computed) by the owner so
        gains realized during last year's bill paying are taxed this year.
        """
        gains = self._pending_realized_gains
        self._pending_realized_gains = 0.0
        return gains

    def _repr_html_(self):
        desc = '<ul>'
        desc += f'<li>Company: {html.escape(self.company)}</li>'
        desc += f'<li>Balance: ${self.balance:,.2f}</li>'
        if self._asset_allocation:
            desc += '<li>Asset Allocation: '
            alloc_str = ', '.join(f'{k}: {v:.0%}' for k, v in self._asset_allocation.items())
            desc += html.escape(alloc_str)
            desc += '</li>'
            if self._derived_expected_return is not None:
                desc += f'<li>Expected Return: {self._derived_expected_return:.2%} (derived from allocation)</li>'
            if self._derived_volatility is not None:
                desc += f'<li>Volatility: {self._derived_volatility:.2%}</li>'
        else:
            desc += f'<li>Growth Rate: {self.growth_rate}% (fixed)</li>'
        desc += '</ul>'
        return desc

    def prepare_start_year_stats(self):
        self.stat_brokerage_balance = self.balance

    def step(self):
        super().step()
        self.stat_brokerage_balance = self.balance
