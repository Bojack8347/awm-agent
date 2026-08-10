# Copyright 2026 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Union


ALLOWED_ASSET_CLASSES = (
    "Cash",
    "US Treasury",
    "Global Investment Grade Corporate Bond",
    "Global High Yield Bond BB-B",
    "Emerging Market Local Currency Government Bonds",
    "Emerging Market Hard Currency Debt",
    "US Equity",
    "Dev. Europe ex UK Equity",
    "Japan Equity",
    "China Equity",
    "India Equity",
    "Commodities",
    "Gold",
    "Hedge Funds",
    "Bitcoin",
)

_ALLOWED_ASSET_CLASS_SET = set(ALLOWED_ASSET_CLASSES)

AssetAllocationEntry = Union[Sequence[Any], Mapping[str, Any]]
AssetAllocationInput = Optional[Union['AssetAllocation', Mapping[str, float], Iterable[AssetAllocationEntry]]]
AssetReturnRateEntry = Union[Sequence[Any], Mapping[str, Any]]
AssetReturnRatesInput = Optional[Union['AssetReturnRates', Mapping[str, float], Iterable[AssetReturnRateEntry]]]


def get_allowed_asset_classes() -> tuple[str, ...]:
    """Return the canonical asset names accepted by asset-allocation inputs."""
    return ALLOWED_ASSET_CLASSES


def validate_asset_names(asset_names: Iterable[str], field_name: str = "asset classes") -> None:
    """Validate that every asset name is in the canonical asset universe."""
    invalid = sorted({asset_name for asset_name in asset_names if asset_name not in _ALLOWED_ASSET_CLASS_SET})
    if invalid:
        invalid_list = ', '.join(invalid)
        allowed_list = ', '.join(ALLOWED_ASSET_CLASSES)
        raise ValueError(
            f"Unsupported {field_name}: {invalid_list}. "
            f"Allowed asset classes: {allowed_list}"
        )


def _entry_to_pair(entry: AssetAllocationEntry) -> tuple[str, float]:
    if isinstance(entry, Mapping):
        asset_name = (
            entry.get("asset")
            or entry.get("asset_class")
            or entry.get("asset_name")
            or entry.get("name")
        )
        if asset_name is None:
            raise ValueError(
                "Asset allocation entries must include one of: "
                "'asset', 'asset_class', 'asset_name', or 'name'"
            )
        if "weight" in entry:
            weight = entry["weight"]
        elif "allocation" in entry:
            weight = entry["allocation"]
        elif "value" in entry:
            weight = entry["value"]
        else:
            raise ValueError(
                "Asset allocation entries must include one of: "
                "'weight', 'allocation', or 'value'"
            )
        return str(asset_name), float(weight)

    if isinstance(entry, Sequence) and not isinstance(entry, (str, bytes)) and len(entry) == 2:
        return str(entry[0]), float(entry[1])

    raise ValueError(
        "Asset allocation entries must be two-item sequences like "
        "('US Equity', 0.6) or dicts like {'asset': 'US Equity', 'weight': 0.6}"
    )


def _return_rate_entry_to_pair(entry: AssetReturnRateEntry) -> tuple[str, float]:
    if isinstance(entry, Mapping):
        asset_name = (
            entry.get("asset")
            or entry.get("asset_class")
            or entry.get("asset_name")
            or entry.get("name")
        )
        if asset_name is None:
            raise ValueError(
                "Asset return-rate entries must include one of: "
                "'asset', 'asset_class', 'asset_name', or 'name'"
            )
        for rate_key in (
            "return_rate",
            "expected_return",
            "annual_return",
            "annual_return_rate",
            "growth_rate",
            "rate",
            "return",
            "value",
        ):
            if rate_key in entry:
                return str(asset_name), float(entry[rate_key])
        raise ValueError(
            "Asset return-rate entries must include one of: "
            "'return_rate', 'expected_return', 'annual_return', "
            "'annual_return_rate', 'growth_rate', 'rate', 'return', or 'value'"
        )

    if isinstance(entry, Sequence) and not isinstance(entry, (str, bytes)) and len(entry) == 2:
        return str(entry[0]), float(entry[1])

    raise ValueError(
        "Asset return-rate entries must be two-item sequences like "
        "('US Equity', 10.0) or dicts like {'asset': 'US Equity', 'return_rate': 10.0}"
    )


def coerce_asset_allocation(
    asset_allocation: AssetAllocationInput,
    normalize: bool = False,
) -> Optional[Dict[str, float]]:
    """Convert an allocation input into a validated ``dict`` of weights.

    Args:
        asset_allocation: ``None``, an ``AssetAllocation``, a mapping of
            asset name to weight, or an iterable of asset/weight entries.
        normalize: If true, divide weights by their total. This lets future
            agents provide percentage-like weights such as ``60`` and ``40``.

    Returns:
        A new dict mapping canonical asset class names to weights, or ``None``.
    """
    if asset_allocation is None:
        return None

    if isinstance(asset_allocation, AssetAllocation):
        weights = dict(asset_allocation.weights)
    elif isinstance(asset_allocation, Mapping):
        weights = {str(asset_name): float(weight) for asset_name, weight in asset_allocation.items()}
    else:
        weights = {}
        for entry in asset_allocation:
            asset_name, weight = _entry_to_pair(entry)
            weights[asset_name] = weights.get(asset_name, 0.0) + weight

    validate_asset_names(weights.keys(), "asset allocation names")

    if any(weight < 0 for weight in weights.values()):
        raise ValueError("Asset allocation weights cannot be negative")

    total = sum(weights.values())
    if normalize:
        if total <= 0:
            raise ValueError("Asset allocation weights must sum to a positive value before normalization")
        weights = {asset_name: weight / total for asset_name, weight in weights.items()}

    validate_asset_allocation(weights)
    return weights


