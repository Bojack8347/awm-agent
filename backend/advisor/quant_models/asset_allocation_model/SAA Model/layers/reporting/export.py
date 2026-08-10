"""
Simplified reporting module for the three-layer portfolio optimization.

Outputs a clean Excel file with:
- Asset allocation with one row per vehicle (active managers + passive ETFs)
- Portfolio-level metrics (target vol, achieved vol, expected return)
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd

from layers.layer_types import Layer1Result, ManagerSelectionResult
from layers.L3.portfolio_metrics import (
    compute_portfolio_expected_return_and_volatility,
)


def export_portfolio_results(
    output_path: Path,
    layer1_result: Layer1Result,
    active_allocations: Dict[str, float],
    manager_result: ManagerSelectionResult,
    target_volatility: float,
    achieved_volatility: float,
    passive_vehicles: Dict[str, Dict[str, str]],
    passive_vols: Dict[str, float],
    layer2_info: Dict | None = None,
) -> None:
    """
    Export simplified portfolio construction results to Excel.
    
    Args:
        output_path: Path to save the Excel file
        layer1_result: Results from Layer 1 SAA optimization
        active_allocations: Active vs passive split per asset class from Layer 2
        manager_result: Manager allocations from Layer 3
        target_volatility: Target portfolio volatility
        achieved_volatility: Achieved portfolio volatility
        passive_vehicles: Dict with 'tickers' and 'names' for passive ETFs
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        # Sheet 1: Asset Allocation (one row per vehicle)
        _write_allocation_by_vehicle(
            writer,
            layer1_result,
            active_allocations,
            manager_result,
            passive_vehicles,
        )
        
        # Sheet 2: Portfolio Summary
        _write_portfolio_summary(
            writer,
            layer1_result,
            target_volatility,
            achieved_volatility,
            active_allocations,
            manager_result,
            passive_vols,
        )

        if layer2_info:
            _write_te_alignment_sheet(
                writer,
                layer1_result,
                active_allocations,
                manager_result,
                layer2_info,
            )


def _write_allocation_by_vehicle(
    writer: pd.ExcelWriter,
    layer1_result: Layer1Result,
    active_allocations: Dict[str, float],
    manager_result: ManagerSelectionResult,
    passive_vehicles: Dict[str, Dict[str, str]],
) -> None:
    """Write asset allocation with one row per vehicle."""
    rows = []
    
    passive_tickers = passive_vehicles.get("tickers", {})
    passive_names = passive_vehicles.get("names", {})
    
    for asset_class in layer1_result.selected_weights.index:
        equilibrium_weight = layer1_result.equilibrium_weights.get(asset_class, 0.0)
        dynamic_weight = layer1_result.selected_weights.get(asset_class, 0.0)
        active_alloc = active_allocations.get(asset_class, 0.0)
        
        # Calculate weights
        active_weight = dynamic_weight * active_alloc
        passive_weight = dynamic_weight * (1 - active_alloc)
        
        # Get manager allocations for this asset class
        manager_allocs = manager_result.allocations.get(asset_class, {})
        
        # Add rows for active managers
        for isin, manager_weight_in_active in manager_allocs.items():
            mgr_row = manager_result.manager_data[manager_result.manager_data["ISIN"] == isin]
            if mgr_row.empty:
                continue
            
            mgr = mgr_row.iloc[0]
            # Portfolio weight for this manager = active_weight × manager's share within active
            manager_portfolio_weight = active_weight * manager_weight_in_active
            
            rows.append({
                "Asset Class": asset_class,
                "Equilibrium Weight (%)": equilibrium_weight * 100,
                "Dynamic Weight (%)": dynamic_weight * 100,
                "Active Weight (%)": active_weight * 100,
                "Passive Weight (%)": passive_weight * 100,
                "Portfolio Weight (%)": manager_portfolio_weight * 100,
                "Vehicle Type": "Active",
                "ISIN/Ticker": isin,
            })
        
        # Add row for passive vehicle (ETF)
        passive_ticker = passive_tickers.get(asset_class, "")
        
        rows.append({
            "Asset Class": asset_class,
            "Equilibrium Weight (%)": equilibrium_weight * 100,
            "Dynamic Weight (%)": dynamic_weight * 100,
            "Active Weight (%)": active_weight * 100,
            "Passive Weight (%)": passive_weight * 100,
            "Portfolio Weight (%)": passive_weight * 100,
            "Vehicle Type": "Passive",
            "ISIN/Ticker": passive_ticker,
        })
    
    df = pd.DataFrame(rows)
    # Sort by asset class then by vehicle type (Active before Passive)
    df["_sort_type"] = df["Vehicle Type"].map({"Active": 0, "Passive": 1})
    df = df.sort_values(["Asset Class", "_sort_type", "Portfolio Weight (%)"], ascending=[True, True, False])
    df = df.drop(columns=["_sort_type"])
    
    df.to_excel(writer, sheet_name="Asset_Allocation", index=False)


