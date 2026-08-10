from __future__ import annotations

import sys
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "layers.L2"

import argparse
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from layers.layer_types import (
    Layer1Config,
    Layer1Result,
    Layer2Config,
    ManagerSelectionConfig,
    ManagerSelectionResult,
)
from layers.L1.layer1_saa import run_layer1
from layers.L3.layer3_manager_selection import ManagerSelectionEngine
from layers.reporting import export_portfolio_results

ROOT = Path(__file__).resolve().parents[2]


class ActiveRiskAllocator:
    """Layer 2 engine using Black-Litterman for asset-class active risk allocation."""

    def __init__(
        self,
        config: Layer2Config,
        *,
        active_risk_percentage: float | None = None,
    ):
        self.config = config
        if active_risk_percentage is not None and not 0.0 <= active_risk_percentage <= 1.0:
            raise ValueError("active_risk_percentage must be between 0 and 1")
        self.active_risk_percentage = active_risk_percentage

    def run(
        self,
        layer1: Layer1Result,
    ) -> Tuple[Dict[str, float], Dict[str, float], float, Dict[str, float], Dict]:
        """
        Run Layer 2: Black-Litterman asset class active risk budget allocation.
        Returns Layer2Result and target active risks for Layer 3.
        """
        asset_alloc_df = self._load_asset_allocation()
        conviction_df = self._load_conviction()
        
        # Load risk budget
        risk_budget_df = self._load_risk_budget()
        passive_risk_pct, active_risk_pct, active_risk_source = (
            self._resolve_risk_budget_percentages(risk_budget_df)
        )
        layer1_active_risk_budget, layer1_active_risk_source = self._get_layer1_active_risk_budget(layer1)
        layer1_active_risk_realized = self._get_layer1_active_risk_realized(layer1)
        passive_risk_budget = passive_risk_pct * layer1_active_risk_budget
        active_risk_budget = active_risk_pct * layer1_active_risk_budget

        # Use ALL asset classes from Layer 1 (not just active ones)
        asset_classes = layer1.selected_weights.index.tolist()
        
        # Identify which asset classes can have active exposure (TE > 0)
        conviction_df_indexed = conviction_df.set_index("Asset Class")
        active_mask = conviction_df_indexed["Expected Tracking Error"] > 0
        active_asset_classes = conviction_df_indexed[active_mask].index.tolist()
        
        print(f"   • Total asset classes: {len(asset_classes)}")
        print(f"   • Active-eligible asset classes: {len(active_asset_classes)}")
        print(f"   • Passive-only asset classes: {len(asset_classes) - len(active_asset_classes)}")

        # Build mappings for ALL asset classes - use Layer 1 dynamic weights (DO NOT renormalize)
        saa_weights = layer1.selected_weights.to_dict()

        passive_tickers = asset_alloc_df.set_index("Asset Class")["Ticker"].to_dict()
        
        # Get passive vols for ALL asset classes from Passive Vehicle Selection file
        passive_vols = self._compute_passive_vols(asset_classes, asset_alloc_df)
        
        # Build TE, IR, confidence for ALL asset classes
        # For passive-only asset classes, set values to 0
        expected_tes = {}
        expected_irs = {}
        confidence_levels = {}
        
        for ac in asset_classes:
            if ac in conviction_df_indexed.index:
                row = conviction_df_indexed.loc[ac]
                # TE, IR, Confidence from the Active TE IR CL sheet
                expected_tes[ac] = row.get("Expected Tracking Error", 0.0)
                expected_irs[ac] = row.get("Expected Information Ratio", 0.0)
                confidence_levels[ac] = row.get("Confidence Level", 0.0)
            else:
                # Asset class not in conviction file - set to passive-only
                expected_tes[ac] = 0.0
                expected_irs[ac] = 0.0
                confidence_levels[ac] = 0.0

        asset_to_index = asset_alloc_df.set_index("Asset Class")["Index"].to_dict()

        # Load user-provided active return correlation matrix from Excel
        full_corr_matrix = self._load_active_return_correlation()
        
        if full_corr_matrix is not None:
            # Use user-provided correlation matrix
            print("   ✓ Using user-provided active return correlation matrix from Excel")
            
            # Extract correlations for ALL asset classes
            correlations = full_corr_matrix.reindex(
                index=asset_classes, columns=asset_classes, fill_value=0.0
            )
            # Set diagonal to 1.0
            for ac in asset_classes:
                correlations.loc[ac, ac] = 1.0
        else:
            # Fallback: Use identity matrix (no correlation assumed)
            print("   ⚠ Correlation matrix not found, using identity matrix (zero correlation)")
            n = len(asset_classes)
            correlations = pd.DataFrame(np.eye(n), index=asset_classes, columns=asset_classes)

        # Apply Black-Litterman at asset class level
        posterior_alphas, covariance_matrix = self._black_litterman_asset_allocation(
            asset_classes,
            saa_weights,
            expected_tes,
            expected_irs,
            confidence_levels,
            correlations,
        )

        # Optimize active risk budget allocation across asset classes
        active_alloc, achieved_vol, risk_budget_shares = self._optimize_active_risk_budget(
            asset_classes,
            active_asset_classes,
            saa_weights,
            posterior_alphas,
            covariance_matrix,
            expected_tes,
            active_risk_budget,
            passive_vols,
            correlations,
        )

        # Calculate budget-implied target TEs for Layer 3.
        # Layer 2 allocates a portfolio-level active-risk budget by asset class:
        #   assigned_active_risk = budget_share * active_risk_budget
        # Layer 3 optimizes the active sleeve TE. To align the handoff, convert
        # the assigned portfolio active risk back into a sleeve-level TE target:
        #   assigned_active_risk = alpha * SAA_weight * target_te
        # This keeps Layer 3 targets tied to Layer 1 weights and Layer 2 budget
        # shares rather than passing through raw expected TE inputs unchanged.
        target_active_risks = {}
        portfolio_active_risk_targets = {}
        portfolio_active_risk_capacity = {}
        for ac in asset_classes:
            alpha = active_alloc.get(ac, 0.0)
            te = expected_tes.get(ac, 0.0)
            saa_w = saa_weights.get(ac, 0.0)
            budget_share = risk_budget_shares.get(ac, 0.0)
            assigned_active_risk = budget_share * active_risk_budget

            if alpha > 0 and saa_w > 0:
                target_te = assigned_active_risk / (alpha * saa_w)
            else:
                target_te = 0.0

            target_active_risks[ac] = max(0.0, target_te)
            portfolio_active_risk_targets[ac] = assigned_active_risk
            portfolio_active_risk_capacity[ac] = alpha * saa_w * te

        return target_active_risks, active_alloc, achieved_vol, risk_budget_shares, {
            "active_risk_budget": active_risk_budget,
            "passive_risk_budget": passive_risk_budget,
            "layer1_active_risk_budget": layer1_active_risk_budget,
            "layer1_active_risk_realized": layer1_active_risk_realized,
            "layer1_active_risk_source": layer1_active_risk_source,
            "passive_risk_pct": passive_risk_pct,
            "active_risk_pct": active_risk_pct,
            "active_risk_source": active_risk_source,
            "expected_tes": expected_tes,
            "target_active_risks": target_active_risks,
            "portfolio_active_risk_targets": portfolio_active_risk_targets,
            "portfolio_active_risk_capacity": portfolio_active_risk_capacity,
            "risk_budget_shares": risk_budget_shares,
            "passive_tickers": passive_tickers,
            "passive_names": asset_to_index,
            "passive_vols": passive_vols,
        }

    def finalize(
        self,
        layer1: Layer1Result,
        active_alloc: Dict[str, float],
        achieved_vol: float,
        manager_selection: ManagerSelectionResult,
        passive_vehicles: Dict[str, Dict[str, str]],
        passive_vols: Dict[str, float],
        layer2_info: Dict | None = None,
    ) -> Path:
        """Export final results after Layer 3 completes."""
        output_path = Path(self.config.output_file).resolve()
        
        export_portfolio_results(
            output_path=output_path,
            layer1_result=layer1,
            active_allocations=active_alloc,
            manager_result=manager_selection,
            target_volatility=self.config.target_volatility,
            achieved_volatility=achieved_vol,
            passive_vehicles=passive_vehicles,
            passive_vols=passive_vols,
            layer2_info=layer2_info,
        )
        
        return output_path

    def _load_risk_budget(self) -> pd.DataFrame:
        """Load active/passive risk budget allocation."""
        risk_budget_file = ROOT / "Inputs" / "L2 Active Risk Allocation" / "Risk Budgeting.xlsx"
        return pd.read_excel(risk_budget_file)

    def _resolve_risk_budget_percentages(
        self,
        risk_budget_df: pd.DataFrame,
    ) -> Tuple[float, float, str]:
        """Prefer the signed API mandate over workbook implementation defaults."""
        if self.active_risk_percentage is not None:
            active = float(self.active_risk_percentage)
            return 1.0 - active, active, "signed_mandate_api"
        return (
            float(risk_budget_df.iloc[0]["Passive"]),
            float(risk_budget_df.iloc[0]["Active"]),
            "risk_budget_workbook",
        )

    @staticmethod
    def _get_layer1_active_risk_budget(layer1: Layer1Result) -> Tuple[float, str]:
        """Return the Layer 1 TE budget in absolute volatility units."""
        profile_results = layer1.raw_results.get(layer1.profile_name, {}) if layer1.raw_results else {}
        dynamic_data = profile_results.get("dynamic", {})
        diagnostics = dynamic_data.get("diagnostics", {})
        parameters = diagnostics.get("parameters", {})

        variance_budget = parameters.get("active_risk_budget_variance")
        if variance_budget is not None:
            variance_budget = float(variance_budget)
            if np.isfinite(variance_budget) and variance_budget >= 0:
                return float(np.sqrt(variance_budget)), "layer1_active_risk_budget_variance"

        budget_ratio = parameters.get("active_risk_budget_volatility")
        variance_target = parameters.get("variance_target_sigma_squared")
        if budget_ratio is not None and variance_target is not None:
            budget_ratio = float(budget_ratio)
            variance_target = float(variance_target)
            if np.isfinite(budget_ratio) and np.isfinite(variance_target) and variance_target >= 0:
                return float(budget_ratio * np.sqrt(variance_target)), "layer1_budget_ratio_times_variance_target"

        metrics = dynamic_data.get("metrics", {})
        realized_te = metrics.get("tracking_error")
        if realized_te is not None:
            realized_te = float(realized_te)
            if np.isfinite(realized_te):
                return realized_te, "layer1_realized_tracking_error_fallback"

        return float(layer1.target_vol), "target_volatility_fallback"

    @staticmethod
    def _get_layer1_active_risk_realized(layer1: Layer1Result) -> float:
        """Return the realized Layer 1 dynamic-vs-equilibrium tracking error."""
        profile_results = layer1.raw_results.get(layer1.profile_name, {}) if layer1.raw_results else {}
        dynamic_data = profile_results.get("dynamic", {})
        metrics = dynamic_data.get("metrics", {})
        realized_te = metrics.get("tracking_error")
        return float(realized_te) if realized_te is not None else 0.0
    
    def _load_conviction(self) -> pd.DataFrame:
        return pd.read_excel(
            self.config.active_exposure_file,
            sheet_name=self.config.conviction_sheet,
        )

    def _load_asset_allocation(self) -> pd.DataFrame:
        """Load passive vehicle selection data including index mapping, tickers, and volatility."""
        df = pd.read_excel(
            self.config.asset_allocation_file,
            sheet_name=self.config.asset_allocation_sheet,
        )
        # Handle SAA Weight column if present (for backward compatibility)
        if "SAA Weight (%)" in df.columns:
            df = df.rename(columns={"SAA Weight (%)": "SAA_Weight"})
            df["SAA_Weight"] = df["SAA_Weight"].astype(float) / 100.0
        
        # Ensure Volatility column exists and is properly formatted
        if "Volatility" in df.columns:
            df["Volatility"] = df["Volatility"].astype(float)
            print(f"   ✓ Loaded passive volatilities from Passive Vehicle Selection file")
        
        return df

    @staticmethod
    def _compute_passive_vols(
        asset_classes: List[str],
        asset_alloc_df: pd.DataFrame,
    ) -> Dict[str, float]:
        """Get passive volatilities from the Passive Vehicle Selection file."""
        vols: Dict[str, float] = {}
        
        # Create a mapping from asset class to volatility
        if "Volatility" in asset_alloc_df.columns:
            vol_mapping = asset_alloc_df.set_index("Asset Class")["Volatility"].to_dict()
            for ac in asset_classes:
                vols[ac] = vol_mapping.get(ac, 0.0)
        else:
            # Fallback if Volatility column doesn't exist
            print("   ⚠ Volatility column not found in Passive Vehicle Selection file, using zeros")
            for ac in asset_classes:
                vols[ac] = 0.0
        
        return vols

    def _load_active_return_correlation(self) -> pd.DataFrame:
        """Load user-provided active return correlation matrix from Excel Sheet."""
        try:
            # Read the correlation matrix from the configured sheet (default: "Active Return Correlation")
            corr_df = pd.read_excel(
                self.config.active_exposure_file,
                sheet_name=self.config.correlation_sheet,
            )
            
            # First column contains asset class names (row labels)
            if "Unnamed: 0" in corr_df.columns:
                corr_df = corr_df.rename(columns={"Unnamed: 0": "Asset Class"})
            
            # Set asset class as index
            corr_df = corr_df.set_index("Asset Class")
            
            # Ensure it's a square matrix with matching index and columns
            # The columns should already be asset class names
            return corr_df
            
        except Exception as e:
            print(f"Warning: Could not load correlation matrix from '{self.config.correlation_sheet}' sheet: {e}")
            print("Falling back to identity matrix (zero correlation assumption)")
            # Return identity matrix as fallback
            return None

    def _black_litterman_asset_allocation(
        self,
        asset_classes: List[str],
        saa_weights: Dict[str, float],
        expected_tes: Dict[str, float],
        expected_irs: Dict[str, float],
        confidence_levels: Dict[str, float],
        correlation_matrix: pd.DataFrame,
        tau: float = 0.05,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Apply Black-Litterman to asset class active allocation."""
        n = len(asset_classes)
        
        # Equilibrium: equal active risk allocation
        equilibrium_weights = np.ones(n) / n
        equilibrium_alpha = np.zeros(n)
        
        # Views: Expected alpha = IR × TE
        views_alpha = np.array([
            expected_irs.get(ac, 0.0) * expected_tes.get(ac, 0.0)
            for ac in asset_classes
        ])
        
        # Confidence levels
        confidence = np.array([confidence_levels.get(ac, 1.0) for ac in asset_classes])
        confidence = np.maximum(confidence, 0.1)
        
        # Covariance matrix: Corr × (TE ⊗ TE')
        te_vector = np.array([expected_tes.get(ac, 0.0) for ac in asset_classes])
        corr_values = correlation_matrix.loc[asset_classes, asset_classes].values
        covariance_matrix = corr_values * np.outer(te_vector, te_vector)
        
        # Black-Litterman posterior
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
        posterior_alpha = equilibrium_alpha + adjustment
        
        return posterior_alpha, covariance_matrix

    def _optimize_active_risk_budget(
        self,
        asset_classes: List[str],
        active_asset_classes: List[str],
        saa_weights: Dict[str, float],
        posterior_alphas: np.ndarray,
        covariance_matrix: np.ndarray,
        expected_tes: Dict[str, float],
        active_risk_budget: float,
        passive_vols: Dict[str, float],
        correlations: pd.DataFrame,
        risk_aversion: float = 2.5,
    ) -> Tuple[Dict[str, float], float, Dict[str, float]]:
        """
        Optimize allocation of active risk budget across ALL asset classes.
        Passive-only asset classes are constrained to alpha = 0.
        
        Args:
            asset_classes: ALL asset classes (15 total)
            active_asset_classes: Asset classes eligible for active exposure (4 total)
            
        Returns:
            active_alloc: Dict of active allocation (alpha) per asset class
            achieved_vol: Achieved portfolio volatility (includes ALL asset classes)
            risk_budget_shares: Dict of risk budget share per asset class
        """
        
        optimizer = _RiskBudgetOptimizer(
            asset_classes=asset_classes,
            active_asset_classes=active_asset_classes,
            saa_weights=saa_weights,
            posterior_alphas=posterior_alphas,
            covariance_matrix=covariance_matrix,
            expected_tes=expected_tes,
            active_risk_budget=active_risk_budget,
            passive_vols=passive_vols,
            correlations=correlations,
            risk_aversion=risk_aversion,
        )
        
        return optimizer.optimize()


class _RiskBudgetOptimizer:
    """Optimizer that allocates active risk budget across ALL asset classes using BL utility.
    
    Passive-only asset classes are constrained to have alpha = 0.
    """

    def __init__(
        self,
        asset_classes: List[str],
        active_asset_classes: List[str],
        saa_weights: Dict[str, float],
        posterior_alphas: np.ndarray,
        covariance_matrix: np.ndarray,
        expected_tes: Dict[str, float],
        active_risk_budget: float,
        passive_vols: Dict[str, float],
        correlations: pd.DataFrame,
        risk_aversion: float = 2.5,
    ):
        self.asset_classes = asset_classes  # ALL 15 asset classes
        self.active_asset_classes = active_asset_classes  # Only 4 active-eligible
        self.saa_weights = saa_weights
        self.posterior_alphas = posterior_alphas
        self.covariance_matrix = covariance_matrix
        self.expected_tes = expected_tes
        self.active_risk_budget = active_risk_budget
        self.passive_vols = passive_vols
        self.correlations = correlations
        self.risk_aversion = risk_aversion
        
        # Create index mapping for active asset classes
        self.active_indices = [i for i, ac in enumerate(self.asset_classes) if ac in self.active_asset_classes]
        self.passive_indices = [i for i, ac in enumerate(self.asset_classes) if ac not in self.active_asset_classes]

    def optimize(self) -> Tuple[Dict[str, float], float, Dict[str, float]]:
        """
        Optimize allocation of active risk budget shares across ALL asset classes.
        Passive-only asset classes are constrained to have risk_budget_share = 0.
        
        Returns:
            active_alloc: Dict of active allocation (alpha) per asset class
            achieved_vol: Achieved portfolio volatility
            risk_budget_shares: Dict of risk budget share per asset class
        """
        if not self.asset_classes:
            return {}, float("nan"), {}

        n = len(self.asset_classes)
        x0 = np.zeros(n)  # Start with zeros
        # Initialize only active asset classes with equal shares
        if len(self.active_indices) > 0:
            for idx in self.active_indices:
                x0[idx] = 1.0 / len(self.active_indices)
        
        bounds = [(0.0, 1.0) for _ in range(n)]

        def objective(weights):
            """Maximize BL utility: expected alpha - risk penalty."""
            utility = weights @ self.posterior_alphas - (self.risk_aversion / 2.0) * (
                weights @ self.covariance_matrix @ weights
            )
            return -utility

        def constraint_sum(weights):
            """Weights must sum to 1 (allocate 100% of risk budget)."""
            return np.sum(weights) - 1.0
        
        def constraint_passive_zero(weights):
            """Passive-only asset classes must have zero risk budget allocation."""
            # Return array of constraints: weights[i] = 0 for all passive indices
            return np.array([weights[i] for i in self.passive_indices])

        constraints = [
            {"type": "eq", "fun": constraint_sum},
        ]
        
        # Add equality constraint for each passive-only asset class
        for idx in self.passive_indices:
            constraints.append({
                "type": "eq",
                "fun": lambda w, i=idx: w[i]  # Force this weight to be 0
            })

        result = minimize(
            objective,
            x0,
            method="SLSQP",
            bounds=bounds,
            constraints=constraints,
            options={"ftol": 1e-9, "maxiter": 1000},
        )

        if not result.success:
            print(f"Warning: Risk budget optimization did not converge: {result.message}")
            optimal_weights = x0
        else:
            optimal_weights = result.x

        optimal_weights = np.array(optimal_weights, dtype=float)
        optimal_weights[np.abs(optimal_weights) < 1e-12] = 0.0
        for idx in self.passive_indices:
            optimal_weights[idx] = 0.0
        active_total = float(np.sum(optimal_weights[self.active_indices])) if self.active_indices else 0.0
        if active_total > 0:
            for idx in self.active_indices:
                optimal_weights[idx] = optimal_weights[idx] / active_total

        # Calculate active allocations from risk budget shares
        active_alloc = {}
        risk_budget_shares = {}
        
        for i, ac in enumerate(self.asset_classes):
            budget_share = float(optimal_weights[i])
            active_risk = budget_share * self.active_risk_budget
            saa_w = self.saa_weights.get(ac, 0.0)
            te = self.expected_tes.get(ac, 0.0)
            
            # Calculate active allocation: active_risk = alpha × TE × SAA_weight
            if saa_w > 0 and te > 0:
                alpha = active_risk / (te * saa_w)
                alpha = min(1.0, max(0.0, alpha))  # Cap between 0 and 100%
            else:
                alpha = 0.0
            
            active_alloc[ac] = alpha
            risk_budget_shares[ac] = budget_share

        # Calculate achieved portfolio volatility
        achieved_vol = self._calculate_portfolio_volatility(active_alloc)
        
        return active_alloc, achieved_vol, risk_budget_shares

    def _calculate_portfolio_volatility(self, active_alloc: Dict[str, float]) -> float:
        """Calculate total portfolio volatility with blended active/passive allocations for ALL asset classes."""
        total_var = 0.0
        
        # Iterate over ALL asset classes
        for ac_i in self.asset_classes:
            alpha_i = active_alloc.get(ac_i, 0.0)
            blended_vol_i = self._blended_volatility(ac_i, alpha_i)
            
            for ac_j in self.asset_classes:
                alpha_j = active_alloc.get(ac_j, 0.0)
                blended_vol_j = self._blended_volatility(ac_j, alpha_j)
                
                corr_ij = (
                    self.correlations.loc[ac_i, ac_j]
                    if ac_i in self.correlations.index and ac_j in self.correlations.columns
                    else 0.0
                )
                saa_i = self.saa_weights.get(ac_i, 0.0)
                saa_j = self.saa_weights.get(ac_j, 0.0)
                
                total_var += saa_i * saa_j * blended_vol_i * blended_vol_j * corr_ij
        
        return float(np.sqrt(total_var))

    def _blended_volatility(self, asset_class: str, alpha: float) -> float:
        """Calculate blended volatility for an asset class given active allocation."""
        passive_vol = self.passive_vols.get(asset_class, 0.0)
        te = self.expected_tes.get(asset_class, 0.0)
        active_vol = passive_vol + te  # Simplified: active vol ≈ passive vol + TE
        
        if active_vol <= 0:
            return passive_vol
        
        # Correlation between active and passive
        rho = np.sqrt(max(0.0, 1 - (te / active_vol) ** 2)) if active_vol > 0 else 1.0
        rho = max(0.0, min(1.0, rho))
        
        return float(
            np.sqrt(
                alpha**2 * active_vol**2
                + (1 - alpha) ** 2 * passive_vol**2
                + 2 * alpha * (1 - alpha) * active_vol * passive_vol * rho
            )
        )


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the layered optimization workflow."""
    parser = argparse.ArgumentParser(
        description="Layered multi-manager portfolio optimization pipeline with BL at Layer 2",
    )
    parser.add_argument(
        "--risk-profile",
        default="RP1",
        help="Risk profile to use from SAA results (e.g., RP1, RP2, ...).",
    )
    parser.add_argument(
        "--weight-type",
        choices=("dynamic", "equilibrium"),
        default="dynamic",
        help="Which SAA weights to feed into Layer 2.",
    )
    return parser.parse_args()


def build_layer1_config(args: argparse.Namespace) -> Layer1Config:
    """Build the configuration for the Strategic Asset Allocation layer."""
    data_file = ROOT / "Inputs" / "L1 Asset Allocation" / "CMA.xlsx"
    output_file = ROOT / "outputs" / "SAA_Results.xlsx"
    return Layer1Config(
        data_file=data_file,
        output_file=output_file,
        risk_profile=args.risk_profile,
        weight_type=args.weight_type,
        target_volatility=getattr(args, 'target_volatility', None),
        excluded_asset_classes=list(getattr(args, 'excluded_asset_classes', None) or []),
    )


def build_layer2_config(args: argparse.Namespace, layer1_target_vol: float) -> Layer2Config:
    """Build the configuration for the active risk allocation layer.
    
    Args:
        args: Command line arguments
        layer1_target_vol: Target volatility from Layer 1 SAA (used as Layer 2 target for consistency)
    """
    return Layer2Config(
        active_exposure_file=ROOT / "Inputs" / "L2 Active Risk Allocation" / "Active Exposure Conviction.xlsx",
        asset_allocation_file=ROOT / "Inputs" / "L2 Active Risk Allocation" / "Passive Vehicle Selection.xlsx",
        output_file=ROOT / "outputs" / "Portfolio_Construction_Results.xlsx",
        target_volatility=layer1_target_vol,  # Use Layer 1 target for consistency
    )


def build_layer3_config() -> ManagerSelectionConfig:
    """Build the configuration for the manager selection layer."""
    return ManagerSelectionConfig(
        manager_selection_file=ROOT / "Inputs" / "L3 Active Manager Selection" / "Active Manager Selection.xlsx",
    )


def run_layered_optimization(args: argparse.Namespace) -> Path:
    """Execute the full three-layer optimization workflow with new Layer 2 BL approach."""
    layer1_config = build_layer1_config(args)
    layer3_config = build_layer3_config()

    print("=" * 94)
    print(" " * 10 + "Layered Multi-Manager Portfolio Optimization (BL at Layer 2)")
    print("=" * 94 + "\n")

    print("▶ Layer 1: Strategic Asset Allocation (SAA)")
    layer1_result = run_layer1(layer1_config)
    print(f"   • Risk profile: {layer1_result.profile_name}")
    print(f"   • Target volatility: {layer1_result.target_vol:.2%}")
    print(f"   • Weights source: {args.weight_type.title()}")
    
    # Build Layer 2 config using Layer 1's target volatility for consistency
    layer2_config = build_layer2_config(args, layer1_result.target_vol)

    print("\n▶ Layer 2: Asset Class Active Risk Budget Allocation (Black-Litterman)")
    layer2_engine = ActiveRiskAllocator(layer2_config)
    target_active_risks, active_alloc, achieved_vol, risk_budget_shares, layer2_info = layer2_engine.run(layer1_result)
    
    active_risk_budget = layer2_info["active_risk_budget"]
    passive_risk_budget = layer2_info["passive_risk_budget"]
    layer1_active_risk_budget = layer2_info["layer1_active_risk_budget"]
    layer1_active_risk_realized = layer2_info["layer1_active_risk_realized"]
    active_risk_pct = layer2_info["active_risk_pct"]
    passive_risk_pct = layer2_info["passive_risk_pct"]

    print(f"   • Target portfolio volatility: {layer2_config.target_volatility:.2%}")
    print(f"   • Risk budget: {passive_risk_pct*100:.0f}% Passive / {active_risk_pct*100:.0f}% Active")
    print(f"   • Layer 1 active risk budget: {layer1_active_risk_budget*100:.2f}%")
    print(f"   • Layer 1 realized active risk: {layer1_active_risk_realized*100:.2f}%")
    print(f"   • Layer 2 passive implementation budget: {passive_risk_budget*100:.2f}%")
    print(f"   • Layer 2 active implementation budget: {active_risk_budget*100:.2f}%")
    print(f"   • Achieved volatility: {achieved_vol:.2%}")
    active_alloc_count = sum(1 for alpha in active_alloc.values() if alpha > 1e-10)
    print(f"   • Asset classes with active allocation: {active_alloc_count}")
    
    print("\n   Risk Budget Allocation:")
    for ac in active_alloc.keys():
        alpha = active_alloc[ac]
        budget_share = risk_budget_shares.get(ac, 0.0)
        target_te = target_active_risks.get(ac, 0.0)
        print(f"      {ac:30s}: {budget_share*100:5.1f}% of budget -> {alpha*100:6.2f}% active / {(1-alpha)*100:6.2f}% passive, target TE {target_te*100:6.2f}%")

    print("\n▶ Layer 3: Manager Selection (Black-Litterman with target TE)")
    manager_engine = ManagerSelectionEngine(layer3_config)
    manager_result = manager_engine.run(
        target_tes=target_active_risks,  # Pass target TEs from Layer 2
    )
    
    managed_asset_classes = ", ".join(manager_result.allocations.keys()) or "None"
    print(f"   • Asset classes with managers: {managed_asset_classes}")

    print("\n▶ Exporting results...")
    passive_vehicles = {
        "tickers": layer2_info.get("passive_tickers", {}),
        "names": layer2_info.get("passive_names", {}),
    }
    passive_vols = layer2_info.get("passive_vols", {})
    output_path = layer2_engine.finalize(
        layer1_result,
        active_alloc,
        achieved_vol,
        manager_result,
        passive_vehicles,
        passive_vols,
        layer2_info=layer2_info,
    )
    print(f"   • Results saved to: {output_path}")

    print("\nProcess complete ✅")
    return output_path


def main() -> None:
    """CLI entry-point for the layered optimization workflow."""
    args = parse_args()
    run_layered_optimization(args)


if __name__ == "__main__":
    main()
