"""Single source of truth for AWM agent-facing deterministic tools."""

from __future__ import annotations

from typing import Any, Dict, Tuple

from advisor.tools.deterministic_tools.common.compactors import (
    cashflow_compactor,
    cashflow_manifest_extras,
)
from advisor.tools.deterministic_tools.common.definition import ToolDefinition
from advisor.tools.deterministic_tools.run_cashflow_projection.tool import (
    PARAMS_SCHEMA as RUN_CASHFLOW_PROJECTION_SCHEMA,
)
from advisor.tools.deterministic_tools.run_cashflow_projection.tool import (
    TOOL_SPEC as RUN_CASHFLOW_PROJECTION_SPEC,
)
from advisor.tools.deterministic_tools.solve_cashflow_contribution.tool import (
    PARAMS_SCHEMA as SOLVE_CASHFLOW_CONTRIBUTION_SCHEMA,
)
from advisor.tools.deterministic_tools.solve_cashflow_contribution.tool import (
    TOOL_SPEC as SOLVE_CASHFLOW_CONTRIBUTION_SPEC,
)
from advisor.tools.deterministic_tools.get_cashflow_analysis.tool import (
    PARAMS_SCHEMA as GET_CASHFLOW_ANALYSIS_SCHEMA,
)
from advisor.tools.deterministic_tools.get_cashflow_analysis.tool import (
    TOOL_SPEC as GET_CASHFLOW_ANALYSIS_SPEC,
)
from advisor.tools.deterministic_tools.audit_cashflow_analysis.tool import (
    PARAMS_SCHEMA as AUDIT_CASHFLOW_ANALYSIS_SCHEMA,
)
from advisor.tools.deterministic_tools.audit_cashflow_analysis.tool import (
    TOOL_SPEC as AUDIT_CASHFLOW_ANALYSIS_SPEC,
)
from advisor.tools.deterministic_tools.run_asset_allocation.tool import (
    PARAMS_SCHEMA as RUN_ASSET_ALLOCATION_SCHEMA,
)
from advisor.tools.deterministic_tools.run_asset_allocation.tool import (
    TOOL_SPEC as RUN_ASSET_ALLOCATION_SPEC,
)
from advisor.tools.deterministic_tools.get_asset_allocation_analysis.tool import (
    PARAMS_SCHEMA as GET_ASSET_ALLOCATION_ANALYSIS_SCHEMA,
)
from advisor.tools.deterministic_tools.get_asset_allocation_analysis.tool import (
    TOOL_SPEC as GET_ASSET_ALLOCATION_ANALYSIS_SPEC,
)
from advisor.tools.deterministic_tools.compare_quant_analyses.tool import (
    PARAMS_SCHEMA as COMPARE_QUANT_ANALYSES_SCHEMA,
)
from advisor.tools.deterministic_tools.compare_quant_analyses.tool import (
    TOOL_SPEC as COMPARE_QUANT_ANALYSES_SPEC,
)
from advisor.tools.deterministic_tools.calculate_cashflow_metrics.tool import (
    PARAMS_SCHEMA as CALCULATE_CASHFLOW_METRICS_SCHEMA,
)
from advisor.tools.deterministic_tools.calculate_cashflow_metrics.tool import (
    TOOL_SPEC as CALCULATE_CASHFLOW_METRICS_SPEC,
)
from advisor.tools.deterministic_tools.calculate_financial_math.tool import (
    V2_PARAMS_SCHEMA as CALCULATE_FINANCIAL_MATH_SCHEMA,
)
from advisor.tools.deterministic_tools.calculate_financial_math.tool import (
    TOOL_SPEC as CALCULATE_FINANCIAL_MATH_SPEC,
)
from advisor.tools.deterministic_tools.cancel_specialist_job.tool import (
    PARAMS_SCHEMA as CANCEL_SPECIALIST_JOB_SCHEMA,
)
from advisor.tools.deterministic_tools.cancel_specialist_job.tool import (
    TOOL_SPEC as CANCEL_SPECIALIST_JOB_SPEC,
)
from advisor.tools.deterministic_tools.report_calculation_capability_gap.tool import (
    PARAMS_SCHEMA as REPORT_CALCULATION_CAPABILITY_GAP_SCHEMA,
)
from advisor.tools.deterministic_tools.report_calculation_capability_gap.tool import (
    TOOL_SPEC as REPORT_CALCULATION_CAPABILITY_GAP_SPEC,
)
from advisor.tools.deterministic_tools.query_wolfram_alpha.tool import (
    PARAMS_SCHEMA as QUERY_WOLFRAM_ALPHA_SCHEMA,
)
from advisor.tools.deterministic_tools.query_wolfram_alpha.tool import (
    TOOL_SPEC as QUERY_WOLFRAM_ALPHA_SPEC,
)
from advisor.tools.deterministic_tools.research_public_financial_fact.tool import (
    PARAMS_SCHEMA as RESEARCH_PUBLIC_FINANCIAL_FACT_SCHEMA,
)
from advisor.tools.deterministic_tools.research_public_financial_fact.tool import (
    TOOL_SPEC as RESEARCH_PUBLIC_FINANCIAL_FACT_SPEC,
)
from advisor.tools.deterministic_tools.review_public_fact_reuse.tool import (
    PARAMS_SCHEMA as REVIEW_PUBLIC_FACT_REUSE_SCHEMA,
)
from advisor.tools.deterministic_tools.review_public_fact_reuse.tool import (
    TOOL_SPEC as REVIEW_PUBLIC_FACT_REUSE_SPEC,
)
from advisor.tools.deterministic_tools.analyze_portfolio_risk.tool import (
    PARAMS_SCHEMA as ANALYZE_PORTFOLIO_RISK_SCHEMA,
)
from advisor.tools.deterministic_tools.analyze_portfolio_risk.tool import (
    TOOL_SPEC as ANALYZE_PORTFOLIO_RISK_SPEC,
)
from advisor.tools.deterministic_tools.analyze_asset_location.tool import (
    PARAMS_SCHEMA as ANALYZE_ASSET_LOCATION_SCHEMA,
)
from advisor.tools.deterministic_tools.analyze_asset_location.tool import (
    TOOL_SPEC as ANALYZE_ASSET_LOCATION_SPEC,
)
from advisor.tools.deterministic_tools.commit_facts.tool import PARAMS_SCHEMA as COMMIT_FACTS_SCHEMA
from advisor.tools.deterministic_tools.commit_facts.tool import TOOL_SPEC as COMMIT_FACTS_SPEC
from advisor.tools.deterministic_tools.present_fact_confirmation.tool import PARAMS_SCHEMA as PRESENT_FACT_CONFIRMATION_SCHEMA
from advisor.tools.deterministic_tools.present_fact_confirmation.tool import TOOL_SPEC as PRESENT_FACT_CONFIRMATION_SPEC
from advisor.tools.deterministic_tools.resolve_fact_confirmation.tool import PARAMS_SCHEMA as RESOLVE_FACT_CONFIRMATION_SCHEMA
from advisor.tools.deterministic_tools.resolve_fact_confirmation.tool import TOOL_SPEC as RESOLVE_FACT_CONFIRMATION_SPEC
from advisor.tools.deterministic_tools.create_investment_assessment.tool import (
    PARAMS_SCHEMA as CREATE_INVESTMENT_ASSESSMENT_SCHEMA,
)
from advisor.tools.deterministic_tools.create_investment_assessment.tool import (
    TOOL_SPEC as CREATE_INVESTMENT_ASSESSMENT_SPEC,
)
from advisor.tools.deterministic_tools.draft_fact.tool import PARAMS_SCHEMA as DRAFT_FACT_SCHEMA
from advisor.tools.deterministic_tools.draft_fact.tool import TOOL_SPEC as DRAFT_FACT_SPEC
from advisor.tools.deterministic_tools.estimate_allocation_risk_return.tool import (
    PARAMS_SCHEMA as ESTIMATE_ALLOCATION_SCHEMA,
)
from advisor.tools.deterministic_tools.estimate_allocation_risk_return.tool import (
    TOOL_SPEC as ESTIMATE_ALLOCATION_SPEC,
)
from advisor.tools.deterministic_tools.lookup_risk_return_frontier.tool import (
    PARAMS_SCHEMA as LOOKUP_FRONTIER_SCHEMA,
)
from advisor.tools.deterministic_tools.lookup_risk_return_frontier.tool import (
    TOOL_SPEC as LOOKUP_FRONTIER_SPEC,
)
from advisor.tools.deterministic_tools.record_assessment_signoff.tool import (
    PARAMS_SCHEMA as RECORD_SIGNOFF_SCHEMA,
)
from advisor.tools.deterministic_tools.record_assessment_signoff.tool import (
    TOOL_SPEC as RECORD_SIGNOFF_SPEC,
)
from advisor.tools.deterministic_tools.record_deterministic_service_outcome.tool import (
    PARAMS_SCHEMA as RECORD_SERVICE_SCHEMA,
)
from advisor.tools.deterministic_tools.record_deterministic_service_outcome.tool import (
    TOOL_SPEC as RECORD_SERVICE_SPEC,
)
from advisor.tools.deterministic_tools.record_policy_review_outcome.tool import (
    PARAMS_SCHEMA as RECORD_POLICY_REVIEW_SCHEMA,
)
from advisor.tools.deterministic_tools.record_confirmation_decision.tool import (
    PARAMS_SCHEMA as RECORD_CONFIRMATION_DECISION_SCHEMA,
)
from advisor.tools.deterministic_tools.record_confirmation_decision.tool import (
    TOOL_SPEC as RECORD_CONFIRMATION_DECISION_SPEC,
)
from advisor.tools.deterministic_tools.record_policy_review_outcome.tool import (
    TOOL_SPEC as RECORD_POLICY_REVIEW_SPEC,
)
from advisor.tools.deterministic_tools.retrieve_conversation_history.tool import (
    PARAMS_SCHEMA as RETRIEVE_CONVERSATION_HISTORY_SCHEMA,
)
from advisor.tools.deterministic_tools.retrieve_conversation_history.tool import (
    TOOL_SPEC as RETRIEVE_CONVERSATION_HISTORY_SPEC,
)
from advisor.tools.deterministic_tools.save_consultation_checkpoint.tool import (
    PARAMS_SCHEMA as SAVE_CHECKPOINT_SCHEMA,
)
from advisor.tools.deterministic_tools.save_consultation_checkpoint.tool import (
    TOOL_SPEC as SAVE_CHECKPOINT_SPEC,
)
from advisor.tools.deterministic_tools.save_fact.tool import PARAMS_SCHEMA as SAVE_FACT_SCHEMA
from advisor.tools.deterministic_tools.save_fact.tool import TOOL_SPEC as SAVE_FACT_SPEC
from advisor.tools.deterministic_tools.update_objective_status.tool import PARAMS_SCHEMA as UPDATE_OBJECTIVE_SCHEMA
from advisor.tools.deterministic_tools.update_objective_status.tool import TOOL_SPEC as UPDATE_OBJECTIVE_SPEC
from advisor.tools.deterministic_tools.upsert_money_pool.tool import PARAMS_SCHEMA as UPSERT_MONEY_POOL_SCHEMA
from advisor.tools.deterministic_tools.upsert_money_pool.tool import TOOL_SPEC as UPSERT_MONEY_POOL_SPEC


