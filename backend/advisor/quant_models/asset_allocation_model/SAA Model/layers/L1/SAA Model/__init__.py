from .equilibrium_saa import EquilibriumSAA
from .dynamic_saa import DynamicSAA
"""
SAA Model Package

A modular Strategic Asset Allocation optimization framework that implements 
a two-step SAA optimization process.

Modules:
- config: Configuration and parameters
- data_processor: Historical data processing and loading
- equilibrium_saa: Equilibrium SAA optimization model  
- dynamic_saa: Dynamic SAA optimization model
- results_exporter: Results export and formatting
- utils: Utility functions and validation
- main: Main orchestration logic
"""

__version__ = "2.0.0"
__author__ = "SAA Team"

# Import main components for easy access
from .main import run_saa_optimization
from .config import *

__all__ = [
    'run_saa_optimization',
    'DATA_FILE_PATH',
    'OUTPUT_FILE_PATH',
    'LIQUIDITY_TARGET',
    'ACTIVE_RISK_BUDGET', 
    'LAMBDA_ACTIVE',
    'LOOKBACK_YEARS'
]
