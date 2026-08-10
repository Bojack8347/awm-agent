"""Production runtime for AWM's native OpenAI Agents SDK loop."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
import inspect
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set
import uuid
import warnings

logger = logging.getLogger(__name__)

from agents import (
    Agent,
    RunConfig,
    RunHooks,
    Runner,
    gen_trace_id,
    set_default_openai_api,
    set_default_openai_client,
    set_tracing_disabled,
    trace,
)
from agents.run_state import RunState
from agents.stream_events import AgentUpdatedStreamEvent, RawResponsesStreamEvent, RunItemStreamEvent
# ── DeepSeek / custom LLM provider ──
# LLM_BASE_URL points the SDK at a non-OpenAI provider. DeepSeek and similar
# endpoints expose an OpenAI-compatible surface, but that surface is Chat
# Completions — not the Responses API the SDK targets by default — so the API
# style has to be switched alongside the client or every call 404s on
# /responses. Misconfiguration fails startup instead of falling back to OpenAI:
# silently billing and answering from the wrong provider is worse than not
# booting. Note OPENAI_PROXY_URL is a separate mechanism for reaching real
# OpenAI through a proxy; this block is for a different provider entirely.
_llm_base_url = os.getenv("LLM_BASE_URL", "").strip()
if _llm_base_url:
    _api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not _api_key:
        raise RuntimeError(
            f"LLM_BASE_URL is set to {_llm_base_url!r} but OPENAI_API_KEY is empty. "
            "The custom LLM provider cannot be authenticated; refusing to start "
            "rather than fall back to the default provider."
        )
    from openai import AsyncOpenAI

    set_default_openai_client(
        AsyncOpenAI(api_key=_api_key, base_url=_llm_base_url),
        use_for_tracing=False,
    )
    set_default_openai_api("chat_completions")
    # The SDK's tracing exporter uploads every run to OpenAI's backend. On a
    # non-OpenAI provider that both leaks conversation metadata to a vendor AWM
    # no longer uses and fails auth (the key is DeepSeek's), logging a 401 per
    # run. AWM keeps its own traces via advisor.tracing, so switch it off.
    set_tracing_disabled(True)
    logger.info(
        "Custom LLM client registered: base_url=%s api=chat_completions tracing=off",
        _llm_base_url,
    )
from dotenv import dotenv_values

from client_file.interfaces import ClientFileReader
from advisor.tools.deterministic_tools.execution import ToolExecutor
from advisor.tracing.tracing import append_trace, format_trace_timeline, new_span_id, utc_now_iso
from advisor.agents.agents import build_main_advisor_agent
from advisor.agents.background_jobs import (
    SpecialistJobCancelled,
    jobs_for_prompt,
    specialist_job_cancellation_requested,
)
from advisor.agents.catalog import (
    MAIN_ADVISOR,
    WORKFLOW_NAME,
    agent_key_for_name,
)
from advisor.agents.context import AwmAgentContext
from advisor.agents.skills import DEFAULT_SKILL_REGISTRY
from advisor.agents.tools import _latest_cashflow_missing_fields
from advisor.agents.quant_contracts import (
    QuantConclusionValidation,
    QuantValidationIssue,
    build_quant_response_annotations,
    format_quant_response_for_client,
    quant_recommendation_policy,
    propagate_quant_warnings,
    render_asset_allocation_failure_fallback,
    render_quantitative_missing_data_fallback,
    render_quantitative_reporting_fallback,
    validate_quantitative_response,
)

# Relocated helpers are re-exported to preserve the original import surface.
# AwmAgentsRuntime, run_agent_streamed_sync, trace, and RunState remain bound
# here because tests patch these names on advisor.agents.runtime.
from advisor.agents.runtime._shared import (
    _runtime_number,
    _subagent_artifact_key,
)
from advisor.agents.runtime.artifact_parsing import (
    _allocation_from_recommended_securities,
    _decode_json_candidates,
    _extract_subagent_artifacts,
    _is_raw_investment_assessment,
    _is_raw_investment_policy,
    _json_values_from_tool_output,
    _normalize_subagent_artifact,
    _subagent_artifacts_from_value,
    _wrap_raw_investment_policy,
)
from advisor.agents.runtime.artifact_store import (
    _combined_subagent_artifacts,
    _dedupe_subagent_artifacts,
    _extend_subagent_artifacts,
    _subagent_artifacts_from_tool_results,
)
from advisor.agents.runtime.assessment_artifacts import (
    _assessment_card_paragraphs,
    _investment_assessment_artifact_from_payload,
    _investment_assessment_artifacts_from_client_file,
    _investment_assessment_artifacts_from_tool_results,
    _pending_unsigned_investment_assessments,
)
from advisor.agents.runtime.claim_checks import (
    _is_fact_writeback_only_turn,
    _pending_assessment_created_this_turn,
    _proposal_writeback_succeeded,
    _should_repair_assessment_creation,
    _should_repair_proposal_claim,
    _writeback_claim_errors,
)
from advisor.agents.runtime.client_file_state import (
    _assessment_payload_is_signed,
    _client_file_has_assessment_ready_pool,
    _client_file_has_pending_or_signed_assessment,
    _client_file_has_proposed_policy,
    _client_file_has_signed_assessment,
    _client_file_ready_for_assessment_creation,
    _client_file_ready_for_assessment_presentation,
    _client_file_ready_for_proposal_construction,
    _client_file_snapshot_version,
    _durable_client_file_contains_commit,
    _money_pool_is_assessment_ready,
)
from advisor.agents.runtime.guard_messages import (
    _ASSESSMENT_CARD_REQUIRED_RESPONSE,
    _FACT_DUMP_CLAIM_BLOCKED_RESPONSE,
    _MISSING_QUANT_EVIDENCE_RESPONSE,
    _PENDING_DRAFT_COMMIT_BLOCKED_RESPONSE,
    _PROJECTION_RUN_BLOCKED_RESPONSE,
    _PROPOSAL_PROMISE_BLOCKED_RESPONSE,
    _PROPOSAL_STALLED_BLOCKED_RESPONSE,
    _QUANT_CLAIM_BLOCKED_RESPONSE,
    _WRITEBACK_CLAIM_BLOCKED_RESPONSE,
    _blocked_response_for_claim_errors,
    _calculation_capability_gap_response,
    _structured_clarification,
    _writeback_claim_blocked_response,
)
from advisor.agents.runtime.hooks import AwmRunHooks
from advisor.agents.runtime.interruptions import (
    _apply_approval_decisions,
    _json_safe_interruption_value,
    _restore_arc_interruption_context,
    _serialize_arc_interruption_context,
    _serialize_interruptions,
)
from advisor.agents.runtime.quant_intent import (
    _is_explicit_read_only_quant_request,
    _remove_applied_assumptions_appendix,
    _stored_followup_requests_assumptions,
)
from advisor.agents.runtime.response_assembly import (
    _ANALYSIS_REFERENCE_RE,
    _append_artifact_reference,
    _artifact_context_from_recent_turns,
    _artifact_context_from_tool_results,
    _build_response,
    _budget_recent_turns_with_status,
    _format_conversation_summary,
    _latest_pending_clarification,
    _message_with_pending_clarification,
    _normalize_recent_turns,
    _turn_timing_breakdown,
)
from advisor.agents.runtime.token_budget import conversation_memory_enabled
from advisor.agents.runtime.response_guards import _apply_final_response_guards
from advisor.agents.runtime.sdk_env import (
    _agent_run_timeout_seconds,
    _ensure_openai_proxy_env,
    _sdk_error_output,
    _should_retry_sdk_run,
)
from advisor.agents.runtime.skill_selection import (
    _activation_only_continuation_tools,
    _build_skill_candidates,
    _client_file_needs_first_time_onboarding,
    _current_turn_requests_investment_assessment_signoff,
    _expand_rerun_request,
    _infer_actionable_workflow_skill,
    _infer_state_active_skill,
    _onboarding_objective_is_complete,
    _previous_user_message,
    _post_fact_continuation_tools,
    _set_initial_active_skill,
)
from advisor.agents.runtime.solution_artifacts import (
    _assessment_basis,
    _dict_items,
    _find_client_file_assessment,
    _find_client_file_money_pool,
    _horizon_years_from_money_pool,
    _investment_solution_artifact_from_allocation_writeback,
    _policy_scope_from_money_pool,
    _source_assessment_from_client_file_assessment,
)
from advisor.agents.runtime.streaming import (
    _raw_response_text_delta,
    _record_stream_event,
    _run_agent_streamed,
    _stream_has_complete_text_item,
)


def _cashflow_missing_input_repair_fields(
    context: AwmAgentContext,
) -> List[str]:
    """Return the latest unresolved fields eligible for one semantic repair."""

    if (
        context.cashflow_missing_input_repair_attempted
        or _structured_clarification(context.tool_results) is not None
    ):
        return []
    return _latest_cashflow_missing_fields(context.tool_results)


class AwmAgentsRuntime:
    """Synchronous API facade over the official SDK's autonomous agent loop."""

    def __init__(
        self,
        *,
        client_file_reader: ClientFileReader,
        tool_executor: ToolExecutor,
        conversation_history_reader: Optional[Callable[..., Iterable[Dict[str, Any]]]] = None,
        conversation_history_reverse_reader: Optional[
            Callable[..., Iterable[Dict[str, Any]]]
        ] = None,
        conversation_summary_reader: Optional[Callable[[str, str], List[Dict[str, Any]]]] = None,
        conversation_compaction_runner: Optional[Callable[..., Any]] = None,
        agent: Optional[Agent[AwmAgentContext]] = None,
        run_config: Optional[RunConfig] = None,
    ) -> None:
        self._client_file_reader = client_file_reader
        self._tool_executor = tool_executor
        self._conversation_history_reader = conversation_history_reader
        self._conversation_history_reverse_reader = conversation_history_reverse_reader
        self._conversation_summary_reader = conversation_summary_reader
        self._conversation_compaction_runner = conversation_compaction_runner
        self._agent = agent or build_main_advisor_agent()
        self._run_config = run_config

    def _rewrite_quant_response_once(
        self,
        *,
        response_text: str,
        validation: QuantConclusionValidation,
        context: AwmAgentContext,
        user_message: str,
        trace_id: str,
        turn_id: str,
        parent_span_id: str,
    ) -> Optional[str]:
        """Give the client-facing advisor one tool-free evidence correction pass."""

        evidence = [
            {
                "tool": envelope.tool,
                "permitted_use": envelope.permitted_use,
                "conclusion_code": envelope.conclusion_code,
                "claims": [
                    {
                        "metric_key": claim.metric_key,
                        "value": claim.value,
                        "value_decimal": claim.value_decimal,
                        "display_value": claim.display_value,
                        "unit": claim.unit,
                        "claim_id": claim.claim_id,
                        "evidence_ref": claim.evidence_ref,
                    }
                    for claim in envelope.claims
                ],
                "warnings": envelope.warnings,
                "assumptions": envelope.assumptions,
            }
            for envelope in validation.evidence
        ]
        correction_prompt = (
            "[INTERNAL RESPONSE SELF-CHECK]\n"
            "Review and correct your own draft for the same client question. This is one "
            "response correction pass, not a new analysis. Do not call tools. Use only the "
            "typed evidence below. Return only the complete revised client response.\n"
            "Keep the answer conversational and easy for a client without a financial "
            "background to understand. Preserve useful explanation and caveats. Every number "
            "must exactly match a typed claim, including its unit and metric meaning; do not "
            "round, estimate, or derive a new value. Do not make a stronger conclusion or "
            "recommendation than permitted_use allows.\n\n"
            f"Client question:\n{str(user_message or '').strip()}\n\n"
            f"Draft that needs correction:\n{str(response_text or '').strip()}\n\n"
            "Validation issues:\n"
            + json.dumps(
                [issue.model_dump(mode="json") for issue in validation.errors],
                ensure_ascii=False,
                default=str,
            )
            + "\n\nTyped evidence:\n"
            + json.dumps(evidence, ensure_ascii=False, default=str)
        )
        context.trace_events = append_trace(
            context.trace_events,
            event="sdk_quant_response_self_check",
            node=self._agent.name,
            summary="The Main Advisor is correcting its own quantitative response once.",
            trace_id=trace_id,
            turn_id=turn_id,
            parent_span_id=parent_span_id,
            status="retrying",
            output_data={
                "validation_errors": [
                    issue.model_dump(mode="json") for issue in validation.errors
                ]
            },
            data={"runtime": "openai_agents_sdk", "tools_enabled": False},
        )
        context.streamed_text_buffer = ""
        context.streamed_text_delta_emitted = False
        context.streamed_output_text_done = False
        correction_agent = self._agent.clone(
            tools=[],
            handoffs=[],
            model_settings=replace(
                self._agent.model_settings,
                tool_choice="none",
            ),
        )
        try:
            corrected = run_agent_streamed_sync(
                agent=correction_agent,
                input_items=[
                    {"role": "user", "content": str(user_message or "")},
                    {"role": "user", "content": correction_prompt},
                ],
                context=context,
                hooks=AwmRunHooks(),
                max_turns=1,
                run_config=self._run_config,
                response_delta_callback=None,
                timeout_seconds=_agent_run_timeout_seconds(),
            )
        except Exception as exc:  # pragma: no cover - SDK boundary safety
            context.trace_events = append_trace(
                context.trace_events,
                event="sdk_quant_response_self_check_failed",
                node=self._agent.name,
                summary="The Main Advisor self-check failed; the existing safe fallback remains active.",
                trace_id=trace_id,
                turn_id=turn_id,
                parent_span_id=parent_span_id,
                status="failed",
                output_data=_sdk_error_output(exc),
                data={"runtime": "openai_agents_sdk"},
            )
            return None
        corrected_text = str(corrected.final_output or context.streamed_text_buffer or "").strip()
        if not corrected_text:
            return None
        context.trace_events = append_trace(
            context.trace_events,
            event="sdk_quant_response_self_check_completed",
            node=self._agent.name,
            summary="The Main Advisor produced a corrected response for evidence validation.",
            trace_id=trace_id,
            turn_id=turn_id,
            parent_span_id=parent_span_id,
            output_data={"response_text": corrected_text},
            data={"runtime": "openai_agents_sdk", "tools_enabled": False},
        )
        return corrected_text

    def execute_client_action(
        self,
        *,
        client_id: str,
        session_id: str,
        action: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute an authenticated UI decision before the Agent narration loop."""

        if action.get("type") != "investment_assessment_decision":
            return {
                "tool": "record_assessment_signoff",
                "ok": False,
                "error": "unsupported_client_action",
            }
        decision = str(action.get("decision") or "").strip().lower()
        arguments = {
            "signed_off": decision == "agree",
            "assessment_id": str(action.get("assessment_id") or "").strip(),
            "assessment_version": action.get("assessment_version"),
            "investment_consultation_id": str(
                action.get("investment_consultation_id") or ""
            ).strip(),
            "money_pool_id": str(action.get("money_pool_id") or "").strip(),
            "user_message": "Authenticated app button tap.",
        }
        return self._tool_executor.execute(
            client_id=client_id,
            session_id=session_id,
            tool_name="record_assessment_signoff",
            arguments=arguments,
        )

    def execute_signed_assessment_proposal(
        self,
        *,
        client_id: str,
        session_id: str,
        signed_assessment_ref: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Run the real allocation engine after an authenticated sign-off."""
        return self._tool_executor.execute(
            client_id=client_id,
            session_id=session_id,
            tool_name="run_asset_allocation",
            arguments={"assessment_ref": dict(signed_assessment_ref or {})},
        )

    def resolve_fact_confirmation(
        self,
        *,
        client_id: str,
        session_id: str,
        confirmation_set: Dict[str, Any],
        client_message: str,
    ) -> Dict[str, Any]:
        """Resolve a confirmation set — called by Agent via LLM, not hardcoded."""

        return self._tool_executor.execute(
            client_id=client_id,
            session_id=session_id,
            tool_name="resolve_fact_confirmation",
            arguments={
                "confirmation_set_id": confirmation_set["confirmation_set_id"],
                "prompt_message_id": confirmation_set["prompt_message_id"],
                "client_message": client_message,
                "decisions": [
                    {
                        "confirmation_item_id": item["confirmation_item_id"],
                        "decision": "confirmed",
                    }
                    for item in confirmation_set.get("items") or []
                    if item.get("status") == "pending"
                ],
            },
        )

    def calculate_annual_surplus(
        self,
        *,
        client_id: str,
        session_id: str,
    ) -> Dict[str, Any]:
        """Precompute the canonical income-minus-spending metric."""

        client_file = self._client_file_reader.read(client_id).payload
        return self._tool_executor.execute(
            client_id=client_id,
            session_id=session_id,
            tool_name="calculate_financial_math",
            arguments={
                "schema_version": "awm.financial_math.v2",
                "client_file_version": int(client_file.get("client_file_version") or 0),
                "sources": [
                    {
                        "id": "income",
                        "kind": "client_fact",
                        "selector": "annual_income",
                    },
                    {
                        "id": "spending",
                        "kind": "client_fact",
                        "selector": "annual_spending",
                    },
                ],
                "steps": [
                    {
                        "id": "annual_surplus",
                        "operation": "metric",
                        "template": "annual_surplus",
                        "arguments": ["$income", "$spending"],
                    }
                ],
                "outputs": ["$annual_surplus"],
            },
        )

    def _refresh_context_client_file(
        self,
        context: AwmAgentContext,
        commit_result: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Reload durable state after tools before guards or follow-up planning."""

        if not context.tool_results and commit_result is None:
            return True
        mid_turn = commit_result is not None
        event_name = (
            "client_file_refreshed_mid_turn"
            if mid_turn
            else "sdk_context_refreshed_after_tools"
        )
        before_version = _client_file_snapshot_version(context.client_file)
        try:
            durable_client_file = self._client_file_reader.read(
                context.client_id
            ).payload
            if mid_turn and not _durable_client_file_contains_commit(
                durable_client_file,
                commit_result,
            ):
                context.mid_turn_commit_refresh_verified = False
                context.mid_turn_commit_refresh_reason = "committed_facts_missing"
                context.trace_events = append_trace(
                    context.trace_events,
                    event=event_name,
                    node="load_context",
                    summary=(
                        "Blocked same-turn specialist dispatch because the durable "
                        "Client File did not contain the committed facts."
                    ),
                    trace_id=context.trace_id,
                    turn_id=context.turn_id,
                    parent_span_id=context.root_span_id,
                    status="failed",
                    data={
                        "runtime": "openai_agents_sdk",
                        "reason": "committed_facts_missing",
                        "snapshot_version_before": before_version,
                        "snapshot_version_after": _client_file_snapshot_version(
                            durable_client_file
                        ),
                    },
                )
                return False
            context.client_file = durable_client_file
            if mid_turn:
                context.mid_turn_commit_refresh_verified = True
                context.mid_turn_commit_refresh_reason = None
            context.trace_events = append_trace(
                context.trace_events,
                event=event_name,
                node="load_context",
                summary=(
                    "Verified committed facts against a fresh durable Client File read."
                    if mid_turn
                    else "Reloaded Client File after tool execution."
                ),
                trace_id=context.trace_id,
                turn_id=context.turn_id,
                parent_span_id=context.root_span_id,
                status="success",
                data={
                    "runtime": "openai_agents_sdk",
                    "tool_count": len(context.tool_results),
                    "snapshot_version_before": before_version,
                    "snapshot_version_after": _client_file_snapshot_version(
                        durable_client_file
                    ),
                },
            )
            return True
        except Exception as exc:  # pragma: no cover - persistence boundary safety
            if mid_turn:
                context.mid_turn_commit_refresh_verified = False
                context.mid_turn_commit_refresh_reason = "durable_read_failed"
            context.trace_events = append_trace(
                context.trace_events,
                event=(
                    event_name if mid_turn else "sdk_context_refresh_failed"
                ),
                node="load_context",
                summary=(
                    "Blocked same-turn specialist dispatch because the durable "
                    "Client File refresh failed."
                    if mid_turn
                    else "Client File refresh failed after tool execution."
                ),
                trace_id=context.trace_id,
                turn_id=context.turn_id,
                parent_span_id=context.root_span_id,
                status="failed",
                output_data=_sdk_error_output(exc),
                data={
                    "runtime": "openai_agents_sdk",
                    "reason": "durable_read_failed",
                    "snapshot_version_before": before_version,
                    "snapshot_version_after": None,
                },
            )
            return False

    def run_turn(
        self,
        *,
        client_id: str,
        session_id: str,
        user_message: str,
        turn_type: str = "user_message",
        channel: str = "text",
        recent_turns: Optional[List[Dict[str, Any]]] = None,
        response_delta_callback: Optional[Callable[[str], None]] = None,
        allowed_tools: Optional[Iterable[str]] = None,
        active_skill: Optional[str] = None,
        required_tool: Optional[str] = None,
        trusted_action_context: Optional[Dict[str, Any]] = None,
        initial_tool_results: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        started_at = utc_now_iso()
        turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        trace_id = gen_trace_id()
        root_span_id = new_span_id("sdk_turn")
        durable_history_requested = recent_turns is None
        main_advisor_memory = (
            durable_history_requested
            and channel == "text"
            and conversation_memory_enabled()
            and agent_key_for_name(self._agent.name) == MAIN_ADVISOR.key
        )
        conversation_summaries: List[Dict[str, Any]] = []
        if main_advisor_memory and self._conversation_summary_reader is not None:
            conversation_summaries = self._conversation_summary_reader(
                client_id,
                session_id,
            )
        summary_block = _format_conversation_summary(conversation_summaries)
        boundary = (
            conversation_summaries[-1].get("covered_through_message_id")
            if conversation_summaries
            else None
        )
        history_newest_first = False
        if recent_turns is None:
            if self._conversation_history_reverse_reader is not None:
                recent_turns = self._conversation_history_reverse_reader(
                    session_id,
                    after_message_id=boundary,
                    page_size=200,
                    client_id=client_id,
                )
                history_newest_first = True
            elif self._conversation_history_reader is not None:
                recent_turns = self._conversation_history_reader(
                    session_id,
                    after_message_id=boundary,
                    page_size=500,
                    client_id=client_id,
                )
        raw_recent_turns, history, history_trimmed = _budget_recent_turns_with_status(
            recent_turns or [],
            current_user_message=user_message,
            summary_block=summary_block,
            newest_first=history_newest_first,
        )
        if (
            history_trimmed
            and main_advisor_memory
            and self._conversation_compaction_runner is not None
        ):
            try:
                self._conversation_compaction_runner(
                    client_id=client_id,
                    session_id=session_id,
                    hard_only=True,
                    trigger="inline_hard",
                )
                conversation_summaries = (
                    self._conversation_summary_reader(client_id, session_id)
                    if self._conversation_summary_reader is not None
                    else []
                )
                summary_block = _format_conversation_summary(conversation_summaries)
                boundary = (
                    conversation_summaries[-1].get("covered_through_message_id")
                    if conversation_summaries
                    else None
                )
                if self._conversation_history_reverse_reader is not None:
                    reloaded_turns = self._conversation_history_reverse_reader(
                        session_id,
                        after_message_id=boundary,
                        page_size=200,
                        client_id=client_id,
                    )
                    reloaded_newest_first = True
                else:
                    reloaded_turns = self._conversation_history_reader(
                        session_id,
                        after_message_id=boundary,
                        page_size=500,
                        client_id=client_id,
                    )
                    reloaded_newest_first = False
                raw_recent_turns, history, _ = _budget_recent_turns_with_status(
                    reloaded_turns,
                    current_user_message=user_message,
                    summary_block=summary_block,
                    newest_first=reloaded_newest_first,
                )
            except Exception:
                logger.exception(
                    "Inline conversation compaction failed",
                    extra={
                        "client_id": client_id,
                        "session_id": session_id,
                        "trigger": "inline_hard",
                    },
                )
        pending_clarification = _latest_pending_clarification(raw_recent_turns)
        carried_artifact_references = [
            reference
            for summary in conversation_summaries
            for reference in (summary.get("carried_artifact_references") or [])
            if isinstance(reference, dict)
        ]
        artifact_context = _artifact_context_from_recent_turns(
            raw_recent_turns,
            pending_operation=pending_clarification,
            carried_artifact_references=carried_artifact_references,
        )
        agent_user_message = _message_with_pending_clarification(
            _expand_rerun_request(user_message, history),
            pending_clarification,
        )
        # Skill and tool selection belongs to the model. Python may supply advisory
        # candidates, and an explicit app hint may activate an installed skill. The
        # only post-hoc intervention is a validator-driven repair pass.
        client_file = self._client_file_reader.read(client_id).payload
        background_jobs = jobs_for_prompt(
            client_id=client_id,
            current_client_file=client_file,
        )
        effective_allowed_tools = set(allowed_tools) if allowed_tools is not None else None
        context = AwmAgentContext(
            client_id=client_id,
            session_id=session_id,
            user_message=user_message,
            client_file=client_file,
            tool_executor=self._tool_executor,
            trace_id=trace_id,
            turn_id=turn_id,
            root_span_id=root_span_id,
            channel=channel,
            allowed_tools=effective_allowed_tools,
            artifact_context=artifact_context,
            trusted_action_context=dict(trusted_action_context or {}),
            background_jobs=background_jobs,
            conversation_summaries=conversation_summaries,
            tool_results=list(initial_tool_results or []),
            mid_turn_client_file_refresher=self._refresh_context_client_file,
        )
        _set_initial_active_skill(
            context,
            agent_key=agent_key_for_name(self._agent.name),
            explicit_active_skill=active_skill,
            recent_history=history,
        )
        run_agent = self._agent
        required_tool_name = str(required_tool or "").strip()
        if required_tool_name:
            required_tools = [
                tool
                for tool in self._agent.tools
                if str(getattr(tool, "name", "") or "").strip()
                == required_tool_name
            ]
            if not required_tools:
                raise ValueError(
                    f"Required Agent tool is not registered: {required_tool_name}"
                )
            run_agent = self._agent.clone(
                tools=required_tools,
                model_settings=replace(
                    self._agent.model_settings,
                    tool_choice="required",
                ),
            )
        context.trace_events = append_trace(
            [],
            event="sdk_context_loaded",
            node="load_context",
            summary="Loaded Client File and durable conversation history.",
            trace_id=trace_id,
            turn_id=turn_id,
            parent_span_id=root_span_id,
            input_data={
                "client_file": client_file,
                "recent_turns": history,
                "artifact_context": artifact_context,
            },
            data={
                "session_id": session_id,
                "history_count": len(history),
                "conversation_summary_count": len(conversation_summaries),
                "artifact_reference_count": len(
                    artifact_context.get("references") or []
                ),
            },
        )
        input_items: List[Dict[str, Any]] = [
            *(
                [{"role": "developer", "content": summary_block}]
                if summary_block
                else []
            ),
            *history,
            {"role": "user", "content": agent_user_message},
        ]
        hooks = AwmRunHooks()
        try:
            _ensure_openai_proxy_env()
            with trace(
                WORKFLOW_NAME,
                trace_id=trace_id,
                group_id=session_id,
                metadata={"client_id": client_id, "turn_id": turn_id},
                disabled=bool(self._run_config and self._run_config.tracing_disabled),
            ):
                result = run_agent_streamed_sync(
                    agent=run_agent,
                    input_items=input_items,
                    context=context,
                    hooks=hooks,
                    max_turns=MAIN_ADVISOR.max_turns,
                    run_config=self._run_config,
                    response_delta_callback=None,
                    timeout_seconds=_agent_run_timeout_seconds(),
                )
        except Exception as exc:  # pragma: no cover - SDK boundary safety
            retry_result = None
            recovered_stream_text = str(context.streamed_text_buffer or "").strip()
            if recovered_stream_text and (context.streamed_output_text_done or context.tool_results):
                elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
                context.trace_events = append_trace(
                    context.trace_events,
                    event="sdk_stream_timeout_recovered",
                    node=self._agent.name,
                    summary="Recovered a streamed advisor response after the SDK stream failed to close.",
                    trace_id=trace_id,
                    turn_id=turn_id,
                    parent_span_id=root_span_id,
                    status="recovered",
                    duration_ms=elapsed_ms,
                    output_data={
                        "response_text": recovered_stream_text,
                        "error": _sdk_error_output(exc),
                    },
                    data={
                        "runtime": "openai_agents_sdk",
                        "tool_result_count": len(context.tool_results),
                        "streamed_output_text_done": context.streamed_output_text_done,
                    },
                )
                guarded_response, guard_errors, conclusion_validation = _apply_final_response_guards(
                    recovered_stream_text,
                    context=context,
                    user_message=user_message,
                )
                if response_delta_callback is not None and guarded_response:
                    context.streamed_text_delta_emitted = True
                    response_delta_callback(guarded_response)
                return _build_response(
                    trace_id=trace_id,
                    turn_id=turn_id,
                    root_span_id=root_span_id,
                    turn_type=turn_type,
                    channel=channel,
                    selected_skill=self._agent.name,
                    user_message=user_message,
                    response_text=guarded_response,
                    started_at=started_at,
                    elapsed_ms=elapsed_ms,
                    history=history,
                    durable_history_loaded=(
                        self._conversation_history_reverse_reader is not None
                        or self._conversation_history_reader is not None
                    ),
                    context=context,
                    errors=[
                        {"type": type(exc).__name__, "message": str(exc), "recovered": True},
                        *guard_errors,
                    ],
                    status="failed" if guard_errors else "completed",
                    conclusion_validation=conclusion_validation,
                )
            if _should_retry_sdk_run(exc, context):
                context.trace_events = append_trace(
                    context.trace_events,
                    event="sdk_run_retrying",
                    node=self._agent.name,
                    summary="Agents SDK run failed before tool execution; retrying once.",
                    trace_id=trace_id,
                    turn_id=turn_id,
                    parent_span_id=root_span_id,
                    status="retrying",
                    output_data=_sdk_error_output(exc),
                    data={"runtime": "openai_agents_sdk"},
                )
                try:
                    retry_result = run_agent_streamed_sync(
                        agent=run_agent,
                        input_items=input_items,
                        context=context,
                        hooks=AwmRunHooks(),
                        max_turns=MAIN_ADVISOR.max_turns,
                        run_config=self._run_config,
                        response_delta_callback=None,
                        timeout_seconds=_agent_run_timeout_seconds(),
                    )
                except Exception as retry_exc:
                    exc = retry_exc
            if retry_result is not None:
                result = retry_result
            else:
                elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
                context.trace_events = append_trace(
                    context.trace_events,
                    event="sdk_run_failed",
                    node=self._agent.name,
                    summary="Agents SDK run failed before producing a final response.",
                    trace_id=trace_id,
                    turn_id=turn_id,
                    parent_span_id=root_span_id,
                    status="failed",
                    duration_ms=elapsed_ms,
                    output_data=_sdk_error_output(exc),
                    data={"runtime": "openai_agents_sdk"},
                )
                return _build_response(
                    trace_id=trace_id,
                    turn_id=turn_id,
                    root_span_id=root_span_id,
                    turn_type=turn_type,
                    channel=channel,
                    selected_skill=self._agent.name,
                    user_message=user_message,
                    response_text="Something went wrong on my side just now. Please try that again in a moment.",
                    started_at=started_at,
                    elapsed_ms=elapsed_ms,
                    history=history,
                    durable_history_loaded=(
                        self._conversation_history_reverse_reader is not None
                        or self._conversation_history_reader is not None
                    ),
                    context=context,
                    errors=[{"type": type(exc).__name__, "message": str(exc)}],
                    status="failed",
                )
        interruptions = getattr(result, "interruptions", []) or []
        if interruptions:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
            run_state = result.to_state()
            context.trace_events = append_trace(
                context.trace_events,
                event="sdk_run_interrupted",
                node=result.last_agent.name,
                summary="Agents SDK paused for tool approval.",
                trace_id=trace_id,
                turn_id=turn_id,
                parent_span_id=root_span_id,
                status="interrupted",
                duration_ms=elapsed_ms,
                output_data={"interruption_count": len(interruptions)},
                data={"last_agent": result.last_agent.name},
            )
            return _build_response(
                trace_id=trace_id,
                turn_id=turn_id,
                root_span_id=root_span_id,
                turn_type=turn_type,
                channel=channel,
                selected_skill=result.last_agent.name,
                user_message=user_message,
                response_text="I need approval before continuing this action.",
                started_at=started_at,
                elapsed_ms=elapsed_ms,
                history=history,
                durable_history_loaded=(
                    self._conversation_history_reverse_reader is not None
                    or self._conversation_history_reader is not None
                ),
                context=context,
                status="interrupted",
                interrupted_state=run_state.to_json(
                    context_serializer=_serialize_arc_interruption_context,
                    strict_context=True,
                ),
                interruptions=_serialize_interruptions(interruptions),
            )
        activation_continuation_tools = _activation_only_continuation_tools(
            context,
            agent_key=agent_key_for_name(self._agent.name),
        )
        context.trace_events = append_trace(
            context.trace_events,
            event="sdk_skill_activation_continuation_evaluated",
            node=self._agent.name,
            summary="AWM evaluated whether a selected skill must continue to a business action.",
            trace_id=trace_id,
            turn_id=turn_id,
            parent_span_id=root_span_id,
            status="success",
            data={
                "runtime": "openai_agents_sdk",
                "active_skill": context.active_skills.get(
                    agent_key_for_name(self._agent.name)
                ),
                "successful_tools": [
                    str(item.get("tool") or "")
                    for item in context.tool_results
                    if isinstance(item, dict) and item.get("ok") is not False
                ],
                "continuation_tools": sorted(activation_continuation_tools),
            },
        )
        if activation_continuation_tools:
            context.trace_events = append_trace(
                context.trace_events,
                event="sdk_skill_activation_continuing",
                node=self._agent.name,
                summary=(
                    "The selected skill produced no business action; continuing once "
                    "with that skill's tools required."
                ),
                trace_id=trace_id,
                turn_id=turn_id,
                parent_span_id=root_span_id,
                status="retrying",
                data={
                    "runtime": "openai_agents_sdk",
                    "active_skill": context.active_skills.get(
                        agent_key_for_name(self._agent.name)
                    ),
                    "allowed_tools": sorted(activation_continuation_tools),
                },
            )
            previous_allowed_tools = context.allowed_tools
            # The first phase deliberately exposes only activation controls.
            # Once the model has selected a skill, replace that phase's
            # visibility with the selected skill's bounded tools.
            context.allowed_tools = activation_continuation_tools
            context.streamed_text_delta_emitted = False
            context.streamed_text_buffer = ""
            context.streamed_output_text_done = False
            continuation_agent = self._agent.clone(
                model_settings=replace(
                    self._agent.model_settings,
                    tool_choice="auto",
                )
            )
            continuation_items = [
                *input_items,
                {
                    "role": "user",
                    "content": (
                        "[INTERNAL WORKFLOW CONTINUATION] The workflow skill is already "
                        "active. Do not activate it again. Continue the client's current "
                        "request now by calling the appropriate enabled skill tool. If a "
                        "safe action cannot be selected, call request_clarification with "
                        "the single smallest question needed."
                    ),
                },
            ]
            try:
                result = run_agent_streamed_sync(
                    agent=continuation_agent,
                    input_items=continuation_items,
                    context=context,
                    hooks=AwmRunHooks(),
                    max_turns=MAIN_ADVISOR.max_turns,
                    run_config=self._run_config,
                    response_delta_callback=None,
                    timeout_seconds=_agent_run_timeout_seconds(),
                )
            except Exception as continuation_exc:  # pragma: no cover - SDK boundary safety
                context.trace_events = append_trace(
                    context.trace_events,
                    event="sdk_skill_activation_continuation_failed",
                    node=self._agent.name,
                    summary="The one-shot post-activation continuation failed.",
                    trace_id=trace_id,
                    turn_id=turn_id,
                    parent_span_id=root_span_id,
                    status="failed",
                    output_data=_sdk_error_output(continuation_exc),
                    data={"runtime": "openai_agents_sdk"},
                )
            finally:
                context.allowed_tools = previous_allowed_tools
        self._refresh_context_client_file(context)
        post_fact_continuation_tools = _post_fact_continuation_tools(
            context,
            agent_key=agent_key_for_name(self._agent.name),
        )
        context.trace_events = append_trace(
            context.trace_events,
            event="sdk_post_fact_continuation_evaluated",
            node=self._agent.name,
            summary=(
                "AWM evaluated the agent-authored action recorded with the fact commit."
            ),
            trace_id=trace_id,
            turn_id=turn_id,
            parent_span_id=root_span_id,
            status="success",
            data={
                "runtime": "openai_agents_sdk",
                "post_commit_action": context.post_fact_continuation_action,
                "continuation_tools": sorted(post_fact_continuation_tools),
            },
        )
        if post_fact_continuation_tools:
            context.post_fact_continuation_attempted = True
            context.trace_events = append_trace(
                context.trace_events,
                event="sdk_post_fact_continuation_started",
                node=self._agent.name,
                summary=(
                    "The Main Advisor is completing its recorded calculation intent "
                    "against the refreshed Client File."
                ),
                trace_id=trace_id,
                turn_id=turn_id,
                parent_span_id=root_span_id,
                status="retrying",
                data={
                    "runtime": "openai_agents_sdk",
                    "post_commit_action": context.post_fact_continuation_action,
                    "allowed_tools": sorted(post_fact_continuation_tools),
                },
            )
            previous_allowed_tools = context.allowed_tools
            context.allowed_tools = post_fact_continuation_tools
            context.streamed_text_delta_emitted = False
            context.streamed_text_buffer = ""
            context.streamed_output_text_done = False
            continuation_agent = self._agent.clone(
                model_settings=replace(
                    self._agent.model_settings,
                    tool_choice="required",
                )
            )
            continuation_items = [
                *input_items,
                {
                    "role": "user",
                    "content": (
                        "[INTERNAL POST-FACT CONTINUATION] The facts are committed and "
                        "the Client File has been refreshed. Complete the pending action "
                        "you recorded in commit_facts. Consult Financial Planning now, or "
                        "call request_clarification with the single smallest unresolved "
                        "input. Do not activate another skill and do not stop in prose."
                    ),
                },
            ]
            try:
                result = run_agent_streamed_sync(
                    agent=continuation_agent,
                    input_items=continuation_items,
                    context=context,
                    hooks=AwmRunHooks(),
                    max_turns=MAIN_ADVISOR.max_turns,
                    run_config=self._run_config,
                    response_delta_callback=None,
                    timeout_seconds=_agent_run_timeout_seconds(),
                )
            except Exception as continuation_exc:  # pragma: no cover - SDK boundary safety
                context.trace_events = append_trace(
                    context.trace_events,
                    event="sdk_post_fact_continuation_failed",
                    node=self._agent.name,
                    summary="The one-shot post-fact continuation failed.",
                    trace_id=trace_id,
                    turn_id=turn_id,
                    parent_span_id=root_span_id,
                    status="failed",
                    output_data=_sdk_error_output(continuation_exc),
                    data={"runtime": "openai_agents_sdk"},
                )
            finally:
                context.allowed_tools = previous_allowed_tools
            self._refresh_context_client_file(context)
        missing_input_fields = (
            _cashflow_missing_input_repair_fields(context)
            if agent_key_for_name(self._agent.name) == MAIN_ADVISOR.key
            else []
        )
        if missing_input_fields:
            context.cashflow_missing_input_repair_attempted = True
            context.trace_events = append_trace(
                context.trace_events,
                event="sdk_cashflow_missing_input_repair",
                node=self._agent.name,
                summary=(
                    "The Main Advisor is reconciling the latest client reply with "
                    "the cash-flow model's missing typed inputs once."
                ),
                trace_id=trace_id,
                turn_id=turn_id,
                parent_span_id=root_span_id,
                status="retrying",
                output_data={"missing_fields": missing_input_fields},
                data={
                    "runtime": "openai_agents_sdk",
                    "routing": "agent_semantic_decision",
                },
            )
            context.streamed_text_buffer = ""
            context.streamed_text_delta_emitted = False
            context.streamed_output_text_done = False
            repair_items = [
                {"role": "user", "content": str(user_message or "")},
                {
                    "role": "user",
                    "content": (
                        "[INTERNAL CASH-FLOW INPUT REPAIR]\n"
                        "The cash-flow attempt was blocked because the typed Client File still "
                        "reports these missing fields:\n"
                        + json.dumps(missing_input_fields, ensure_ascii=False, default=str)
                        + "\nUse your semantic understanding of the client's latest message; do not use "
                        "keyword or length rules. If that message clearly supplies any listed "
                        "field, activate `regular-consult` if needed and write only those supplied "
                        "values with the existing fact tools. Treat approximate, ambiguous, "
                        "conflicting, or high-impact values according to the existing confirmation "
                        "rules; do not invent defaults. After a successful write, call "
                        "`consult_financial_planning_specialist` once to rerun the projection from "
                        "the refreshed Client File. If a value remains genuinely unresolved, call "
                        "`request_clarification` with only the unresolved fields and one plain-language "
                        "question. Do not repeat fields the client already supplied and do not "
                        "calculate in prose."
                    ),
                },
            ]
            try:
                result = run_agent_streamed_sync(
                    agent=self._agent,
                    input_items=repair_items,
                    context=context,
                    hooks=AwmRunHooks(),
                    max_turns=MAIN_ADVISOR.max_turns,
                    run_config=self._run_config,
                    response_delta_callback=None,
                    timeout_seconds=_agent_run_timeout_seconds(),
                )
                self._refresh_context_client_file(context)
                context.trace_events = append_trace(
                    context.trace_events,
                    event="sdk_cashflow_missing_input_repair_completed",
                    node=self._agent.name,
                    summary="The Main Advisor completed the bounded missing-input repair pass.",
                    trace_id=trace_id,
                    turn_id=turn_id,
                    parent_span_id=root_span_id,
                    output_data={"repair_attempted": True},
                    data={"runtime": "openai_agents_sdk"},
                )
            except Exception as repair_exc:  # pragma: no cover - SDK boundary safety
                context.trace_events = append_trace(
                    context.trace_events,
                    event="sdk_cashflow_missing_input_repair_failed",
                    node=self._agent.name,
                    summary=(
                        "The missing-input repair failed; the existing safe fallback remains active."
                    ),
                    trace_id=trace_id,
                    turn_id=turn_id,
                    parent_span_id=root_span_id,
                    status="failed",
                    output_data=_sdk_error_output(repair_exc),
                    data={"runtime": "openai_agents_sdk"},
                )
        raw_response_text = str(result.final_output or context.streamed_text_buffer or "")
        response_rewriter = None
        if agent_key_for_name(self._agent.name) == MAIN_ADVISOR.key:
            response_rewriter = lambda draft, validation: self._rewrite_quant_response_once(
                response_text=draft,
                validation=validation,
                context=context,
                user_message=user_message,
                trace_id=trace_id,
                turn_id=turn_id,
                parent_span_id=root_span_id,
            )
        response_text, guard_errors, conclusion_validation = _apply_final_response_guards(
            raw_response_text,
            context=context,
            user_message=user_message,
            response_rewriter=response_rewriter,
        )
        writeback_errors = [
            error for error in guard_errors if error.get("source") == "writeback_claim_validator"
        ]
        if writeback_errors and _should_repair_proposal_claim(writeback_errors, context):
            context.proposal_claim_repair_attempted = True
            context.trace_events = append_trace(
                context.trace_events,
                event="sdk_proposal_claim_repair",
                node=result.last_agent.name,
                summary="Retrying once after a proposal/allocation claim lacked run_asset_allocation evidence.",
                trace_id=trace_id,
                turn_id=turn_id,
                parent_span_id=root_span_id,
                status="retrying",
                output_data={"errors": writeback_errors},
                data={"runtime": "openai_agents_sdk"},
            )
            context.active_skills["main_advisor"] = "investment-consult"
            context.skill_candidates["main_advisor"] = [
                {
                    "skill_name": "investment-consult",
                    "source": "durable_proposal_construction_ready",
                    "reason": (
                        "Proposal/allocation claims were blocked; call "
                        "consult_investment_solution_specialist so run_asset_allocation can produce the proposal."
                    ),
                }
            ]
            context.streamed_text_buffer = ""
            context.streamed_text_delta_emitted = False
            context.streamed_output_text_done = False
            # Use a short clean prompt: long chat history often contains invented allocations
            # that teach the model to keep narrating instead of calling tools.
            client_snapshot = {
                "money_pools": (context.client_file or {}).get("money_pools"),
                "investment_assessments": (context.client_file or {}).get("investment_assessments"),
                "recent_writebacks": ((context.client_file or {}).get("recent_writebacks") or [])[:8],
                "policies": (context.client_file or {}).get("policies"),
            }
            repair_items = [
                {
                    "role": "user",
                    "content": str(user_message or ""),
                },
                {
                    "role": "user",
                    "content": (
                        "[INTERNAL WORKFLOW CORRECTION] Ignore any prior assistant prose that invented "
                        "allocations, expected returns, or holdings. Those were not model results.\n"
                        "Client File snapshot:\n"
                        + json.dumps(client_snapshot, ensure_ascii=False, default=str)[:7000]
                        + "\nRequired actions in this turn only:\n"
                        "1) If the money pool for the requested proposal is missing, call `upsert_money_pool` "
                        "using amount, horizon, and risk from the Client File snapshot or current user request.\n"
                        "2) If assessment sign-off is not already recorded, call `record_assessment_signoff` "
                        "with signed_off=true for that pool.\n"
                        "3) Immediately call `consult_investment_solution_specialist` so `run_asset_allocation` runs.\n"
                        "Do not invent numbers. Do not claim the next step is ready until that specialist succeeds."
                    ),
                },
            ]
            try:
                result = run_agent_streamed_sync(
                    agent=self._agent,
                    input_items=repair_items,
                    context=context,
                    hooks=AwmRunHooks(),
                    max_turns=MAIN_ADVISOR.max_turns,
                    run_config=self._run_config,
                    response_delta_callback=response_delta_callback,
                    timeout_seconds=_agent_run_timeout_seconds(),
                )
                self._refresh_context_client_file(context)
                response_text, guard_errors, conclusion_validation = _apply_final_response_guards(
                    str(result.final_output or context.streamed_text_buffer or ""),
                    context=context,
                    user_message=user_message,
                )
                writeback_errors = [
                    error
                    for error in guard_errors
                    if error.get("source") == "writeback_claim_validator"
                ]
            except Exception as repair_exc:  # pragma: no cover - SDK boundary safety
                context.trace_events = append_trace(
                    context.trace_events,
                    event="sdk_proposal_claim_repair_failed",
                    node=self._agent.name,
                    summary="Proposal-claim repair run failed before producing a final response.",
                    trace_id=trace_id,
                    turn_id=turn_id,
                    parent_span_id=root_span_id,
                    status="failed",
                    output_data=_sdk_error_output(repair_exc),
                    data={"runtime": "openai_agents_sdk"},
                )
        elif _should_repair_assessment_creation(
            guard_errors,
            context,
            conclusion_validation=conclusion_validation,
        ):
            context.assessment_claim_repair_attempted = True
            context.trace_events = append_trace(
                context.trace_events,
                event="sdk_assessment_claim_repair",
                node=result.last_agent.name,
                summary=(
                    "Retrying once after an assessment/recommendation claim lacked "
                    "consult_financial_planning_specialist evidence."
                ),
                trace_id=trace_id,
                turn_id=turn_id,
                parent_span_id=root_span_id,
                status="retrying",
                output_data={
                    "errors": guard_errors,
                    "sanitized_errors": [
                        issue.model_dump(mode="json")
                        for issue in (conclusion_validation.sanitized_errors or [])
                    ],
                },
                data={"runtime": "openai_agents_sdk"},
            )
            context.active_skills["main_advisor"] = "investment-consult"
            context.skill_candidates["main_advisor"] = [
                {
                    "skill_name": "investment-consult",
                    "source": "durable_assessment_creation_ready",
                    "reason": (
                        "A defined money pool is ready for assessment creation; call "
                        "consult_financial_planning_specialist to create the pending assessment."
                    ),
                }
            ]
            context.streamed_text_buffer = ""
            context.streamed_text_delta_emitted = False
            context.streamed_output_text_done = False
            client_snapshot = {
                "money_pools": (context.client_file or {}).get("money_pools"),
                "investment_assessments": (context.client_file or {}).get("investment_assessments"),
                "facts": {
                    key: value
                    for key, value in ((context.client_file or {}).get("facts") or {}).items()
                    if any(
                        token in str(key).lower()
                        for token in (
                            "risk",
                            "divers",
                            "concentrat",
                            "employer",
                            "liquidity",
                            "emergency",
                            "simpl",
                            "college",
                            "funding",
                        )
                    )
                },
                "recent_writebacks": ((context.client_file or {}).get("recent_writebacks") or [])[:8],
            }
            repair_agent = self._agent
            repair_items = [
                {
                    "role": "user",
                    "content": str(user_message or ""),
                },
                {
                    "role": "user",
                    "content": (
                        "[INTERNAL WORKFLOW CORRECTION] Do not invent an analysis, recommendation, "
                        "or assessment in prose. Do not claim sign-off was recorded.\n"
                        "A defined money pool is on file. If a pending assessment already exists, "
                        "re-run the assessment path so the client summary card is returned again; "
                        "do not invent a new decision.\n"
                        "Client File snapshot:\n"
                        + json.dumps(client_snapshot, ensure_ascii=False, default=str)[:7000]
                        + "\nRequired actions in this turn only:\n"
                        "1) Activate `investment-consult` if it is not already active.\n"
                        "2) Immediately call `consult_financial_planning_specialist` and ask for an "
                        "`internal investment assessment` for the defined money pool "
                        "(idempotent replay is fine when a pending assessment already exists).\n"
                        "3) If the specialist returns missing mandate inputs, ask only for those "
                        "inputs in plain language. Do not invent volatility, returns, or holdings.\n"
                        "4) When the specialist returns a pending assessment, present its client "
                        "summary for the client's review. Wait for an explicit Agree action before "
                        "any sign-off tool."
                    ),
                },
            ]
            try:
                result = run_agent_streamed_sync(
                    agent=repair_agent,
                    input_items=repair_items,
                    context=context,
                    hooks=AwmRunHooks(),
                    max_turns=MAIN_ADVISOR.max_turns,
                    run_config=self._run_config,
                    response_delta_callback=response_delta_callback,
                    timeout_seconds=_agent_run_timeout_seconds(),
                )
                self._refresh_context_client_file(context)
                response_text, guard_errors, conclusion_validation = _apply_final_response_guards(
                    str(result.final_output or context.streamed_text_buffer or ""),
                    context=context,
                    user_message=user_message,
                )
                writeback_errors = [
                    error
                    for error in guard_errors
                    if error.get("source") == "writeback_claim_validator"
                ]
            except Exception as repair_exc:  # pragma: no cover - SDK boundary safety
                context.trace_events = append_trace(
                    context.trace_events,
                    event="sdk_assessment_claim_repair_failed",
                    node=self._agent.name,
                    summary="Assessment-claim repair run failed before producing a final response.",
                    trace_id=trace_id,
                    turn_id=turn_id,
                    parent_span_id=root_span_id,
                    status="failed",
                    output_data=_sdk_error_output(repair_exc),
                    data={"runtime": "openai_agents_sdk"},
                )
        if writeback_errors:
            context.trace_events = append_trace(
                context.trace_events,
                event="sdk_writeback_claim_blocked",
                node=result.last_agent.name,
                summary="Blocked a final response that claimed a writeback without matching tool evidence.",
                trace_id=trace_id,
                turn_id=turn_id,
                parent_span_id=root_span_id,
                status="failed",
                output_data={"errors": writeback_errors},
                data={"runtime": "openai_agents_sdk"},
            )
        if conclusion_validation.status == "blocked":
            context.trace_events = append_trace(
                context.trace_events,
                event="sdk_quant_conclusion_blocked",
                node=result.last_agent.name,
                summary="Blocked a final quantitative response that failed deterministic evidence validation.",
                trace_id=trace_id,
                turn_id=turn_id,
                parent_span_id=root_span_id,
                status="failed",
                output_data={"validation": conclusion_validation.model_dump(mode="json")},
                data={"runtime": "openai_agents_sdk"},
            )
        elif conclusion_validation.sanitized_errors:
            context.trace_events = append_trace(
                context.trace_events,
                event="sdk_quant_response_sanitized",
                node=result.last_agent.name,
                summary=(
                    "Replaced unsupported quantitative narration with a deterministic "
                    "reporting-only summary."
                ),
                trace_id=trace_id,
                turn_id=turn_id,
                parent_span_id=root_span_id,
                output_data={"validation": conclusion_validation.model_dump(mode="json")},
                data={"runtime": "openai_agents_sdk"},
            )
        elif conclusion_validation.status != "not_applicable":
            context.trace_events = append_trace(
                context.trace_events,
                event="sdk_quant_conclusion_validated",
                node=result.last_agent.name,
                summary="Validated the final response against deterministic quantitative evidence.",
                trace_id=trace_id,
                turn_id=turn_id,
                parent_span_id=root_span_id,
                output_data={"validation": conclusion_validation.model_dump(mode="json")},
                data={"runtime": "openai_agents_sdk"},
            )
        if response_delta_callback is not None and response_text and not context.streamed_text_delta_emitted:
            context.streamed_text_delta_emitted = True
            response_delta_callback(response_text)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        context.trace_events = append_trace(
            context.trace_events,
            event="sdk_response_completed",
            node=result.last_agent.name,
            summary="Agents SDK produced the final user response.",
            trace_id=trace_id,
            turn_id=turn_id,
            parent_span_id=root_span_id,
            duration_ms=elapsed_ms,
            output_data={"response_text": response_text},
            data={"last_agent": result.last_agent.name},
        )
        return _build_response(
            trace_id=trace_id,
            turn_id=turn_id,
            root_span_id=root_span_id,
            turn_type=turn_type,
            channel=channel,
            selected_skill=result.last_agent.name,
            user_message=user_message,
            response_text=response_text,
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            history=history,
            durable_history_loaded=(
                self._conversation_history_reverse_reader is not None
                or self._conversation_history_reader is not None
            ),
            context=context,
            status="failed" if guard_errors else "completed",
            errors=guard_errors,
            conclusion_validation=conclusion_validation,
        )

    def resume_turn(
        self,
        *,
        client_id: str,
        session_id: str,
        interrupted_state: Dict[str, Any],
        approval_decisions: Optional[List[Dict[str, Any]]] = None,
        channel: str = "text",
        response_delta_callback: Optional[Callable[[str], None]] = None,
        allowed_tools: Optional[Iterable[str]] = None,
        active_skill: Optional[str] = None,
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        started_at = utc_now_iso()
        turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        trace_id = gen_trace_id()
        root_span_id = new_span_id("sdk_resume")
        resume_client_file = self._client_file_reader.read(client_id).payload
        context = AwmAgentContext(
            client_id=client_id,
            session_id=session_id,
            user_message="[resume interrupted SDK run]",
            client_file=resume_client_file,
            tool_executor=self._tool_executor,
            trace_id=trace_id,
            turn_id=turn_id,
            root_span_id=root_span_id,
            channel=channel,
            allowed_tools=set(allowed_tools) if allowed_tools is not None else None,
            background_jobs=jobs_for_prompt(
                client_id=client_id,
                current_client_file=resume_client_file,
            ),
            mid_turn_client_file_refresher=self._refresh_context_client_file,
        )
        _restore_arc_interruption_context(context, interrupted_state)
        _set_initial_active_skill(
            context,
            agent_key=agent_key_for_name(self._agent.name),
            explicit_active_skill=active_skill,
        )
        context.trace_events = append_trace(
            [],
            event="sdk_resume_context_loaded",
            node="load_context",
            summary="Loaded Client File and serialized Agents SDK run state for resume.",
            trace_id=trace_id,
            turn_id=turn_id,
            parent_span_id=root_span_id,
            input_data={"interrupted_state": interrupted_state},
            data={"session_id": session_id},
        )
        try:
            state = _resolve_sync_awaitable(
                RunState.from_json(
                    self._agent,
                    interrupted_state,
                    context_override=context,
                    strict_context=False,
                )
            )
            _apply_approval_decisions(state, approval_decisions or [])
            _ensure_openai_proxy_env()
            with trace(
                WORKFLOW_NAME,
                trace_id=trace_id,
                group_id=session_id,
                metadata={"client_id": client_id, "turn_id": turn_id, "resume": "true"},
                disabled=bool(self._run_config and self._run_config.tracing_disabled),
            ):
                result = run_agent_streamed_sync(
                    agent=self._agent,
                    input_items=state,
                    context=context,
                    hooks=AwmRunHooks(),
                    max_turns=MAIN_ADVISOR.max_turns,
                    run_config=self._run_config,
                    response_delta_callback=None,
                    timeout_seconds=_agent_run_timeout_seconds(),
                )
        except Exception as exc:  # pragma: no cover - SDK boundary safety
            retry_result = None
            if "state" in locals() and _should_retry_sdk_run(exc, context):
                context.trace_events = append_trace(
                    context.trace_events,
                    event="sdk_resume_retrying",
                    node=self._agent.name,
                    summary="Agents SDK resume failed before tool execution; retrying once.",
                    trace_id=trace_id,
                    turn_id=turn_id,
                    parent_span_id=root_span_id,
                    status="retrying",
                    output_data=_sdk_error_output(exc),
                    data={"runtime": "openai_agents_sdk"},
                )
                try:
                    retry_result = run_agent_streamed_sync(
                        agent=self._agent,
                        input_items=state,
                        context=context,
                        hooks=AwmRunHooks(),
                        max_turns=MAIN_ADVISOR.max_turns,
                        run_config=self._run_config,
                        response_delta_callback=None,
                        timeout_seconds=_agent_run_timeout_seconds(),
                    )
                except Exception as retry_exc:
                    exc = retry_exc
            if retry_result is not None:
                result = retry_result
            else:
                elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
                context.trace_events = append_trace(
                    context.trace_events,
                    event="sdk_resume_failed",
                    node=self._agent.name,
                    summary="Agents SDK resume failed before producing a final response.",
                    trace_id=trace_id,
                    turn_id=turn_id,
                    parent_span_id=root_span_id,
                    status="failed",
                    duration_ms=elapsed_ms,
                    output_data=_sdk_error_output(exc),
                    data={"runtime": "openai_agents_sdk"},
                )
                return _build_response(
                    trace_id=trace_id,
                    turn_id=turn_id,
                    root_span_id=root_span_id,
                    turn_type="resume",
                    channel=channel,
                    selected_skill=self._agent.name,
                    user_message="[resume interrupted SDK run]",
                    response_text="I couldn’t pick that up where we left off. Please restart that step.",
                    started_at=started_at,
                    elapsed_ms=elapsed_ms,
                    history=[],
                    durable_history_loaded=False,
                    context=context,
                    errors=[{"type": type(exc).__name__, "message": str(exc)}],
                    status="failed",
                )
        interruptions = getattr(result, "interruptions", []) or []
        if interruptions:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
            run_state = result.to_state()
            context.trace_events = append_trace(
                context.trace_events,
                event="sdk_resume_interrupted",
                node=result.last_agent.name,
                summary="Agents SDK resume paused again for tool approval.",
                trace_id=trace_id,
                turn_id=turn_id,
                parent_span_id=root_span_id,
                status="interrupted",
                duration_ms=elapsed_ms,
                output_data={"interruption_count": len(interruptions)},
                data={"last_agent": result.last_agent.name},
            )
            return _build_response(
                trace_id=trace_id,
                turn_id=turn_id,
                root_span_id=root_span_id,
                turn_type="resume",
                channel=channel,
                selected_skill=result.last_agent.name,
                user_message="[resume interrupted SDK run]",
                response_text="I still need approval before continuing this action.",
                started_at=started_at,
                elapsed_ms=elapsed_ms,
                history=[],
                durable_history_loaded=False,
                context=context,
                status="interrupted",
                interrupted_state=run_state.to_json(
                    context_serializer=_serialize_arc_interruption_context,
                    strict_context=True,
                ),
                interruptions=_serialize_interruptions(interruptions),
            )
        response_text, guard_errors, conclusion_validation = _apply_final_response_guards(
            str(result.final_output or context.streamed_text_buffer or ""),
            context=context,
            user_message="[resume interrupted SDK run]",
        )
        writeback_errors = [
            error for error in guard_errors if error.get("source") == "writeback_claim_validator"
        ]
        if writeback_errors:
            context.trace_events = append_trace(
                context.trace_events,
                event="sdk_writeback_claim_blocked",
                node=result.last_agent.name,
                summary="Blocked a resumed response that claimed a writeback without matching tool evidence.",
                trace_id=trace_id,
                turn_id=turn_id,
                parent_span_id=root_span_id,
                status="failed",
                output_data={"errors": writeback_errors},
                data={"runtime": "openai_agents_sdk", "resume": True},
            )
        if conclusion_validation.status == "blocked":
            context.trace_events = append_trace(
                context.trace_events,
                event="sdk_quant_conclusion_blocked",
                node=result.last_agent.name,
                summary="Blocked a resumed quantitative response that failed deterministic evidence validation.",
                trace_id=trace_id,
                turn_id=turn_id,
                parent_span_id=root_span_id,
                status="failed",
                output_data={"validation": conclusion_validation.model_dump(mode="json")},
                data={"runtime": "openai_agents_sdk", "resume": True},
            )
        elif conclusion_validation.sanitized_errors:
            context.trace_events = append_trace(
                context.trace_events,
                event="sdk_quant_response_sanitized",
                node=result.last_agent.name,
                summary=(
                    "Replaced unsupported resumed narration with a deterministic "
                    "reporting-only summary."
                ),
                trace_id=trace_id,
                turn_id=turn_id,
                parent_span_id=root_span_id,
                output_data={"validation": conclusion_validation.model_dump(mode="json")},
                data={"runtime": "openai_agents_sdk", "resume": True},
            )
        elif conclusion_validation.status != "not_applicable":
            context.trace_events = append_trace(
                context.trace_events,
                event="sdk_quant_conclusion_validated",
                node=result.last_agent.name,
                summary="Validated the resumed response against deterministic quantitative evidence.",
                trace_id=trace_id,
                turn_id=turn_id,
                parent_span_id=root_span_id,
                output_data={"validation": conclusion_validation.model_dump(mode="json")},
                data={"runtime": "openai_agents_sdk", "resume": True},
            )
        if response_delta_callback is not None and response_text:
            context.streamed_text_delta_emitted = True
            response_delta_callback(response_text)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        context.trace_events = append_trace(
            context.trace_events,
            event="sdk_resume_completed",
            node=result.last_agent.name,
            summary="Agents SDK resumed and produced the final user response.",
            trace_id=trace_id,
            turn_id=turn_id,
            parent_span_id=root_span_id,
            duration_ms=elapsed_ms,
            output_data={"response_text": response_text},
            data={"last_agent": result.last_agent.name},
        )
        return _build_response(
            trace_id=trace_id,
            turn_id=turn_id,
            root_span_id=root_span_id,
            turn_type="resume",
            channel=channel,
            selected_skill=result.last_agent.name,
            user_message="[resume interrupted SDK run]",
            response_text=response_text,
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            history=[],
            durable_history_loaded=False,
            context=context,
            status="failed" if guard_errors else "completed",
            errors=guard_errors,
            conclusion_validation=conclusion_validation,
        )


def _resolve_sync_awaitable(value: Any) -> Any:
    if not inspect.isawaitable(value):
        return value

    async def resolve() -> Any:
        return await value

    return asyncio.run(resolve())


def run_agent_streamed_sync(
    *,
    agent: Agent[AwmAgentContext],
    input_items: Any,
    context: AwmAgentContext,
    hooks: RunHooks[AwmAgentContext],
    max_turns: int,
    run_config: Optional[RunConfig],
    response_delta_callback: Optional[Callable[[str], None]] = None,
    timeout_seconds: Optional[float] = None,
):
    try:
        already_running_loop = asyncio.get_running_loop()
    except RuntimeError:
        already_running_loop = None
    if already_running_loop is not None:
        raise RuntimeError("AwmAgentsRuntime cannot run a streamed SDK turn inside an active event loop.")

    policy = asyncio.get_event_loop_policy()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            loop = policy.get_event_loop()
        except RuntimeError:
            loop = policy.new_event_loop()
            policy.set_event_loop(loop)

    task = loop.create_task(
        _run_agent_streamed(
            agent=agent,
            input_items=input_items,
            context=context,
            hooks=hooks,
            max_turns=max_turns,
            run_config=run_config,
            response_delta_callback=response_delta_callback,
        )
    )
    try:
        if timeout_seconds and timeout_seconds > 0:
            return loop.run_until_complete(asyncio.wait_for(task, timeout=timeout_seconds))
        return loop.run_until_complete(task)
    except BaseException:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                loop.run_until_complete(task)
        raise
    finally:
        if not loop.is_closed():
            with contextlib.suppress(RuntimeError):
                loop.run_until_complete(loop.shutdown_asyncgens())
