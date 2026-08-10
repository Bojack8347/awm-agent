"""
Layered portfolio optimization package.

Exposes configuration and result dataclasses plus helper factories
for layer-specific engines (SAA, active risk allocation, manager selection).
"""

from .layer_types import (
    Layer1Config,
    Layer1Result,
    Layer2Config,
    ManagerSelectionConfig,
    ManagerSelectionResult,
)
from .L1.layer1_saa import run_layer1
from .L2.layer2_active_risk import run_layered_optimization
from .L3.layer3_manager_selection import ManagerSelectionEngine
from .reporting.export import export_portfolio_results

__all__ = [
    "Layer1Config",
    "Layer1Result",
    "Layer2Config",
    "ManagerSelectionConfig",
    "ManagerSelectionResult",
    "run_layer1",
    "ManagerSelectionEngine",
    "run_layered_optimization",
    "export_portfolio_results",
]

