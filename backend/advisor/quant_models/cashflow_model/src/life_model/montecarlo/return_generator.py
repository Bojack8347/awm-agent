# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

"""
Correlated return generator for investment accounts.

This module generates correlated random returns at the investment account level
using Cholesky decomposition. The correlation between accounts is derived from
their asset allocations.
"""

from typing import Dict, Iterable, List, Optional, Tuple
import numpy as np

from .account_parameters import AccountStochasticParams
from .market_assumptions import YearVaryingMarketAssumptions


class AccountCorrelatedReturnGenerator:
    """Generates correlated returns at the investment account level.
    
    Uses Cholesky decomposition to generate correlated normal random variables,
    then transforms them to account returns using each account's expected return
    and volatility.
    
    Example:
        >>> params = [
        ...     AccountStochasticParams("acc1", 0.08, 0.15),
        ...     AccountStochasticParams("acc2", 0.06, 0.10),
        ... ]
        >>> corr = np.array([[1.0, 0.7], [0.7, 1.0]])
        >>> gen = AccountCorrelatedReturnGenerator(params, corr, ["acc1", "acc2"])
        >>> returns = gen.generate_yearly_returns()
        >>> print(returns)  # e.g., {'acc1': 0.12, 'acc2': 0.04}
    """
    
    def __init__(self, 
                 account_params: List[AccountStochasticParams],
                 account_correlation_matrix: np.ndarray,
                 account_order: List[str]):
        """Initialize the return generator.
        
        Args:
            account_params: List of stochastic parameters for each account
            account_correlation_matrix: MxM correlation matrix between accounts
            account_order: List of account IDs in the same order as the matrix
        
        Raises:
            ValueError: If matrix is not positive definite
        """
        self.account_params = {p.account_id: p for p in account_params}
        self.account_order = account_order
        self.correlation_matrix = account_correlation_matrix
        self._expected_returns = np.array(
            [self.account_params[account_id].expected_return for account_id in account_order],
            dtype=float,
        )
        self._volatilities = np.array(
            [self.account_params[account_id].volatility for account_id in account_order],
            dtype=float,
        )
        
        # Cholesky decomposition for correlated sampling
        # L such that L @ L^T = correlation_matrix
        try:
            self._cholesky = np.linalg.cholesky(account_correlation_matrix)
        except np.linalg.LinAlgError as e:
            raise ValueError(
                "Correlation matrix is not positive definite. "
                "This may occur with certain allocation combinations."
            ) from e
    
    def generate_yearly_returns(self, year: Optional[int] = None) -> Dict[str, float]:
        """Generate one year of correlated returns for all accounts.

        Args:
            year: Simulation year (accepted for interface compatibility with
                year-varying generators; constant assumptions ignore it).

        Returns:
            Dict mapping account_id to annual return for this simulation year.
            Returns are in decimal form (e.g., 0.08 for 8% return).
        """
        n = len(self.account_order)
        if n == 0:
            return {}
        
        # Generate uncorrelated standard normal samples
        uncorrelated_z = np.random.standard_normal(n)
        
        # Transform to correlated samples using Cholesky: z_corr = L @ z_uncorr
        correlated_z = self._cholesky @ uncorrelated_z
        
        # Transform to account returns: R_i = mu_i + sigma_i * z_i
        returns = {}
        for i, account_id in enumerate(self.account_order):
            params = self.account_params[account_id]
            returns[account_id] = params.expected_return + params.volatility * correlated_z[i]
        
        return returns

    def transform_standard_normals(self, values: np.ndarray) -> np.ndarray:
        """Transform standard-normal draws whose final axis is account order."""

        standard_normals = np.asarray(values, dtype=float)
        if standard_normals.shape[-1:] != (len(self.account_order),):
            raise ValueError(
                "Standard-normal draw shape must end with the account dimension"
            )
        if standard_normals.ndim == 1:
            correlated = self._cholesky @ standard_normals
            return np.array(
                [
                    self.account_params[account_id].expected_return
                    + self.account_params[account_id].volatility * correlated[index]
                    for index, account_id in enumerate(self.account_order)
                ],
                dtype=float,
            )
        correlated = np.matmul(standard_normals, self._cholesky.T)
        return self._expected_returns + correlated * self._volatilities

    def generate_return_tensor(
        self,
        *,
        num_paths: int,
        years: Iterable[int],
        rng: np.random.RandomState,
    ) -> np.ndarray:
        """Generate all path/year/account returns with one vectorized RNG call."""

        year_list = list(years)
        standard_normals = rng.standard_normal(
            (int(num_paths), len(year_list), len(self.account_order))
        )
        # Preserve the exact arithmetic order of the historical seeded path:
        # one matrix-vector transform per simulated year. The RNG draw itself
        # is batched, avoiding thousands of Python-to-NumPy calls.
        output = np.empty_like(standard_normals, dtype=float)
        for path_index in range(int(num_paths)):
            for year_index in range(len(year_list)):
                output[path_index, year_index, :] = self.transform_standard_normals(
                    standard_normals[path_index, year_index, :]
                )
        return output
    
    def generate_multi_year_returns(self, num_years: int) -> List[Dict[str, float]]:
        """Generate multiple years of correlated returns.
        
        Args:
            num_years: Number of years to generate returns for
        
        Returns:
            List of yearly return dictionaries
        """
        return [self.generate_yearly_returns() for _ in range(num_years)]