def coerce_asset_return_rates(
    asset_return_rates: AssetReturnRatesInput,
) -> Optional[Dict[str, float]]:
    """Convert return-rate inputs into a validated ``dict``.

    Args:
        asset_return_rates: ``None``, an ``AssetReturnRates``, a mapping of
            asset name to expected return rate, or an iterable of asset/rate
            entries. Rates are percentage-form values, so ``10.0`` means 10%.

    Returns:
        A new dict mapping canonical asset class names to return rates, or
        ``None``.
    """
    if asset_return_rates is None:
        return None

    if isinstance(asset_return_rates, AssetReturnRates):
        rates = dict(asset_return_rates.rates)
    elif isinstance(asset_return_rates, Mapping):
        rates = {str(asset_name): float(rate) for asset_name, rate in asset_return_rates.items()}
    else:
        rates = {}
        for entry in asset_return_rates:
            asset_name, rate = _return_rate_entry_to_pair(entry)
            if asset_name in rates:
                raise ValueError(f"Duplicate asset return rate for asset class: {asset_name}")
            rates[asset_name] = rate

    validate_asset_names(rates.keys(), "asset return rate names")
    return rates


@dataclass(frozen=True)
class AssetAllocation:
    """Structured asset allocation chosen by a scenario or decision agent."""

    weights: Dict[str, float]

    def __init__(self, weights: AssetAllocationInput, normalize: bool = False):
        normalized_weights = coerce_asset_allocation(weights, normalize=normalize)
        object.__setattr__(self, "weights", {} if normalized_weights is None else normalized_weights)

    @classmethod
    def from_weights(cls, weights: AssetAllocationInput, normalize: bool = False) -> 'AssetAllocation':
        """Create an allocation from raw agent-provided weights."""
        return cls(weights, normalize=normalize)

    @classmethod
    def from_percentages(cls, weights: AssetAllocationInput) -> 'AssetAllocation':
        """Create an allocation from percentage-style weights such as 60/40."""
        return cls(weights, normalize=True)

    def as_dict(self) -> Dict[str, float]:
        """Return a plain dict for account APIs and serialization."""
        return dict(self.weights)


@dataclass(frozen=True)
class AssetReturnRates:
    """Structured asset return rates chosen by a scenario or decision agent."""

    rates: Dict[str, float]

    def __init__(self, rates: AssetReturnRatesInput):
        normalized_rates = coerce_asset_return_rates(rates)
        object.__setattr__(self, "rates", {} if normalized_rates is None else normalized_rates)

    @classmethod
    def from_rates(cls, rates: AssetReturnRatesInput) -> 'AssetReturnRates':
        """Create return rates from raw agent-provided percentage values."""
        return cls(rates)

    @classmethod
    def from_percentages(cls, rates: AssetReturnRatesInput) -> 'AssetReturnRates':
        """Create return rates from percentage values such as 10.0 for 10%."""
        return cls(rates)

    def as_dict(self) -> Dict[str, float]:
        """Return a plain dict for account APIs and serialization."""
        return dict(self.rates)


def validate_asset_allocation(
    asset_allocation: AssetAllocationInput,
    tolerance: float = 0.001,
) -> None:
    """Validate asset allocation names and weights.

    Args:
        asset_allocation: Allocation input containing canonical asset class
            names and weights.
        tolerance: Allowed absolute difference from a total weight of 1.0.
    """
    if asset_allocation is None:
        return
    if isinstance(asset_allocation, AssetAllocation):
        asset_allocation = asset_allocation.weights
    elif not isinstance(asset_allocation, Mapping):
        asset_allocation = coerce_asset_allocation(asset_allocation)

    validate_asset_names(asset_allocation.keys(), "asset allocation names")
    if any(weight < 0 for weight in asset_allocation.values()):
        raise ValueError("Asset allocation weights cannot be negative")
    total = sum(asset_allocation.values())
    if abs(total - 1.0) > tolerance:
        raise ValueError(f"Asset allocation must sum to 1.0, got {total}")


def validate_asset_return_rates(asset_return_rates: AssetReturnRatesInput) -> None:
    """Validate that return-rate inputs use canonical asset names."""
    coerce_asset_return_rates(asset_return_rates)


def calculate_weighted_return_rate(
    asset_allocation: AssetAllocationInput,
    asset_return_rates: AssetReturnRatesInput,
) -> float:
    """Calculate a portfolio return rate from allocation weights and return rates.

    Args:
        asset_allocation: Asset class weights, expected to sum to 1.0.
        asset_return_rates: Annual expected return rates in percentage form,
            e.g. 10.0 for 10%. Accepts a dict, AssetReturnRates object, or
            Agent-style asset/rate entries.

    Returns:
        Weighted annual return rate in percentage form.
    """
    asset_allocation = coerce_asset_allocation(asset_allocation)
    asset_return_rates = coerce_asset_return_rates(asset_return_rates)
    if asset_return_rates is None:
        raise ValueError("asset_return_rates are required to calculate weighted return")

    missing = [
        asset_class
        for asset_class, weight in asset_allocation.items()
        if abs(weight) > 1e-12 and asset_class not in asset_return_rates
    ]
    if missing:
        missing_list = ', '.join(sorted(missing))
        raise ValueError(f"Missing return rates for asset classes: {missing_list}")

    return sum(
        weight * asset_return_rates[asset_class]
        for asset_class, weight in asset_allocation.items()
    )
