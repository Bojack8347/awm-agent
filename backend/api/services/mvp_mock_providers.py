"""Mock provider implementations for the AWM MVP UI backend.

These classes intentionally sit behind provider-shaped boundaries. The UI and
business services should call the provider interface, not inline fixture data,
so KYC, account linking, market data, and brokerage can later be replaced by
real integrations without changing route contracts.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List

from api.services.cashflow_projection_mapper import build_projection_artifact_from_cashflow_result


class MockKycProvider:
    """Deterministic identity verification used by MVP onboarding."""

    def verify(self, identity: Dict[str, Any]) -> Dict[str, Any]:
        last4 = str(identity.get("tax_id_last4") or identity.get("ssn_last4") or "")
        status = "failed" if last4 == "0000" else "pending" if last4 == "1111" else "verified"
        return {
            "provider": "mock_kyc",
            "status": status,
            "check_id": _stable_id("kyc", identity.get("legal_name"), last4),
            "required_followups": [] if status == "verified" else ["review_identity_details"],
        }


class MockExternalAccountProvider:
    """Fake account-linking provider for banks, brokerage, insurance, and uploads."""

    def connect(self, connection_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        institution = payload.get("institution") or {
            "brokerage": "AWM Mock Brokerage",
            "bank": "AWM Mock Bank",
            "insurance": "AWM Mock Insurance",
            "manual_upload": "Manual Upload",
        }.get(connection_type, "AWM Mock Institution")
        return {
            "provider": "mock_external_account",
            "environment": "simulator",
            "permission_status": "granted",
            "status": "connected_no_holdings",
            "connection_status": "connected_no_holdings",
            "data_status": "no_holdings_returned",
            "provenance": "simulator_mock",
            "institution": institution,
            "external_account_id": _stable_id("acct", connection_type, institution),
            "holdings": [],
        }


class MockBrokerProvider:
    """Broker-shaped provider that creates filled orders without real trading."""

    def submit_orders(self, proposal: Dict[str, Any]) -> Dict[str, Any]:
        securities = _proposal_securities(proposal)
        orders = []
        for security in securities:
            orders.append({
                "order_id": _stable_id("ord", proposal.get("id"), security.get("symbol")),
                "symbol": security.get("symbol"),
                "side": "buy",
                "status": "filled",
                "notional": security.get("notional") or security.get("target_value") or 10000,
                "filled_quantity": security.get("quantity") or 10,
                "average_price": security.get("price") or 100,
            })
        return {
            "provider": "mock_broker",
            "status": "filled",
            "orders": orders,
            "fills": [
                {
                    "fill_id": _stable_id("fill", order["order_id"]),
                    "order_id": order["order_id"],
                    "symbol": order["symbol"],
                    "quantity": order["filled_quantity"],
                    "price": order["average_price"],
                }
                for order in orders
            ],
            "settlement_status": "settled",
        }


class MockMarketDataProvider:
    """Market-data provider for monitoring report fixtures."""

    def market_drop_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        symbol = str(payload.get("symbol") or "QQQ").upper()
        drop_pct = float(payload.get("drop_pct") or -7.8)
        return {
            "provider": "mock_market_data",
            "symbol": symbol,
            "event_type": "market_drop",
            "headline": f"{symbol} moved {drop_pct:.1f}% in the latest mock scenario",
            "drop_pct": drop_pct,
            "price_points": [
                {"label": "T-5", "value": 100.0},
                {"label": "T-4", "value": 98.8},
                {"label": "T-3", "value": 97.9},
                {"label": "T-2", "value": 95.1},
                {"label": "T-1", "value": 93.6},
                {"label": "Now", "value": round(100.0 + drop_pct, 2)},
            ],
        }


class DeterministicAgentProvider:
    """Deterministic artifact and Q&A generator for UI acceptance."""

    PROPOSAL_SECTIONS = [
        "hero",
        "scope",
        "recommended_securities",
        "allocation",
        "portfolio_analytics",
        "fixed_income",
        "stress_tests",
        "historical_performance",
        "simulated_projection",
        "governance",
        "disclosure",
    ]
    REPORT_SECTIONS = ["hero", "performance", "risk_stats", "narrative", "recommendation"]
    UPDATE_SECTIONS = ["signals", "context", "trade_recommendations", "allocation_shift", "expected_impact"]

    def projection_artifact(self) -> Dict[str, Any]:
        years = [2026, 2027, 2028, 2029, 2030, 2031]
        result = {
            "metadata": {"version": "mvp-deterministic-cashflow-shape"},
            "success_rate": 0.91,
            "years": years,
            "percentile_bands": {
                "Net Worth": {
                    "Bottom 10%": [420000, 445000, 471000, 498000, 527000, 558000],
                    "Median": [420000, 468000, 519000, 573000, 631000, 694000],
                    "Top 10%": [420000, 497000, 581000, 672000, 771000, 878000],
                },
                "Total Cash Inflows": {"Median": [0, 174000, 179220, 184597, 190135, 195839]},
                "Total Cash Outflows": {"Median": [0, 110400, 114264, 118263, 122402, 126686]},
                "Net Cashflow": {"Median": [0, 63600, 64956, 66334, 67733, 69153]},
                "Income": {"Median": [0, 174000, 179220, 184597, 190135, 195839]},
                "Spending": {"Median": [0, 72000, 74160, 76385, 78677, 81037]},
                "Taxes": {"Median": [0, 38400, 39904, 41478, 43115, 44814]},
                "Housing": {"Median": [0, 30000, 30900, 31827, 32782, 33765]},
                "Base Living Spending": {"Median": [0, 42000, 43260, 44558, 45895, 47272]},
                "Bank Balance": {"Median": [130000, 166600, 204956, 245290, 287663, 332141]},
                "Brokerage Balance": {"Median": [320000, 352000, 388080, 428445, 473348, 523069]},
                "401k Balance": {"Median": [280000, 310000, 342500, 377625, 415556, 456483]},
                "Home Value": {"Median": [620000, 638600, 657758, 677491, 697816, 718751]},
                "Total Assets": {"Median": [1350000, 1467200, 1593294, 1728851, 1874383, 2030444]},
                "Mortgage Balance": {"Median": [310000, 302000, 293800, 285395, 276779, 267948]},
                "Total Liabilities": {"Median": [310000, 302000, 293800, 285395, 276779, 267948]},
                "Mortgage Payments": {"Median": [0, 22800, 22800, 22800, 22800, 22800]},
                "Mortgage Interest Paid": {"Median": [0, 12710, 12382, 12038, 11648, 11249]},
            },
        }
        return build_projection_artifact_from_cashflow_result(result, title="Projection Analytics")

    def proposal_artifact(self, journey: Dict[str, Any] | None = None) -> Dict[str, Any]:
        fields = (journey or {}).get("collected_fields") or {}
        amount = fields.get("amount") or 100000
        horizon = fields.get("horizon") or "5 years"
        source = (journey or {}).get("generation_source") or "mock_asset_allocation_model"
        sections = [
            _section("hero", "Core Growth Proposal", {
                "headline": "Core Growth Proposal",
                "summary": f"Invest ${amount:,.0f} over {horizon} with a balanced ETF portfolio.",
                "status": "ready_for_review",
            }),
            _section("scope", "Mandate", {
                "objective": "Long-term taxable investing with controlled drawdown risk.",
                "constraints": fields.get("constraints") or ["Maintain liquidity reserve", "Avoid leverage"],
            }),
            _section("recommended_securities", "Recommended Securities", {
                "securities": [
                    {"symbol": "VTI", "name": "Vanguard Total Stock Market ETF", "weight": 0.48, "notional": amount * 0.48},
                    {"symbol": "VXUS", "name": "Vanguard Total International Stock ETF", "weight": 0.22, "notional": amount * 0.22},
                    {"symbol": "BND", "name": "Vanguard Total Bond Market ETF", "weight": 0.25, "notional": amount * 0.25},
                    {"symbol": "SGOV", "name": "iShares 0-3 Month Treasury Bond ETF", "weight": 0.05, "notional": amount * 0.05},
                ],
            }),
            _section("allocation", "Allocation", {
                "chart": [
                    {"label": "US equity", "value": 48},
                    {"label": "International equity", "value": 22},
                    {"label": "Core bonds", "value": 25},
                    {"label": "Treasury bills", "value": 5},
                ],
            }),
            _section("portfolio_analytics", "Portfolio Analytics", {
                "expected_return": 0.064,
                "expected_volatility": 0.118,
                "max_drawdown_mock": -0.182,
                "liquidity_score": "high",
            }),
            _section("fixed_income", "Fixed Income Sleeve", {
                "duration_years": 6.2,
                "yield_to_maturity": 0.043,
                "role": "Diversify equity risk and fund rebalance opportunities.",
            }),
            _section("stress_tests", "Stress Tests", {
                "scenarios": [
                    {"name": "Equity shock", "impact": -0.13},
                    {"name": "Rate spike", "impact": -0.045},
                    {"name": "USD rally", "impact": -0.025},
                ],
            }),
            _section("historical_performance", "Historical Performance", {
                "annualized_return_mock": 0.071,
                "best_year_mock": 0.224,
                "worst_year_mock": -0.158,
            }),
            _section("simulated_projection", "Simulated Projection", {
                "base_case_10y": round(float(amount) * 1.84, 2),
                "downside_10y": round(float(amount) * 1.21, 2),
                "upside_10y": round(float(amount) * 2.43, 2),
            }),
            _section("governance", "Governance", {
                "rebalance_frequency": "quarterly",
                "review_triggers": ["allocation drift > 5%", "drawdown > 10%", "major goal change"],
            }),
            _section("disclosure", "Disclosure", {
                "copy": "MVP data is deterministic and for UI acceptance only. This is not investment advice.",
            }),
        ]
        artifact = _artifact("proposal", "Core Growth Proposal", sections)
        artifact["generation_source"] = source
        artifact["engine_run"] = _engine_run("proposal", source, fields, {
            "amount": amount,
            "horizon": horizon,
            "section_ids": artifact["section_ids"],
        })
        artifact["callback"] = {
            "event_type": "proposal.ready",
            "notification_type": "business_card",
            "title": "Investment proposal is ready",
            "body": "AWM finished the mock asset allocation model allocation and prepared the proposal for review.",
        }
        return artifact

    def monitoring_report_artifact(self, policy: Dict[str, Any], market_event: Dict[str, Any]) -> Dict[str, Any]:
        breached = abs(market_event["drop_pct"]) >= 8
        sections = [
            _section("hero", "Market Drop Report", {
                "headline": market_event["headline"],
                "policy_id": policy.get("id"),
                "severity": "action_required" if breached else "attention",
            }),
            _section("performance", "Performance", {
                "policy_return_today": market_event["drop_pct"] * 0.62,
                "benchmark_return_today": market_event["drop_pct"],
                "series": market_event["price_points"],
            }),
            _section("risk_stats", "Risk Stats", {
                "current_drawdown": abs(market_event["drop_pct"]) / 100,
                "guardrail": 0.1,
                "breached": breached,
            }),
            _section("narrative", "Narrative", {
                "copy": "The mock portfolio is cushioned by bonds and cash, but equity exposure still drove a visible decline.",
            }),
            _section("recommendation", "Recommendation", {
                "action": "review_rebalance",
                "copy": "Consider modestly restoring target weights if the signal persists.",
            }),
        ]
        artifact = _artifact("monitoring_report", "Market Drop Report", sections)
        artifact["risk_alert"] = {
            "severity": "action_required" if breached else "watch",
            "trigger": "drawdown_near_guardrail",
            "message": "AWM detected a policy risk event and prepared a monitoring report.",
            "requires_policy_update": True,
        }
        return artifact

    def policy_update_artifact(self, policy: Dict[str, Any], report: Dict[str, Any]) -> Dict[str, Any]:
        sections = [
            _section("signals", "Signals", {"signals": ["drawdown_near_guardrail", "equity_drift"]}),
            _section("context", "Context", {"report_id": report.get("id"), "policy_id": policy.get("id")}),
            _section("trade_recommendations", "Trade Recommendations", {
                "trades": [
                    {"symbol": "BND", "side": "buy", "notional": 2500},
                    {"symbol": "VTI", "side": "buy", "notional": 1500},
                ],
            }),
            _section("allocation_shift", "Allocation Shift", {
                "before": [{"label": "Equity", "value": 67}, {"label": "Fixed income", "value": 28}, {"label": "Cash", "value": 5}],
                "after": [{"label": "Equity", "value": 70}, {"label": "Fixed income", "value": 25}, {"label": "Cash", "value": 5}],
            }),
            _section("expected_impact", "Expected Impact", {
                "tracking_error_change": 0.004,
                "downside_cushion_change": -0.006,
                "rationale": "Restore strategic allocation while the portfolio remains inside guardrails.",
            }),
        ]
        artifact = _artifact("policy_update", "Policy Update Proposal", sections)
        artifact["callback"] = {
            "event_type": "policy_update.ready",
            "notification_type": "decision_prompt",
            "title": "Policy update available",
            "body": "AWM prepared a revised allocation after the market event.",
            "actions": ["apply", "revise", "discuss"],
        }
        return artifact

    def revised_proposal_artifact(self, policy: Dict[str, Any], update: Dict[str, Any], instructions: Dict[str, Any]) -> Dict[str, Any]:
        base = self.proposal_artifact({
            "id": policy.get("related_id") or policy.get("id"),
            "generation_source": "mock_asset_allocation_model_revision",
            "collected_fields": {
                "amount": 150000,
                "horizon": "15 years",
                "constraints": [
                    "Keep emergency reserve untouched",
                    "Reduce growth exposure after risk alert",
                    str(instructions.get("instruction") or "Client requested revision"),
                ],
            },
        })
        base["title"] = "Revised Core Growth Proposal"
        base["revision"] = {
            "source_policy_id": policy.get("id"),
            "source_update_id": update.get("id"),
            "reason": instructions.get("instruction") or "Client requested a revised proposal.",
            "status": "ready_for_review",
        }
        for section in base["sections"]:
            if section["section_id"] == "hero":
                section["title"] = "Revised Proposal"
                section["payload"]["headline"] = "Revised Core Growth Proposal"
                section["payload"]["summary"] = "Updated after the monitoring alert and client revision request."
            if section["section_id"] == "allocation":
                section["payload"]["chart"] = [
                    {"label": "US equity", "value": 44},
                    {"label": "International equity", "value": 20},
                    {"label": "Core bonds", "value": 31},
                    {"label": "Treasury bills", "value": 5},
                ]
        base["section_ids"] = [section["section_id"] for section in base["sections"]]
        return base

    def answer_question(self, artifact: Dict[str, Any], section_id: str, question: str) -> Dict[str, Any]:
        section = _find_section(artifact, section_id)
        title = section.get("title") if section else section_id
        return {
            "answer": f"For '{title}', the MVP answer is deterministic: {question.strip() or 'the section'} is explained by the section payload and current policy context.",
            "citations": [{"artifact_id": artifact.get("id"), "section_id": section_id}],
        }


def _section(section_id: str, title: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"section_id": section_id, "title": title, "payload": payload}


def _artifact(artifact_type: str, title: str, sections: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "artifact_type": artifact_type,
        "title": title,
        "schema_version": "mvp.v1",
        "sections": sections,
        "section_ids": [section["section_id"] for section in sections],
    }


def _engine_run(domain: str, engine_name: str, inputs: Dict[str, Any], outputs: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": _stable_id("eng", domain, engine_name, inputs, outputs),
        "domain": domain,
        "engine_name": engine_name,
        "engine_version": "mvp-ui.v1",
        "status": "succeeded",
        "inputs": inputs,
        "outputs": outputs,
    }


def _find_section(artifact: Dict[str, Any], section_id: str) -> Dict[str, Any] | None:
    for section in artifact.get("sections") or []:
        if section.get("section_id") == section_id:
            return section
    return None


def _proposal_securities(proposal: Dict[str, Any]) -> List[Dict[str, Any]]:
    for section in proposal.get("sections") or []:
        if section.get("section_id") == "recommended_securities":
            payload = section.get("payload") or {}
            securities = payload.get("securities")
            if isinstance(securities, list):
                return securities
    return [{"symbol": "VTI", "notional": 10000, "quantity": 10, "price": 100}]


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = "|".join(str(part or "") for part in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"
