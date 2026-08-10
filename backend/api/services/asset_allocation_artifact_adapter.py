"""Adapters from real asset allocation model engine output to AWM UI artifacts.

The MVP UI can run on deterministic mocks, but the production boundary should
be explicit: asset allocation model returns portfolio-construction analytics, while AWM screens
render sectioned advisory artifacts. This module keeps that transformation in
one place so mock providers and real providers do not blur together.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


PROPOSAL_SECTION_IDS = [
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


def proposal_artifact_from_asset_allocation(
    asset_allocation_result: Dict[str, Any],
    *,
    journey: Optional[Dict[str, Any]] = None,
    cashflow_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a stable AWM proposal artifact from a real asset allocation model response."""
    asset_allocation_result = normalize_asset_allocation_result(asset_allocation_result)
    journey = journey or {}
    fields = journey.get("collected_fields") if isinstance(journey.get("collected_fields"), dict) else {}
    total_investment = _number(asset_allocation_result.get("total_investment")) or _number(fields.get("amount")) or 0.0
    expected_return = _pct_to_decimal(asset_allocation_result.get("portfolio_expected_return_pct"))
    expected_volatility = _pct_to_decimal(asset_allocation_result.get("portfolio_expected_volatility_pct"))
    summary = asset_allocation_result.get("portfolio_summary") if isinstance(asset_allocation_result.get("portfolio_summary"), dict) else {}
    layers = asset_allocation_result.get("layers") if isinstance(asset_allocation_result.get("layers"), dict) else {}
    layer1 = layers.get("layer1") if isinstance(layers.get("layer1"), dict) else {}
    layer2 = layers.get("layer2") if isinstance(layers.get("layer2"), dict) else {}

    securities = _map_securities(asset_allocation_result, total_investment)
    allocation_chart = _map_asset_allocation(asset_allocation_result)
    fixed_income_weight = _fixed_income_weight(allocation_chart)
    money_pool = _money_pool_payload(journey, fields, total_investment)
    horizon_years = max(1, int(round(_number(money_pool.get("horizon_years")) or 10)))
    projection = _projection_payload(
        total_investment,
        expected_return,
        expected_volatility,
        cashflow_result,
        horizon_years=horizon_years,
    )
    scope_of_purpose = _scope_of_purpose(fields.get("objective"), horizon_years)
    policy_payload = {
        "title": "Core Growth Proposal",
        "money_pool": money_pool,
        "policy": {
            "title": "Core Growth Proposal",
            "capital_required": total_investment,
            "scope_of_purpose": scope_of_purpose,
            "horizon_years": money_pool.get("horizon_years"),
            "expected_return": expected_return,
            "expected_volatility": expected_volatility,
            "target_allocation": {
                row["label"]: round((row["value"] or 0.0) / 100.0, 8)
                for row in allocation_chart
            },
            "recommended_securities": securities,
            "risk_management_policy": _risk_management_rules(fields, layer2),
        },
        "portfolio_analytics": {
            "expected_return": expected_return,
            "expected_volatility": expected_volatility,
            "source": "asset_allocation_model",
        },
        "recommended_securities": securities,
    }
    mobile_section_5b = _mobile_section_5b_from_policy_payload(policy_payload)
    mobile_section_5b["simulatedProjection"] = _mobile_simulated_projection(
        projection,
        total_investment=total_investment,
        expected_return=expected_return,
        expected_volatility=expected_volatility,
        horizon_years=horizon_years,
    )

    sections = [
        _section("hero", "Core Growth Proposal", {
            "headline": "Core Growth Proposal",
            "summary": _hero_summary(total_investment, expected_return, expected_volatility),
            "status": "ready_for_review" if asset_allocation_result.get("success") else "engine_error",
            "engine": "asset_allocation_model",
            "risk_profile": layer1.get("profile_name"),
        }),
        _section("scope", "Mandate", {
            "objective": fields.get("objective") or "Portfolio construction using the asset allocation model strategic allocation engine.",
            "constraints": fields.get("constraints") or [],
            "excluded_asset_classes": asset_allocation_result.get("excluded_asset_classes") or [],
            "target_volatility": layer1.get("target_vol"),
        }),
        _section("recommended_securities", "Recommended Securities", {"securities": securities}),
        _section("allocation", "Allocation", {"chart": allocation_chart}),
        _section("portfolio_analytics", "Portfolio Analytics", {
            "expected_return": expected_return,
            "expected_volatility": expected_volatility,
            "tracking_error": _number(summary.get("total_tracking_error")),
            "manager_count": summary.get("manager_count"),
            "asset_class_count": summary.get("asset_class_count"),
            "achieved_volatility_layer2": _number(summary.get("achieved_volatility_layer2")),
            "source": "asset_allocation_model.portfolio_summary",
        }),
        _section("fixed_income", "Fixed Income Sleeve", {
            "weight": fixed_income_weight,
            "estimated_notional": round(total_investment * fixed_income_weight, 2) if total_investment else 0.0,
            "included_asset_classes": [
                row["label"] for row in allocation_chart if _is_fixed_income_label(row["label"])
            ],
            "source": "asset_allocation_model.investment_allocations.by_asset_class",
        }),
        _section("stress_tests", "Stress Tests", {
            "status": "not_provided_by_asset_allocation_model",
            "required_provider": "risk_model_or_market_scenario_engine",
            "available_inputs": ["expected_volatility", "asset_class_weights", "security_weights"],
        }),
        _section("historical_performance", "Historical Performance", {
            "status": "not_provided_by_asset_allocation_model",
            "required_provider": "market_data_total_return_history",
        }),
        _section("simulated_projection", "Simulated Projection", projection),
        _section("governance", "Governance", {
            "rebalance_frequency": "quarterly",
            "review_triggers": ["allocation drift > 5%", "drawdown > 10%", "major goal change"],
            "active_risk_budget": layer2.get("active_risk_budget"),
            "active_risk_pct": layer2.get("active_risk_pct"),
            "passive_risk_pct": layer2.get("passive_risk_pct"),
            "risk_budget_shares": layer2.get("risk_budget_shares") or {},
        }),
        _section("disclosure", "Disclosure", {
            "copy": (
                "Generated from the asset allocation model portfolio construction engine. Stress tests, historical "
                "returns, suitability review, trading, and monitoring signals require separate providers."
            ),
            "source": "real_asset_allocation_model",
        }),
    ]

    artifact = {
        "artifact_type": "proposal",
        "title": "Core Growth Proposal",
        "schema_version": "mvp.v1",
        "money_pool": money_pool,
        "policy": policy_payload["policy"],
        "portfolio_analytics": policy_payload["portfolio_analytics"],
        "recommended_securities": securities,
        "mobile_section_5b": mobile_section_5b,
        "sections": sections,
        "section_ids": [section["section_id"] for section in sections],
        "generation_source": "real_asset_allocation_model",
        "engine_run": {
            "engine_name": "asset_allocation_model",
            "engine_version": "asset_allocation.api.v1",
            "status": "succeeded" if asset_allocation_result.get("success") else "failed",
            "inputs": {
                "journey_id": journey.get("id"),
                "target_volatility": layer1.get("target_vol"),
                "total_investment": total_investment,
                "excluded_asset_classes": asset_allocation_result.get("excluded_asset_classes") or [],
            },
            "outputs": {
                "expected_return": expected_return,
                "expected_volatility": expected_volatility,
                "security_count": len(securities),
                "section_ids": PROPOSAL_SECTION_IDS,
            },
        },
        "callback": {
            "event_type": "proposal.ready",
            "notification_type": "business_card",
            "title": "Investment proposal is ready",
            "body": "AWM finished the asset allocation model allocation and prepared the proposal for review.",
        },
    }
    return artifact


