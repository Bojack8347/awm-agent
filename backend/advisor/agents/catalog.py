"""Agent definitions parsed from authoritative instruction frontmatter."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from advisor.agents.manifest_loader import load_agent_records


DEFAULT_AGENT_RUN_TIMEOUT_SECONDS = 180.0


@dataclass(frozen=True)
class AgentDefinition:
    key: str
    name: str
    description: str
    default_model: str
    instructions: str
    system_instructions: str
    capabilities: Tuple[str, ...]
    tool_names: Tuple[str, ...]
    max_turns: int
    reasoning_effort: Optional[str] = None
    tool_choice: Optional[str] = None
    tool_use_behavior: Optional[str] = None
    parallel_tool_calls: bool = False
    timeout_seconds: Optional[float] = None
    execution: str = "wait"
    channel_scope: str = "text"
    instructions_path: str = ""
    system_instructions_path: str = ""

    @property
    def model(self) -> str:
        return (
            os.getenv(f"AWM_AGENT_{self.key.upper()}_MODEL")
            or self.default_model
        ).strip()

    @property
    def resolved_timeout_seconds(self) -> float:
        return (
            self.timeout_seconds
            if self.timeout_seconds is not None
            else DEFAULT_AGENT_RUN_TIMEOUT_SECONDS
        )


def _definition_from_record(record: dict) -> AgentDefinition:
    return AgentDefinition(
        key=record["key"],
        name=record["name"],
        description=record["description"],
        default_model=record["default_model"],
        instructions=record["instructions"],
        system_instructions=record["system_instructions"],
        capabilities=record["capabilities"],
        tool_names=record["tool_names"],
        max_turns=record["max_turns"],
        reasoning_effort=record.get("reasoning_effort"),
        tool_choice=record.get("tool_choice"),
        tool_use_behavior=record.get("tool_use_behavior"),
        parallel_tool_calls=record["parallel_tool_calls"],
        timeout_seconds=record.get("timeout_seconds"),
        execution=record["execution"],
        channel_scope=record.get("channel_scope", "text"),
        instructions_path=record["instructions_path"],
        system_instructions_path=record["system_instructions_path"],
    )


AGENT_DEFINITIONS: Tuple[AgentDefinition, ...] = tuple(
    _definition_from_record(record)
    for record in load_agent_records()
)
AGENT_DEFINITIONS_BY_NAME: Dict[str, AgentDefinition] = {
    definition.name: definition
    for definition in AGENT_DEFINITIONS
}
AGENT_DEFINITIONS_BY_KEY: Dict[str, AgentDefinition] = {
    definition.key: definition
    for definition in AGENT_DEFINITIONS
}


def _agent(key: str) -> AgentDefinition:
    try:
        return AGENT_DEFINITIONS_BY_KEY[key]
    except KeyError as exc:
        raise ValueError(f"Required AWM agent declaration is missing: {key}") from exc


MAIN_ADVISOR = _agent("main_advisor")
ONBOARDING_SPECIALIST = _agent("onboarding")
DIAGNOSIS_SPECIALIST = _agent("diagnosis")
POLICY_REVIEW_SPECIALIST = _agent("policy_review")
ASSESSMENT_REVALIDATION_SPECIALIST = _agent("assessment_revalidation")
FINANCIAL_PLANNING_SPECIALIST = _agent("financial_planning")
INVESTMENT_SOLUTION_SPECIALIST = _agent("investment_solution")

DIAGNOSIS_SNAPSHOT_REQUEST = (
    "Generate a diagnosis snapshot for the supplied Client File knowledge snapshot. "
    "Use deterministic tools for quantitative findings when needed. Return JSON only with "
    "diagnosis_data and optional model_metadata. diagnosis_data should contain grounded "
    "categories or diagnoses, severity, rationale, and evidence identifiers when available."
)

WORKFLOW_NAME = "AWM Main Advisor"
TRACE_RUNTIME_NAME = "openai_agents_sdk"


def agent_key_for_name(agent_name: str) -> str:
    return AGENT_DEFINITIONS_BY_NAME.get(agent_name, MAIN_ADVISOR).key
