"""SDK tool wrapper for the onboarding specialist."""

from __future__ import annotations

from typing import Any

TOOL_NAME = "consult_onboarding_specialist"
CAPABILITY = "dispatch_onboarding"


def build_onboarding_specialist_tool(
    agent: Any,
    description: str,
    *,
    is_enabled: Any = True,
    max_turns: int | None = None,
) -> Any:
    resolved_max_turns = max_turns or getattr(agent, "_arc_max_turns", None)
    if not resolved_max_turns:
        raise ValueError("Onboarding agent is missing its max_turns profile")
    return agent.as_tool(
        tool_name=TOOL_NAME,
        tool_description=description,
        is_enabled=is_enabled,
        max_turns=resolved_max_turns,
    )
