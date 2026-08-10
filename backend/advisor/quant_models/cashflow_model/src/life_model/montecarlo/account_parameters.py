# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

"""
Account parameters calculator for deriving account-level stochastic parameters.

This module calculates expected return, volatility, and correlation for investment
accounts based on their asset allocations and market assumptions.
"""

from dataclasses import dataclass
from typing import List, Tuple
import numpy as np

from ..account.asset_allocation import AssetAllocationInput, coerce_asset_allocation
from .market_assumptions import MarketAssumptions


@dataclass
class AccountStochasticParams:
    """Derived stochastic parameters for an investment account.
    
    Attributes:
        account_id: Unique identifier for the account
        expected_return: Expected annual return derived from asset allocation
        volatility: Annual volatility derived from asset allocation
    """
    account_id: str
    expected_return: float
    volatility: float


class AccountParametersCalculator:
    """Derives account-level return, volatility, and correlation from asset allocations.
    
    Given a client's asset allocation for each investment account and the internal
    team's market assumptions, this calculator computes:
    - Expected return for each account
    - Volatility for each account
    - Correlation matrix between all accounts
    
    Example:
        >>> market = MarketAssumptions.create_default()
        >>> calc = AccountParametersCalculator(market)
        >>> params = calc.calculate_account_params(
        ...     "account_1",
        ...     {"US Equity": 0.6, "US Treasury": 0.4}
        ... )
        >>> print(f"Expected return: {params.expected_return:.2%}")
        Expected return: 7.60%
    """
    
    def __init__(self, market_assumptions: MarketAssumptions):
        """Initialize with market assumptions.
        
        Args:
            market_assumptions: Internal team's market assumptions for asset classes
        """
        self.market = market_assumptions
    
    def calculate_account_params(self, 
                                  account_id: str,
                                  asset_allocation: AssetAllocationInput) -> AccountStochasticParams:
        """Calculate expected return and volatility for a single account.
        
        Args:
            account_id: Unique identifier for the account
            asset_allocation: Allocation input mapping asset class name to
                weight, or an equivalent AssetAllocation/entry list.
        
        Returns:
            AccountStochasticParams with derived expected return and volatility
        
        Raises:
            ValueError: If allocation weights do not sum to 1.0 or use an
                unsupported asset class name.
        """
        weights = self._allocation_to_weights(asset_allocation)
        
        # E[R] = w^T * mu
        expected_return = float(weights @ self.market.get_returns_vector())
        
        # sigma = sqrt(w^T * Sigma * w)
        variance = float(weights @ self.market.covariance_matrix @ weights)
        volatility = np.sqrt(max(variance, 0))  # Guard against numerical issues
        
        return AccountStochasticParams(account_id, expected_return, volatility)
    
    def calculate_account_correlation_matrix(
            self, 
            accounts: List[Tuple[str, AssetAllocationInput]]
    ) -> Tuple[np.ndarray, List[str], List[AccountStochasticParams]]:
        """Calculate correlation matrix between multiple accounts.
        
        Given a list of accounts with their asset allocations, this method derives
        the correlation between each pair of accounts based on their overlapping
        asset class exposures.
        
        Args:
            accounts: List of (account_id, asset_allocation) tuples
        
        Returns:
            Tuple of:
            - correlation_matrix: MxM numpy array of account correlations
            - account_order: List of account IDs in matrix order
            - account_params_list: List of AccountStochasticParams for each account
        
        Raises:
            ValueError: If any account has zero volatility (can't compute correlation)
        """
        n = len(accounts)
        if n == 0:
            return np.array([[]]), [], []
        
        account_ids = [acc[0] for acc in accounts]
        
        # Calculate params and weights for each account
        params_list = []
        weights_list = []
        for account_id, allocation in accounts:
            params = self.calculate_account_params(account_id, allocation)
            params_list.append(params)
            weights_list.append(self._allocation_to_weights(allocation))
        
        # Build correlation matrix between accounts
        corr_matrix = np.zeros((n, n))
        for i in range(n):
            for j in range(n):
                if i == j:
                    corr_matrix[i, j] = 1.0
                else:
                    # Cov(R_i, R_j) = w_i^T * Sigma * w_j
                    cov_ij = float(
                        weights_list[i] @ self.market.covariance_matrix @ weights_list[j]
                    )
                    
                    # rho_ij = Cov / (sigma_i * sigma_j)
                    sigma_i = params_list[i].volatility
                    sigma_j = params_list[j].volatility
                    
                    if sigma_i > 0 and sigma_j > 0:
                        corr_matrix[i, j] = cov_ij / (sigma_i * sigma_j)
                    else:
                        # If either account has zero volatility, correlation is undefined
                        # Use 0 as a safe default
                        corr_matrix[i, j] = 0.0
        
        # Ensure matrix is positive semi-definite (for Cholesky decomposition)
        corr_matrix = self._ensure_positive_definite(corr_matrix)
        
        return corr_matrix, account_ids, params_list
    
    def _allocation_to_weights(self, allocation: AssetAllocationInput) -> np.ndarray:
        """Convert allocation dict to weight vector in asset_class_order.
        
        Args:
            allocation: Allocation input accepted by coerce_asset_allocation.
        
        Returns:
            numpy array of weights in the same order as market.asset_class_order
        """
        allocation = coerce_asset_allocation(allocation)
        missing = [
            asset_class
            for asset_class, weight in allocation.items()
            if abs(weight) > 1e-12 and asset_class not in self.market.asset_classes
        ]
        if missing:
            missing_list = ', '.join(sorted(missing))
            raise ValueError(f"Asset classes missing from market assumptions: {missing_list}")

        weights = np.zeros(len(self.market.asset_class_order))
        for i, asset_class in enumerate(self.market.asset_class_order):
            weights[i] = allocation.get(asset_class, 0.0)
        return weights
    
    @staticmethod
    def _ensure_positive_definite(matrix: np.ndarray, epsilon: float = 1e-8) -> np.ndarray:
        """Ensure correlation matrix is positive definite for Cholesky decomposition.
        
        Due to numerical precision issues, computed correlation matrices may not be
        exactly positive definite. This method adjusts eigenvalues if needed.
        
        Args:
            matrix: Correlation matrix to adjust
            epsilon: Minimum eigenvalue threshold
        
        Returns:
            Adjusted positive definite matrix
        """
        # Check if already positive definite
        try:
            np.linalg.cholesky(matrix)
            return matrix
        except np.linalg.LinAlgError:
            pass
        
        # Eigenvalue adjustment
        eigenvalues, eigenvectors = np.linalg.eigh(matrix)
        eigenvalues = np.maximum(eigenvalues, epsilon)
        adjusted = eigenvectors @ np.diag(eigenvalues) @ eigenvectors.T
        
        # Ensure diagonal is exactly 1.0 (correlation matrix property)
        d = np.sqrt(np.diag(adjusted))
        adjusted = adjusted / np.outer(d, d)
        
        return adjusted
