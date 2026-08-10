"""OpenAI Agents SDK tool adapters for AWM's agent-facing boundaries."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set

from agents import FunctionTool
from agents.tool_context import ToolContext

from advisor.agents.agent_tool_catalog import (
    agent_tool_is_enabled,
    calculation_capability_gap_reported,
    is_deterministic_agent_tool,
    iter_subagent_tool_names,
    resolve_candidate_tool_names,
    resolve_effective_tool_names,
)
from advisor.tools.deterministic_tools._schema import OBJECT_SCHEMA
from advisor.tools.deterministic_tools.agent_tool_catalog import agent_tool_schemas_by_name
from advisor.tools.deterministic_tools.execution_control import (
    ReadOnlyToolExecutionControl,
    bind_read_only_tool_execution_control,
)
from advisor.tools.deterministic_tools.registry import build_default_tool_registry
from advisor.tracing.tracing import (
    append_trace,
    persistent_safe_tool_result,
    utc_now_iso,
)
from advisor.agents.context import AwmAgentContext
from advisor.agents.instructions import format_skill_instructions
from advisor.agents.quant_contracts import (
    STRICT_QUANT_TOOL_NAMES,
    QuantToolArgumentError,
    attach_quant_evidence,
    normalize_quant_tool_arguments,
)
from advisor.agents.skills import SkillDefinition


_TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = agent_tool_schemas_by_name()

# Guarded execution/KYC/settlement remains outside the conversational model loop.
_EXCLUDED_MODEL_TOOLS = {"record_deterministic_service_outcome"}
_DEFAULT_TOOL_TIMEOUT_SECONDS = 30.0
_QUANT_TOOL_TIMEOUT_SECONDS = {
    # These match the production engine request budgets. A shorter SDK deadline
    # used to cancel valid cold-start calculations before the adapter timeout.
    "run_cashflow_projection": 90.0,
    "solve_cashflow_contribution": 180.0,
    "run_asset_allocation": 60.0,
    "estimateAllocationRiskReturn": 90.0,
    "lookupRiskReturnFrontier": 90.0,
    "query_wolfram_alpha": 10.0,
    "research_public_financial_fact": 50.0,
}
_ACTIVATE_SKILL_TOOL_NAME = "activate_skill"
_ACTIVATE_SKILL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"skill_name": {"type": "string"}},
    "required": ["skill_name"],
    "additionalProperties": False,
}
_REQUEST_CLARIFICATION_TOOL_NAME = "request_clarification"
_REQUEST_CLARIFICATION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160,
            "description": "Short semantic operation that is paused.",
        },
        "question": {
            "type": "string",
            "minLength": 1,
            "maxLength": 600,
            "description": "Exactly one concrete client-facing clarification question.",
        },
        "missing_fields": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 120},
            "minItems": 1,
            "maxItems": 8,
        },
        "candidate_references": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 180},
                    "label": {"type": "string", "minLength": 1, "maxLength": 180},
                    "kind": {"type": "string", "minLength": 1, "maxLength": 80},
                },
                "required": ["id", "label", "kind"],
                "additionalProperties": False,
            },
            "maxItems": 8,
        },
    },
    "required": [
        "operation",
        "question",
        "missing_fields",
        "candidate_references",
    ],
    "additionalProperties": False,
}
# Only read-only numerical engines may outlive a cancelled asyncio await in a
# worker thread.  Write tools stay on the existing approval-protected path so a
# timeout can never detach an irreversible Client File mutation.
_BACKGROUND_READ_ONLY_TOOL_NAMES = frozenset(
    {
        "run_cashflow_projection",
        "solve_cashflow_contribution",
        "run_asset_allocation",
        "query_wolfram_alpha",
    }
)
SubagentToolBuilder = Callable[[Callable[[Any, Any], Any]], Any]


def build_arc_agent_tools(
    *,
    agent_key: str,
    base_tool_names: Iterable[str],
    installed_skills: Iterable[SkillDefinition],
    subagent_tool_builders: Mapping[str, SubagentToolBuilder] | None = None,
    include_subagent_tools: bool = True,
) -> List[Any]:
    """Register AWM capabilities with strict active-skill gating."""

    tools: List[Any] = []
    base_tool_names = set(base_tool_names)
    installed_skills = list(installed_skills)
    subagent_tool_builders = subagent_tool_builders or {}
    installed_skill_map = {skill.name: skill for skill in installed_skills}
    candidate_tool_names = resolve_candidate_tool_names(base_tool_names, installed_skills)
    for definition in build_default_tool_registry().list():
        if definition.name in _EXCLUDED_MODEL_TOOLS or not is_deterministic_agent_tool(definition.name):
            continue
        if definition.name not in candidate_tool_names:
            continue
        tools.append(
            _build_deterministic_function_tool(
                definition=definition,
                agent_key=agent_key,
                base_tool_names=base_tool_names,
                installed_skills=installed_skills,
            )
        )
    if include_subagent_tools:
        for tool_name in iter_subagent_tool_names():
            if tool_name not in candidate_tool_names:
                continue
            builder = subagent_tool_builders.get(tool_name)
            if builder is None:
                raise ValueError(f"subagent tool {tool_name} is declared but no builder was provided")
            tools.append(
                builder(
                    _build_agent_tool_is_enabled(
                        agent_key=agent_key,
                        tool_name=tool_name,
                        base_tool_names=base_tool_names,
                        installed_skills=installed_skills,
                    )
                )
            )
    if installed_skills:
        tools.append(_build_activate_skill_tool(agent_key, base_tool_names, installed_skill_map))
    if agent_key == "main_advisor":
        tools.append(_build_request_clarification_tool(agent_key))
    return tools


def _build_deterministic_function_tool(
    *,
    definition,
    agent_key: str,
    base_tool_names: Set[str],
    installed_skills: List[SkillDefinition],
) -> FunctionTool:
    schema = _TOOL_SCHEMAS.get(definition.name, OBJECT_SCHEMA)
    is_enabled = _build_agent_tool_is_enabled(
        agent_key=agent_key,
        tool_name=definition.name,
        base_tool_names=base_tool_names,
        installed_skills=installed_skills,
    )
    execute_in_background = bool(
        definition.read_only and definition.name in _BACKGROUND_READ_ONLY_TOOL_NAMES
    )
    tool_timeout_seconds = _QUANT_TOOL_TIMEOUT_SECONDS.get(
        definition.name,
        _DEFAULT_TOOL_TIMEOUT_SECONDS,
    )
    timeout_error_function = (
        _build_quant_tool_timeout_error_function(
            tool_name=definition.name,
            read_only=definition.read_only,
            writeback_target=definition.writeback_target,
            default_timeout_seconds=tool_timeout_seconds,
        )
        if execute_in_background
        else None
    )
    sdk_enforces_tool_timeout = (
        "timeout_seconds" in inspect.signature(FunctionTool).parameters
    )
    tool_holder: Dict[str, FunctionTool] = {}

    async def invoke(
        tool_context: ToolContext[AwmAgentContext],
        arguments_json: str,
        *,
        tool_name: str = definition.name,
    ) -> Dict[str, Any]:
        context = tool_context.context
        started = time.perf_counter()
        started_at = utc_now_iso()
        try:
            if not agent_tool_is_enabled(
                context,
                agent_key=agent_key,
                tool_name=tool_name,
                base_tool_names=base_tool_names,
                installed_skills=installed_skills,
            ):
                raise PermissionError(f"tool {tool_name} is not enabled for this run")
            arguments = json.loads(arguments_json or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")
            arguments = normalize_quant_tool_arguments(
                tool_name,
                arguments,
                user_message=context.user_message,
            )
            if tool_name in {
                "run_cashflow_projection",
                "commit_facts",
            }:
                arguments["client_file"] = copy.deepcopy(context.client_file)
            if tool_name == "commit_facts":
                # The authenticated current user turn, not model-authored prose,
                # is the only valid confirmation source.
                arguments["user_message"] = context.user_message
                arguments["confirmation_text"] = context.user_message
            if tool_name == "record_confirmation_decision":
                arguments["client_message"] = context.user_message
            if tool_name == "record_assessment_signoff":
                arguments["user_message"] = context.user_message
            if tool_name == "record_policy_review_outcome":
                arguments["user_message"] = context.user_message
            execution_arguments = copy.deepcopy(arguments)
            if tool_name in {"calculate_financial_math", "present_fact_confirmation"}:
                execution_arguments["_arc_companion_turn_id"] = context.turn_id
            if tool_name == "calculate_financial_math":
                execution_arguments["_arc_authenticated_user_message"] = (
                    context.user_message
                )
            if execute_in_background:
                active_tool = tool_holder.get("tool")
                active_agent = getattr(tool_context, "agent", None)
                for candidate in getattr(active_agent, "tools", []) or []:
                    if getattr(candidate, "name", None) == tool_name:
                        active_tool = candidate
                        break
                timeout_seconds = float(
                    getattr(
                        active_tool,
                        "timeout_seconds",
                        tool_timeout_seconds,
                    )
                )
                execution_control = ReadOnlyToolExecutionControl.with_timeout(
                    timeout_seconds
                )
                worker_task = asyncio.create_task(
                    _execute_read_only_tool_detached(
                        execution_control,
                        context.tool_executor,
                        client_id=context.client_id,
                        session_id=context.session_id,
                        tool_name=tool_name,
                        arguments=execution_arguments,
                    )
                )
                try:
                    # Resolve AWM's timeout just before the SDK's outer
                    # deadline so the deterministic timeout result wins the
                    # cancellation race on every supported SDK version.
                    timeout_lead = min(0.05, timeout_seconds * 0.8)
                    result = await asyncio.wait_for(
                        worker_task,
                        timeout=max(0.001, timeout_seconds - timeout_lead),
                    )
                except (TimeoutError, asyncio.TimeoutError):
                    execution_control.cancel()
                    return timeout_error_function(
                        tool_context,
                        _ReadOnlyToolDeadlineExceeded(timeout_seconds),
                    )
                except asyncio.CancelledError:
                    execution_control.cancel()
                    raise
            else:
                result = context.tool_executor.execute(
                    client_id=context.client_id,
                    session_id=context.session_id,
                    tool_name=tool_name,
                    arguments=execution_arguments,
                )
            if result.get("ok"):
                _apply_fact_writeback_to_context(
                    context,
                    tool_name=tool_name,
                    result=result,
                    arguments=arguments,
                )
                if (
                    tool_name == "commit_facts"
                    and context.mid_turn_client_file_refresher is not None
                ):
                    context.mid_turn_client_file_refresher(context, result)
        except PermissionError as exc:
            result = {"tool": tool_name, "ok": False, "error": "tool_permission_denied", "detail": str(exc)}
        except QuantToolArgumentError as exc:
            result = {
                "tool": tool_name,
                "ok": False,
                "error": "tool_arguments_invalid",
                "validation_errors": exc.validation_errors,
            }
        except Exception as exc:  # pragma: no cover - SDK boundary safety
            result = {"tool": tool_name, "ok": False, "error": "tool_execution_failed", "detail": str(exc)}

        result = attach_quant_evidence(tool_name, result)
        result["executed_by_agent"] = agent_key
        context.tool_results.append(result)
        context.trace_events = append_trace(
            context.trace_events,
            event="sdk_tool_completed",
            node=tool_name,
            summary=f"Agents SDK executed AWM tool {tool_name}.",
            trace_id=context.trace_id,
            turn_id=context.turn_id,
            parent_span_id=context.root_span_id,
            status="success" if result.get("ok") else "failed",
            started_at=started_at,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            input_data=_trace_safe_tool_arguments(
                tool_name,
                arguments if "arguments" in locals() else {},
            ),
            output_data=persistent_safe_tool_result(result),
            data={
                "runtime": "openai_agents_sdk",
                "tool": tool_name,
                "agent_key": agent_key,
            },
        )
        return _agent_visible_tool_result(
            tool_name,
            result,
            user_message=context.user_message,
        )

    tool = _build_function_tool(
        name=definition.name,
        description=definition.description,
        params_json_schema=schema,
        on_invoke_tool=invoke,
        strict_json_schema=definition.name in STRICT_QUANT_TOOL_NAMES,
        is_enabled=is_enabled,
        needs_approval=_build_tool_approval_requirement(definition),
        timeout_seconds=tool_timeout_seconds,
        timeout_error_function=timeout_error_function,
        internal_timeout=execute_in_background and not sdk_enforces_tool_timeout,
    )
    tool_holder["tool"] = tool
    return tool


def _build_tool_approval_requirement(definition):
    needs_approval = bool(definition.irreversible or definition.requires_explicit_consent)
    if not needs_approval or definition.name != "record_assessment_signoff":
        return needs_approval

    async def record_assessment_signoff_needs_approval(run_context, arguments, _tool_name) -> bool:
        context = getattr(run_context, "context", None)
        user_message = str(getattr(context, "user_message", "") or "")
        return not _authenticated_assessment_action_matches_tool_args(
            user_message,
            arguments if isinstance(arguments, dict) else {},
        )

    return record_assessment_signoff_needs_approval


def _authenticated_assessment_action_matches_tool_args(
    user_message: str,
    arguments: Dict[str, Any],
) -> bool:
    text = " ".join(str(user_message or "").lower().split())
    if (
        "authenticated app button tap" not in text
        or "trusted client action payload" not in text
        or "investment_assessment_decision" not in text
    ):
        return False
    signed_off = arguments.get("signed_off")
    if signed_off is True:
        expected_decision = '"decision": "agree"'
    elif signed_off is False:
        expected_decision = '"decision": "cancel"'
    else:
        return False
    if expected_decision not in text:
        return False
    assessment_id = str(arguments.get("assessment_id") or "").strip().lower()
    money_pool_id = str(arguments.get("money_pool_id") or "").strip().lower()
    version = arguments.get("assessment_version")
    if (
        not assessment_id
        or not money_pool_id
        or not isinstance(version, int)
        or isinstance(version, bool)
    ):
        return False
    return (
        assessment_id in text
        and money_pool_id in text
        and f'"assessment_version": {version}' in text
    )


def _trace_safe_tool_arguments(
    tool_name: str,
    arguments: Mapping[str, Any],
) -> Dict[str, Any]:
    """Avoid retaining a rejected external query in diagnostic traces."""

    if tool_name != "query_wolfram_alpha":
        return dict(arguments)
    query = str(arguments.get("query") or "")
    return {
        "query_sha256": (
            f"sha256:{hashlib.sha256(query.encode('utf-8')).hexdigest()}"
            if query
            else ""
        ),
        "query_length": len(query),
        "expected_unit": arguments.get("expected_unit"),
    }


def _agent_visible_tool_result(
    tool_name: str,
    result: Dict[str, Any],
    *,
    user_message: str = "",
) -> Dict[str, Any]:
    """Give the model one semantic view while retaining full evidence in context."""

    if tool_name == "report_calculation_capability_gap":
        full_result = (
            result.get("full_result")
            if isinstance(result.get("full_result"), dict)
            else {}
        )
        return {
            "tool": tool_name,
            "ok": result.get("ok") is True,
            "status": full_result.get("status") or "unsupported",
            "terminal": True,
            "retry_allowed": False,
            "reason_code": full_result.get("reason_code"),
            "source_refs": copy.deepcopy(full_result.get("source_refs") or []),
            "client_message": (
                result.get("client_message")
                or full_result.get("client_message")
            ),
            "note": (
                "Return client_message exactly. Do not call another tool or perform "
                "the calculation in prose."
            ),
        }

    cashflow_tools = {"run_cashflow_projection", "get_cashflow_analysis"}
    allocation_tools = {
        "run_asset_allocation",
        "get_asset_allocation_analysis",
    }
    derived_quant_tools = {
        "audit_cashflow_analysis",
        "solve_cashflow_contribution",
        "compare_quant_analyses",
        "calculate_cashflow_metrics",
        "calculate_financial_math",
        "query_wolfram_alpha",
        "analyze_portfolio_risk",
        "analyze_asset_location",
    }
    if tool_name not in {
        "run_cashflow_projection",
        "get_cashflow_analysis",
        "run_asset_allocation",
        "get_asset_allocation_analysis",
        *derived_quant_tools,
    }:
        return result
    common_keys = (
        "tool",
        "ok",
        "execution_ok",
        "valid_for_reporting",
        "valid_for_conclusion",
        "valid_for_recommendation",
        "error",
        "details",
        "detail",
        "missing_data",
        "warnings",
        "analysis_id",
        "allocation_id",
        "source_assessment",
        "source_allocation",
        "source_allocations",
        "available_detail_columns",
        "missing_detail_columns",
        "missing_calendar_years",
        "retrieved_without_rerun",
        "requires_rerun",
        "note",
    )
    compact = {
        key: copy.deepcopy(result.get(key))
        for key in common_keys
        if key in result
    }
    if tool_name in cashflow_tools:
        analysis = (
            result.get("analysis")
            if isinstance(result.get("analysis"), dict)
            else {}
        )
        request = (
            analysis.get("request")
            if isinstance(analysis.get("request"), dict)
            else {}
        )
        compact["cashflow_agent_view"] = _task_scoped_quant_view(
            tool_name,
            result.get("cashflow_agent_view") or {},
            result.get("recommendation_evidence") or {},
            user_message,
        )
        compact["analysis_status"] = copy.deepcopy(analysis.get("status") or {})
        compact["headline"] = copy.deepcopy(analysis.get("headline") or {})
        compact["next_question"] = analysis.get("next_question")
        compact["request_summary"] = {
            "scenario_label": request.get("scenario_label"),
            "requested_input": copy.deepcopy(request.get("requested_input") or {}),
            "applied_changes": copy.deepcopy(request.get("applied_changes") or []),
            "unsupported_changes": copy.deepcopy(
                request.get("unsupported_changes") or []
            ),
        }
    elif tool_name in allocation_tools:
        allocation_view = copy.deepcopy(
            result.get("asset_allocation_agent_view") or {}
        )
        holdings = (
            allocation_view.get("holdings")
            if isinstance(allocation_view.get("holdings"), dict)
            else {}
        )
        # ``all_securities`` is the useful complete holding list.  Each raw
        # security may also carry optimizer-internal fields already summarized
        # elsewhere in the view, so keep only the fields needed for narration.
        if isinstance(holdings.get("all_securities"), list):
            holdings["all_securities"] = [
                {
                    key: copy.deepcopy(item.get(key))
                    for key in (
                        "ticker",
                        "isin",
                        "name",
                        "asset_class",
                        "security_type",
                        "weight",
                        "amount",
                    )
                    if key in item
                }
                for item in holdings["all_securities"]
                if isinstance(item, dict)
            ]
        compact["asset_allocation_agent_view"] = _task_scoped_quant_view(
            tool_name,
            allocation_view,
            result.get("recommendation_evidence") or {},
            user_message,
        )
        compact["status"] = copy.deepcopy(result.get("status") or {})
    else:
        compact["quant_result"] = _task_scoped_derived_quant_result(
            tool_name,
            result.get("full_result") or {},
            user_message,
        )
        if tool_name == "calculate_financial_math":
            resolved_sources = (
                (result.get("full_result") or {}).get("resolved_sources")
                if isinstance(result.get("full_result"), dict)
                else []
            )
            if isinstance(resolved_sources, list) and any(
                isinstance(source, dict)
                and source.get("kind")
                in {"cashflow_claim", "cashflow_series_value"}
                for source in resolved_sources
            ):
                compact["response_requirements"] = {
                    "audience": "client_without_financial_background",
                    "required_structure": [
                        "Explain the compared operands in plain language.",
                        "State the direct calculated difference or result.",
                        "Explain what the result means and what it does not mean for the plan.",
                        (
                            "Offer one optional supported follow-up calculation as an "
                            "'If you want, I can...' question, without recommending a "
                            "financial action."
                        ),
                    ],
                    "instruction": (
                        "The final answer is incomplete if it only reports the formula or number. "
                        "Do not use 'recommend' or 'should' for the optional follow-up."
                    ),
                }
        compact["source_allocation_analysis_id"] = result.get(
            "source_allocation_analysis_id"
        )
        compact["selected_cashflow_analysis_id"] = result.get(
            "selected_cashflow_analysis_id"
        )
    evidence = (
        result.get("recommendation_evidence")
        if isinstance(result.get("recommendation_evidence"), dict)
        else {}
    )
    if evidence:
        compact["evidence_summary"] = {
            key: copy.deepcopy(evidence.get(key))
            for key in (
                "schema_version",
                "tool",
                "status",
                "execution_ok",
                "valid_for_reporting",
                "valid_for_conclusion",
                "valid_for_recommendation",
                "permitted_use",
                "conclusion_code",
                "warnings",
                "assumptions",
                "errors",
            )
            if key in evidence
        }
        claim_refs: Dict[str, str] = {}
        relevant_derived_claims = _relevant_derived_claim_keys(
            tool_name,
            user_message,
        )
        for claim in evidence.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            metric_key = str(claim.get("metric_key") or "").strip()
            claim_id = str(claim.get("claim_id") or "").strip()
            if not metric_key or not claim_id:
                continue
            if relevant_derived_claims is not None and not any(
                metric_key == prefix or metric_key.startswith(prefix)
                for prefix in relevant_derived_claims
            ):
                continue
            claim_refs[metric_key] = (
                f"{evidence.get('tool') or tool_name}/{claim_id}"
            )
        # Cash-flow and allocation context packs already carry each selected
        # claim's id/reference. Repeating every reference here wastes context.
        if claim_refs and tool_name in derived_quant_tools:
            compact["evidence_claim_refs"] = claim_refs
    if compact.get("execution_ok") is True and compact.get("valid_for_reporting") is False:
        compact["terminal"] = True
        compact["retry_allowed"] = False
        compact["note"] = (
            "The quantitative engine completed but deterministic validation rejected "
            "the result. Do not repeat the unchanged tool call in this turn."
        )
    return compact


def _task_scoped_quant_view(
    tool_name: str,
    view: Dict[str, Any],
    evidence: Dict[str, Any],
    user_message: str,
) -> Dict[str, Any]:
    """Build a bounded question-specific evidence pack for model context."""

    if not isinstance(view, dict):
        view = {}
    if not isinstance(evidence, dict):
        evidence = {}
    text = " ".join(str(user_message or "").lower().split())
    cashflow = tool_name in {"run_cashflow_projection", "get_cashflow_analysis"}
    default_keys = (
        {
            "success_probability",
            "projected_terminal_value",
            "shortfall",
            "minimum_liquidity",
        }
        if cashflow
        else {
            "portfolio_expected_return_annual_decimal",
            "portfolio_expected_volatility_annual_decimal",
            "target_volatility_annual_decimal",
            "target_volatility_difference_bps",
            "target_volatility_tolerance_bps",
            "target_volatility_passed",
        }
    )
    keyword_groups = (
        (
            ("percentile", "p10", "p50", "p90", "downside", "dispersion"),
            {
                "terminal_value_percentiles",
                "shortfall_percentiles",
                "projected_terminal_value",
            },
        ),
        (
            ("depletion", "run out", "first year"),
            {
                "first_depletion_year_distribution",
                "first_shortfall_year_distribution",
            },
        ),
        (
            (
                "milestone",
                "trajectory",
                "over time",
                "at retirement",
                "retirement year",
                "net worth in ",
            ),
            {
                "milestone_percentile_trajectory",
                "queried_percentile_trajectory",
                "requested_detail_percentile_trajectory",
            },
        ),
        (
            (
                "tax",
                "income",
                "spending",
                "withdrawal",
                "mortgage balance",
                "mortgage payment",
                "account balance",
            ),
            {
                "requested_detail_percentile_trajectory",
                "queried_percentile_trajectory",
            },
        ),
        (
            ("shortfall", "unmet cash", "debt"),
            {"shortfall", "shortfall_percentiles", "first_shortfall_year_distribution"},
        ),
        (
            ("liquid", "cash", "reserve"),
            {"minimum_liquidity", "reserve_breach_probability"},
        ),
        (
            ("return",),
            {"portfolio_expected_return_annual_decimal"},
        ),
        (
            ("volatility", "risk target", "tolerance"),
            {
                "portfolio_expected_volatility_annual_decimal",
                "target_volatility_annual_decimal",
                "target_volatility_difference_bps",
                "target_volatility_tolerance_bps",
                "target_volatility_passed",
            },
        ),
        (
            ("active", "passive", "sleeve"),
            {
                "active_risk_percentage",
                "passive_risk_percentage",
                "active_sleeve_weight",
                "passive_sleeve_weight",
            },
        ),
    )
    focused_keys: set[str] = set()
    for markers, keys in keyword_groups:
        if any(marker in text for marker in markers):
            focused_keys.update(keys)
    selected = focused_keys if cashflow and focused_keys else set(default_keys) | focused_keys
    if any(marker in text for marker in ("percentile", "p10", "p50", "p90")) and not any(
        marker in text for marker in ("liquid", "cash", "reserve")
    ):
        selected.discard("minimum_liquidity")

    claims: Dict[str, Dict[str, Any]] = {}
    all_claims = [
        item
        for item in evidence.get("claims") or []
        if isinstance(item, dict)
    ]
    if cashflow and focused_keys and not any(
        str(item.get("metric_key") or "") in selected for item in all_claims
    ):
        selected = set(default_keys)
    wants_holdings = any(
        marker in text
        for marker in ("holding", "security", "ticker", "fund", "position")
    )
    wants_composition = any(
        marker in text
        for marker in ("allocation", "weight", "asset class", "category", "composition")
    )
    for claim in all_claims:
        metric_key = str(claim.get("metric_key") or "").strip()
        include = metric_key in selected
        if wants_holdings and metric_key.startswith("security."):
            include = True
        if wants_composition and (
            metric_key.startswith("asset_weight.")
            or metric_key.startswith("category_weight.")
        ):
            include = True
        if not include:
            continue
        claims[metric_key] = {
            "value": copy.deepcopy(claim.get("value")),
            "unit": claim.get("unit"),
            "claim_id": claim.get("claim_id"),
            "evidence_ref": claim.get("evidence_ref"),
        }
        if len(claims) >= 30:
            break

    limitations = [
        copy.deepcopy(item)
        for item in view.get("limitations") or []
        if isinstance(item, dict)
        and (
            item.get("code") in {
                "simulation_not_guarantee",
                "expected_metrics_not_guarantees",
                "estimate_only",
                "read_only_no_execution",
            }
            or any(
                token in text
                for token in str(item.get("code") or "").split("_")
                if len(token) > 4
            )
        )
    ]
    pack: Dict[str, Any] = {
        "schema_version": "awm.quant_context_pack.v1",
        "analysis_id": view.get("analysis_id"),
        "task_query": str(user_message or "")[:500],
        "permission": copy.deepcopy(
            view.get("permission")
            or {
                key: evidence.get(key)
                for key in (
                    "permitted_use",
                    "valid_for_reporting",
                    "valid_for_conclusion",
                    "valid_for_recommendation",
                    "conclusion_code",
                )
            }
        ),
        "headline": copy.deepcopy(
            view.get("interpretations")
            or view.get("constraint_summary")
            or {}
        ),
        "claims": claims,
        "warnings": copy.deepcopy(evidence.get("warnings") or []),
        "limitations": limitations,
        "model_links": copy.deepcopy(view.get("model_links") or {}),
        "detail_reference": {
            "analysis_id": view.get("analysis_id"),
            "retrieval_tool": (
                "get_cashflow_analysis"
                if cashflow
                else "get_asset_allocation_analysis"
            ),
        },
    }
    if cashflow:
        assumption_summary = view.get("assumption_summary")
        if isinstance(assumption_summary, dict):
            if any(
                marker in text
                for marker in ("assumption", "default", "tax", "mortgage", "growth")
            ):
                pack["assumption_deltas"] = copy.deepcopy(assumption_summary)
            else:
                pack["assumption_deltas"] = {
                    key: assumption_summary.get(key)
                    for key in (
                        "configured_default_count",
                        "effective_parameter_count",
                    )
                }
        pack["scenario"] = copy.deepcopy(view.get("scenario") or {})
    else:
        pack["mandate"] = copy.deepcopy(view.get("mandate") or {})
        holdings = view.get("holdings") if isinstance(view.get("holdings"), dict) else {}
        if wants_holdings:
            all_securities = holdings.get("all_securities")
            pack["holdings"] = {
                "security_count": holdings.get("security_count"),
                "securities": copy.deepcopy(
                    all_securities
                    if isinstance(all_securities, list)
                    else holdings.get("top_holdings") or []
                ),
            }
        elif holdings:
            pack["holdings"] = {
                "security_count": holdings.get("security_count"),
                "securities": copy.deepcopy((holdings.get("top_holdings") or [])[:5]),
            }
    return pack


def _task_scoped_derived_quant_result(
    tool_name: str,
    full_result: Dict[str, Any],
    user_message: str,
) -> Dict[str, Any]:
    """Remove derived-analysis sections unrelated to the current question."""

    if not isinstance(full_result, dict):
        return {}
    output = copy.deepcopy(full_result)
    text = " ".join(str(user_message or "").lower().split())
    if tool_name == "compare_quant_analyses":
        deltas = output.get("deltas")
        if isinstance(deltas, list):
            compact_deltas = [
                {
                    key: copy.deepcopy(value)
                    for key, value in row.items()
                    if key != "relative_delta"
                }
                for row in deltas
                if isinstance(row, dict)
            ]
            topic_fragments = {
                "expected_return": ("expected return", "return"),
                "volatility": ("volatility", "risk"),
                "total_investment": ("total", "amount", "capital"),
                "active_sleeve": ("active",),
                "passive_sleeve": ("passive",),
                "equity": ("equity", "stock"),
                "cash": ("cash",),
                "fixed_income": ("bond", "fixed income"),
                "terminal_value": ("terminal", "ending balance", "net worth"),
                "success_probability": ("success", "probability"),
                "shortfall": ("shortfall",),
                "liquidity": ("liquidity", "reserve"),
            }
            requested_fragments = {
                fragment
                for fragment, markers in topic_fragments.items()
                if any(marker in text for marker in markers)
            }
            if requested_fragments:
                relevant = [
                    row
                    for row in compact_deltas
                    if any(
                        fragment
                        in str(row.get("metric_key") or "").lower()
                        for fragment in requested_fragments
                    )
                ]
                if relevant:
                    compact_deltas = relevant
            output["deltas"] = compact_deltas[:6]
            output["displayed_delta_count"] = len(output["deltas"])
            output["total_delta_count"] = len(deltas)
    elif tool_name == "solve_cashflow_contribution":
        # The tested grid remains in the server-side result for audit, but the
        # specialist should narrate only the selected/boundary result. Exposing
        # every candidate encourages raw-table dumps and unsupported side math.
        output.pop("tested_points", None)
    elif tool_name == "analyze_portfolio_risk":
        wants_stress = any(
            marker in text
            for marker in ("stress", "shock", "crash", "drawdown scenario")
        )
        wants_contribution = any(
            marker in text
            for marker in (
                "contribut",
                "driver",
                "drives risk",
                "most risk",
                "marginal",
            )
        )
        wants_drawdown = any(
            marker in text
            for marker in (
                "maximum drawdown",
                "maximum-drawdown",
                "max drawdown",
                "max-drawdown",
                "path-based drawdown",
                "path-based maximum drawdown",
            )
        )
        wants_fees = any(
            marker in text
            for marker in ("fee", "expense ratio", "cost drag")
        )
        if not wants_drawdown:
            output.pop("drawdown_analysis", None)
        if not wants_fees:
            output.pop("fee_drag_analysis", None)
        if (wants_drawdown or wants_fees) and not wants_stress and not wants_contribution:
            output.pop("risk_contributions", None)
            output.pop("stress_scenarios", None)
            methodology = output.get("methodology")
            if isinstance(methodology, dict):
                output["methodology"] = {
                    key: methodology.get(key)
                    for key in ("drawdown", "fee_drag")
                    if (
                        (key == "drawdown" and wants_drawdown)
                        or (key == "fee_drag" and wants_fees)
                    )
                }
        elif wants_stress and not wants_contribution:
            output.pop("risk_contributions", None)
            methodology = output.get("methodology")
            if isinstance(methodology, dict):
                output["methodology"] = {
                    "stress": methodology.get("stress")
                }
        elif wants_contribution and not wants_stress:
            output.pop("stress_scenarios", None)
            methodology = output.get("methodology")
            if isinstance(methodology, dict):
                output["methodology"] = {
                    "risk_contribution": methodology.get("risk_contribution")
                }
    elif tool_name == "analyze_asset_location":
        placements = output.get("placements")
        if isinstance(placements, list):
            wants_ordering = any(
                marker in text
                for marker in ("score", "order", "why", "tax efficiency")
            )
            output["placements"] = [
                {
                    key: copy.deepcopy(row.get(key))
                    for key in (
                        "asset_class",
                        "portfolio_weight",
                        "retirement_amount",
                        "taxable_amount",
                        *(
                            ("tax_inefficiency_score",)
                            if wants_ordering
                            else ()
                        ),
                    )
                    if key in row
                }
                for row in placements
                if isinstance(row, dict)
            ]
    return output


def _relevant_derived_claim_keys(
    tool_name: str,
    user_message: str,
) -> Optional[set[str]]:
    """Return claim prefixes worth exposing beside a scoped derived result."""

    text = " ".join(str(user_message or "").lower().split())
    if tool_name == "compare_quant_analyses":
        return {""}
    if tool_name == "analyze_portfolio_risk":
        prefixes = {
            "expected_return_annual_decimal",
            "expected_volatility_annual_decimal",
        }
        wants_stress = any(
            marker in text for marker in ("stress", "shock", "crash")
        )
        wants_contribution = any(
            marker in text
            for marker in ("contribut", "driver", "drives risk", "most risk", "marginal")
        )
        wants_drawdown = any(
            marker in text
            for marker in (
                "maximum drawdown",
                "maximum-drawdown",
                "max drawdown",
                "max-drawdown",
                "path-based drawdown",
                "path-based maximum drawdown",
            )
        )
        wants_fees = any(
            marker in text for marker in ("fee", "expense ratio", "cost drag")
        )
        if wants_stress or not wants_contribution:
            prefixes.add("stress_scenario.")
        if wants_contribution or not wants_stress:
            prefixes.add("risk_contribution.")
        if wants_drawdown:
            prefixes.add("drawdown.")
        if wants_fees:
            prefixes.add("fee_drag.")
        return prefixes
    if tool_name == "analyze_asset_location":
        prefixes = {
            "account_capacity.",
            "reconciliation.",
            "placement.",
        }
        return prefixes
    if tool_name == "solve_cashflow_contribution":
        return {""}
    if tool_name == "audit_cashflow_analysis":
        return {""}
    if tool_name in {
        "calculate_cashflow_metrics",
        "calculate_financial_math",
        "query_wolfram_alpha",
    }:
        return {""}
    return None


def _execute_read_only_tool(
    execution_control: ReadOnlyToolExecutionControl,
    tool_executor: Any,
    *,
    client_id: str,
    session_id: str,
    tool_name: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Run a synchronous read-only adapter with cooperative cancellation."""

    with bind_read_only_tool_execution_control(execution_control):
        return tool_executor.execute(
            client_id=client_id,
            session_id=session_id,
            tool_name=tool_name,
            arguments=arguments,
        )


