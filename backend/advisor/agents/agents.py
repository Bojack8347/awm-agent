"""Native OpenAI Agents SDK declarations for AWM."""

from __future__ import annotations

from typing import Dict, List

from agents import Agent, ModelSettings, RunContextWrapper
from openai.types.shared import Reasoning

from advisor.agents.catalog import (
    ASSESSMENT_REVALIDATION_SPECIALIST,
    AGENT_DEFINITIONS,
    DIAGNOSIS_SPECIALIST,
    FINANCIAL_PLANNING_SPECIALIST,
    INVESTMENT_SOLUTION_SPECIALIST,
    MAIN_ADVISOR,
    ONBOARDING_SPECIALIST,
    POLICY_REVIEW_SPECIALIST,
    AgentDefinition,
)
from advisor.agents.context import AwmAgentContext
from advisor.agents.instructions import build_agent_instructions, build_main_instructions
from advisor.agents.skills import DEFAULT_SKILL_REGISTRY, SkillDefinition
from advisor.agents.tools import build_arc_agent_tools
from advisor.agents.agent_tool_catalog import (
    ASSESSMENT_REVALIDATION_TOOL_NAME,
    DIAGNOSIS_TOOL_NAME,
    FINANCIAL_PLANNING_TOOL_NAME,
    INVESTMENT_SOLUTION_TOOL_NAME,
    ONBOARDING_TOOL_NAME,
    POLICY_REVIEW_TOOL_NAME,
)
from advisor.llm.model_capabilities import effective_reasoning_effort
from advisor.tools.subagent_tools.assessment_revalidation_specialist.tool import (
    build_assessment_revalidation_specialist_tool,
)
from advisor.tools.subagent_tools.diagnosis_specialist.tool import (
    build_diagnosis_specialist_tool,
)
from advisor.tools.subagent_tools.financial_planning_specialist.tool import (
    build_financial_planning_specialist_tool,
)
from advisor.tools.subagent_tools.financial_planning_specialist.background_tool import (
    build_background_financial_planning_specialist_tool,
)
from advisor.tools.subagent_tools.investment_solution_specialist.background_tool import (
    build_background_investment_solution_specialist_tool,
)
from advisor.tools.subagent_tools.investment_solution_specialist.tool import (
    build_investment_solution_specialist_tool,
)
from advisor.tools.subagent_tools.onboarding_specialist.tool import (
    build_onboarding_specialist_tool,
)
from advisor.tools.subagent_tools.policy_review_specialist.tool import (
    build_policy_review_specialist_tool,
)


def _main_advisor_instructions(
    run_context: RunContextWrapper[AwmAgentContext],
    _agent: Agent[AwmAgentContext],
) -> str:
    active_skill_name = run_context.context.active_skills.get(MAIN_ADVISOR.key)
    return build_main_instructions(
        run_context.context.client_file,
        active_skill_name=active_skill_name,
        skill_candidates=run_context.context.skill_candidates,
        artifact_context=run_context.context.artifact_context,
        trusted_action_context=run_context.context.trusted_action_context,
        background_jobs=run_context.context.background_jobs,
    )


def _agent_instructions(definition: AgentDefinition):
    def instructions(
        run_context: RunContextWrapper[AwmAgentContext],
        _agent: Agent[AwmAgentContext],
    ) -> str:
        active_skill_name = run_context.context.active_skills.get(definition.key)
        return build_agent_instructions(
            definition,
            active_skill_name=active_skill_name,
            skill_candidates=run_context.context.skill_candidates,
            artifact_context=run_context.context.artifact_context,
            trusted_action_context=run_context.context.trusted_action_context,
            background_jobs=run_context.context.background_jobs,
            client_file=run_context.context.client_file,
        )

    return instructions


def _installed_skills(definition: AgentDefinition) -> List[SkillDefinition]:
    return DEFAULT_SKILL_REGISTRY.for_agent(definition.key)


def _model_settings(definition: AgentDefinition) -> ModelSettings:
    effort = effective_reasoning_effort(definition.model, definition.reasoning_effort)
    return ModelSettings(
        parallel_tool_calls=definition.parallel_tool_calls,
        reasoning=(Reasoning(effort=effort) if effort else None),
        tool_choice=definition.tool_choice,
    )


def _build_agent(definition: AgentDefinition) -> Agent[AwmAgentContext]:
    model = definition.model
    agent = Agent(
        name=definition.name,
        handoff_description=definition.description,
        instructions=_agent_instructions(definition),
        tools=build_arc_agent_tools(
            agent_key=definition.key,
            base_tool_names=definition.tool_names,
            installed_skills=_installed_skills(definition),
        ),
        model=model,
        model_settings=_model_settings(definition),
        tool_use_behavior=definition.tool_use_behavior or "run_llm_again",
    )
    setattr(agent, "_arc_max_turns", definition.max_turns)
    setattr(agent, "_arc_timeout_seconds", definition.resolved_timeout_seconds)
    return agent


def build_financial_planning_specialist() -> Agent[AwmAgentContext]:
    return _build_agent(FINANCIAL_PLANNING_SPECIALIST)


def build_onboarding_specialist() -> Agent[AwmAgentContext]:
    return _build_agent(ONBOARDING_SPECIALIST)


def build_investment_solution_specialist() -> Agent[AwmAgentContext]:
    return _build_agent(INVESTMENT_SOLUTION_SPECIALIST)


def build_policy_review_specialist() -> Agent[AwmAgentContext]:
    return _build_agent(POLICY_REVIEW_SPECIALIST)


def build_assessment_revalidation_specialist() -> Agent[AwmAgentContext]:
    return _build_agent(ASSESSMENT_REVALIDATION_SPECIALIST)


