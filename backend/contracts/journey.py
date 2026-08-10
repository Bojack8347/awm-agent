"""Journey API response contracts."""

from __future__ import annotations

from typing import Any, Dict, Optional

from contracts.base import AwmContractModel


class JourneyActivationResponseContract(AwmContractModel):
    """Response contract for policy activation on a journey."""

    success: bool
    journey_id: Optional[str] = None
    policy_status: Optional[str] = None
    activated_at: Optional[str] = None
    activation_snapshot_version: Optional[int] = None
    diagnosis_refreshed: Optional[bool] = None
    diagnosis_status: Optional[str] = None
    diagnosis_refresh_queued: Optional[bool] = None
    error: Optional[str] = None
    details: Optional[Any] = None


def validate_journey_activation_response(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate activation response shape while preserving the original dict."""
    JourneyActivationResponseContract.model_validate(payload)
    return payload
