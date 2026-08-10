# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

"""
Market assumptions for asset classes - provided by internal team.

This module contains the MarketAssumptions class which holds return, volatility,
and correlation assumptions for asset classes. The asset class list is dynamic
and determined by what the client uses in their asset allocations.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional
import numpy as np

from ..account.asset_allocation import ALLOWED_ASSET_CLASSES, validate_asset_names


@dataclass
class AssetClassAssumptions:
    """Return and volatility assumptions for a single asset class.
    
    Attributes:
        name: Canonical asset class name (e.g., "US Equity")
        expected_return: Annual expected return as decimal (e.g., 0.10 for 10%)
        volatility: Annual standard deviation as decimal (e.g., 0.18 for 18%)
    """
    name: str
    expected_return: float
    volatility: float
    
    def __post_init__(self):
        if self.volatility < 0:
            raise ValueError(f"Volatility cannot be negative: {self.volatility}")


class MarketAssumptions:
    """Internal team-provided market assumptions for asset classes.
    
    Asset classes are dynamic - determined by what the client uses.
    Internal team provides return, volatility, and correlation for each.
    
    Example:
        >>> assumptions = MarketAssumptions.create_default()
        >>> print(assumptions.asset_class_order)
        ['Cash', 'US Treasury', ...]
        >>> print(assumptions.get_returns_vector())
        [0.10, 0.12, ...]
    """
    
    def __init__(self, 
                 asset_classes: Dict[str, AssetClassAssumptions],
                 correlation_matrix: np.ndarray,
                 asset_class_order: List[str]):
        """Initialize market assumptions.
        
        Args:
            asset_classes: Dict mapping asset class name to its assumptions
            correlation_matrix: NxN correlation matrix for asset classes
            asset_class_order: Order of asset classes in the correlation matrix
        
        Raises:
            ValueError: If matrix dimensions don't match or asset classes missing
        """
        self.asset_classes = asset_classes
        self.correlation_matrix = correlation_matrix
        self.asset_class_order = asset_class_order
        self._validate()
        self._covariance_matrix = self._compute_covariance_matrix()
    
    def _validate(self):
        """Validate that all inputs are consistent."""
        n = len(self.asset_class_order)
        
        if self.correlation_matrix.shape != (n, n):
            raise ValueError(
                f"Correlation matrix shape {self.correlation_matrix.shape} "
                f"doesn't match {n} asset classes"
            )
        
        missing = [name for name in self.asset_class_order 
                   if name not in self.asset_classes]
        if missing:
            raise ValueError(f"Asset classes missing from assumptions: {missing}")

        validate_asset_names(self.asset_classes.keys(), "market assumption asset names")
        validate_asset_names(self.asset_class_order, "market assumption asset order")
        
        # Check correlation matrix is symmetric and has 1s on diagonal
        if not np.allclose(self.correlation_matrix, self.correlation_matrix.T):
            raise ValueError("Correlation matrix must be symmetric")
        
        if not np.allclose(np.diag(self.correlation_matrix), 1.0):
            raise ValueError("Correlation matrix diagonal must be 1.0")
    
    def _compute_covariance_matrix(self) -> np.ndarray:
        """Compute covariance matrix from correlation and volatilities.
        
        Cov = diag(sigma) @ Corr @ diag(sigma)
        """
        vols = np.array([self.asset_classes[name].volatility 
                         for name in self.asset_class_order])
        vol_diag = np.diag(vols)
        return vol_diag @ self.correlation_matrix @ vol_diag
    
    @property
    def covariance_matrix(self) -> np.ndarray:
        """Get the covariance matrix for asset classes."""
        return self._covariance_matrix
    
    def get_returns_vector(self) -> np.ndarray:
        """Get expected returns as numpy array in asset_class_order."""
        return np.array([self.asset_classes[name].expected_return 
                         for name in self.asset_class_order])
    
    def get_volatilities_vector(self) -> np.ndarray:
        """Get volatilities as numpy array in asset_class_order."""
        return np.array([self.asset_classes[name].volatility 
                         for name in self.asset_class_order])
    
    def with_asset_overrides(self, overrides: Optional[Dict[str, Dict[str, float]]]) -> 'MarketAssumptions':
        """Return a copy with per-asset expected returns/volatilities replaced.

        Override values are annual percentages (matching the configuration and
        scenario-file units). Only ``expected_return`` and ``volatility`` can
        be overridden here; the correlation matrix is preserved, so
        correlation changes belong in the configuration's ``market_beta``
        values.

        Args:
            overrides: Mapping of asset class name to a dict with optional
                ``expected_return`` and/or ``volatility`` keys, or None.

        Returns:
            A new MarketAssumptions instance with the overrides applied.
        """
        if not overrides:
            return self

        unknown = [name for name in overrides if name not in self.asset_classes]
        if unknown:
            raise ValueError(f"Unknown asset classes in overrides: {', '.join(sorted(unknown))}")

        asset_classes = dict(self.asset_classes)
        for name, entry in overrides.items():
            entry = entry or {}
            unsupported = set(entry) - {'expected_return', 'volatility'}
            if unsupported:
                raise ValueError(
                    f"Unsupported override fields for {name}: {', '.join(sorted(unsupported))}. "
                    "Per-year and profile overrides support expected_return and volatility; "
                    "correlation (market_beta) changes belong in the configuration."
                )
            current = asset_classes[name]
            expected_return = current.expected_return
            volatility = current.volatility
            if entry.get('expected_return') is not None:
                expected_return = entry['expected_return'] / 100
            if entry.get('volatility') is not None:
                volatility = entry['volatility'] / 100
            asset_classes[name] = AssetClassAssumptions(name, expected_return, volatility)

        return MarketAssumptions(asset_classes, self.correlation_matrix, self.asset_class_order)

    # Built-in fallbacks, used for anything the configuration omits.
    # Values are (expected_return, volatility) in decimal form.
    _BUILTIN_ASSUMPTIONS = {
        "Cash": (0.02, 0.01),
        "US Treasury": (0.035, 0.05),
        "Global Investment Grade Corporate Bond": (0.045, 0.07),
        "Global High Yield Bond BB-B": (0.065, 0.12),
        "Emerging Market Local Currency Government Bonds": (0.07, 0.14),
        "Emerging Market Hard Currency Debt": (0.065, 0.13),
        "US Equity": (0.09, 0.18),
        "Dev. Europe ex UK Equity": (0.08, 0.20),
        "Japan Equity": (0.075, 0.20),
        "China Equity": (0.10, 0.28),
        "India Equity": (0.11, 0.26),
        "Commodities": (0.05, 0.18),
        "Gold": (0.04, 0.16),
        "Hedge Funds": (0.06, 0.10),
        "Bitcoin": (0.15, 0.70),
    }

    _BUILTIN_MARKET_BETAS = {
        "Cash": 0.05,
        "US Treasury": 0.15,
        "Global Investment Grade Corporate Bond": 0.25,
        "Global High Yield Bond BB-B": 0.40,
        "Emerging Market Local Currency Government Bonds": 0.45,
        "Emerging Market Hard Currency Debt": 0.45,
        "US Equity": 0.65,
        "Dev. Europe ex UK Equity": 0.68,
        "Japan Equity": 0.66,
        "China Equity": 0.72,
        "India Equity": 0.72,
        "Commodities": 0.35,
        "Gold": 0.15,
        "Hedge Funds": 0.45,
        "Bitcoin": 0.35,
    }

    @classmethod
    def create_default(cls) -> 'MarketAssumptions':
        """Create market assumptions from the financial configuration.

        Per-asset expected returns, volatilities, and market betas come from
        the ``market_assumptions.asset_classes`` section of the financial
        configuration (annual percentages in the YAML), so economic scenarios
        can override any subset of assets or fields — e.g. a recession
        scenario can cut equity returns and raise volatilities. Built-in
        values cover anything the configuration omits.
        """
        # Imported here to keep this module importable without the config
        # package being initialized first.
        from ..config.config_manager import config

        configured = config.financial.get('market_assumptions.asset_classes') or {}

        asset_classes = {}
        market_beta = {}
        for name in ALLOWED_ASSET_CLASSES:
            expected_return, volatility = cls._BUILTIN_ASSUMPTIONS[name]
            beta = cls._BUILTIN_MARKET_BETAS[name]
            entry = configured.get(name) or {}
            if entry.get('expected_return') is not None:
                expected_return = entry['expected_return'] / 100
            if entry.get('volatility') is not None:
                volatility = entry['volatility'] / 100
            if entry.get('market_beta') is not None:
                beta = entry['market_beta']
            asset_classes[name] = AssetClassAssumptions(name, expected_return, volatility)
            market_beta[name] = beta

        order = list(ALLOWED_ASSET_CLASSES)
        n = len(order)
        corr = np.eye(n)
        for i, asset_i in enumerate(order):
            for j, asset_j in enumerate(order):
                if i != j:
                    corr[i, j] = market_beta[asset_i] * market_beta[asset_j]

        return cls(asset_classes, corr, order)


class YearVaryingMarketAssumptions:
    """Term structure of market assumptions: a base set plus per-year overrides.

    Built for workflows where an upstream model supplies each year's expected
    returns and volatilities. Years without an override use the base
    assumptions unchanged; override values are annual percentages, matching
    the configuration and scenario-file units. The cross-asset correlation
    matrix always comes from the base assumptions.

    Example:
        >>> base = MarketAssumptions.create_default()
        >>> schedule = YearVaryingMarketAssumptions(base, {
        ...     2030: {"US Equity": {"expected_return": -10.0, "volatility": 30.0}},
        ... })
        >>> schedule.for_year(2030).asset_classes["US Equity"].volatility
        0.3
    """

    def __init__(self, base: MarketAssumptions,
                 yearly_overrides: Optional[Dict[int, Dict[str, Dict[str, float]]]] = None):
        """Initialize the schedule.

        Args:
            base: Assumptions used for every year without an override.
            yearly_overrides: Mapping of year to per-asset overrides, each a
                dict with optional ``expected_return``/``volatility`` keys in
                annual percentages.
        """
        self.base = base
        self.yearly_overrides = {
            int(year): dict(assets or {})
            for year, assets in (yearly_overrides or {}).items()
        }
        self._cache: Dict[int, MarketAssumptions] = {}
        # Validate eagerly so bad override data fails at construction time
        for year, assets in self.yearly_overrides.items():
            self._cache[year] = base.with_asset_overrides(assets)

    @property
    def override_years(self) -> List[int]:
        """Years that carry overrides, sorted."""
        return sorted(self.yearly_overrides)

    def for_year(self, year: Optional[int]) -> MarketAssumptions:
        """Get the effective assumptions for a given simulation year."""
        if year is None:
            return self.base
        return self._cache.get(int(year), self.base)
