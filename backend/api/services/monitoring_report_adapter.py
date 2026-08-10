"""Build monitoring report artifacts from policy/proposal source data."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


REPORT_SECTION_IDS = ["hero", "performance", "risk_stats", "narrative", "recommendation"]


def monitoring_report_from_policy_sources(
    *,
    policy: Dict[str, Any],
    proposal: Optional[Dict[str, Any]],
    holdings: List[Dict[str, Any]],
    market_event: Dict[str, Any],
) -> Dict[str, Any]:
    """Create a section-stable monitoring report from available source data."""
    proposal_payload = proposal.get("payload") if isinstance(proposal, dict) else {}
    proposal_payload = proposal_payload if isinstance(proposal_payload, dict) else {}
    analytics = _section_payload(proposal_payload, "portfolio_analytics")
    allocation = _section_payload(proposal_payload, "allocation")
    securities = _section_payload(proposal_payload, "recommended_securities").get("securities") or []

    drop_pct = _number(market_event.get("drop_pct")) or 0.0
    exposure = _growth_exposure_from_sources(allocation, securities, holdings)
    policy_return_today = round((drop_pct / 100.0) * exposure, 4)
    expected_vol = _number(analytics.get("expected_volatility"))
    expected_return = _number(analytics.get("expected_return"))
    tracking_error = _number(analytics.get("tracking_error"))
    guardrail = 0.10
    current_drawdown = abs(policy_return_today)
    breached = current_drawdown >= guardrail * 0.75 or abs(drop_pct) >= 8.0
    title = "Market Drop Report"

    sections = [
        _section("hero", title, {
            "headline": market_event.get("headline") or f"{market_event.get('symbol', 'Market')} moved {drop_pct:.1f}%",
            "policy_id": policy.get("id"),
            "proposal_id": (proposal or {}).get("id"),
            "severity": "action_required" if breached else "watch",
            "summary": (
                "Market dropped sharply, but the policy drawdown remains inside guardrails."
                if not breached
                else "Market move is close to the policy guardrail and needs review."
            ),
            "market_move": drop_pct / 100.0,
            "policy_impact": policy_return_today,
        }),
        _section("performance", "Performance Impact", {
            "policy_return_today": policy_return_today,
            "benchmark_return_today": drop_pct / 100.0,
            "growth_exposure": exposure,
            "series": market_event.get("price_points") or [],
            "source": "policy_holdings_and_market_event",
        }),
        _section("risk_stats", "Risk Stats", {
            "current_drawdown": current_drawdown,
            "guardrail": guardrail,
            "breached": breached,
            "expected_volatility": expected_vol,
            "expected_return": expected_return,
            "tracking_error": tracking_error,
            "holding_count": len(holdings),
            "source": "proposal_artifact_and_policy_holdings",
        }),
        _section("narrative", "What Happened", {
            "copy": _narrative(drop_pct, policy_return_today, exposure, breached),
            "source": "derived_monitoring_report",
        }),
        _section("recommendation", "What AWM Recommends", {
            "action": "review_rebalance" if breached else "keep_watching",
            "copy": (
                "Review the update proposal and consider trimming risk if the signal persists."
                if breached
                else "Keep monitoring. The current policy impact is still inside the guardrail."
            ),
            "requires_policy_update": True,
        }),
    ]
    artifact = {
        "artifact_type": "monitoring_report",
        "title": title,
        "schema_version": "mvp.v1",
        "sections": sections,
        "section_ids": [section["section_id"] for section in sections],
        "generation_source": "policy_source_monitoring_adapter",
        "source_refs": {
            "policy_id": policy.get("id"),
            "proposal_id": (proposal or {}).get("id"),
            "market_event_provider": market_event.get("provider"),
        },
        "risk_alert": {
            "severity": "action_required" if breached else "watch",
            "trigger": "drawdown_near_guardrail" if breached else "market_move_inside_guardrail",
            "message": "AWM generated a monitoring report from policy, proposal, holdings, and market event data.",
            "requires_policy_update": True,
        },
    }
    return artifact


def _growth_exposure_from_sources(
    allocation: Dict[str, Any],
    securities: Any,
    holdings: List[Dict[str, Any]],
) -> float:
    chart = allocation.get("chart")
    if isinstance(chart, list) and chart:
        growth_pct = 0.0
        total_pct = 0.0
        for item in chart:
            if not isinstance(item, dict):
                continue
            value = _number(item.get("value")) or 0.0
            total_pct += value
            if _is_growth_label(item.get("label")):
                growth_pct += value
        if total_pct > 0:
            return round(max(0.0, min(1.0, growth_pct / total_pct)), 4)

    security_rows = securities if isinstance(securities, list) else []
    if security_rows:
        return _weighted_growth_exposure(security_rows)

    holding_payloads = [
        holding.get("payload") for holding in holdings if isinstance(holding.get("payload"), dict)
    ]
    if holding_payloads:
        return _weighted_growth_exposure(holding_payloads)
    return 0.62


def _weighted_growth_exposure(rows: List[Dict[str, Any]]) -> float:
    growth = 0.0
    total = 0.0
    for row in rows:
        weight = _number(row.get("weight"))
        if weight is None:
            weight = _number(row.get("notional")) or _number(row.get("market_value")) or _number(row.get("amount"))
        if weight is None or weight <= 0:
            continue
        total += weight
        label = row.get("asset_class") or row.get("symbol") or row.get("ticker") or row.get("name")
        if _is_growth_label(label):
            growth += weight
    if total <= 0:
        return 0.62
    return round(max(0.0, min(1.0, growth / total)), 4)


def _is_growth_label(label: Any) -> bool:
    text = str(label or "").lower()
    if any(token in text for token in ("bond", "treasury", "cash", "debt", "fixed", "bnd", "sgov")):
        return False
    return any(token in text for token in ("equity", "stock", "growth", "vti", "voo", "vxus", "qqq"))


def _narrative(drop_pct: float, policy_return: float, exposure: float, breached: bool) -> str:
    market = f"{drop_pct:.1f}%"
    policy = f"{policy_return * 100:.1f}%"
    exposure_pct = f"{exposure * 100:.0f}%"
    if breached:
        return (
            f"The market move was {market}. With roughly {exposure_pct} growth exposure, "
            f"the policy impact is estimated at {policy}, close enough to guardrails to review."
        )
    return (
        f"The market move was {market}. With roughly {exposure_pct} growth exposure, "
        f"the policy impact is estimated at {policy}, still inside current guardrails."
    )


def _section_payload(artifact_payload: Dict[str, Any], section_id: str) -> Dict[str, Any]:
    sections = artifact_payload.get("sections")
    if not isinstance(sections, list):
        return {}
    for section in sections:
        if isinstance(section, dict) and section.get("section_id") == section_id:
            payload = section.get("payload")
            return payload if isinstance(payload, dict) else {}
    return {}


def _number(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _section(section_id: str, title: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"section_id": section_id, "title": title, "payload": payload}


__all__ = ["REPORT_SECTION_IDS", "monitoring_report_from_policy_sources"]
