from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy.optimize import minimize

if __package__ is None or __package__ == "":
    _parent = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(_parent))
    __package__ = "layers.L3"

from layers.layer_types import ManagerSelectionConfig, ManagerSelectionResult


def black_litterman_posterior(
    equilibrium_weights: np.ndarray,
    equilibrium_alpha: np.ndarray,
    views_alpha: np.ndarray,
    confidence: np.ndarray,
    covariance_matrix: np.ndarray,
    tau: float = 0.05,
) -> np.ndarray:
    """Calculate Black-Litterman posterior expected alphas."""
    n = len(equilibrium_weights)
    p_matrix = np.eye(n)

    variances = np.diag(covariance_matrix)
    omega = np.diag((1.0 / (confidence**2)) * variances)
    prior_cov = tau * covariance_matrix
    a_matrix = p_matrix @ prior_cov @ p_matrix.T + omega

    try:
        a_inv = np.linalg.inv(a_matrix)
    except np.linalg.LinAlgError:
        a_inv = np.linalg.pinv(a_matrix)

    adjustment = prior_cov @ p_matrix.T @ a_inv @ (views_alpha - p_matrix @ equilibrium_alpha)
    return equilibrium_alpha + adjustment


def optimize_manager_weights_bl(
    expected_alphas: np.ndarray,
    covariance_matrix: np.ndarray,
    risk_aversion: float = 2.5,
    min_weight: float = 0.0,
    max_weight: float = 1.0,
    target_te: Optional[float] = None,
    te_penalty_weight: float = 10.0,
) -> Tuple[np.ndarray, Dict]:
    """
    Optimize manager weights using mean-variance optimization with optional TE target.
    
    Args:
        expected_alphas: Expected alpha for each manager
        covariance_matrix: Covariance matrix of active returns
        risk_aversion: Risk aversion parameter
        min_weight: Minimum weight per manager
        max_weight: Maximum weight per manager
        target_te: Optional target tracking error (soft constraint)
        te_penalty_weight: Weight for TE deviation penalty
    """
    n = len(expected_alphas)

    def objective(w):
        portfolio_alpha = w @ expected_alphas
        portfolio_variance = w @ covariance_matrix @ w
        portfolio_te = float(np.sqrt(portfolio_variance))
        
        # Base utility
        utility = portfolio_alpha - (risk_aversion / 2.0) * portfolio_variance
        
        # Add TE target penalty if specified
        if target_te is not None and target_te > 0:
            te_deviation = (portfolio_te - target_te) ** 2
            utility -= te_penalty_weight * te_deviation
        
        return -utility

    constraints = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}]
    bounds = [(min_weight, max_weight) for _ in range(n)]
    w0 = np.ones(n) / n

    result = minimize(
        objective,
        w0,
        method="SLSQP",
        bounds=bounds,
        constraints=constraints,
        options={"ftol": 1e-9, "maxiter": 1000},
    )

    if not result.success:
        return w0, {"success": False, "portfolio_alpha": 0.0, "portfolio_te": 0.0, "target_te": target_te}

    optimal_weights = result.x
    portfolio_alpha = optimal_weights @ expected_alphas
    portfolio_variance = optimal_weights @ covariance_matrix @ optimal_weights
    portfolio_te = float(np.sqrt(portfolio_variance))

    info = {
        "success": result.success,
        "portfolio_alpha": portfolio_alpha,
        "portfolio_te": portfolio_te,
        "target_te": target_te,
        "te_deviation": abs(portfolio_te - target_te) if target_te else 0.0,
    }
    return optimal_weights, info


