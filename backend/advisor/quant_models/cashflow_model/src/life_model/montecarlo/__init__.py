# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

"""
Monte Carlo simulation module for probabilistic financial modeling.

This module provides Monte Carlo simulation capabilities with correlated returns
across investment accounts, derived from client-provided asset allocations and
internal market assumptions.
"""

from .config import MonteCarloConfig
from .market_assumptions import MarketAssumptions, AssetClassAssumptions, YearVaryingMarketAssumptions
from .account_parameters import AccountParametersCalculator, AccountStochasticParams
from .return_generator import AccountCorrelatedReturnGenerator, YearVaryingAccountReturnGenerator
from .account_registry import InvestmentAccountRegistry
from .simulator import MonteCarloSimulator
from .results import MonteCarloResults

__all__ = [
    'MonteCarloConfig',
    'MarketAssumptions',
    'YearVaryingMarketAssumptions',
    'AssetClassAssumptions',
    'AccountParametersCalculator',
    'AccountStochasticParams',
    'AccountCorrelatedReturnGenerator',
    'YearVaryingAccountReturnGenerator',
    'InvestmentAccountRegistry',
    'MonteCarloSimulator',
    'MonteCarloResults',
]
