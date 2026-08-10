"""Durable background wrapper for the Financial Planning specialist."""

from __future__ import annotations

import json
from typing import Any

from agents import FunctionTool
from agents.tool_context import ToolContext

from advisor.agents.background_jobs import dispatch_specialist_job
from advisor.agents.context import AwmAgentContext
from advisor.tools.subagent_tools.financial_planning_specialist.tool import (
    TOOL_NAME,
)


_PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "input": {
            "type": "string",
            "description": "Complete objective and bounded context for financial-planning work.",
        },
        "supersede": {
            "type": "boolean",
            "description": (
                "Set true when a newer objective must run after current work finishes. "
                "The running job is never cancelled; its stale result is discarded."
            ),
            "default": False,
        },
    },
    "required": ["input"],
    "additionalProperties": False,
}


def build_background_financial_planning_specialist_tool(
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
            specialist_key="financial_planning",
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
