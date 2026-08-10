"""SDK tool wrapper for the assessment revalidation specialist."""

from __future__ import annotations

from typing import Any

TOOL_NAME = "consult_assessment_revalidation_specialist"
CAPABILITY = "dispatch_assessment_revalidation"


def build_assessment_revalidation_specialist_tool(
    agent: Any,
    description: str,
    *,
    is_enabled: Any = True,
    max_turns: int | None = None,
) -> Any:
    resolved_max_turns = max_turns or getattr(agent, "_arc_max_turns", None)
    if not resolved_max_turns:
        raise ValueError("Assessment revalidation agent is missing its max_turns profile")
    return agent.as_tool(
        tool_name=TOOL_NAME,
        tool_description=description,
        is_enabled=is_enabled,
        max_turns=resolved_max_turns,
    )
