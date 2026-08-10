"""Unified AWM agent-facing tool catalog and access policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Literal, Optional, Set, Tuple

from advisor.agents.skills import SkillDefinition
from advisor.tools.deterministic_tools.agent_tool_catalog import agent_tool_definitions_by_name
from advisor.tools.capabilities import (
    AGENT_CAPABILITY_CATALOG,
    expand_agent_capabilities,
)
from advisor.tools.subagent_tools.financial_planning_specialist.tool import (
    TOOL_NAME as FINANCIAL_PLANNING_TOOL_NAME,
)
from advisor.tools.subagent_tools.investment_solution_specialist.tool import (
    TOOL_NAME as INVESTMENT_SOLUTION_TOOL_NAME,
)
from advisor.tools.subagent_tools.onboarding_specialist.tool import (
    TOOL_NAME as ONBOARDING_TOOL_NAME,
)
from advisor.tools.subagent_tools.policy_review_specialist.tool import (
    TOOL_NAME as POLICY_REVIEW_TOOL_NAME,
)
from advisor.tools.subagent_tools.assessment_revalidation_specialist.tool import (
    TOOL_NAME as ASSESSMENT_REVALIDATION_TOOL_NAME,
)
from advisor.tools.subagent_tools.diagnosis_specialist.tool import (
    TOOL_NAME as DIAGNOSIS_TOOL_NAME,
)


AgentToolKind = Literal["deterministic", "subagent"]
_CALCULATION_CAPABILITY_GAP_TOOL_NAME = "report_calculation_capability_gap"
_EVERGREEN_BASE_TOOL_NAMES = {"retrieve_conversation_history"}


@dataclass(frozen=True)
class AgentToolCatalogEntry:
    name: str
    kind: AgentToolKind


_HIDDEN_DETERMINISTIC_TOOL_NAMES = {"record_deterministic_service_outcome"}
_DETERMINISTIC_TOOL_NAMES: Tuple[str, ...] = tuple(
    name
    for name in agent_tool_definitions_by_name()
    if name not in _HIDDEN_DETERMINISTIC_TOOL_NAMES
)
_ENABLED_SUBAGENT_TOOL_NAMES: Tuple[str, ...] = (
    FINANCIAL_PLANNING_TOOL_NAME,
    INVESTMENT_SOLUTION_TOOL_NAME,
)
_DISABLED_SUBAGENT_TOOL_NAMES: Tuple[str, ...] = (
    ONBOARDING_TOOL_NAME,
    POLICY_REVIEW_TOOL_NAME,
    ASSESSMENT_REVALIDATION_TOOL_NAME,
    DIAGNOSIS_TOOL_NAME,
)
_SUBAGENT_TOOL_NAMES: Tuple[str, ...] = _ENABLED_SUBAGENT_TOOL_NAMES + _DISABLED_SUBAGENT_TOOL_NAMES


AGENT_TOOL_CATALOG: Dict[str, AgentToolCatalogEntry] = {
    **{
        name: AgentToolCatalogEntry(name=name, kind="deterministic")
        for name in _DETERMINISTIC_TOOL_NAMES
    },
    **{
        name: AgentToolCatalogEntry(name=name, kind="subagent")
        for name in _SUBAGENT_TOOL_NAMES
    },
}

CAPABILITY_CATALOG = AGENT_CAPABILITY_CATALOG

_CAPABILITY_TOOL_NAMES = {
    tool_name
    for tool_names in CAPABILITY_CATALOG.values()
    for tool_name in tool_names
}
if _CAPABILITY_TOOL_NAMES != set(AGENT_TOOL_CATALOG):
    missing = sorted(set(AGENT_TOOL_CATALOG) - _CAPABILITY_TOOL_NAMES)
    extra = sorted(_CAPABILITY_TOOL_NAMES - set(AGENT_TOOL_CATALOG))
    raise ValueError(
        f"Capability catalog does not match agent tool catalog; missing={missing}, extra={extra}"
    )


def iter_agent_tool_entries() -> Tuple[AgentToolCatalogEntry, ...]:
    return tuple(AGENT_TOOL_CATALOG.values())


def expand_capabilities(capabilities: Iterable[str]) -> Tuple[str, ...]:
    return expand_agent_capabilities(capabilities)


def iter_subagent_tool_names() -> Tuple[str, ...]:
    return _SUBAGENT_TOOL_NAMES


def iter_enabled_subagent_tool_names() -> Tuple[str, ...]:
    return _ENABLED_SUBAGENT_TOOL_NAMES


def iter_disabled_subagent_tool_names() -> Tuple[str, ...]:
    return _DISABLED_SUBAGENT_TOOL_NAMES


def agent_tool_kind(tool_name: str) -> Optional[AgentToolKind]:
    entry = AGENT_TOOL_CATALOG.get(tool_name)
    return entry.kind if entry else None


def is_deterministic_agent_tool(tool_name: str) -> bool:
    return agent_tool_kind(tool_name) == "deterministic"


def is_subagent_agent_tool(tool_name: str) -> bool:
    return agent_tool_kind(tool_name) == "subagent"


def calculation_capability_gap_reported(context) -> bool:
    """Return whether this turn has reached its terminal calculation boundary."""

    for item in getattr(context, "tool_results", []):
        if not isinstance(item, dict):
            continue
        if (
            item.get("tool") == _CALCULATION_CAPABILITY_GAP_TOOL_NAME
            and item.get("ok") is True
            and (
                item.get("terminal") is True
                or (
                    isinstance(item.get("full_result"), dict)
                    and item["full_result"].get("terminal") is True
                )
            )
        ):
            return True
    return False


def resolve_candidate_tool_names(
    base_tool_names: Iterable[str],
    installed_skills: Iterable[SkillDefinition],
) -> Set[str]:
    names = set(base_tool_names)
    for skill in installed_skills:
        names.update(skill.tool_names)
    return names


def resolve_effective_tool_names(
    base_tool_names: Iterable[str],
    installed_skills: Iterable[SkillDefinition],
    active_skill_name: Optional[str],
) -> Set[str]:
    if active_skill_name:
        for skill in installed_skills:
            if skill.name == active_skill_name:
                return set(skill.tool_names) | (
                    set(base_tool_names) & _EVERGREEN_BASE_TOOL_NAMES
                )
        return set()
    return set(base_tool_names)


def agent_tool_is_enabled(
    context,
    *,
    agent_key: str,
    tool_name: str,
    base_tool_names: Set[str],
    installed_skills: Iterable[SkillDefinition],
) -> bool:
    active_skill_name = context.active_skills.get(agent_key)
    effective_tool_names = resolve_effective_tool_names(
        base_tool_names,
        installed_skills,
        active_skill_name,
    )
    if tool_name not in effective_tool_names:
        return False
    if calculation_capability_gap_reported(context):
        return False
    tool_results = getattr(context, "tool_results", [])
    if agent_key == "financial_planning":
        prior_results = [
            item
            for item in tool_results
            if isinstance(item, dict)
            and item.get("tool") == tool_name
            and item.get("executed_by_agent") in {None, agent_key}
        ]
        prior_attempts = len(prior_results)
        if tool_name == "query_wolfram_alpha" and prior_attempts >= 1:
            # The external fallback is a single bounded attempt. The agent
            # remains responsible for choosing it; this guard only prevents
            # retries from becoming an unbounded provider loop.
            return False
        if tool_name == "calculate_financial_math":
            if any(item.get("ok") is True for item in prior_results):
                # One complete plan may expose multiple outputs. Once it has
                # succeeded, use that evidence rather than creating a second,
                # potentially conflicting calculation trace.
                return False
            if prior_attempts >= 2:
                # Permit one schema/source repair, then require a capability gap
                # or a response from already validated evidence.
                return False
    if tool_name == "get_cashflow_analysis":
        artifact_context = getattr(context, "artifact_context", {})
        references = (
            artifact_context.get("references")
            if isinstance(artifact_context, dict)
            and isinstance(artifact_context.get("references"), list)
            else []
        )
        has_cashflow_reference = any(
            isinstance(item, dict)
            and item.get("domain") == "cashflow"
            and str(item.get("analysis_id") or "").strip()
            for item in references
        )
        has_cashflow_result_this_turn = any(
            isinstance(item, dict)
            and item.get("tool") == "run_cashflow_projection"
            and item.get("ok") is True
            and str(item.get("analysis_id") or "").strip()
            for item in tool_results
        )
        if not has_cashflow_reference and not has_cashflow_result_this_turn:
            # Retrieval is meaningful only when the durable conversation names
            # an immutable analysis. With no reference, keep the real model run
            # available instead of inviting a guaranteed empty lookup.
            return False
    if tool_name == "get_cashflow_analysis" and any(
        isinstance(item, dict)
        and item.get("tool") == "get_cashflow_analysis"
        and item.get("ok") is False
        and (
            item.get("requires_rerun") is True
            or item.get("error") in {
                "cashflow_analysis_not_found",
                "cashflow_analysis_stale",
            }
        )
        for item in tool_results
    ):
        # A missing/stale read-only artifact cannot become available by
        # repeating the same lookup in this turn. Keep the real run tool
        # visible so the specialist can create fresh model evidence.
        return False
    if any(
        isinstance(item, dict)
        and item.get("tool") == "request_clarification"
        and item.get("ok") is True
        for item in getattr(context, "tool_results", [])
    ):
        return False
    return context.allowed_tools is None or tool_name in context.allowed_tools
