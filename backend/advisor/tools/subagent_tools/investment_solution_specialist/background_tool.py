"""Durable background wrapper for the Investment Solution specialist."""

from __future__ import annotations

import json
from typing import Any

from agents import FunctionTool
from agents.tool_context import ToolContext

from advisor.agents.background_jobs import dispatch_specialist_job
from advisor.agents.context import AwmAgentContext
from advisor.tools.subagent_tools.investment_solution_specialist.tool import (
    TOOL_NAME,
)


_PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "input": {
            "type": "string",
            "description": "Complete objective and bounded context for proposal work.",
        },
        "supersede": {
            "type": "boolean",
            "description": (
                "Set true only when the client explicitly replaces running "
                "proposal work with this objective."
            ),
            "default": False,
        },
    },
    "required": ["input"],
    "additionalProperties": False,
}


def build_background_investment_solution_specialist_tool(
    agent: Any,
    description: str,
    *,
    is_enabled: Any = True,
    max_turns: int,
    run_config: Any = None,
) -> FunctionTool:
    async def invoke(
        tool_context: ToolContext[AwmAgentContext],
        arguments_json: str,
    ) -> dict[str, Any]:
        arguments = json.loads(arguments_json or "{}")
        objective = str(arguments.get("input") or "").strip()
        supersede = arguments.get("supersede", False) is True
        receipt = dispatch_specialist_job(
            specialist_key="investment_solution",
            objective=objective,
            source_context=tool_context.context,
            agent=agent,
            max_turns=max_turns,
            run_config=run_config,
            supersede=supersede,
        )
        tool_context.context.tool_results.append(
            {
                "tool": TOOL_NAME,
                "ok": True,
                "executed_by_agent": "main_advisor",
                **receipt,
            }
        )
        return receipt

    tool = FunctionTool(
        name=TOOL_NAME,
        description=description,
        params_json_schema=_PARAMS_SCHEMA,
        on_invoke_tool=invoke,
        strict_json_schema=True,
        is_enabled=is_enabled,
    )
    setattr(tool, "timeout_seconds", 30.0)
    setattr(tool, "timeout_error_function", None)
    return tool
