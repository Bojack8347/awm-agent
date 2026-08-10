"""Unified, agent-facing capability catalog."""

from __future__ import annotations

from typing import Dict, Iterable, Tuple

from advisor.tools.deterministic_tools.capabilities import (
    deterministic_capabilities_by_name,
)
from advisor.tools.subagent_tools.capabilities import subagent_capabilities_by_name


HIDDEN_DETERMINISTIC_TOOL_NAMES = {"record_deterministic_service_outcome"}

_DETERMINISTIC_AGENT_CAPABILITIES = {
    capability: tuple(
        tool_name
        for tool_name in tool_names
        if tool_name not in HIDDEN_DETERMINISTIC_TOOL_NAMES
    )
    for capability, tool_names in deterministic_capabilities_by_name().items()
    if any(tool_name not in HIDDEN_DETERMINISTIC_TOOL_NAMES for tool_name in tool_names)
}


def _merge_capability_catalogs(
    deterministic: Dict[str, Tuple[str, ...]],
    subagent: Dict[str, Tuple[str, ...]],
) -> Dict[str, Tuple[str, ...]]:
    overlap = set(deterministic) & set(subagent)
    if overlap:
        raise ValueError(f"Capability names collide across catalogs: {sorted(overlap)}")
    return {**deterministic, **subagent}


AGENT_CAPABILITY_CATALOG = _merge_capability_catalogs(
    _DETERMINISTIC_AGENT_CAPABILITIES,
    subagent_capabilities_by_name(),
)


def expand_agent_capabilities(capabilities: Iterable[str]) -> Tuple[str, ...]:
    expanded: set[str] = set()
    for capability in capabilities:
        try:
            expanded.update(AGENT_CAPABILITY_CATALOG[capability])
        except KeyError as exc:
            raise ValueError(f"Unknown agent capability: {capability}") from exc
    return tuple(sorted(expanded))
