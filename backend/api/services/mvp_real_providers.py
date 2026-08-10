"""Real provider adapters for the AWM MVP UI backend."""

from __future__ import annotations

import os
from typing import Any, Dict, Mapping, Optional

import requests

from advisor.llm.config import ToolLoopConfig
from api.services.cashflow_projection_mapper import build_projection_artifact_from_cashflow_result
from api.services.asset_allocation_artifact_adapter import proposal_artifact_from_asset_allocation
from advisor.tools.deterministic_tools.run_asset_allocation.execution import build_asset_allocation_headers
from advisor.tools.deterministic_tools.run_cashflow_projection.execution import build_cashflow_headers


class RealModelAgentProvider:
    """Proposal provider that calls real local financial models.

    This provider deliberately covers only financial model generation. KYC,
    external account linking, broker execution, and market data remain behind
    their own provider boundaries and can stay mocked for MVP UI acceptance.
    """

    def __init__(
        self,
        *,
        config: Optional[ToolLoopConfig] = None,
        http_session: Optional[requests.Session] = None,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        self.config = config or ToolLoopConfig.from_env()
        self.http_session = http_session or requests.Session()
        self.timeout_seconds = timeout_seconds or float(os.getenv("AWM_MODEL_PROVIDER_TIMEOUT_SECONDS", "45"))

    def proposal_artifact(self, journey: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """Generate an AWM proposal artifact from asset allocation model and optional cashflow output."""
        journey = journey or {}
        fields = journey.get("collected_fields") if isinstance(journey.get("collected_fields"), dict) else {}
        asset_allocation_result = self._run_asset_allocation(fields)
        if not asset_allocation_result.get("success"):
            raise RuntimeError(f"asset allocation model proposal generation failed: {asset_allocation_result.get('error') or asset_allocation_result}")
        cashflow_result = self._run_cashflow_if_available(fields)
        return proposal_artifact_from_asset_allocation(
            asset_allocation_result,
            journey={**journey, "generation_source": "real_asset_allocation_model"},
            cashflow_result=cashflow_result,
        )

    def projection_artifact(self, cashflow_payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Generate the mobile Projection artifact from the real cashflow engine."""
        payload = dict(cashflow_payload or {})
        response = self.http_session.post(
            f"{self.config.cashflow_model_url}/cashflow/api/v1/simulate",
            json=payload,
            headers=build_cashflow_headers(self.config),
            timeout=self.timeout_seconds,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Cashflow projection generation failed: {response.text[:600]}")
        result = response.json()
        if not isinstance(result, dict) or not result.get("success"):
            raise RuntimeError(f"Cashflow projection generation failed: {result}")
        client_context = _client_context_from_cashflow_payload(payload)
        artifact = build_projection_artifact_from_cashflow_result(result, client_context=client_context)
        artifact["generation_source"] = "real_cashflow_engine"
        artifact["engine"] = result.get("engine")
        return artifact
    def _run_asset_allocation(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "target_volatility": _target_volatility(fields),
            "active_risk_percentage": _active_risk_percentage(fields),
            "investment_amount": _investment_amount(fields),
        }
        exclusions = _excluded_asset_classes_from_fields(fields)
        if exclusions:
            payload["excluded_asset_classes"] = exclusions

        response = self.http_session.post(
            f"{self.config.asset_allocation_model_url}{self.config.asset_allocation_model_optimize_path}",
            json=payload,
            headers=build_asset_allocation_headers(self.config),
            timeout=self.timeout_seconds,
        )
        if response.status_code != 200:
            return {
                "success": False,
                "error": "asset allocation model engine API call failed",
                "status_code": response.status_code,
                "details": response.text[:600],
            }
        result = response.json()
        if isinstance(result, dict):
            result.setdefault("total_investment", payload["investment_amount"])
            return result
        return {"success": False, "error": "asset allocation model engine returned a non-object response"}

    def _run_cashflow_if_available(self, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        cashflow_payload = fields.get("cashflow_payload")
        if not isinstance(cashflow_payload, dict):
            return None
        payload = dict(cashflow_payload)
        payload.setdefault("simulation_mode", fields.get("cashflow_simulation_mode") or "deterministic")
        try:
            response = self.http_session.post(
                f"{self.config.cashflow_model_url}/cashflow/api/v1/simulate",
                json=payload,
                headers=build_cashflow_headers(self.config),
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            return {"success": False, "error": "Cashflow engine API call failed", "details": str(exc)}
        if response.status_code != 200:
            return {
                "success": False,
                "error": "Cashflow engine API call failed",
                "status_code": response.status_code,
                "details": response.text[:600],
            }
        result = response.json()
        return result if isinstance(result, dict) else None


def _client_context_from_cashflow_payload(payload: Mapping[str, Any] | Dict[str, Any]) -> Dict[str, Any]:
    """Pull age (and related labels) from the cashflow engine request payload."""
    context: Dict[str, Any] = {}
    if not isinstance(payload, dict):
        return context
    profile = payload.get("client_profile") if isinstance(payload.get("client_profile"), dict) else {}
    for key in ("age", "current_age", "client_age"):
        value = profile.get(key) if key in profile else payload.get(key)
        try:
            age = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if age > 0:
            context["age"] = age
            break
    for key in ("retirement_age", "life_expectancy"):
        value = profile.get(key)
        if value is not None:
            context[key] = value
    return context


def _investment_amount(fields: Dict[str, Any]) -> float:
    for key in ("amount", "investment_amount", "total_investment"):
        value = fields.get(key)
        try:
            amount = float(value)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            return amount
    return 100000.0


def _target_volatility(fields: Dict[str, Any]) -> float:
    for key in ("target_volatility", "target_volatility_pct"):
        value = fields.get(key)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 1.0:
            number /= 100.0
        return max(0.05, min(0.20, number))

    risk = str(fields.get("risk") or fields.get("risk_profile") or "").strip().lower()
    if risk in {"conservative", "low", "低风险"}:
        return 0.08
    if risk in {"aggressive", "growth", "high", "高风险"}:
        return 0.14
    return 0.10


def _active_risk_percentage(fields: Dict[str, Any]) -> float:
    value = fields.get("active_risk_percentage")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.30
    if number > 1.0:
        number /= 100.0
    return max(0.0, min(1.0, number))


# Map free-text client constraints to asset allocation model asset-class exclusions, so a stated
# constraint like "avoid crypto" is actually enforced in the allocation instead
# of silently ignored (which previously let Bitcoin into a "no crypto" plan).
_CONSTRAINT_EXCLUSIONS = (
    (("crypto", "bitcoin", "btc", "加密", "数字货币", "虚拟货币"), "Bitcoin"),
)


def _excluded_asset_classes_from_fields(fields: Dict[str, Any]) -> list:
    excluded: list = []
    seen: set = set()

    def _add(name: str) -> None:
        cleaned = str(name).strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            excluded.append(cleaned)

    explicit = fields.get("excluded_asset_classes")
    if isinstance(explicit, list):
        for item in explicit:
            _add(item)

    constraints = fields.get("constraints")
    constraint_items = constraints if isinstance(constraints, list) else [constraints]
    blob = " ".join(str(item) for item in constraint_items if item).lower()
    for keywords, asset_class in _CONSTRAINT_EXCLUSIONS:
        if any(keyword in blob for keyword in keywords):
            _add(asset_class)
    return excluded


__all__ = ["RealModelAgentProvider"]
