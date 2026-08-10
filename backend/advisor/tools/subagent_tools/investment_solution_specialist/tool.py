"""SDK tool wrapper for the investment solution specialist."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from agents import Runner, StopAtTools

TOOL_NAME = "consult_investment_solution_specialist"
CAPABILITY = "dispatch_investment_solution"
_SKILL_NAME = "investment-policy-statement"
_ALLOCATION_TOOL_NAME = "run_asset_allocation"


def build_investment_solution_specialist_tool(
    agent: Any,
    description: str,
    *,
    is_enabled: Any = True,
    max_turns: int | None = None,
) -> Any:
    resolved_max_turns = max_turns or getattr(agent, "_arc_max_turns", None)
    if not resolved_max_turns:
        raise ValueError("Investment solution agent is missing its max_turns profile")

    # A specialist invoked as ``agent.as_tool`` does not pass through the
    # top-level AwmAgentsRuntime post-activation continuation hook. Keep its
    # dispatch loop optional so a dynamically empty tool set can still produce
    # a response, then run one bounded continuation after skill activation.
    # Main Advisor still decides whether this specialist should be called.
    dispatch_agent = agent.clone(
        model_settings=replace(agent.model_settings, tool_choice="auto"),
        tool_use_behavior=StopAtTools(
            stop_at_tool_names=[_ALLOCATION_TOOL_NAME]
        ),
    )

    async def extract_output(run_result: Any) -> str:
        context = run_result.context_wrapper.context
        if not _allocation_attempted(context):
            active_skill = str(
                context.active_skills.get("investment_solution") or ""
            ).strip()
            allocation_tool = next(
                (
                    tool
                    for tool in agent.tools
                    if str(getattr(tool, "name", "") or "") == _ALLOCATION_TOOL_NAME
                ),
                None,
            )
            if active_skill == _SKILL_NAME and allocation_tool is not None:
                continuation_agent = agent.clone(
                    tools=[allocation_tool],
                    model_settings=replace(
                        agent.model_settings,
                        tool_choice="auto",
                    ),
                    tool_use_behavior=StopAtTools(
                        stop_at_tool_names=[_ALLOCATION_TOOL_NAME]
                    ),
                )
                continuation = await Runner.run(
                    starting_agent=continuation_agent,
                    input=(
                        "Complete the selected investment-policy-statement workflow now. "
                        "Call run_asset_allocation once using only the exact signed "
                        "assessment_ref in the latest Client File. Return the tool "
                        "result without inventing portfolio values."
                    ),
                    context=context,
                    max_turns=min(int(resolved_max_turns), 4),
                )
                return str(continuation.final_output or "")
        return str(run_result.final_output or "")

    return dispatch_agent.as_tool(
        tool_name=TOOL_NAME,
        tool_description=description,
        custom_output_extractor=extract_output,
        is_enabled=is_enabled,
        max_turns=resolved_max_turns,
    )


def _allocation_attempted(context: Any) -> bool:
    return any(
        isinstance(result, dict)
        and result.get("tool") == _ALLOCATION_TOOL_NAME
        for result in (getattr(context, "tool_results", None) or [])
    )