def build_diagnosis_specialist() -> Agent[AwmAgentContext]:
    return _build_agent(DIAGNOSIS_SPECIALIST)


def build_main_advisor_agent() -> Agent[AwmAgentContext]:
    financial_planning = build_financial_planning_specialist()
    investment_solution = build_investment_solution_specialist()
    installed_skills = _installed_skills(MAIN_ADVISOR)
    tools: List = build_arc_agent_tools(
        agent_key=MAIN_ADVISOR.key,
        base_tool_names=MAIN_ADVISOR.tool_names,
        installed_skills=installed_skills,
        subagent_tool_builders={
            FINANCIAL_PLANNING_TOOL_NAME: lambda is_enabled: _build_financial_planning_dispatch_tool(
                financial_planning,
                is_enabled=is_enabled,
            ),
            INVESTMENT_SOLUTION_TOOL_NAME: lambda is_enabled: _build_investment_solution_dispatch_tool(
                investment_solution,
                is_enabled=is_enabled,
            ),
            ONBOARDING_TOOL_NAME: lambda is_enabled: build_onboarding_specialist_tool(
                build_onboarding_specialist(),
                ONBOARDING_SPECIALIST.description,
                is_enabled=is_enabled,
                max_turns=ONBOARDING_SPECIALIST.max_turns,
            ),
            POLICY_REVIEW_TOOL_NAME: lambda is_enabled: build_policy_review_specialist_tool(
                build_policy_review_specialist(),
                POLICY_REVIEW_SPECIALIST.description,
                is_enabled=is_enabled,
                max_turns=POLICY_REVIEW_SPECIALIST.max_turns,
            ),
            ASSESSMENT_REVALIDATION_TOOL_NAME: lambda is_enabled: build_assessment_revalidation_specialist_tool(
                build_assessment_revalidation_specialist(),
                ASSESSMENT_REVALIDATION_SPECIALIST.description,
                is_enabled=is_enabled,
                max_turns=ASSESSMENT_REVALIDATION_SPECIALIST.max_turns,
            ),
            DIAGNOSIS_TOOL_NAME: lambda is_enabled: build_diagnosis_specialist_tool(
                build_diagnosis_specialist(),
                DIAGNOSIS_SPECIALIST.description,
                is_enabled=is_enabled,
                max_turns=DIAGNOSIS_SPECIALIST.max_turns,
            ),
        },
    )
    model = MAIN_ADVISOR.model
    return Agent(
        name=MAIN_ADVISOR.name,
        handoff_description=MAIN_ADVISOR.description,
        instructions=_main_advisor_instructions,
        tools=tools,
        model=model,
        model_settings=_model_settings(MAIN_ADVISOR),
        tool_use_behavior=MAIN_ADVISOR.tool_use_behavior or "run_llm_again",
    )


def _build_investment_solution_dispatch_tool(
    agent: Agent[AwmAgentContext],
    *,
    is_enabled,
):
    builder = (
        build_background_investment_solution_specialist_tool
        if INVESTMENT_SOLUTION_SPECIALIST.execution == "background"
        else build_investment_solution_specialist_tool
    )
    return builder(
        agent,
        INVESTMENT_SOLUTION_SPECIALIST.description,
        is_enabled=is_enabled,
        max_turns=INVESTMENT_SOLUTION_SPECIALIST.max_turns,
    )


def _build_financial_planning_dispatch_tool(
    agent: Agent[AwmAgentContext],
    *,
    is_enabled,
):
    builder = (
        build_background_financial_planning_specialist_tool
        if FINANCIAL_PLANNING_SPECIALIST.execution == "background"
        else build_financial_planning_specialist_tool
    )
    return builder(
        agent,
        FINANCIAL_PLANNING_SPECIALIST.description,
        is_enabled=is_enabled,
        max_turns=FINANCIAL_PLANNING_SPECIALIST.max_turns,
    )


def describe_agent_graph() -> Dict[str, object]:
    """Return the legacy graph view as a projection of the golden serializer."""

    from advisor.agents.manifest_snapshot import resolved_agent_graph

    snapshot = resolved_agent_graph()
    agents = snapshot["agents"]
    agents_by_name = {
        record["name"]: record
        for record in agents.values()
    }
    specialists = {
        name: record
        for name, record in agents_by_name.items()
        if name != MAIN_ADVISOR.name
    }
    return {
        "main_agent": MAIN_ADVISOR.name,
        "runtime": "openai_agents_sdk",
        "main_agent_tools": list(agents[MAIN_ADVISOR.key]["tool_names"]),
        "agent_base_tools": {
            name: list(record["tool_names"])
            for name, record in agents_by_name.items()
        },
        "installed_skills": {
            name: list(record["installed_skills"])
            for name, record in agents_by_name.items()
        },
        "candidate_tools": {
            name: list(record["candidate_tools"])
            for name, record in agents_by_name.items()
        },
        "specialists_as_tools": [
            agents[key]["name"]
            for key in (
                "financial_planning",
                "investment_solution",
            )
            if agents[key]["subagent_tool_enabled"] is True
        ],
        "disabled_specialists_as_tools": [
            agents[key]["name"]
            for key in (
                "onboarding",
                "policy_review",
                "assessment_revalidation",
                "diagnosis",
            )
            if agents[key]["subagent_tool_enabled"] is False
        ],
        "specialist_tools": {
            name: list(record["tool_names"])
            for name, record in specialists.items()
        },
        "deterministic_boundaries": [
            "Client File persistence",
            "full cashflow model",
            "asset allocation model",
            "policy/proposal lifecycle writes",
            "guarded service execution",
        ],
    }
