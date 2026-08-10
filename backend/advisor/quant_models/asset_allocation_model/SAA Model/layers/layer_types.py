from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd


@dataclass
class Layer1Config:
    """Configuration for Strategic Asset Allocation (Layer 1)."""

    data_file: Path
    output_file: Path
    # Based on baseline we are setting risk profile to use
    # AI should determine gaps and evaluate set of parameters based on user input
    risk_profile: str
    # Insight product determines which weight type to use, set just to dynamic fow
    weight_type: str = "dynamic"
    # If provided, use this target volatility instead of looking up from Excel
    target_volatility: Optional[float] = None
    # Asset classes removed from the optimizer universe before Layer 1.
    excluded_asset_classes: Optional[List[str]] = None


@dataclass
class Layer1Result:
    """Results from Strategic Asset Allocation run."""

    profile_name: str
    target_vol: float
    equilibrium_weights: pd.Series
    dynamic_weights: Optional[pd.Series]
    selected_weights: pd.Series
    asset_clusters: Dict[str, str]
    raw_results: Dict[str, Dict]
    saa_data: Dict[str, object]


@dataclass
class ManagerSelectionConfig:
    """Configuration for Layer 3 active manager selection."""

    manager_selection_file: Path
    sheet_name: str = "Manager Active Exp Conviction"  # Manager data with TE, IR, CL
    correlation_sheet_name: str = "Manager Active Re Corr"  # Manager active return correlation matrix
    tau: float = 0.05
    risk_aversion: float = 2.5


@dataclass
class ManagerSelectionResult:
    """Outputs from manager selection engine."""

    manager_data: pd.DataFrame
    allocations: Dict[str, Dict[str, float]]
    active_vols: Dict[str, float]
    active_tes: Dict[str, float]
    covariance_by_asset: Dict[str, pd.DataFrame]


@dataclass
class Layer2Config:
    """Configuration for active risk allocation (Layer 2)."""

    active_exposure_file: Path
    asset_allocation_file: Path  # Passive Vehicle Selection.xlsx
    output_file: Path
    conviction_sheet: str = "Active TE IR CL"
    correlation_sheet: str = "Active Return Correlation"
    asset_allocation_sheet: str = "Sheet1"
    target_volatility: float = 0.12

