from __future__ import annotations

from typing import Any, Callable, Optional

from agents import Agent, RunConfig, RunHooks, Runner
from agents.stream_events import AgentUpdatedStreamEvent, RawResponsesStreamEvent, RunItemStreamEvent

from advisor.agents.context import AwmAgentContext
from advisor.agents.runtime.artifact_parsing import _extract_subagent_artifacts
from advisor.agents.runtime.artifact_store import _extend_subagent_artifacts
from advisor.tracing.tracing import append_trace


async def _run_agent_streamed(
    *,
    agent: Agent[AwmAgentContext],
    input_items: Any,
    context: AwmAgentContext,
    hooks: RunHooks[AwmAgentContext],
    max_turns: int,
    run_config: Optional[RunConfig],
    response_delta_callback: Optional[Callable[[str], None]] = None,
):
    result = Runner.run_streamed(
        agent,
        input_items,
        context=context,
        hooks=hooks,
        max_turns=max_turns,
        run_config=run_config,
    )
    context.trace_events = append_trace(
        context.trace_events,
        event="sdk_stream_started",
        node=agent.name,
        summary="Agents SDK streamed run started.",
        trace_id=context.trace_id,
        turn_id=context.turn_id,
        parent_span_id=context.root_span_id,
        data={"agent": agent.name, "runtime": "openai_agents_sdk"},
    )
    cancelled_after_text_done = False
    async for event in result.stream_events():
        _record_stream_event(context, event, response_delta_callback=response_delta_callback)
        if not cancelled_after_text_done and _stream_has_complete_text_item(context, event):
            context.trace_events = append_trace(
                context.trace_events,
                event="sdk_stream_completed_after_text_done",
                node=result.last_agent.name,
                summary="Agents SDK stream had completed text output; AWM cancelled the tail and drained the stream.",
                trace_id=context.trace_id,
                turn_id=context.turn_id,
                parent_span_id=context.root_span_id,
                data={"last_agent": result.last_agent.name, "runtime": "openai_agents_sdk"},
            )
            result.cancel()
            cancelled_after_text_done = True
    context.trace_events = append_trace(
        context.trace_events,
        event="sdk_stream_completed",
        node=result.last_agent.name,
        summary="Agents SDK streamed run completed.",
        trace_id=context.trace_id,
        turn_id=context.turn_id,
        parent_span_id=context.root_span_id,
        data={"last_agent": result.last_agent.name, "runtime": "openai_agents_sdk"},
    )
    return result


def _stream_has_complete_text_item(context: AwmAgentContext, event: Any) -> bool:
    if not context.streamed_output_text_done or not isinstance(event, RawResponsesStreamEvent):
        return False
    raw_type = str(getattr(event.data, "type", "") or "")
    return raw_type in {"response.content_part.done", "response.output_item.done"}


def _record_stream_event(
    context: AwmAgentContext,
    event: Any,
    *,
    response_delta_callback: Optional[Callable[[str], None]] = None,
) -> None:
    if isinstance(event, RawResponsesStreamEvent):
        delta = _raw_response_text_delta(event.data)
        first_delta = bool(delta) and not context.streamed_text_delta_emitted
        if delta:
            context.streamed_text_buffer += delta
            context.streamed_text_delta_emitted = True
        if delta and response_delta_callback is not None:
            response_delta_callback(delta)
        raw_type = str(getattr(event.data, "type", "") or "")
        if raw_type == "response.output_text.done":
            context.streamed_output_text_done = True
        if raw_type and (first_delta or raw_type.endswith(".done")):
            context.trace_events = append_trace(
                context.trace_events,
                event="sdk_stream_text_delta" if first_delta else "sdk_stream_raw_done",
                node="model_stream",
                summary="Agents SDK raw model stream emitted first text." if first_delta else "Agents SDK raw model stream item completed.",
                trace_id=context.trace_id,
                turn_id=context.turn_id,
                parent_span_id=context.root_span_id,
                output_data={"delta_chars": len(delta)} if first_delta else {},
                data={"raw_event_type": raw_type},
            )
        return
    if isinstance(event, AgentUpdatedStreamEvent):
        context.trace_events = append_trace(
            context.trace_events,
            event="sdk_stream_agent_updated",
            node=event.new_agent.name,
            summary=f"Agents SDK stream switched to {event.new_agent.name}.",
            trace_id=context.trace_id,
            turn_id=context.turn_id,
            parent_span_id=context.root_span_id,
            data={"agent": event.new_agent.name},
        )
        return
    if not isinstance(event, RunItemStreamEvent):
        return
    item_type = getattr(event.item, "type", None)
    if item_type == "tool_call_item":
        context.trace_events = append_trace(
            context.trace_events,
            event="sdk_stream_tool_call_ready",
            node=getattr(event.item, "tool_name", None) or event.name,
            summary="Agents SDK stream emitted a tool call item.",
            trace_id=context.trace_id,
            turn_id=context.turn_id,
            parent_span_id=context.root_span_id,
            input_data={
                "tool_name": getattr(event.item, "tool_name", None),
                "call_id": getattr(event.item, "call_id", None),
            },
            data={"stream_event": event.name},
        )
    elif item_type == "tool_call_output_item":
        raw_output = getattr(event.item, "output", None)
        extracted_artifacts = _extract_subagent_artifacts(raw_output)
        _extend_subagent_artifacts(context, extracted_artifacts)
        context.trace_events = append_trace(
            context.trace_events,
            event="sdk_stream_tool_output_ready",
            node=event.name,
            summary="Agents SDK stream emitted a tool output item.",
            trace_id=context.trace_id,
            turn_id=context.turn_id,
            parent_span_id=context.root_span_id,
            output_data={"output": raw_output},
            data={
                "stream_event": event.name,
                "subagent_artifact_count": len(extracted_artifacts),
            },
        )


def _raw_response_text_delta(data: Any) -> str:
    # Only surface client-facing output text. Reasoning summaries must not
    # enter the companion/voice stream (they inflate TTFT and can leak chain-of-thought).
    raw_type = str(getattr(data, "type", "") or "")
    if raw_type not in {
        "response.output_text.delta",
        "response.refusal.delta",
    }:
        return ""
    delta = getattr(data, "delta", "")
    return str(delta or "")
