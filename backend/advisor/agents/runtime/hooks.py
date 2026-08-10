from __future__ import annotations

import time
from typing import Any, Dict, Optional

from agents import RunHooks

from advisor.agents.background_jobs import (
    SpecialistJobCancelled,
    specialist_job_cancellation_requested,
)
from advisor.agents.context import AwmAgentContext
from advisor.tracing.tracing import append_trace


class AwmRunHooks(RunHooks[AwmAgentContext]):
    """Mirror SDK lifecycle events into AWM's durable trace contract."""

    def __init__(
        self,
        *,
        specialist_job_id: Optional[str] = None,
        cancellation_event: Any = None,
    ) -> None:
        self._llm_started: Dict[str, float] = {}
        self._tool_started: Dict[str, float] = {}
        self._specialist_job_id = specialist_job_id
        self._cancellation_event = cancellation_event

    def _record(self, context: AwmAgentContext, **event: Any) -> None:
        context.trace_events = append_trace(
            context.trace_events,
            trace_id=context.trace_id,
            turn_id=context.turn_id,
            parent_span_id=context.root_span_id,
            **event,
        )

    async def on_agent_start(self, context, agent) -> None:
        self._record(
            context.context,
            event="sdk_agent_started",
            node=agent.name,
            summary=f"Agents SDK started {agent.name}.",
            data={"agent": agent.name, "runtime": "openai_agents_sdk"},
        )

    async def on_agent_end(self, context, agent, output) -> None:
        self._record(
            context.context,
            event="sdk_agent_completed",
            node=agent.name,
            summary=f"Agents SDK completed {agent.name}.",
            output_data={"output": str(output)},
            data={"agent": agent.name, "runtime": "openai_agents_sdk"},
        )

    async def on_llm_start(self, context, agent, system_prompt, input_items) -> None:
        self._llm_started[agent.name] = time.perf_counter()
        self._record(
            context.context,
            event="sdk_llm_started",
            node=agent.name,
            summary=f"Calling model for {agent.name}.",
            input_data={"input_item_count": len(input_items)},
            data={"agent": agent.name, "model": str(agent.model)},
        )

    async def on_llm_end(self, context, agent, response) -> None:
        started = self._llm_started.pop(agent.name, time.perf_counter())
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        call = {
            "agent": agent.name,
            "model": str(agent.model),
            "duration_ms": duration_ms,
            "response_id": response.response_id,
            "usage": response.usage.model_dump() if hasattr(response.usage, "model_dump") else {},
        }
        context.context.llm_calls.append(call)
        self._record(
            context.context,
            event="sdk_llm_completed",
            node=agent.name,
            summary=f"Model call completed for {agent.name}.",
            duration_ms=duration_ms,
            output_data=call,
            data={"agent": agent.name, "model": str(agent.model)},
        )

    async def on_handoff(self, context, from_agent, to_agent) -> None:
        self._record(
            context.context,
            event="sdk_handoff",
            node=from_agent.name,
            summary=f"{from_agent.name} handed off to {to_agent.name}.",
            data={"from_agent": from_agent.name, "to_agent": to_agent.name},
        )

    async def on_tool_start(self, context, agent, tool) -> None:
        if (
            self._specialist_job_id
            and specialist_job_cancellation_requested(
                self._specialist_job_id,
                cancellation_event=self._cancellation_event,
            )
        ):
            raise SpecialistJobCancelled(
                f"Specialist job {self._specialist_job_id} was cancelled "
                f"before tool {tool.name}"
            )
        call_id = str(getattr(context, "tool_call_id", tool.name))
        self._tool_started[call_id] = time.perf_counter()
        self._record(
            context.context,
            event="sdk_tool_started",
            node=tool.name,
            summary=f"{agent.name} called {tool.name}.",
            input_data={"arguments": getattr(context, "tool_arguments", None)},
            data={"agent": agent.name, "tool": tool.name, "tool_call_id": call_id},
        )

    async def on_tool_end(self, context, agent, tool, result) -> None:
        call_id = str(getattr(context, "tool_call_id", tool.name))
        started = self._tool_started.pop(call_id, time.perf_counter())
        self._record(
            context.context,
            event="sdk_tool_hook_completed",
            node=tool.name,
            summary=f"{agent.name} completed {tool.name}.",
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            output_data={"result": result},
            data={"agent": agent.name, "tool": tool.name, "tool_call_id": call_id},
        )
