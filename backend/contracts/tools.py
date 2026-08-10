"""Financial tool-call contracts used by the advisor loop."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import Field, field_validator

from contracts.base import AwmFlexibleContractModel


class CashflowToolArgsContract(AwmFlexibleContractModel):
    """Arguments accepted by the cashflow simulation tool adapter."""

    simulation_mode: str = "deterministic"
    num_simulations: int = Field(default=100, ge=1)
    seed: Optional[int] = None
    return_individual_runs: Optional[bool] = None
    num_individual_runs: Optional[int] = Field(default=None, ge=1)
    use_latest_asset_allocation: bool = False
    payload_override: Optional[Dict[str, Any]] = None
    bank_balance_override: Optional[float] = None
    investment_balance_override: Optional[float] = None

class AssetAllocationModelToolArgsContract(AwmFlexibleContractModel):
    """Arguments accepted by the asset allocation model optimization tool adapter."""

    target_volatility: float
    active_risk_percentage: float = Field(default=0.0, ge=0.0, le=1.0)
    total_investment: float = Field(gt=0.0)
    excluded_asset_classes: Optional[List[str]] = None

    @field_validator("active_risk_percentage", mode="before")
    @classmethod
    def _normalize_percent(cls, value: Any) -> Any:
        if value is None:
            return 0.0
        try:
            number = float(value)
        except (TypeError, ValueError):
            return value
        if number > 1.0:
            number = number / 100.0
        return max(0.0, min(1.0, number))


def build_cashflow_tool_args(args: Dict[str, Any]) -> CashflowToolArgsContract:
    """Parse cashflow tool args through the shared contract."""
    return CashflowToolArgsContract.model_validate(args or {})


def build_asset_allocation_tool_args(args: Dict[str, Any]) -> AssetAllocationModelToolArgsContract:
    """Parse asset allocation model tool args through the shared contract."""
    return AssetAllocationModelToolArgsContract.model_validate(args or {})