def proposal_artifact_from_advisor_policy(
    advisor_artifact: Dict[str, Any],
    *,
    allocation_result: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Map a ready Investment Solution artifact to the APP proposal contract."""
    if not isinstance(advisor_artifact, dict) or advisor_artifact.get("artifact_type") != "investment_solution_policy":
        return None
    payload = advisor_artifact.get("payload") if isinstance(advisor_artifact.get("payload"), dict) else {}
    policy = payload.get("policy") if isinstance(payload.get("policy"), dict) else {}
    money_pool = payload.get("money_pool") if isinstance(payload.get("money_pool"), dict) else {}
    engine_run = payload.get("engine_run") if isinstance(payload.get("engine_run"), dict) else {}
    missing_data = payload.get("missing_data") if isinstance(payload.get("missing_data"), list) else []
    if payload.get("artifact_status") != "ready" or missing_data:
        return None
    source_assessment = payload.get("source_assessment") if isinstance(payload.get("source_assessment"), dict) else {}

    # The SDK specialist response is intentionally compact and may contain an
    # APP writeback stub.  Prefer the exact successful deterministic-tool
    # result from this turn whenever it is available; it is the authoritative
    # source for securities, weights, expected metrics, and model provenance.
    allocation_full = (
        allocation_result.get("full_result")
        if isinstance(allocation_result, dict)
        and isinstance(allocation_result.get("full_result"), dict)
        else {}
    )
    allocation_succeeded = bool(
        allocation_full
        and allocation_result
        and allocation_result.get("ok") is True
        and allocation_result.get("execution_ok") is True
    )
    if allocation_succeeded:
        engine_payload = dict(allocation_full)
        engine_payload["success"] = True
        artifact = proposal_artifact_from_asset_allocation(
            engine_payload,
            journey={
                "id": payload.get("id"),
                "money_pool_id": source_assessment.get("money_pool_id") or money_pool.get("id"),
                "money_pool": money_pool,
                "collected_fields": {
                    "amount": money_pool.get("amount") or allocation_full.get("total_investment"),
                    "objective": (
                        policy.get("scope_of_purpose")
                        or (payload.get("objective") or {}).get("objective")
                        if isinstance(payload.get("objective"), dict)
                        else policy.get("scope_of_purpose")
                    ),
                    "constraints": money_pool.get("constraints") or [],
                    "horizon_years": money_pool.get("horizon_years"),
                },
            },
        )
        artifact["title"] = str(policy.get("title") or artifact["title"])
        if isinstance(artifact.get("policy"), dict):
            artifact["policy"]["title"] = artifact["title"]
            if policy.get("scope_of_purpose"):
                artifact["policy"]["scope_of_purpose"] = policy["scope_of_purpose"]
                mobile = artifact.get("mobile_section_5b")
                if isinstance(mobile, dict):
                    mobile["scopeAndPurpose"] = policy["scope_of_purpose"]
        target_check = (
            (allocation_full.get("constraint_checks") or {}).get("target_volatility")
            if isinstance(allocation_full.get("constraint_checks"), dict)
            else {}
        )
        signed_target_volatility = (
            target_check.get("target_annual_decimal")
            if isinstance(target_check, dict)
            else None
        )
        if signed_target_volatility is not None:
            artifact["engine_run"]["inputs"]["target_volatility"] = signed_target_volatility
            for section in artifact.get("sections") or []:
                if (
                    isinstance(section, dict)
                    and section.get("section_id") == "scope"
                    and isinstance(section.get("payload"), dict)
                ):
                    section["payload"]["target_volatility"] = signed_target_volatility
        artifact["source_advisor_artifact_id"] = payload.get("id")
        artifact["source_advisor_artifact_type"] = advisor_artifact.get("artifact_type")
        artifact["investment_consultation_id"] = (
            payload.get("investment_consultation_id")
            or source_assessment.get("investment_consultation_id")
        )
        artifact["assessment_id"] = source_assessment.get("assessment_id")
        artifact["assessment_version"] = source_assessment.get("assessment_version")
        artifact["money_pool_id"] = source_assessment.get("money_pool_id") or money_pool.get("id")
        artifact["source_assessment"] = {
            **source_assessment,
            **(
                allocation_result.get("source_assessment")
                if isinstance(allocation_result.get("source_assessment"), dict)
                else {}
            ),
            "status": "signed_off",
        }
        artifact["source_allocation_analysis_id"] = allocation_result.get("analysis_id")
        artifact["generation_source"] = "advisor_investment_solution+asset_allocation_model"
        artifact["upstream_engine_run"] = engine_run
        return artifact

    proposal_writeback = payload.get("proposal_writeback") if isinstance(payload.get("proposal_writeback"), dict) else {}
    app_artifact = proposal_writeback.get("artifact") if isinstance(proposal_writeback.get("artifact"), dict) else {}
    app_payload = app_artifact.get("payload") if isinstance(app_artifact.get("payload"), dict) else {}
    if (
        app_payload.get("artifact_type") == "proposal"
        and isinstance(app_payload.get("sections"), list)
    ):
        artifact = dict(app_payload)
        artifact["source_advisor_artifact_id"] = payload.get("id") or app_artifact.get("id")
        artifact["source_advisor_artifact_type"] = advisor_artifact.get("artifact_type")
        artifact["investment_consultation_id"] = (
            payload.get("investment_consultation_id")
            or source_assessment.get("investment_consultation_id")
            or artifact.get("investment_consultation_id")
        )
        artifact["assessment_id"] = source_assessment.get("assessment_id") or artifact.get("assessment_id")
        artifact["assessment_version"] = source_assessment.get("assessment_version") or artifact.get("assessment_version")
        artifact["money_pool_id"] = source_assessment.get("money_pool_id") or artifact.get("money_pool_id")
        artifact["money_pool"] = (
            payload.get("money_pool")
            if isinstance(payload.get("money_pool"), dict)
            else artifact.get("money_pool")
        )
        artifact["generation_source"] = "advisor_investment_solution+asset_allocation_model"
        artifact["upstream_engine_run"] = engine_run
        return artifact

    allocation = policy.get("target_allocation") if isinstance(policy.get("target_allocation"), dict) else {}
    securities = policy.get("recommended_securities") if isinstance(policy.get("recommended_securities"), list) else []
    amount = _number(money_pool.get("amount")) or _number(engine_run.get("inputs", {}).get("total_investment")) or 0.0
    asset_allocation_result = {
        "success": engine_run.get("status") in {"ready", "succeeded", "success"},
        "total_investment": amount,
        "portfolio_expected_return_pct": policy.get("expected_return"),
        "portfolio_expected_volatility_pct": policy.get("expected_volatility"),
        "securities": securities,
        "investment_allocations": {
            "by_asset_class": {
                str(label): {"weight": weight, "amount": amount * float(weight or 0.0)}
                for label, weight in allocation.items()
                if isinstance(weight, (int, float))
            }
        },
    }
    artifact = proposal_artifact_from_asset_allocation(
        asset_allocation_result,
        journey={
            "id": payload.get("id"),
            "collected_fields": {
                "amount": amount,
                "objective": (payload.get("objective") or {}).get("objective")
                if isinstance(payload.get("objective"), dict)
                else None,
            },
        },
    )
    artifact["title"] = str(policy.get("title") or artifact["title"])
    artifact["money_pool"] = money_pool
    artifact["source_advisor_artifact_id"] = payload.get("id")
    artifact["source_advisor_artifact_type"] = advisor_artifact.get("artifact_type")
    artifact["investment_consultation_id"] = (
        payload.get("investment_consultation_id")
        or source_assessment.get("investment_consultation_id")
    )
    artifact["assessment_id"] = source_assessment.get("assessment_id")
    artifact["assessment_version"] = source_assessment.get("assessment_version")
    artifact["money_pool_id"] = source_assessment.get("money_pool_id") or money_pool.get("id")
    artifact["generation_source"] = "advisor_investment_solution+asset_allocation_model"
    artifact["upstream_engine_run"] = engine_run
    return artifact


def proposal_artifact_from_allocation_result(
    allocation_result: Dict[str, Any],
    *,
    client_state: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Materialize a proposal from one real NEO result and durable signed state.

    Agent selection has already happened before this adapter is called. This
    function performs no intent routing and no financial calculation. It only
    joins the successful tool result to its exact signed assessment and money
    pool, then maps those authoritative records to the APP proposal contract.
    """

    full_result = (
        allocation_result.get("full_result")
        if isinstance(allocation_result, dict)
        and isinstance(allocation_result.get("full_result"), dict)
        else {}
    )
    if not (
        allocation_result.get("tool") == "run_asset_allocation"
        and allocation_result.get("ok") is True
        and allocation_result.get("execution_ok") is True
        and full_result.get("success") is True
    ):
        return None

    source_ref = (
        allocation_result.get("source_assessment")
        if isinstance(allocation_result.get("source_assessment"), dict)
        else {}
    )
    assessment_id = str(source_ref.get("assessment_id") or "").strip()
    money_pool_id = str(source_ref.get("money_pool_id") or "").strip()
    if not assessment_id or not money_pool_id:
        return None

    assessment = _signed_assessment(
        client_state,
        assessment_id=assessment_id,
        money_pool_id=money_pool_id,
    )
    money_pool = _money_pool(
        client_state,
        money_pool_id=money_pool_id,
    )
    if assessment is None or money_pool is None:
        return None

    basis = (
        assessment.get("consultation_basis")
        if isinstance(assessment.get("consultation_basis"), dict)
        else assessment.get("basis")
        if isinstance(assessment.get("basis"), dict)
        else {}
    )
    label = str(
        money_pool.get("label")
        or assessment.get("pool_label")
        or basis.get("pool_label")
        or "Investment Policy"
    ).strip()
    purpose = str(
        money_pool.get("purpose")
        or money_pool.get("purpose_type")
        or basis.get("purpose")
        or "investment growth"
    ).strip()
    signed_ref = {
        **source_ref,
        "assessment_id": assessment_id,
        "assessment_version": (
            assessment.get("assessment_version")
            or source_ref.get("assessment_version")
        ),
        "investment_consultation_id": (
            assessment.get("investment_consultation_id")
            or basis.get("investment_consultation_id")
        ),
        "money_pool_id": money_pool_id,
        "signed_off_at": (
            assessment.get("signed_off_at")
            or source_ref.get("signed_off_at")
        ),
        "status": "signed_off",
    }
    advisor_artifact = {
        "artifact_type": "investment_solution_policy",
        "payload": {
            "id": f"proposal-{assessment_id}",
            "artifact_status": "ready",
            "artifact_type": "investment_policy_proposal",
            "schema_version": "investment_policy_proposal.v1",
            "missing_data": [],
            "source_assessment": signed_ref,
            "investment_consultation_id": signed_ref.get(
                "investment_consultation_id"
            ),
            "money_pool": {
                **money_pool,
                "id": money_pool_id,
                "label": label,
                "amount": (
                    money_pool.get("amount")
                    or basis.get("amount")
                    or full_result.get("total_investment")
                ),
                "horizon_years": (
                    money_pool.get("horizon_years")
                    or basis.get("horizon_years")
                ),
                "risk_tolerance": (
                    money_pool.get("risk_tolerance")
                    or basis.get("risk")
                    or basis.get("target_risk")
                ),
            },
            "policy": {
                "title": label,
                "scope_of_purpose": purpose,
            },
            "engine_run": {
                "analysis_id": allocation_result.get("analysis_id"),
                "status": "succeeded",
            },
        },
    }
    return proposal_artifact_from_advisor_policy(
        advisor_artifact,
        allocation_result=allocation_result,
    )


def _signed_assessment(
    client_state: Dict[str, Any],
    *,
    assessment_id: str,
    money_pool_id: str,
) -> Optional[Dict[str, Any]]:
    rows = (
        client_state.get("investment_assessments")
        if isinstance(client_state, dict)
        and isinstance(client_state.get("investment_assessments"), list)
        else []
    )
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("assessment_id") or row.get("id") or "").strip() != assessment_id:
            continue
        if str(row.get("money_pool_id") or "").strip() != money_pool_id:
            continue
        status = str(
            row.get("assessment_status") or row.get("status") or ""
        ).strip().lower()
        if row.get("signed_off") is True or status in {
            "signed_off",
            "approved",
            "confirmed",
        }:
            return row
    return None