class YearVaryingAccountReturnGenerator:
    """Correlated account returns under year-varying market assumptions.

    For every year that carries an override in the schedule, account-level
    expected returns, volatilities, and the account correlation matrix are
    recomputed from that year's assumptions; all other years reuse the base
    parameters. Generators are built lazily and cached per override year.
    """

    def __init__(self,
                 accounts_with_allocations: List[Tuple[str, Dict[str, float]]],
                 market_schedule: YearVaryingMarketAssumptions):
        """Initialize the generator.

        Args:
            accounts_with_allocations: List of (account_id, asset_allocation)
                tuples, as produced by the investment account registry.
            market_schedule: Term structure of market assumptions.
        """
        self.accounts = list(accounts_with_allocations)
        self.schedule = market_schedule
        self._generators: Dict[Optional[int], AccountCorrelatedReturnGenerator] = {}

    def _generator_for(self, year: Optional[int]) -> AccountCorrelatedReturnGenerator:
        has_override = year is not None and int(year) in self.schedule.yearly_overrides
        key = int(year) if has_override else None
        if key not in self._generators:
            from .account_parameters import AccountParametersCalculator
            market = self.schedule.base if key is None else self.schedule.for_year(key)
            calculator = AccountParametersCalculator(market)
            corr_matrix, account_order, params = (
                calculator.calculate_account_correlation_matrix(self.accounts))
            self._generators[key] = AccountCorrelatedReturnGenerator(
                params, corr_matrix, account_order)
        return self._generators[key]

    def generate_yearly_returns(self, year: Optional[int] = None) -> Dict[str, float]:
        """Generate one year of correlated returns using that year's assumptions."""
        return self._generator_for(year).generate_yearly_returns()

    def generate_return_tensor(
        self,
        *,
        num_paths: int,
        years: Iterable[int],
        rng: np.random.RandomState,
    ) -> Tuple[np.ndarray, List[str]]:
        """Generate path/year returns while retaining year-specific assumptions."""

        year_list = [int(year) for year in years]
        base_generator = self._generator_for(None)
        account_order = list(base_generator.account_order)
        standard_normals = rng.standard_normal(
            (int(num_paths), len(year_list), len(account_order))
        )
        output = np.empty_like(standard_normals, dtype=float)
        generators = []
        for year in year_list:
            generator = self._generator_for(year)
            if list(generator.account_order) != account_order:
                raise ValueError(
                    "Year-varying market assumptions changed account order"
                )
            generators.append(generator)
        for path_index in range(int(num_paths)):
            for year_index, generator in enumerate(generators):
                output[path_index, year_index, :] = (
                    generator.transform_standard_normals(
                        standard_normals[path_index, year_index, :]
                    )
                )
        return output, account_order


class PrecomputedAccountReturnGenerator:
    """Serve one path of a pre-generated year-by-account return tensor."""

    def __init__(self, account_order: Iterable[str], path_returns: np.ndarray):
        self.account_order = [str(account_id) for account_id in account_order]
        self.path_returns = np.asarray(path_returns, dtype=float)
        if self.path_returns.ndim != 2:
            raise ValueError("Precomputed path returns must be a 2-D array")
        if self.path_returns.shape[1] != len(self.account_order):
            raise ValueError(
                "Precomputed return account dimension does not match account order"
            )
        self._year_index = 0

    def generate_yearly_returns(self, year: Optional[int] = None) -> Dict[str, float]:
        if self._year_index >= len(self.path_returns):
            raise IndexError("Precomputed Monte Carlo return path is exhausted")
        values = self.path_returns[self._year_index]
        self._year_index += 1
        return {
            account_id: float(values[index])
            for index, account_id in enumerate(self.account_order)
        }