RUN_CASHFLOW_PROJECTION_TOOL_NAME = RUN_CASHFLOW_PROJECTION_SPEC["name"]
GET_CASHFLOW_ANALYSIS_TOOL_NAME = GET_CASHFLOW_ANALYSIS_SPEC["name"]
AUDIT_CASHFLOW_ANALYSIS_TOOL_NAME = AUDIT_CASHFLOW_ANALYSIS_SPEC["name"]
RUN_ASSET_ALLOCATION_TOOL_NAME = RUN_ASSET_ALLOCATION_SPEC["name"]
GET_ASSET_ALLOCATION_ANALYSIS_TOOL_NAME = GET_ASSET_ALLOCATION_ANALYSIS_SPEC["name"]
ESTIMATE_ALLOCATION_RISK_RETURN_TOOL_NAME = ESTIMATE_ALLOCATION_SPEC["name"]
LOOKUP_RISK_RETURN_FRONTIER_TOOL_NAME = LOOKUP_FRONTIER_SPEC["name"]


AGENT_TOOL_SPECS: Tuple[Dict[str, Any], ...] = (
    SAVE_FACT_SPEC,
    DRAFT_FACT_SPEC,
    COMMIT_FACTS_SPEC,
    PRESENT_FACT_CONFIRMATION_SPEC,
    RESOLVE_FACT_CONFIRMATION_SPEC,
    RECORD_CONFIRMATION_DECISION_SPEC,
    SAVE_CHECKPOINT_SPEC,
    UPSERT_MONEY_POOL_SPEC,
    RUN_CASHFLOW_PROJECTION_SPEC,
    SOLVE_CASHFLOW_CONTRIBUTION_SPEC,
    GET_CASHFLOW_ANALYSIS_SPEC,
    AUDIT_CASHFLOW_ANALYSIS_SPEC,
    RUN_ASSET_ALLOCATION_SPEC,
    GET_ASSET_ALLOCATION_ANALYSIS_SPEC,
    COMPARE_QUANT_ANALYSES_SPEC,
    CALCULATE_CASHFLOW_METRICS_SPEC,
    CALCULATE_FINANCIAL_MATH_SPEC,
    QUERY_WOLFRAM_ALPHA_SPEC,
    RESEARCH_PUBLIC_FINANCIAL_FACT_SPEC,
    REVIEW_PUBLIC_FACT_REUSE_SPEC,
    REPORT_CALCULATION_CAPABILITY_GAP_SPEC,
    CANCEL_SPECIALIST_JOB_SPEC,
    ANALYZE_PORTFOLIO_RISK_SPEC,
    ANALYZE_ASSET_LOCATION_SPEC,
    UPDATE_OBJECTIVE_SPEC,
    RECORD_SERVICE_SPEC,
    RECORD_POLICY_REVIEW_SPEC,
    CREATE_INVESTMENT_ASSESSMENT_SPEC,
    RECORD_SIGNOFF_SPEC,
    ESTIMATE_ALLOCATION_SPEC,
    LOOKUP_FRONTIER_SPEC,
    RETRIEVE_CONVERSATION_HISTORY_SPEC,
)

