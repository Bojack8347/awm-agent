# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

"""
Monte Carlo simulation orchestrator.

This module provides the MonteCarloSimulator class which orchestrates
running multiple simulation iterations with correlated stochastic returns.
"""

from typing import Callable, Dict, Iterable, Optional, Tuple, TYPE_CHECKING
import numpy as np

from typing import Union

from ..account.asset_allocation import coerce_asset_allocation
from .config import MonteCarloConfig
from .market_assumptions import MarketAssumptions, YearVaryingMarketAssumptions
from .account_parameters import AccountParametersCalculator
from .return_generator import (
    AccountCorrelatedReturnGenerator,
    PrecomputedAccountReturnGenerator,
    YearVaryingAccountReturnGenerator,
)
from .account_registry import InvestmentAccountRegistry
from .results import MonteCarloResults

if TYPE_CHECKING:
    from ..model import LifeModel


class MonteCarloSimulator:
    """Orchestrates Monte Carlo simulations at investment account level.
    
    This simulator runs multiple iterations of the LifeModel simulation,
    applying correlated stochastic returns to investment accounts based
    on their asset allocations and market assumptions.
    
    The workflow:
    1. Create a fresh model using the provided factory function
    2. Collect all investment accounts with asset allocations
    3. Calculate account-level correlations from asset allocations
    4. Generate correlated returns each simulation year
    5. Aggregate results across all simulations
    
    Example:
        >>> market = MarketAssumptions.create_default()
        >>> simulator = MonteCarloSimulator(
        ...     market_assumptions=market,
        ...     config=MonteCarloConfig(num_simulations=100)
        ... )
        >>> results = simulator.run(create_model_function)
        >>> print(f"Success rate: {results.success_rate():.1%}")
    """
    
    def __init__(self,
                 market_assumptions: Optional[Union[MarketAssumptions,
                                                    YearVaryingMarketAssumptions]] = None,
                 config: Optional[MonteCarloConfig] = None):
        """Initialize the simulator.

        Args:
            market_assumptions: Market assumptions, either a constant
                               MarketAssumptions or a YearVaryingMarketAssumptions
                               term structure (e.g. supplied per-year by an
                               upstream model). If None, uses the configured
                               defaults.
            config: Simulation configuration. If None, uses defaults.
        """
        self.market = market_assumptions or MarketAssumptions.create_default()
        self.config = config or MonteCarloConfig()
        base_market = (self.market.base
                       if isinstance(self.market, YearVaryingMarketAssumptions)
                       else self.market)
        self.param_calculator = AccountParametersCalculator(base_market)
    
    def run(
        self,
        model_factory: Callable[[], 'LifeModel'],
        result_columns: Optional[Iterable[str]] = None,
        precomputed_return_paths: Optional[np.ndarray] = None,
    ) -> MonteCarloResults:
        """Run Monte Carlo simulation.
        
        Args:
            model_factory: Callable that creates a fresh LifeModel instance
                          with investment accounts configured. This function
                          is called once per simulation iteration.
            result_columns: Optional model-level reporter titles needed for
                          aggregation. Per-agent history is disabled for every
                          Monte Carlo path. If omitted, all model reporters are
                          retained for backward-compatible full projections.
        
        Returns:
            MonteCarloResults containing aggregated simulation data
        """
        rng = np.random.RandomState(self.config.random_seed)
        
        all_results = []
        array_results = []
        result_columns_order = None
        result_years = None
        return_generator_cache: Dict[
            Tuple[Tuple[str, Tuple[Tuple[str, float], ...]], ...],
            Union[
                AccountCorrelatedReturnGenerator,
                YearVaryingAccountReturnGenerator,
            ],
        ] = {}
        expected_signature = None
        expected_years = None
        precomputed_returns = (
            None
            if precomputed_return_paths is None
            else np.asarray(precomputed_return_paths, dtype=float)
        )
        if (
            precomputed_returns is not None
            and (
                precomputed_returns.ndim != 3
                or precomputed_returns.shape[0] != self.config.num_simulations
            )
        ):
            raise ValueError(
                "Precomputed Monte Carlo returns must have shape "
                "(num_simulations, years, accounts)"
            )
        
        for sim_idx in range(self.config.num_simulations):
            # Create fresh model for this simulation
            model = model_factory()
            model.configure_monte_carlo_reporting(
                result_columns,
                array_backed=result_columns is not None,
            )
            
            # Collect accounts with asset allocations
            registry = self._build_registry(model)
            accounts_with_alloc = registry.get_accounts_with_allocations()
            
            if accounts_with_alloc:
                signature = self._account_allocation_signature(accounts_with_alloc)
                years = tuple(range(int(model.start_year) + 1, int(model.end_year) + 1))
                if expected_signature is None:
                    expected_signature = signature
                    expected_years = years
                elif signature != expected_signature or years != expected_years:
                    raise ValueError(
                        "Monte Carlo model factory changed account allocations or "
                        "projection years between paths"
                    )
                if (
                    precomputed_returns is not None
                    and precomputed_returns.shape[1:] != (
                        len(years),
                        len(accounts_with_alloc),
                    )
                ):
                    raise ValueError(
                        "Precomputed Monte Carlo return dimensions do not match "
                        "the model factory"
                    )

                if precomputed_returns is None:
                    template = return_generator_cache.get(signature)
                    if template is None:
                        template = self._build_return_generator(accounts_with_alloc)
                        return_generator_cache[signature] = template
                    if isinstance(template, YearVaryingAccountReturnGenerator):
                        (
                            precomputed_returns,
                            _template_account_order,
                        ) = template.generate_return_tensor(
                            num_paths=self.config.num_simulations,
                            years=years,
                            rng=rng,
                        )
                    else:
                        precomputed_returns = template.generate_return_tensor(
                            num_paths=self.config.num_simulations,
                            years=years,
                            rng=rng,
                        )
                return_gen = PrecomputedAccountReturnGenerator(
                    [account_id for account_id, _allocation in accounts_with_alloc],
                    precomputed_returns[sim_idx],
                )

                # Set model to probabilistic mode
                model.set_simulation_mode('probabilistic', return_gen, registry)
            
            # Run simulation
            model.run()
            
            # Collect results
            if result_columns is not None:
                values, columns = model.datacollector.get_array_result()
                if result_columns_order is None:
                    result_columns_order = columns
                    year_index = columns.index("Year")
                    result_years = values[:, year_index].astype(int).tolist()
                elif columns != result_columns_order:
                    raise ValueError(
                        "Monte Carlo result columns changed between simulation paths"
                    )
                array_results.append(values.copy())
            else:
                df = model.datacollector.get_model_vars_dataframe()
                all_results.append(df)
        
        if result_columns is not None:
            if not array_results:
                return MonteCarloResults([])
            return MonteCarloResults.from_array(
                np.stack(array_results, axis=0),
                columns=result_columns_order or [],
                years=result_years or [],
            )
        return MonteCarloResults(all_results)

    def prepare_return_paths(
        self,
        model_factory: Callable[[], 'LifeModel'],
    ) -> Optional[np.ndarray]:
        """Generate the seeded return tensor once for process-batched execution."""

        model = model_factory()
        registry = self._build_registry(model)
        accounts_with_alloc = registry.get_accounts_with_allocations()
        if not accounts_with_alloc:
            return None
        years = tuple(range(int(model.start_year) + 1, int(model.end_year) + 1))
        template = self._build_return_generator(accounts_with_alloc)
        rng = np.random.RandomState(self.config.random_seed)
        if isinstance(template, YearVaryingAccountReturnGenerator):
            values, _account_order = template.generate_return_tensor(
                num_paths=self.config.num_simulations,
                years=years,
                rng=rng,
            )
            return values
        return template.generate_return_tensor(
            num_paths=self.config.num_simulations,
            years=years,
            rng=rng,
        )

    def _build_return_generator(
        self,
        accounts_with_alloc,
    ) -> Union[
        AccountCorrelatedReturnGenerator,
        YearVaryingAccountReturnGenerator,
    ]:
        """Build immutable stochastic setup for one account-allocation signature."""

        if isinstance(self.market, YearVaryingMarketAssumptions):
            # This generator contains only allocation-derived matrices and a
            # lazy per-year cache. It has no path balance or RNG state.
            return YearVaryingAccountReturnGenerator(
                accounts_with_alloc,
                self.market,
            )
        corr_matrix, account_order, params = (
            self.param_calculator.calculate_account_correlation_matrix(
                accounts_with_alloc
            )
        )
        return AccountCorrelatedReturnGenerator(
            params,
            corr_matrix,
            account_order,
        )

    @staticmethod
    def _account_allocation_signature(
        accounts_with_alloc,
    ) -> Tuple[Tuple[str, Tuple[Tuple[str, float], ...]], ...]:
        """Return a stable key for cache-safe stochastic setup reuse.

        Account objects currently expose IDs containing ``id(self)``. Those
        process-memory suffixes are intentionally excluded: the stochastic
        matrix depends on account kind, order, and allocation, while generated
        returns are rebound to each fresh model's live IDs before execution.
        """

        return tuple(
            (
                MonteCarloSimulator._stable_account_kind(account_id),
                tuple(
                    sorted(
                        (
                            str(asset_class),
                            float(weight),
                        )
                        for asset_class, weight in coerce_asset_allocation(
                            allocation
                        ).items()
                    )
                ),
            )
            for account_id, allocation in accounts_with_alloc
        )

    @staticmethod
    def _stable_account_kind(account_id) -> str:
        """Remove the volatile numeric object-id suffix from an account ID."""

        text = str(account_id)
        prefix, separator, suffix = text.rpartition("_")
        if separator and prefix and suffix.isdigit():
            return prefix
        return text
    
    def _build_registry(self, model: 'LifeModel') -> InvestmentAccountRegistry:
        """Build registry of investment accounts from model.
        
        Args:
            model: LifeModel instance to scan for investment accounts
        
        Returns:
            Registry containing all accounts with asset allocations
        """
        registry = InvestmentAccountRegistry()
        
        for agent in model.agents:
            # Check if agent is an investment account with stochastic support
            if hasattr(agent, 'asset_allocation') and hasattr(agent, 'account_id'):
                registry.register(agent)
        
        return registry
    
    def run_single(self, model_factory: Callable[[], 'LifeModel']) -> 'LifeModel':
        """Run a single probabilistic simulation and return the model.
        
        Useful for debugging or detailed analysis of a single run.
        
        Args:
            model_factory: Callable that creates a fresh LifeModel instance
        
        Returns:
            The LifeModel after running the simulation
        """
        if self.config.random_seed is not None:
            np.random.seed(self.config.random_seed)
        
        model = model_factory()
        
        registry = self._build_registry(model)
        accounts_with_alloc = registry.get_accounts_with_allocations()
        
        if accounts_with_alloc:
            if isinstance(self.market, YearVaryingMarketAssumptions):
                return_gen = YearVaryingAccountReturnGenerator(
                    accounts_with_alloc, self.market
                )
            else:
                corr_matrix, account_order, params = \
                    self.param_calculator.calculate_account_correlation_matrix(
                        accounts_with_alloc
                    )

                return_gen = AccountCorrelatedReturnGenerator(
                    params, corr_matrix, account_order
                )

            model.set_simulation_mode('probabilistic', return_gen, registry)

        model.run()
        return model
