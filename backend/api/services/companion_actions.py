"""Typed parsing for authenticated Companion APP actions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class InvestmentAssessmentDecision:
    type: str
    decision: str
    assessment_id: str
    assessment_version: int
    investment_consultation_id: str
    money_pool_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            key: value
            for key, value in asdict(self).items()
            if value is not None
        }


def parse_client_action(value: Any) -> Optional[InvestmentAssessmentDecision]:
    if value is None or not isinstance(value, dict):
        return None
    if value.get("type") != "investment_assessment_decision":
        return None
    decision = str(value.get("decision") or "").strip().lower()
    assessment_id = str(value.get("assessment_id") or "").strip()
    consultation_id = str(
        value.get("investment_consultation_id") or ""
    ).strip()
    try:
        assessment_version = int(value.get("assessment_version"))
    except (TypeError, ValueError):
        return None
    if (
        decision not in {"agree", "cancel"}
        or not assessment_id
        or not consultation_id
        or assessment_version <= 0
    ):
        return None
    money_pool_id = str(value.get("money_pool_id") or "").strip() or None
    return InvestmentAssessmentDecision(
        type="investment_assessment_decision",
        decision=decision,
        assessment_id=assessment_id,
        assessment_version=assessment_version,
        investment_consultation_id=consultation_id,
        money_pool_id=money_pool_id,
    )


__all__ = ["InvestmentAssessmentDecision", "parse_client_action"]
