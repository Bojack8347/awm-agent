"""Tool contracts exposed to the AWM agent layer."""

from __future__ import annotations

from typing import Dict, Iterable, List

from pydantic import BaseModel, ConfigDict

from advisor.tools.deterministic_tools.agent_tool_catalog import iter_agent_tool_specs


class AgentToolDefinition(BaseModel):
    """Minimal deterministic tool contract for agent orchestration."""

    model_config = ConfigDict(extra="forbid")

    name: str
    capability: str
    description: str
    writeback_target: str
    read_only: bool = False
    irreversible: bool = False
    requires_explicit_consent: bool = False


class ToolRegistry:
    """Registry of deterministic capabilities the Main Agent may call."""

    def __init__(self, tools: Iterable[AgentToolDefinition]):
        self._tools: Dict[str, AgentToolDefinition] = {tool.name: tool for tool in tools}

    def get(self, name: str) -> AgentToolDefinition:
        return self._tools[name]

    def list(self) -> List[AgentToolDefinition]:
        return list(self._tools.values())


def build_default_tool_registry() -> ToolRegistry:
    return ToolRegistry(
        AgentToolDefinition(**tool_spec)
        for tool_spec in iter_agent_tool_specs()
    )