AGENT_TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    SAVE_FACT_SPEC["name"]: SAVE_FACT_SCHEMA,
    DRAFT_FACT_SPEC["name"]: DRAFT_FACT_SCHEMA,
    COMMIT_FACTS_SPEC["name"]: COMMIT_FACTS_SCHEMA,
    PRESENT_FACT_CONFIRMATION_SPEC["name"]: PRESENT_FACT_CONFIRMATION_SCHEMA,
    RESOLVE_FACT_CONFIRMATION_SPEC["name"]: RESOLVE_FACT_CONFIRMATION_SCHEMA,
    RECORD_CONFIRMATION_DECISION_SPEC["name"]: RECORD_CONFIRMATION_DECISION_SCHEMA,
    SAVE_CHECKPOINT_SPEC["name"]: SAVE_CHECKPOINT_SCHEMA,
    UPSERT_MONEY_POOL_SPEC["name"]: UPSERT_MONEY_POOL_SCHEMA,
    RUN_CASHFLOW_PROJECTION_SPEC["name"]: RUN_CASHFLOW_PROJECTION_SCHEMA,
    SOLVE_CASHFLOW_CONTRIBUTION_SPEC["name"]: SOLVE_CASHFLOW_CONTRIBUTION_SCHEMA,
    GET_CASHFLOW_ANALYSIS_SPEC["name"]: GET_CASHFLOW_ANALYSIS_SCHEMA,
    AUDIT_CASHFLOW_ANALYSIS_SPEC["name"]: AUDIT_CASHFLOW_ANALYSIS_SCHEMA,
    RUN_ASSET_ALLOCATION_SPEC["name"]: RUN_ASSET_ALLOCATION_SCHEMA,
    GET_ASSET_ALLOCATION_ANALYSIS_SPEC["name"]: GET_ASSET_ALLOCATION_ANALYSIS_SCHEMA,
    COMPARE_QUANT_ANALYSES_SPEC["name"]: COMPARE_QUANT_ANALYSES_SCHEMA,
    CALCULATE_CASHFLOW_METRICS_SPEC["name"]: CALCULATE_CASHFLOW_METRICS_SCHEMA,
    CALCULATE_FINANCIAL_MATH_SPEC["name"]: CALCULATE_FINANCIAL_MATH_SCHEMA,
    QUERY_WOLFRAM_ALPHA_SPEC["name"]: QUERY_WOLFRAM_ALPHA_SCHEMA,
    RESEARCH_PUBLIC_FINANCIAL_FACT_SPEC[
        "name"
    ]: RESEARCH_PUBLIC_FINANCIAL_FACT_SCHEMA,
    REVIEW_PUBLIC_FACT_REUSE_SPEC["name"]: REVIEW_PUBLIC_FACT_REUSE_SCHEMA,
    REPORT_CALCULATION_CAPABILITY_GAP_SPEC[
        "name"
    ]: REPORT_CALCULATION_CAPABILITY_GAP_SCHEMA,
    CANCEL_SPECIALIST_JOB_SPEC["name"]: CANCEL_SPECIALIST_JOB_SCHEMA,
    ANALYZE_PORTFOLIO_RISK_SPEC["name"]: ANALYZE_PORTFOLIO_RISK_SCHEMA,
    ANALYZE_ASSET_LOCATION_SPEC["name"]: ANALYZE_ASSET_LOCATION_SCHEMA,
    UPDATE_OBJECTIVE_SPEC["name"]: UPDATE_OBJECTIVE_SCHEMA,
    RECORD_SERVICE_SPEC["name"]: RECORD_SERVICE_SCHEMA,
    RECORD_POLICY_REVIEW_SPEC["name"]: RECORD_POLICY_REVIEW_SCHEMA,
    CREATE_INVESTMENT_ASSESSMENT_SPEC["name"]: CREATE_INVESTMENT_ASSESSMENT_SCHEMA,
    RECORD_SIGNOFF_SPEC["name"]: RECORD_SIGNOFF_SCHEMA,
    ESTIMATE_ALLOCATION_SPEC["name"]: ESTIMATE_ALLOCATION_SCHEMA,
    LOOKUP_FRONTIER_SPEC["name"]: LOOKUP_FRONTIER_SCHEMA,
    RETRIEVE_CONVERSATION_HISTORY_SPEC[
        "name"
    ]: RETRIEVE_CONVERSATION_HISTORY_SCHEMA,
}


