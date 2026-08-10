# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

"""Configuration for Monte Carlo simulations."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class MonteCarloConfig:
    """Configuration for Monte Carlo simulation parameters.
    
    Attributes:
        num_simulations: Number of Monte Carlo iterations to run. Default 100.
        random_seed: Optional seed for reproducible results. Default None.
    """
    num_simulations: int = 100
    random_seed: Optional[int] = None
    
    def __post_init__(self):
        if self.num_simulations < 1:
            raise ValueError("num_simulations must be at least 1")
