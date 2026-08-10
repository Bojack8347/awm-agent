"""Human-readable serialization of the fully resolved agent graph."""

from __future__ import annotations

from typing import Any, Dict

from advisor.agents import agent_tool_catalog
from advisor.agents.catalog import (
    AGENT_DEFINITIONS,
    DEFAULT_AGENT_RUN_TIMEOUT_SECONDS,
)
from advisor.agents.skills import DEFAULT_SKILL_REGISTRY


def resolved_agent_graph() -> Dict[str, Any]:
    enabled_subagent_tools = set(agent_tool_catalog.iter_enabled_subagent_tool_names())
    disabled_subagent_tools = set(
        agent_tool_catalog.iter_disabled_subagent_tool_names()
    )

    def agent_snapshot(agent) -> Dict[str, Any]:
        installed_skills = sorted(
            DEFAULT_SKILL_REGISTRY.for_agent(agent.key),
            key=lambda item: item.name,
        )
        dispatch_tool_name = (
            None if agent.key == "main_advisor" else f"consult_{agent.key}_specialist"
        )
        return {
            "name": agent.name,
            "description": agent.description,
            "channel_scope": agent.channel_scope,
            "default_model": agent.default_model,
            "max_turns": agent.max_turns,
            "reasoning_effort": agent.reasoning_effort,
            "tool_choice": agent.tool_choice,
            "tool_use_behavior": (agent.tool_use_behavior or "run_llm_again"),
            "parallel_tool_calls": agent.parallel_tool_calls,
            "timeout_seconds": agent.resolved_timeout_seconds,
            "execution": agent.execution,
            "capabilities": list(agent.capabilities),
            "tool_names": list(agent.tool_names),
            "installed_skills": [skill.name for skill in installed_skills],
            "candidate_tools": sorted(
                agent_tool_catalog.resolve_candidate_tool_names(
                    agent.tool_names,
                    installed_skills,
                )
            ),
            "effective_tools_by_skill": {
                skill.name: sorted(
                    agent_tool_catalog.resolve_effective_tool_names(
                        agent.tool_names,
                        installed_skills,
                        skill.name,
                    )
                )
                for skill in installed_skills
            },
            "subagent_tool_enabled": (
                None
                if dispatch_tool_name is None
                else dispatch_tool_name in enabled_subagent_tools
            ),
        }

    return {
        "defaults": {
            "parallel_tool_calls": False,
            "timeout_seconds": DEFAULT_AGENT_RUN_TIMEOUT_SECONDS,
        },
        "agents": {
            agent.key: agent_snapshot(agent)
            for agent in sorted(AGENT_DEFINITIONS, key=lambda item: item.key)
        },
        "skills": {
            skill.name: {
                "summary": skill.summary,
                "when_to_use": skill.when_to_use,
                "allowed_agents": list(skill.allowed_agents),
                "capabilities": list(skill.capabilities),
                "tool_names": list(skill.tool_names),
            }
            for skill in sorted(
                DEFAULT_SKILL_REGISTRY.list(),
                key=lambda item: item.name,
            )
        },
        "specialist_dispatch": {
            "enabled": sorted(enabled_subagent_tools),
            "disabled": sorted(disabled_subagent_tools),
        },
    }
