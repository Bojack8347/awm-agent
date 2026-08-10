"""Derived capability catalog for deterministic agent tools."""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, Tuple

from advisor.tools.deterministic_tools.agent_tool_catalog import iter_agent_tool_specs


def _build_capability_catalog() -> Dict[str, Tuple[str, ...]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    seen_tools: set[str] = set()
    for spec in iter_agent_tool_specs():
        name = str(spec.get("name") or "").strip()
        capability = str(spec.get("capability") or "").strip()
        if not name:
            raise ValueError("Deterministic TOOL_SPEC is missing a non-empty name")
        if not capability:
            raise ValueError(f"Deterministic tool {name!r} is missing capability")
        if "read_only" not in spec:
            raise ValueError(f"Deterministic tool {name!r} is missing read_only")
        if name in seen_tools:
            raise ValueError(f"Deterministic tool {name!r} is declared more than once")
        seen_tools.add(name)
        grouped[capability].append(name)
    return {
        capability: tuple(sorted(tool_names))
        for capability, tool_names in sorted(grouped.items())
    }


DETERMINISTIC_CAPABILITY_CATALOG = _build_capability_catalog()


def deterministic_capabilities_by_name() -> Dict[str, Tuple[str, ...]]:
    return dict(DETERMINISTIC_CAPABILITY_CATALOG)
