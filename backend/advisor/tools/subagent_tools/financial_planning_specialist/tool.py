"""SDK tool wrapper for the financial planning specialist."""

from __future__ import annotations

from dataclasses import replace
import json
from typing import Any

from agents import Runner, StopAtTools

TOOL_NAME = "consult_financial_planning_specialist"
CAPABILITY = "dispatch_financial_planning"
_ASSESSMENT_SKILL_NAME = "internal-investment-assessment"
_ASSESSMENT_TOOL_NAME = "create_investment_assessment"


def build_financial_planning_specialist_tool(
    agent: Any,
    description: str,
    *,
    is_enabled: Any = True,
    max_turns: int | None = None,
) -> Any:
    resolved_max_turns = max_turns or getattr(agent, "_arc_max_turns", None)
    if not resolved_max_turns:
        raise ValueError("Financial planning agent is missing its max_turns profile")
    # The specialist must see a projection result so it can decide whether the
    # delegated request also needs a typed follow-up calculation. Main Advisor
    # still owns the outer dispatch; this only keeps the bounded specialist run
    # alive after cash flow completes.
    dispatch_agent = agent.clone(tool_use_behavior="run_llm_again")

    async def extract_output(run_result: Any) -> str:
        context = run_result.context_wrapper.context
        active_skill = str(
            context.active_skills.get("financial_planning") or ""
        ).strip()
        if active_skill == _ASSESSMENT_SKILL_NAME and not _assessment_attempted(context):
            assessment_tool = next(
                (
                    tool
                    for tool in agent.tools
                    if str(getattr(tool, "name", "") or "") == _ASSESSMENT_TOOL_NAME
                ),
                None,
            )
            if assessment_tool is not None:
                client_file = getattr(context, "client_file", None)
                money_pools = (
                    client_file.get("money_pools")
                    if isinstance(client_file, dict)
                    else []
                )
                user_message = str(getattr(context, "user_message", "") or "")
                continuation_agent = agent.clone(
                    tools=[assessment_tool],
                    model_settings=replace(agent.model_settings, tool_choice="required"),
                    tool_use_behavior=StopAtTools(
                        stop_at_tool_names=[_ASSESSMENT_TOOL_NAME]
                    ),
                )
                continuation = await Runner.run(
                    starting_agent=continuation_agent,
                    input=(
                        "Complete the selected internal-investment-assessment workflow now. "
                        "Call create_investment_assessment once using the exact confirmed money "
                        "pool and mandate fields below. Translate confirmed client language into "
                        "the tool's canonical enums and numeric units; never send sentinel or "
                        "placeholder values such as -1 or invalid. Return the tool result without "
                        "rerunning cash flow or inventing values.\n"
                        f"Current user request: {user_message[:1200]}\n"
                        "Latest Client File money pools:\n"
                        + json.dumps(money_pools, ensure_ascii=False, default=str)[:6000]
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


def _assessment_attempted(context: Any) -> bool:
    return any(
        isinstance(result, dict)
        and result.get("tool") == _ASSESSMENT_TOOL_NAME
        for result in (getattr(context, "tool_results", None) or [])
    )