async def _execute_read_only_tool_detached(
    execution_control: ReadOnlyToolExecutionControl,
    tool_executor: Any,
    *,
    client_id: str,
    session_id: str,
    tool_name: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Run a synchronous adapter without joining an abandoned worker."""

    loop = asyncio.get_running_loop()
    result_future: asyncio.Future[Dict[str, Any]] = loop.create_future()

    def resolve_result(result: Dict[str, Any] | None, error: BaseException | None) -> None:
        if result_future.done():
            return
        if error is not None:
            result_future.set_exception(error)
        else:
            result_future.set_result(result or {})

    def execute() -> None:
        try:
            result = _execute_read_only_tool(
                execution_control,
                tool_executor,
                client_id=client_id,
                session_id=session_id,
                tool_name=tool_name,
                arguments=arguments,
            )
            loop.call_soon_threadsafe(resolve_result, result, None)
        except BaseException as exc:  # pragma: no cover - worker boundary safety
            try:
                loop.call_soon_threadsafe(resolve_result, None, exc)
            except RuntimeError:
                pass

    threading.Thread(
        target=execute,
        name=f"awm-{tool_name}",
        daemon=True,
    ).start()
    return await result_future


class _ReadOnlyToolDeadlineExceeded(TimeoutError):
    def __init__(self, timeout_seconds: float) -> None:
        super().__init__(
            f"read-only tool exceeded its {timeout_seconds:g}-second deadline"
        )
        self.timeout_seconds = timeout_seconds


def _build_quant_tool_timeout_error_function(
    *,
    tool_name: str,
    read_only: bool,
    writeback_target: str,
    default_timeout_seconds: float,
):
    """Return the single SDK-deadline handler for a read-only quant tool."""

    def on_timeout(tool_context: ToolContext[AwmAgentContext], error: Exception) -> Dict[str, Any]:
        context = tool_context.context
        timeout_seconds = float(getattr(error, "timeout_seconds", default_timeout_seconds))
        warning = (
            f"{tool_name} exceeded the {timeout_seconds:g}-second overall deadline; "
            "no quantitative result was accepted."
        )
        result: Dict[str, Any] = {
            "tool": tool_name,
            "tool_call_id": tool_context.tool_call_id,
            "ok": False,
            "read_only": read_only,
            "writeback_target": writeback_target,
            "error": "tool_execution_timeout",
            "detail": str(error),
            "timeout_seconds": timeout_seconds,
            "retryable": True,
            "warnings": [warning],
        }
        trace_input: Dict[str, Any] = {}
        if tool_name == "run_cashflow_projection":
            requested_input: Dict[str, Any] = {}
            try:
                decoded = json.loads(tool_context.tool_arguments or "{}")
                if isinstance(decoded, dict):
                    requested_input = decoded
            except (TypeError, ValueError):
                pass
            trace_input = requested_input
            result["analysis"] = {
                "schema_version": "awm.cashflow_result.v2",
                "request": {
                    "call_id": tool_context.tool_call_id,
                    "requested_input": requested_input,
                    "effective_input": None,
                },
                "status": {
                    "execution": "timed_out",
                    "validation": "failed",
                    "valid_for_recommendation": False,
                    "warnings": [warning],
                    "error": "tool_execution_timeout",
                    "missing_required_metrics": [],
                    "normalization_errors": [],
                },
                "metrics": {},
                "missing_data": [],
            }
        elif tool_name == "run_asset_allocation":
            result.update(
                {
                    "status": {
                        "execution": "timed_out",
                        "valid_for_recommendation": False,
                        "error": "tool_execution_timeout",
                    },
                    "valid_for_recommendation": False,
                    "constraint_checks": {},
                }
            )

        result = attach_quant_evidence(tool_name, result)
        # The cancelled invocation cannot reach its normal append/trace path.
        # Record exactly one immediate timeout result for the model and caller.
        context.tool_results.append(result)
        context.trace_events = append_trace(
            context.trace_events,
            event="sdk_tool_timed_out",
            node=tool_name,
            summary=f"Agents SDK stopped waiting for AWM tool {tool_name} at its overall deadline.",
            trace_id=context.trace_id,
            turn_id=context.turn_id,
            parent_span_id=context.root_span_id,
            status="failed",
            duration_ms=round(timeout_seconds * 1000, 3),
            input_data=trace_input,
            output_data=result,
            data={
                "runtime": "openai_agents_sdk",
                "tool": tool_name,
                "tool_call_id": tool_context.tool_call_id,
                "deadline_scope": "overall_tool_invocation",
            },
        )
        return _agent_visible_tool_result(
            tool_name,
            result,
            user_message=context.user_message,
        )

    return on_timeout


def _build_agent_tool_is_enabled(
    *,
    agent_key: str,
    tool_name: str,
    base_tool_names: Set[str],
    installed_skills: List[SkillDefinition],
):
    async def is_enabled(run_context, _agent) -> bool:
        context = run_context.context
        completed_quant_tool = {
            "consult_financial_planning_specialist": "run_cashflow_projection",
            "consult_investment_solution_specialist": "run_asset_allocation",
        }.get(tool_name)
        tool_results = getattr(context, "tool_results", None) or []
        if completed_quant_tool and any(
            isinstance(result, dict)
            and result.get("tool") == completed_quant_tool
            for result in tool_results
        ):
            retry_after_fact_write = (
                tool_name == "consult_financial_planning_specialist"
                and _cashflow_retry_after_fact_write_is_available(tool_results)
            )
            if not retry_after_fact_write:
                return False
        if (
            getattr(
                context,
                "mid_turn_commit_refresh_verified",
                None,
            )
            is False
            and tool_name in set(iter_subagent_tool_names())
        ):
            return False
        if (
            tool_name == "consult_financial_planning_specialist"
            and getattr(context, "post_fact_continuation_attempted", False)
            and getattr(context, "post_fact_continuation_action", None)
            == "cashflow_projection"
            and context.allowed_tools is not None
            and tool_name in context.allowed_tools
            and not calculation_capability_gap_reported(context)
        ):
            # The model already selected this pending action in commit_facts.
            # This turn-scoped authorization only bypasses the prior fact skill's
            # tool visibility; it does not infer or choose a route.
            return True
        return agent_tool_is_enabled(
            context,
            agent_key=agent_key,
            tool_name=tool_name,
            base_tool_names=base_tool_names,
            installed_skills=installed_skills,
        )

    return is_enabled


def _build_activate_skill_tool(
    agent_key: str,
    base_tool_names: Set[str],
    installed_skill_map: Dict[str, SkillDefinition],
) -> FunctionTool:
    installed_skill_names = sorted(installed_skill_map)
    schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "enum": installed_skill_names,
                "description": (
                    "Exact installed AWM skill id. Use one of: "
                    + ", ".join(installed_skill_names)
                ),
            }
        },
        "required": ["skill_name"],
        "additionalProperties": False,
    }

    async def is_enabled(run_context, _agent) -> bool:
        if (
            _clarification_already_requested(run_context.context)
            or calculation_capability_gap_reported(run_context.context)
        ):
            return False
        allowed_tools = run_context.context.allowed_tools
        return allowed_tools is None or _ACTIVATE_SKILL_TOOL_NAME in allowed_tools

    async def invoke(tool_context: ToolContext[AwmAgentContext], arguments_json: str) -> Dict[str, Any]:
        context = tool_context.context
        started = time.perf_counter()
        started_at = utc_now_iso()
        try:
            if context.allowed_tools is not None and _ACTIVATE_SKILL_TOOL_NAME not in context.allowed_tools:
                raise PermissionError(f"tool {_ACTIVATE_SKILL_TOOL_NAME} is not enabled for this run")
            arguments = json.loads(arguments_json or "{}")
            skill_name = str(arguments.get("skill_name") or "")
            skill = installed_skill_map.get(skill_name)
            if skill is None:
                raise ValueError(f"skill {skill_name} is not installed for agent {agent_key}")
            context.active_skills[agent_key] = skill.name
            enabled_tools = sorted(resolve_effective_tool_names(base_tool_names, [skill], skill.name))
            result = {
                "tool": _ACTIVATE_SKILL_TOOL_NAME,
                "ok": True,
                "agent_key": agent_key,
                "active_skill": skill.name,
                "enabled_tools": enabled_tools,
                "instructions": format_skill_instructions(skill),
            }
        except PermissionError as exc:
            result = {
                "tool": _ACTIVATE_SKILL_TOOL_NAME,
                "ok": False,
                "error": "tool_permission_denied",
                "detail": str(exc),
            }
        except Exception as exc:
            result = {
                "tool": _ACTIVATE_SKILL_TOOL_NAME,
                "ok": False,
                "error": "skill_activation_failed",
                "detail": str(exc),
            }

        context.tool_results.append(result)
        context.trace_events = append_trace(
            context.trace_events,
            event="sdk_skill_activated" if result.get("ok") else "sdk_skill_activation_failed",
            node=_ACTIVATE_SKILL_TOOL_NAME,
            summary=f"Agents SDK activated AWM skill for {agent_key}.",
            trace_id=context.trace_id,
            turn_id=context.turn_id,
            parent_span_id=context.root_span_id,
            status="success" if result.get("ok") else "failed",
            started_at=started_at,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            input_data=arguments if "arguments" in locals() else {},
            output_data={key: value for key, value in result.items() if key != "instructions"},
            data={"runtime": "openai_agents_sdk", "tool": _ACTIVATE_SKILL_TOOL_NAME, "agent_key": agent_key},
        )
        return result

    return _build_function_tool(
        name=_ACTIVATE_SKILL_TOOL_NAME,
        description=(
            "Activate one installed AWM skill by its exact id and return its full instructions. "
            "Never use an agent or specialist display name."
        ),
        params_json_schema=schema,
        on_invoke_tool=invoke,
        strict_json_schema=True,
        is_enabled=is_enabled,
        timeout_seconds=_DEFAULT_TOOL_TIMEOUT_SECONDS,
    )


def _build_request_clarification_tool(agent_key: str) -> FunctionTool:
    """Build the model-selected, deterministic pause boundary for ambiguity."""

    async def is_enabled(run_context, _agent) -> bool:
        context = run_context.context
        if not _clarification_is_available(context):
            return False
        allowed_tools = context.allowed_tools
        return (
            allowed_tools is None
            or _REQUEST_CLARIFICATION_TOOL_NAME in allowed_tools
        )

    async def invoke(
        tool_context: ToolContext[AwmAgentContext],
        arguments_json: str,
    ) -> Dict[str, Any]:
        context = tool_context.context
        started = time.perf_counter()
        started_at = utc_now_iso()
        try:
            if not _clarification_is_available(context):
                raise RuntimeError(
                    "clarification is only available before a business tool executes"
                )
            if (
                context.allowed_tools is not None
                and _REQUEST_CLARIFICATION_TOOL_NAME not in context.allowed_tools
            ):
                raise PermissionError(
                    f"tool {_REQUEST_CLARIFICATION_TOOL_NAME} is not enabled for this run"
                )
            arguments = json.loads(arguments_json or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("clarification arguments must be a JSON object")
            operation = " ".join(str(arguments.get("operation") or "").split())
            question = " ".join(str(arguments.get("question") or "").split())
            missing_fields = list(
                dict.fromkeys(
                    " ".join(str(item or "").split())
                    for item in (arguments.get("missing_fields") or [])
                    if str(item or "").strip()
                )
            )
            candidate_references = [
                {
                    "id": str(item.get("id") or "").strip(),
                    "label": " ".join(str(item.get("label") or "").split()),
                    "kind": str(item.get("kind") or "").strip(),
                }
                for item in (arguments.get("candidate_references") or [])
                if isinstance(item, dict)
                and str(item.get("id") or "").strip()
                and str(item.get("label") or "").strip()
                and str(item.get("kind") or "").strip()
            ]
            if not operation or not question or not missing_fields:
                raise ValueError(
                    "operation, one question, and at least one missing field are required"
                )
            pending_intent = {
                "schema_version": "awm.pending_clarification.v1",
                "operation": operation,
                "question": question,
                "missing_fields": missing_fields,
                "candidate_references": candidate_references,
                "active_skill": context.active_skills.get(agent_key),
                "original_user_message": context.user_message,
            }
            result = {
                "tool": _REQUEST_CLARIFICATION_TOOL_NAME,
                "ok": True,
                "read_only": True,
                "question": question,
                "pending_intent": pending_intent,
                "note": (
                    "The turn is paused. Return the question exactly and do not call "
                    "another business tool until the client answers."
                ),
            }
        except PermissionError as exc:
            result = {
                "tool": _REQUEST_CLARIFICATION_TOOL_NAME,
                "ok": False,
                "error": "tool_permission_denied",
                "detail": str(exc),
            }
        except Exception as exc:
            result = {
                "tool": _REQUEST_CLARIFICATION_TOOL_NAME,
                "ok": False,
                "error": "clarification_request_invalid",
                "detail": str(exc),
            }

        context.tool_results.append(result)
        context.trace_events = append_trace(
            context.trace_events,
            event=(
                "sdk_clarification_requested"
                if result.get("ok")
                else "sdk_clarification_request_failed"
            ),
            node=_REQUEST_CLARIFICATION_TOOL_NAME,
            summary=(
                "AWM paused before skill or tool execution to resolve ambiguity."
                if result.get("ok")
                else "AWM could not create a structured clarification request."
            ),
            trace_id=context.trace_id,
            turn_id=context.turn_id,
            parent_span_id=context.root_span_id,
            status="success" if result.get("ok") else "failed",
            started_at=started_at,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            input_data=arguments if "arguments" in locals() else {},
            output_data=result,
            data={
                "runtime": "openai_agents_sdk",
                "tool": _REQUEST_CLARIFICATION_TOOL_NAME,
                "agent_key": agent_key,
            },
        )
        return result

    return _build_function_tool(
        name=_REQUEST_CLARIFICATION_TOOL_NAME,
        description=(
            "Pause before activating a skill or calling a business tool when the "
            "client's intended operation or referenced scenario, analysis, money "
            "pool, or portfolio is materially ambiguous. Ask exactly one concrete "
            "question and provide bounded candidate references when known."
        ),
        params_json_schema=_REQUEST_CLARIFICATION_SCHEMA,
        on_invoke_tool=invoke,
        strict_json_schema=True,
        is_enabled=is_enabled,
        timeout_seconds=_DEFAULT_TOOL_TIMEOUT_SECONDS,
    )


def _clarification_already_requested(context: AwmAgentContext) -> bool:
    return any(
        isinstance(item, dict)
        and item.get("tool") == _REQUEST_CLARIFICATION_TOOL_NAME
        and item.get("ok") is True
        for item in context.tool_results
    )


def _clarification_is_available(context: AwmAgentContext) -> bool:
    """Keep a clarification pause ahead of business execution, not after it."""

    if (
        _clarification_already_requested(context)
        or calculation_capability_gap_reported(context)
    ):
        return False
    if (
        getattr(context, "post_fact_continuation_attempted", False)
        and getattr(context, "post_fact_continuation_action", None)
        == "cashflow_projection"
        and context.allowed_tools is not None
        and _REQUEST_CLARIFICATION_TOOL_NAME in context.allowed_tools
    ):
        return True
    if (
        context.cashflow_missing_input_repair_attempted
        and _latest_cashflow_missing_fields(context.tool_results)
    ):
        return True
    control_tools = {
        _ACTIVATE_SKILL_TOOL_NAME,
        _REQUEST_CLARIFICATION_TOOL_NAME,
    }
    return not any(
        isinstance(item, dict)
        and str(item.get("tool") or "") not in control_tools
        for item in context.tool_results
    )


def _latest_cashflow_missing_fields(
    tool_results: Iterable[Dict[str, Any]],
) -> List[str]:
    """Return missing fields only from the latest cash-flow attempt."""

    for result in reversed(list(tool_results)):
        if not isinstance(result, dict) or result.get("tool") != "run_cashflow_projection":
            continue
        analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else {}
        raw_missing = result.get("missing_data") or analysis.get("missing_data") or []
        return [str(item).strip() for item in raw_missing if str(item).strip()]
    return []


def _cashflow_retry_after_fact_write_is_available(
    tool_results: Iterable[Dict[str, Any]],
) -> bool:
    """Allow one fresh projection after a missing-input run and fact write."""

    results = list(tool_results)
    projection_indexes = [
        index
        for index, result in enumerate(results)
        if isinstance(result, dict) and result.get("tool") == "run_cashflow_projection"
    ]
    if len(projection_indexes) != 1 or not _latest_cashflow_missing_fields(results):
        return False
    projection_index = projection_indexes[0]
    return any(
        isinstance(result, dict)
        and result.get("ok") is True
        and result.get("tool") in {"save_fact", "commit_facts"}
        for result in results[projection_index + 1 :]
    )


def _apply_fact_writeback_to_context(
    context: AwmAgentContext,
    *,
    tool_name: str,
    result: Dict[str, Any],
    arguments: Dict[str, Any],
) -> None:
    """Keep the in-turn Client File view current after draft/commit writebacks."""

    client_file = context.client_file if isinstance(context.client_file, dict) else {}
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else None

    if (
        tool_name == "commit_facts"
        and arguments.get("post_commit_action") == "cashflow_projection"
    ):
        context.post_fact_continuation_action = "cashflow_projection"
        result["post_commit_action"] = "cashflow_projection"

    if tool_name == "draft_fact":
        facts = (
            payload.get("facts")
            if isinstance(payload, dict) and isinstance(payload.get("facts"), dict)
            else arguments.get("facts") if isinstance(arguments.get("facts"), dict) else {}
        )
        if not facts:
            return
        drafts = list(client_file.get("draft_facts") or []) if isinstance(client_file.get("draft_facts"), list) else []
        draft_payload = (
            dict(payload)
            if isinstance(payload, dict)
            else {
                "fact_type": arguments.get("fact_type") or "captured_fact",
                "facts": dict(facts),
                "status": "draft",
                "lifecycle_stage": "draft",
            }
        )
        draft_id = str(draft_payload.get("draft_id") or "")
        if draft_id:
            drafts = [
                item
                for item in drafts
                if not isinstance(item, dict)
                or str(item.get("draft_id") or "") != draft_id
            ]
        drafts.insert(0, draft_payload)
        context.client_file = {**client_file, "draft_facts": drafts}
        return

    if tool_name in {"save_fact", "commit_facts"}:
        facts = payload.get("facts") if isinstance(payload, dict) and isinstance(payload.get("facts"), dict) else {}
        structured = (
            payload.get("structured_facts")
            if isinstance(payload, dict) and isinstance(payload.get("structured_facts"), dict)
            else {}
        )
        if not facts:
            return
        existing_facts = dict(client_file.get("facts") or {}) if isinstance(client_file.get("facts"), dict) else {}
        existing_facts.update(facts)
        existing_structured = (
            dict(client_file.get("structured_facts") or {})
            if isinstance(client_file.get("structured_facts"), dict)
            else {}
        )
        existing_structured.update(structured)
        resolved_draft_ids = {
            str(item)
            for item in (
                payload.get("resolved_draft_ids")
                if isinstance(payload, dict)
                and isinstance(payload.get("resolved_draft_ids"), list)
                else []
            )
        }
        existing_drafts = (
            list(client_file.get("draft_facts") or [])
            if isinstance(client_file.get("draft_facts"), list)
            else []
        )
        remaining_drafts = (
            [
                item
                for item in existing_drafts
                if not isinstance(item, dict)
                or str(
                    item.get("draft_id")
                    or item.get("source_event_id")
                    or ""
                )
                not in resolved_draft_ids
            ]
            if tool_name == "commit_facts"
            else existing_drafts
        )
        context.client_file = {
            **client_file,
            "facts": existing_facts,
            "structured_facts": existing_structured,
            "draft_facts": remaining_drafts,
        }
        return

    if tool_name == "upsert_money_pool":
        pool = payload if isinstance(payload, dict) else {}
        if not pool:
            pool = {
                **arguments,
                "id": result.get("pool_id"),
                "label": result.get("label") or arguments.get("label"),
                "amount": result.get("amount", arguments.get("amount")),
                "state": result.get("state"),
            }
        pool_id = str(pool.get("id") or pool.get("money_pool_id") or "").strip()
        pool_label = " ".join(str(pool.get("label") or "").casefold().split())
        pools = (
            list(client_file.get("money_pools") or [])
            if isinstance(client_file.get("money_pools"), list)
            else []
        )
        retained = []
        for item in pools:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or item.get("money_pool_id") or "").strip()
            item_label = " ".join(str(item.get("label") or "").casefold().split())
            if pool_id and item_id == pool_id:
                continue
            if pool_label and item_label == pool_label:
                continue
            retained.append(item)
        context.client_file = {
            **client_file,
            "money_pools": [pool, *retained],
        }
        return

    if tool_name == "create_investment_assessment":
        assessment = (
            payload
            if isinstance(payload, dict)
            else result.get("assessment")
            if isinstance(result.get("assessment"), dict)
            else {}
        )
        if not assessment:
            return
        assessment_id = str(assessment.get("assessment_id") or assessment.get("id") or "")
        assessments = (
            list(client_file.get("investment_assessments") or [])
            if isinstance(client_file.get("investment_assessments"), list)
            else []
        )
        assessments = [
            item
            for item in assessments
            if isinstance(item, dict)
            and str(item.get("assessment_id") or item.get("id") or "") != assessment_id
        ]
        context.client_file = {
            **client_file,
            "investment_assessments": [assessment, *assessments],
        }
        return

    if tool_name == "record_assessment_signoff":
        assessment = payload if isinstance(payload, dict) else {}
        if not assessment:
            return
        assessments = (
            list(client_file.get("investment_assessments") or [])
            if isinstance(client_file.get("investment_assessments"), list)
            else []
        )
        assessments = [item for item in assessments if isinstance(item, dict)]
        assessments.insert(0, assessment)
        recent = (
            list(client_file.get("recent_writebacks") or [])
            if isinstance(client_file.get("recent_writebacks"), list)
            else []
        )
        recent = [item for item in recent if isinstance(item, dict)]
        recent.insert(
            0,
            {
                "record": "client_file.plans",
                "operation": "record_assessment_signoff",
                "subject": assessment.get("pool_label") or assessment.get("assessment_id"),
                "values": assessment,
            },
        )
        context.client_file = {
            **client_file,
            "investment_assessments": assessments,
            "recent_writebacks": recent,
        }


def _build_function_tool(
    *,
    timeout_seconds: float,
    internal_timeout: bool = False,
    **kwargs: Any,
) -> FunctionTool:
    """Create an Agents SDK FunctionTool across SDK versions."""

    signature = inspect.signature(FunctionTool)
    if internal_timeout or "timeout_error_function" not in signature.parameters:
        kwargs.pop("timeout_error_function", None)
    if not internal_timeout and "timeout_seconds" in signature.parameters:
        return FunctionTool(timeout_seconds=timeout_seconds, **kwargs)
    # Compatibility with SDK releases predating function-tool deadlines.
    timeout_error_function = kwargs.pop("timeout_error_function", None)
    tool = FunctionTool(**kwargs)
    setattr(tool, "timeout_seconds", timeout_seconds)
    if not internal_timeout:
        setattr(tool, "timeout_error_function", timeout_error_function)
    return tool