class ManagerSelectionEngine:
    """Layer 3 engine to allocate among managers within each asset class."""

    def __init__(self, config: ManagerSelectionConfig):
        self.config = config

    def load_manager_data(self) -> pd.DataFrame:
        """Load manager data from the Manager Active Exp Conviction sheet."""
        return pd.read_excel(self.config.manager_selection_file, sheet_name=self.config.sheet_name)

    def load_correlation_matrix(self) -> Optional[pd.DataFrame]:
        """Load user-provided manager active return correlation matrix from Excel."""
        try:
            corr_df = pd.read_excel(
                self.config.manager_selection_file,
                sheet_name=self.config.correlation_sheet_name,
                header=None,
            )
            
            # First row contains ISIN headers (skip first cell which is empty)
            isins = corr_df.iloc[0, 1:].tolist()
            
            # Data is in rows 1 onwards, column 0 is ISIN, columns 1+ are correlation values
            data = corr_df.iloc[1:, 1:].values.astype(float)
            
            # Create properly indexed DataFrame
            correlation_matrix = pd.DataFrame(data, index=isins, columns=isins)
            print(f"   ✓ Loaded user-provided manager correlation matrix ({len(isins)}x{len(isins)})")
            
            return correlation_matrix
            
        except Exception as e:
            print(f"   ⚠ Could not load manager correlation matrix from '{self.config.correlation_sheet_name}': {e}")
            print("   Using default correlation of 0.5 for all manager pairs")
            return None

    def run(
        self,
        target_tes: Optional[Dict[str, float]] = None,
    ) -> ManagerSelectionResult:
        """
        Run manager selection with optional target TEs from Layer 2.
        
        Args:
            target_tes: Optional target tracking errors per asset class from Layer 2
        """
        manager_data = self.load_manager_data()
        
        # Load user-provided correlation matrix
        user_correlation_matrix = self.load_correlation_matrix()

        allocations: Dict[str, Dict[str, float]] = {}
        covariance_by_asset: Dict[str, pd.DataFrame] = {}

        for asset_class in sorted(manager_data["AssetClass"].dropna().unique()):
            target_te = target_tes.get(asset_class) if target_tes else None
            alloc, cov = self._allocate_asset_class(
                manager_data,
                asset_class,
                target_te=target_te,
                user_correlation_matrix=user_correlation_matrix,
            )
            if alloc is not None:
                allocations[asset_class] = alloc
            if cov is not None:
                covariance_by_asset[asset_class] = cov

        active_vols, active_tes = self._compute_blended_metrics(manager_data, allocations)

        return ManagerSelectionResult(
            manager_data=manager_data,
            allocations=allocations,
            active_vols=active_vols,
            active_tes=active_tes,
            covariance_by_asset=covariance_by_asset,
        )

    def _allocate_asset_class(
        self,
        manager_data: pd.DataFrame,
        asset_class: str,
        target_te: Optional[float] = None,
        user_correlation_matrix: Optional[pd.DataFrame] = None,
    ) -> Tuple[Optional[Dict[str, float]], Optional[pd.DataFrame]]:
        ac_managers = manager_data[manager_data["AssetClass"] == asset_class].copy()
        if ac_managers.empty:
            return None, None

        manager_isins = ac_managers["ISIN"].tolist()

        if len(ac_managers) == 1:
            isin = ac_managers.iloc[0]["ISIN"]
            covariance = pd.DataFrame([[ac_managers.iloc[0]["Expected Tracking Error"] ** 2]], index=[isin], columns=[isin])
            return {isin: 1.0}, covariance

        # Use all managers for optimization (no need for historical data filtering if user correlation is provided)
        n_managers = len(manager_isins)
        ac_managers_indexed = ac_managers.set_index("ISIN")

        # Determine correlation matrix to use
        if user_correlation_matrix is not None:
            # Use user-provided correlation matrix
            # Extract correlations for managers in this asset class
            available_isins = [isin for isin in manager_isins if isin in user_correlation_matrix.index]
            if len(available_isins) == len(manager_isins):
                corr_matrix = user_correlation_matrix.loc[manager_isins, manager_isins]
            else:
                # Some managers not in user correlation matrix, use default 0.5 for missing
                missing_isins = [isin for isin in manager_isins if isin not in user_correlation_matrix.index]
                print(f"    ⚠ {asset_class}: Managers {missing_isins} not in correlation matrix, using default 0.5")
                corr_matrix = self._build_default_correlation_matrix(manager_isins, user_correlation_matrix)
        else:
            # No user-provided correlation matrix, use default 0.5 for all pairs
            print(f"    ⚠ {asset_class}: Using default correlation of 0.5 for all manager pairs")
            corr_matrix = pd.DataFrame(
                0.5 * np.ones((n_managers, n_managers)) + 0.5 * np.eye(n_managers),
                index=manager_isins,
                columns=manager_isins,
            )

        equilibrium_weights = np.ones(n_managers) / n_managers
        equilibrium_alpha = np.zeros(n_managers)
        views_alpha = (
            ac_managers_indexed.loc[manager_isins, "Expected Information Ratio"].values
            * ac_managers_indexed.loc[manager_isins, "Expected Tracking Error"].values
        )
        confidence = ac_managers_indexed.loc[manager_isins, "Confidence Level"].values

        corr_values = corr_matrix.loc[manager_isins, manager_isins].values
        te_vector = ac_managers_indexed.loc[manager_isins, "Expected Tracking Error"].values
        covariance_matrix = corr_values * np.outer(te_vector, te_vector)

        posterior_alpha = black_litterman_posterior(
            equilibrium_weights=equilibrium_weights,
            equilibrium_alpha=equilibrium_alpha,
            views_alpha=views_alpha,
            confidence=confidence,
            covariance_matrix=covariance_matrix,
            tau=self.config.tau,
        )

        optimal_weights, opt_info = optimize_manager_weights_bl(
            expected_alphas=posterior_alpha,
            covariance_matrix=covariance_matrix,
            risk_aversion=self.config.risk_aversion,
            min_weight=0.0,
            max_weight=1.0,
            target_te=target_te,
            te_penalty_weight=10.0,
        )
        
        # Log TE alignment if target was provided
        if target_te is not None and opt_info.get("success"):
            achieved_te = opt_info.get("portfolio_te", 0.0)
            deviation = opt_info.get("te_deviation", 0.0)
            print(f"      {asset_class}: Target TE {target_te*100:.2f}%, Achieved {achieved_te*100:.2f}%, Deviation {deviation*100:.2f}%")

        allocation = {
            isin: weight for isin, weight in zip(manager_isins, optimal_weights)
        }

        covariance_df = pd.DataFrame(
            covariance_matrix,
            index=manager_isins,
            columns=manager_isins,
        )
        return allocation, covariance_df

    @staticmethod
    def _build_default_correlation_matrix(
        manager_isins: List[str],
        user_correlation_matrix: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        """
        Build correlation matrix using user-provided values where available,
        defaulting to 0.5 for missing pairs.
        """
        n = len(manager_isins)
        
        # Start with default correlation of 0.5, diagonal = 1.0
        corr_matrix = pd.DataFrame(
            0.5 * np.ones((n, n)) + 0.5 * np.eye(n),
            index=manager_isins,
            columns=manager_isins,
        )
        
        # Fill in values from user-provided matrix where available
        if user_correlation_matrix is not None:
            for isin_i in manager_isins:
                for isin_j in manager_isins:
                    if isin_i in user_correlation_matrix.index and isin_j in user_correlation_matrix.columns:
                        corr_matrix.loc[isin_i, isin_j] = user_correlation_matrix.loc[isin_i, isin_j]
        
        return corr_matrix

    @staticmethod
    def _compute_blended_metrics(
        manager_data: pd.DataFrame,
        allocations: Dict[str, Dict[str, float]],
    ) -> Tuple[Dict[str, float], Dict[str, float]]:
        active_vols: Dict[str, float] = {}
        active_tes: Dict[str, float] = {}

        for asset_class, alloc in allocations.items():
            ac_managers = manager_data[manager_data["AssetClass"] == asset_class]
            total_weight = 0.0
            total_te = 0.0

            for _, mgr in ac_managers.iterrows():
                isin = mgr["ISIN"]
                weight = alloc.get(isin, 0.0)
                if weight > 0:
                    total_weight += weight
                    total_te += weight * mgr["Expected Tracking Error"]

            if total_weight > 0:
                active_tes[asset_class] = total_te / total_weight
                # Use TE as a proxy for active volatility since vol_1y may not be available
                active_vols[asset_class] = active_tes[asset_class]
            else:
                active_vols[asset_class] = 0.0
                active_tes[asset_class] = 0.0

        return active_vols, active_tes