def _money_pool(
    client_state: Dict[str, Any],
    *,
    money_pool_id: str,
) -> Optional[Dict[str, Any]]:
    rows = (
        client_state.get("money_pools")
        if isinstance(client_state, dict)
        and isinstance(client_state.get("money_pools"), list)
        else []
    )
    for row in rows:
        if (
            isinstance(row, dict)
            and str(row.get("id") or "").strip() == money_pool_id
        ):
            return row
    return None


def normalize_asset_allocation_result(asset_allocation_result: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize real asset-allocation-model output to AWM's proposal shape.

    The standalone model response is layered:
    `layers.layer1.selected_weights`, `layers.layer2.active_allocations`, and
    `layers.layer3.allocations_by_asset_class`. Older AWM code and tests use a
    flatter `securities` + `investment_allocations` contract. This function
    keeps both shapes compatible.
    """
    result = dict(asset_allocation_result or {})
    layers = result.get("layers") if isinstance(result.get("layers"), dict) else {}
    layer1 = layers.get("layer1") if isinstance(layers.get("layer1"), dict) else {}
    layer2 = layers.get("layer2") if isinstance(layers.get("layer2"), dict) else {}
    layer3 = layers.get("layer3") if isinstance(layers.get("layer3"), dict) else {}
    summary = result.get("portfolio_summary") if isinstance(result.get("portfolio_summary"), dict) else {}

    total_investment = _number(result.get("total_investment")) or _number(result.get("investment_amount")) or 0.0
    if total_investment:
        result["total_investment"] = total_investment

    expected_return = (
        result.get("portfolio_expected_return_pct")
        if result.get("portfolio_expected_return_pct") is not None
        else result.get("portfolio_expected_return_annual_decimal")
        if result.get("portfolio_expected_return_annual_decimal") is not None
        else summary.get("portfolio_expected_return")
        if summary.get("portfolio_expected_return") is not None
        else layer3.get("portfolio_expected_return")
    )
    expected_volatility = (
        result.get("portfolio_expected_volatility_pct")
        if result.get("portfolio_expected_volatility_pct") is not None
        else result.get("portfolio_expected_volatility_annual_decimal")
        if result.get("portfolio_expected_volatility_annual_decimal") is not None
        else summary.get("portfolio_expected_volatility")
        if summary.get("portfolio_expected_volatility") is not None
        else layer3.get("portfolio_expected_volatility")
    )
    if expected_return is not None:
        result["portfolio_expected_return_pct"] = expected_return
    if expected_volatility is not None:
        result["portfolio_expected_volatility_pct"] = expected_volatility

    if "investment_allocations" not in result:
        selected_weights = _numeric_mapping(layer1.get("selected_weights"))
        if selected_weights:
            result["investment_allocations"] = {
                "by_asset_class": {
                    label: {
                        "weight": weight,
                        "amount": round(total_investment * weight, 2) if total_investment else None,
                    }
                    for label, weight in selected_weights.items()
                }
            }

    if "investment_allocations" not in result:
        securities = result.get("securities") if isinstance(result.get("securities"), list) else []
        weights_by_asset_class: Dict[str, float] = {}
        for security in securities:
            if not isinstance(security, dict):
                continue
            asset_class = str(security.get("asset_class") or "").strip()
            weight = _number(security.get("weight"))
            if asset_class and weight is not None and weight > 0:
                weights_by_asset_class[asset_class] = weights_by_asset_class.get(asset_class, 0.0) + weight
        if weights_by_asset_class:
            result["investment_allocations"] = {
                "by_asset_class": {
                    label: {
                        "weight": round(weight, 8),
                        "amount": round(total_investment * weight, 2) if total_investment else None,
                    }
                    for label, weight in weights_by_asset_class.items()
                }
            }

    if not isinstance(result.get("securities"), list) or not result["securities"]:
        securities = _securities_from_layers(
            total_investment=total_investment,
            layer1=layer1,
            layer2=layer2,
            layer3=layer3,
        )
        if securities:
            result["securities"] = securities

    if isinstance(summary, dict):
        if "achieved_volatility_layer2" not in summary and layer2.get("achieved_volatility") is not None:
            summary["achieved_volatility_layer2"] = layer2.get("achieved_volatility")
        if "asset_class_count" not in summary:
            selected_weights = _numeric_mapping(layer1.get("selected_weights"))
            if selected_weights:
                summary["asset_class_count"] = len(selected_weights)
        if "manager_count" not in summary:
            allocations = layer3.get("allocations_by_asset_class")
            if isinstance(allocations, dict):
                summary["manager_count"] = sum(len(item) for item in allocations.values() if isinstance(item, dict))
        result["portfolio_summary"] = summary

    return result


def _map_securities(asset_allocation_result: Dict[str, Any], total_investment: float) -> List[Dict[str, Any]]:
    securities = asset_allocation_result.get("securities")
    if not isinstance(securities, list):
        return []
    mapped: List[Dict[str, Any]] = []
    for row in securities:
        if not isinstance(row, dict):
            continue
        weight = _number(row.get("weight")) or 0.0
        symbol = str(row.get("ticker") or row.get("symbol") or row.get("isin") or "").strip()
        if not symbol:
            continue
        notional = _number(row.get("amount"))
        if notional is None and total_investment > 0:
            notional = total_investment * weight
        raw_name = str(row.get("security_name") or row.get("name") or "").strip()
        display_name = raw_name if raw_name and raw_name.upper() != symbol.upper() else str(row.get("asset_class") or symbol)
        mapped.append({
            "symbol": symbol,
            "isin": row.get("isin"),
            "name": display_name,
            "asset_class": row.get("asset_class"),
            "management_style": row.get("security_type") or "passive",
            "weight": round(weight, 6),
            "notional": round(float(notional or 0.0), 2),
        })
    return sorted(mapped, key=lambda item: item["weight"], reverse=True)


def _securities_from_layers(
    *,
    total_investment: float,
    layer1: Dict[str, Any],
    layer2: Dict[str, Any],
    layer3: Dict[str, Any],
) -> List[Dict[str, Any]]:
    selected_weights = _numeric_mapping(layer1.get("selected_weights"))
    active_allocations = _numeric_mapping(layer2.get("active_allocations"))
    passive_tickers = layer2.get("passive_tickers") if isinstance(layer2.get("passive_tickers"), dict) else {}
    passive_names = layer2.get("passive_names") if isinstance(layer2.get("passive_names"), dict) else {}
    manager_allocations = (
        layer3.get("allocations_by_asset_class")
        if isinstance(layer3.get("allocations_by_asset_class"), dict)
        else {}
    )
    securities: List[Dict[str, Any]] = []
    for asset_class, asset_weight in selected_weights.items():
        active_share = max(0.0, min(1.0, active_allocations.get(asset_class, 0.0)))
        managers = manager_allocations.get(asset_class)
        if isinstance(managers, dict) and active_share > 0:
            for symbol, manager_weight in _numeric_mapping(managers).items():
                weight = asset_weight * active_share * manager_weight
                if weight <= 0:
                    continue
                securities.append(_security_row(
                    symbol=symbol,
                    name=symbol,
                    asset_class=asset_class,
                    security_type="active",
                    weight=weight,
                    total_investment=total_investment,
                ))
        passive_weight = asset_weight * (1.0 - active_share)
        ticker = str(passive_tickers.get(asset_class) or "").strip()
        if passive_weight > 0 and ticker:
            securities.append(_security_row(
                symbol=ticker,
                name=str(passive_names.get(asset_class) or ticker),
                asset_class=asset_class,
                security_type="passive",
                weight=passive_weight,
                total_investment=total_investment,
            ))
    return sorted(securities, key=lambda item: item["weight"], reverse=True)


def _security_row(
    *,
    symbol: str,
    name: str,
    asset_class: str,
    security_type: str,
    weight: float,
    total_investment: float,
) -> Dict[str, Any]:
    return {
        "isin": symbol,
        "ticker": symbol,
        "symbol": symbol,
        "security_name": name,
        "asset_class": asset_class,
        "security_type": security_type,
        "weight": round(weight, 8),
        "amount": round(total_investment * weight, 2) if total_investment else 0.0,
    }


def _map_asset_allocation(asset_allocation_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    allocations = ((asset_allocation_result.get("investment_allocations") or {}).get("by_asset_class") or {})
    if not isinstance(allocations, dict):
        allocations = {}
    rows: List[Dict[str, Any]] = []
    for label, payload in allocations.items():
        if not isinstance(payload, dict):
            continue
        weight = _number(payload.get("weight")) or 0.0
        rows.append({"label": str(label), "value": round(weight * 100.0, 2)})
    return sorted(rows, key=lambda item: item["value"], reverse=True)


def _projection_payload(
    total_investment: float,
    expected_return: Optional[float],
    expected_volatility: Optional[float],
    cashflow_result: Optional[Dict[str, Any]],
    *,
    horizon_years: int = 10,
) -> Dict[str, Any]:
    cashflow_summary = _cashflow_summary(cashflow_result)
    if cashflow_summary:
        return {**cashflow_summary, "source": "cashflow_engine"}
    if total_investment <= 0 or expected_return is None:
        return {"status": "not_available", "source": "asset_allocation_model_missing_investment_or_return"}
    terminal_base = total_investment * ((1.0 + expected_return) ** horizon_years)
    vol = expected_volatility or 0.0
    downside = terminal_base * max(0.0, 1.0 - 1.5 * vol)
    upside = terminal_base * (1.0 + 1.5 * vol)
    return {
        "horizon_years": horizon_years,
        "base_case_terminal": round(terminal_base, 2),
        "downside_terminal": round(downside, 2),
        "upside_terminal": round(upside, 2),
        # Compatibility aliases for consumers of the original projection shape.
        "base_case_10y": round(terminal_base, 2),
        "downside_10y": round(downside, 2),
        "upside_10y": round(upside, 2),
        "source": "derived_from_asset_allocation_model_return_volatility",
    }


def _scope_of_purpose(objective: Any, horizon_years: int) -> str:
    purpose = str(objective or "investment growth").strip().replace("_", " ")
    return f"Support {purpose} over the confirmed {horizon_years}-year horizon."


def _money_pool_payload(journey: Dict[str, Any], fields: Dict[str, Any], total_investment: float) -> Dict[str, Any]:
    money_pool = journey.get("money_pool") if isinstance(journey.get("money_pool"), dict) else {}
    horizon = (
        _number(money_pool.get("horizon_years"))
        or _number(fields.get("horizon_years"))
        or _number(fields.get("time_horizon_years"))
        or _number(fields.get("horizon"))
    )
    return {
        **money_pool,
        "id": money_pool.get("id") or journey.get("money_pool_id"),
        "amount": _number(money_pool.get("amount")) or total_investment,
        "horizon_years": horizon,
        "objective": fields.get("objective") or money_pool.get("objective"),
        "constraints": fields.get("constraints") or money_pool.get("constraints") or [],
    }


def _risk_management_rules(fields: Dict[str, Any], layer2: Dict[str, Any]) -> List[str]:
    explicit = fields.get("risk_management_policy") or fields.get("monitoring_rules")
    if isinstance(explicit, list):
        rules = [str(item).strip() for item in explicit if str(item).strip()]
        if rules:
            return rules
    if isinstance(explicit, str) and explicit.strip():
        rules = [line.strip(" -\t") for line in explicit.splitlines() if line.strip(" -\t")]
        if rules:
            return rules
    rules: List[str] = []
    active_risk_budget = _number(layer2.get("active_risk_budget"))
    if active_risk_budget is not None and active_risk_budget > 0:
        rules.append(f"Escalate active-risk changes when the active risk budget moves from {active_risk_budget}.")
    if not rules:
        rules.extend([
            "Review the allocation quarterly.",
            "Review sooner if allocation drift exceeds 5%, drawdown exceeds 10%, or the goal changes materially.",
        ])
    return rules


def _mobile_section_5b_from_policy_payload(policy_payload: Dict[str, Any]) -> Dict[str, Any]:
    from advisor.tools.subagent_tools.investment_solution_specialist.mobile_section_5b import (
        build_mobile_section_5b,
    )

    return build_mobile_section_5b(policy_payload)


def _mobile_simulated_projection(
    projection: Dict[str, Any],
    *,
    total_investment: float,
    expected_return: Optional[float],
    expected_volatility: Optional[float],
    horizon_years: int = 10,
) -> Dict[str, Any]:
    if projection.get("source") == "cashflow_engine":
        terminal = projection.get("terminal_value_percentiles")
        if isinstance(terminal, dict):
            values = [
                _number(terminal.get("p10") or terminal.get("10") or terminal.get("lower")),
                _number(terminal.get("p50") or terminal.get("50") or terminal.get("median")),
                _number(terminal.get("p90") or terminal.get("90") or terminal.get("upper")),
            ]
            if all(value is not None for value in values):
                return {
                    "source": "cashflow_engine",
                    "years": [0, 10],
                    "scenarios": [
                        {"label": "Lower return scenario", "values": [total_investment, values[0]], "finalValue": values[0]},
                        {"label": "Median return scenario", "values": [total_investment, values[1]], "finalValue": values[1]},
                        {"label": "Higher return scenario", "values": [total_investment, values[2]], "finalValue": values[2]},
                    ],
                }
    base = _number(projection.get("base_case_terminal")) or _number(projection.get("base_case_10y"))
    lower = _number(projection.get("downside_terminal")) or _number(projection.get("downside_10y"))
    upper = _number(projection.get("upside_terminal")) or _number(projection.get("upside_10y"))
    if total_investment <= 0 or base is None:
        return {"source": projection.get("source") or "not_available", "scenarios": []}
    return {
        "source": projection.get("source") or "derived_from_asset_allocation_model_return_volatility",
        "years": [0, horizon_years / 2.0, horizon_years],
        "expectedReturn": expected_return,
        "expectedVolatility": expected_volatility,
        "scenarios": [
            {
                "label": "Lower return scenario",
                "values": _compound_path(total_investment, lower if lower is not None else base, years=horizon_years),
                "finalValue": lower if lower is not None else base,
            },
            {
                "label": "Median return scenario",
                "values": _compound_path(total_investment, base, years=horizon_years),
                "finalValue": base,
            },
            {
                "label": "Higher return scenario",
                "values": _compound_path(total_investment, upper if upper is not None else base, years=horizon_years),
                "finalValue": upper if upper is not None else base,
            },
        ],
    }


def _compound_path(start: float, end: float, *, years: int) -> List[float]:
    if start <= 0 or end <= 0 or years <= 0:
        return [round(start, 2), round(end, 2)]
    mid_year = years / 2.0
    ratio = end / start
    mid = start * (ratio ** (mid_year / years))
    return [round(start, 2), round(mid, 2), round(end, 2)]


def _cashflow_summary(cashflow_result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(cashflow_result, dict):
        return {}
    full = cashflow_result.get("full_result") if isinstance(cashflow_result.get("full_result"), dict) else cashflow_result
    summary = full.get("summary") if isinstance(full.get("summary"), dict) else {}
    if not summary:
        return {}
    return {
        "success_probability": summary.get("success_probability"),
        "terminal_value_percentiles": summary.get("terminal_value_percentiles"),
        "ending_balance": summary.get("ending_balance"),
    }


def _hero_summary(total: float, expected_return: Optional[float], expected_volatility: Optional[float]) -> str:
    amount = f"${total:,.0f}" if total else "the selected capital"
    ret = f"{expected_return * 100:.1f}%" if expected_return is not None else "model-derived"
    vol = f"{expected_volatility * 100:.1f}%" if expected_volatility is not None else "model-derived"
    return f"Invest {amount} with an asset allocation model targeting {ret} expected return and {vol} expected volatility."


def _fixed_income_weight(allocation_chart: List[Dict[str, Any]]) -> float:
    total_pct = sum(float(row.get("value") or 0.0) for row in allocation_chart if _is_fixed_income_label(row.get("label")))
    return round(total_pct / 100.0, 6)


def _is_fixed_income_label(label: Any) -> bool:
    text = str(label or "").lower()
    return any(token in text for token in ("bond", "treasury", "debt", "cash", "credit"))


def _pct_to_decimal(value: Any) -> Optional[float]:
    number = _number(value)
    if number is None:
        return None
    decimal = number / 100.0 if abs(number) > 1.0 else number
    return round(decimal, 8)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _numeric_mapping(value: Any) -> Dict[str, float]:
    if not isinstance(value, dict):
        return {}
    mapped: Dict[str, float] = {}
    for key, raw in value.items():
        number = _number(raw)
        if number is not None:
            mapped[str(key)] = number
    return mapped


def _section(section_id: str, title: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"section_id": section_id, "title": title, "payload": payload}


__all__ = [
    "PROPOSAL_SECTION_IDS",
    "normalize_asset_allocation_result",
    "proposal_artifact_from_allocation_result",
    "proposal_artifact_from_advisor_policy",
    "proposal_artifact_from_asset_allocation",
]