def _write_portfolio_summary(
    writer: pd.ExcelWriter,
    layer1_result: Layer1Result,
    target_volatility: float,
    achieved_volatility: float,
    active_allocations: Dict[str, float],
    manager_result: ManagerSelectionResult,
    passive_vols: Dict[str, float],
) -> None:
    """Write portfolio-level summary metrics."""
    # Calculate total weights
    total_weight = layer1_result.selected_weights.sum()
    
    # Get expected return from Layer 1 raw results if available
    expected_return = 0.0
    if layer1_result.raw_results:
        profile_data = layer1_result.raw_results.get(layer1_result.profile_name, {})
        dynamic_data = profile_data.get("dynamic", {})
        metrics = dynamic_data.get("metrics", {})
        expected_return = metrics.get("expected_return", 0.0)
    
    portfolio_expected_return, portfolio_expected_volatility = (
        compute_portfolio_expected_return_and_volatility(
            layer1_result=layer1_result,
            active_allocations=active_allocations,
            manager_result=manager_result,
            passive_vols=passive_vols,
        )
    )

    summary_data = {
        "Metric": [
            "Risk Profile",
            "Target Volatility (%)",
            "Achieved Volatility (%)",
            "Expected Return (%)",
            "Portfolio Expected Return (%)",
            "Portfolio Expected Volatility (%)",
            "Total Portfolio Weight (%)",
            "Number of Asset Classes",
        ],
        "Value": [
            layer1_result.profile_name,
            f"{target_volatility * 100:.2f}",
            f"{achieved_volatility * 100:.2f}",
            f"{expected_return * 100:.2f}",
            f"{portfolio_expected_return * 100:.2f}",
            f"{portfolio_expected_volatility * 100:.2f}",
            f"{total_weight * 100:.2f}",
            len(layer1_result.selected_weights),
        ],
    }
    
    df = pd.DataFrame(summary_data)
    df.to_excel(writer, sheet_name="Portfolio_Summary", index=False)


def _write_te_alignment_sheet(
    writer: pd.ExcelWriter,
    layer1_result: Layer1Result,
    active_allocations: Dict[str, float],
    manager_result: ManagerSelectionResult,
    layer2_info: Dict,
) -> None:
    """Write Layer 1/2/3 tracking-error alignment diagnostics."""
    expected_tes = layer2_info.get("expected_tes", {})
    target_tes = layer2_info.get("target_active_risks", {})
    risk_targets = layer2_info.get("portfolio_active_risk_targets", {})
    risk_capacity = layer2_info.get("portfolio_active_risk_capacity", {})
    risk_budget_shares = layer2_info.get("risk_budget_shares", {})
    layer1_active_risk_budget = float(layer2_info.get("layer1_active_risk_budget", 0.0))
    layer1_active_risk_realized = float(layer2_info.get("layer1_active_risk_realized", 0.0))
    layer2_active_risk_budget = float(layer2_info.get("active_risk_budget", 0.0))
    layer2_passive_risk_budget = float(layer2_info.get("passive_risk_budget", 0.0))

    rows = []
    for asset_class in layer1_result.selected_weights.index:
        saa_weight = float(layer1_result.selected_weights.get(asset_class, 0.0))
        active_alloc = float(active_allocations.get(asset_class, 0.0))
        expected_te = float(expected_tes.get(asset_class, 0.0))
        target_te = float(target_tes.get(asset_class, 0.0))
        budget_share = float(risk_budget_shares.get(asset_class, 0.0))
        achieved_te = _compute_covariance_based_active_te(manager_result, asset_class)

        portfolio_target = float(risk_targets.get(asset_class, 0.0))
        portfolio_capacity = float(risk_capacity.get(asset_class, 0.0))
        portfolio_achieved = saa_weight * active_alloc * achieved_te

        rows.append({
            "Asset Class": asset_class,
            "SAA Weight (%)": saa_weight * 100,
            "Active Allocation (%)": active_alloc * 100,
            "Layer1 Active Risk Budget (%)": layer1_active_risk_budget * 100,
            "Layer1 Realized Active Risk (%)": layer1_active_risk_realized * 100,
            "Layer2 Active Implementation Budget (%)": layer2_active_risk_budget * 100,
            "Layer2 Passive Implementation Budget (%)": layer2_passive_risk_budget * 100,
            "Layer2 Risk Budget Share (%)": budget_share * 100,
            "Raw Expected TE (%)": expected_te * 100,
            "Layer2 Budget-Implied Target TE (%)": target_te * 100,
            "Layer3 Achieved TE (%)": achieved_te * 100,
            "Portfolio Active Risk Target (%)": portfolio_target * 100,
            "Portfolio Active Risk Capacity at Raw TE (%)": portfolio_capacity * 100,
            "Portfolio Active Risk Achieved (%)": portfolio_achieved * 100,
            "Achieved Minus Target (%)": (portfolio_achieved - portfolio_target) * 100,
        })

    pd.DataFrame(rows).to_excel(writer, sheet_name="TE_Alignment", index=False)


def _compute_covariance_based_active_te(
    manager_result: ManagerSelectionResult,
    asset_class: str,
) -> float:
    """Calculate achieved manager-sleeve TE with the Layer 3 covariance matrix."""
    manager_allocs = manager_result.allocations.get(asset_class, {})
    if not manager_allocs:
        return 0.0

    cov = manager_result.covariance_by_asset.get(asset_class)
    if cov is None:
        return float(manager_result.active_tes.get(asset_class, 0.0))

    isins = list(manager_allocs.keys())
    if any(isin not in cov.index for isin in isins):
        return float(manager_result.active_tes.get(asset_class, 0.0))

    weights = np.array([manager_allocs[isin] for isin in isins], dtype=float)
    cov_mat = cov.loc[isins, isins].values.astype(float)
    variance = float(weights @ cov_mat @ weights)
    return float(np.sqrt(max(variance, 0.0)))
