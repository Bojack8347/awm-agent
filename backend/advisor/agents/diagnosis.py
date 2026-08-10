"""SDK-backed diagnosis snapshot runner.

This module is the production diagnosis entrypoint for background refreshes.
It runs the real OpenAI Agents SDK Diagnosis Specialist directly; it is not a
compatibility wrapper around the retired legacy ToolLoopRunner diagnosis agent.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable, Dict, Optional

from agents import Agent, RunConfig, gen_trace_id, trace
from pydantic import BaseModel

from advisor.tools.deterministic_tools.execution import (
    ContractOnlyFinancialPlanningQueryService,
    RegistryToolExecutor,
    ToolExecutor,
    V2PersistentToolExecutor,
    build_production_subagents,
)
from advisor.tracing.tracing import append_trace, new_span_id
from advisor.agents.agents import build_diagnosis_specialist
from advisor.agents.catalog import DIAGNOSIS_SNAPSHOT_REQUEST, DIAGNOSIS_SPECIALIST, WORKFLOW_NAME
from advisor.agents.context import AwmAgentContext
from advisor.agents.runtime import AwmRunHooks, run_agent_streamed_sync


DiagnosisRunFn = Callable[..., Dict[str, Any]]


def run_diagnosis_snapshot(
    *,
    client_id: str,
    knowledge_snapshot: Dict[str, Any],
    knowledge_snapshot_version: int,
    tool_executor: ToolExecutor,
    agent: Optional[Agent[AwmAgentContext]] = None,
    run_config: Optional[RunConfig] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the OpenAI Agents SDK Diagnosis Specialist for one snapshot."""

    started = time.perf_counter()
    trace_id = gen_trace_id()
    turn_id = f"diag_{uuid.uuid4().hex[:12]}"
    root_span_id = new_span_id("sdk_diagnosis")
    diagnosis_agent = agent or build_diagnosis_specialist()
    resolved_session_id = session_id or f"diagnosis:{client_id}:{knowledge_snapshot_version}"
    context = AwmAgentContext(
        client_id=client_id,
        session_id=resolved_session_id,
        user_message=DIAGNOSIS_SNAPSHOT_REQUEST,
        client_file=knowledge_snapshot,
        tool_executor=tool_executor,
        trace_id=trace_id,
        turn_id=turn_id,
        root_span_id=root_span_id,
        channel="background",
        allowed_tools=None,
    )
    context.active_skills[DIAGNOSIS_SPECIALIST.key] = "diagnosis"
    context.trace_events = append_trace(
        [],
        event="sdk_diagnosis_context_loaded",
        node="load_diagnosis_snapshot",
        summary="Loaded knowledge snapshot for SDK diagnosis refresh.",
        trace_id=trace_id,
        turn_id=turn_id,
        parent_span_id=root_span_id,
        input_data={
            "client_id": client_id,
            "knowledge_snapshot_version": knowledge_snapshot_version,
            "knowledge_snapshot": knowledge_snapshot,
        },
        data={"agent": DIAGNOSIS_SPECIALIST.name, "runtime": "openai_agents_sdk"},
    )
    input_items = [
        {
            "role": "user",
            "content": (
                f"{DIAGNOSIS_SNAPSHOT_REQUEST}\n\n"
                f"client_id: {client_id}\n"
                f"knowledge_snapshot_version: {knowledge_snapshot_version}\n"
                "knowledge_snapshot_json:\n"
                f"{json.dumps(knowledge_snapshot, ensure_ascii=False, indent=2)}"
            ),
        }
    ]
    with trace(
        WORKFLOW_NAME,
        trace_id=trace_id,
        group_id=resolved_session_id,
        metadata={
            "client_id": client_id,
            "turn_id": turn_id,
            "diagnosis_refresh": True,
            "knowledge_snapshot_version": str(knowledge_snapshot_version),
        },
        disabled=bool(run_config and run_config.tracing_disabled),
    ):
        result = run_agent_streamed_sync(
            agent=diagnosis_agent,
            input_items=input_items,
            context=context,
            hooks=AwmRunHooks(),
            max_turns=DIAGNOSIS_SPECIALIST.max_turns,
            run_config=run_config,
        )

    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    parsed = _parse_diagnosis_output(result.final_output)
    diagnosis_data = _extract_diagnosis_data(parsed)
    model_metadata = _extract_model_metadata(parsed)
    model_metadata.update(
        {
            "runtime": "openai_agents_sdk",
            "agent": result.last_agent.name,
            "trace_id": trace_id,
            "turn_id": turn_id,
            "elapsed_ms": elapsed_ms,
            "llm_calls": context.llm_calls,
            "tool_results": context.tool_results,
            "trace_events": context.trace_events,
        }
    )
    return {
        "knowledge_snapshot_version": knowledge_snapshot_version,
        "diagnosis_data": diagnosis_data,
        "model_metadata": model_metadata,
    }


def build_production_diagnosis_runner() -> DiagnosisRunFn:
    """Bind diagnosis refreshes to production SDK tools and real engines."""

    from client_file.repository import build_production_client_file_repository

    repository = build_production_client_file_repository()
    financial_planning, _investment_solution, _revalidation = build_production_subagents()
    tool_executor = V2PersistentToolExecutor(
        fallback=RegistryToolExecutor(
            client_file_writer=repository.writer,
            financial_planning_query_service=ContractOnlyFinancialPlanningQueryService(
                financial_planning_agent=financial_planning,
            ),
        )
    )

    def _run(
        *,
        client_id: str,
        knowledge_snapshot: Dict[str, Any],
        knowledge_snapshot_version: int,
    ) -> Dict[str, Any]:
        return run_diagnosis_snapshot(
            client_id=client_id,
            knowledge_snapshot=knowledge_snapshot,
            knowledge_snapshot_version=knowledge_snapshot_version,
            tool_executor=tool_executor,
        )

    return _run


def _parse_diagnosis_output(output: Any) -> Dict[str, Any]:
    if isinstance(output, BaseModel):
        return output.model_dump()
    if isinstance(output, dict):
        return output
    if isinstance(output, str):
        text = output.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else {"diagnosis_data": {"raw_output": value}}
        except json.JSONDecodeError:
            return {
                "diagnosis_data": {"summary": text, "categories": []},
                "model_metadata": {"parse_error": True},
            }
    return {"diagnosis_data": {"raw_output": repr(output), "categories": []}}


def _extract_diagnosis_data(parsed: Dict[str, Any]) -> Dict[str, Any]:
    diagnosis_data = parsed.get("diagnosis_data")
    if isinstance(diagnosis_data, dict):
        return diagnosis_data
    for key in ("diagnoses", "categories", "findings"):
        value = parsed.get(key)
        if isinstance(value, list):
            return {key: value}
    return parsed if parsed else {"categories": []}


def _extract_model_metadata(parsed: Dict[str, Any]) -> Dict[str, Any]:
    metadata = parsed.get("model_metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}