def _cashflow_concurrent_safe(args: Dict[str, Any]) -> bool:
    """Cashflow is concurrency-safe unless it reads a sibling allocation result."""
    return not bool(args.get("use_latest_asset_allocation", False))


def _definition_from_spec(spec: Dict[str, Any]) -> ToolDefinition:
    name = str(spec["name"])
    read_only = bool(spec.get("read_only"))
    concurrent_safe: Any = read_only
    compactor = None
    manifest_extras = None
    if name == RUN_CASHFLOW_PROJECTION_TOOL_NAME:
        concurrent_safe = _cashflow_concurrent_safe
        compactor = cashflow_compactor
        manifest_extras = cashflow_manifest_extras
    elif name == RUN_ASSET_ALLOCATION_TOOL_NAME:
        concurrent_safe = True
    return ToolDefinition(
        name=name,
        capability=str(spec["capability"]),
        description=str(spec.get("description") or ""),
        is_concurrent_safe=concurrent_safe,
        is_read_only=read_only,
        compactor=compactor,
        manifest_extras=manifest_extras,
    )


AGENT_TOOL_DEFINITIONS: Dict[str, ToolDefinition] = {
    definition.name: definition
    for definition in (_definition_from_spec(spec) for spec in AGENT_TOOL_SPECS)
}


def iter_agent_tool_specs() -> Tuple[Dict[str, Any], ...]:
    return AGENT_TOOL_SPECS


def agent_tool_schemas_by_name() -> Dict[str, Dict[str, Any]]:
    return dict(AGENT_TOOL_SCHEMAS)


def agent_tool_definitions_by_name() -> Dict[str, ToolDefinition]:
    return dict(AGENT_TOOL_DEFINITIONS)
