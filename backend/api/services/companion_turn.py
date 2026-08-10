"""Canonical application service for one Companion turn."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, fields
from typing import Any, Callable, Dict, List, Optional, Protocol

from api.services.companion_turn_runs import CompanionTurnRunService

from advisor.tracing.trace_persistence import persist_advisor_turn_trace
from advisor.tracing.tracing import append_trace, format_trace_timeline
from api.services.companion_actions import InvestmentAssessmentDecision
from api.services.projection_materializer import materialize_projection_artifacts
from api.services.proposal_materializer import (
    materialize_proposal_artifacts,
    ready_proposals_for_assessment,
)

logger = logging.getLogger(__name__)


class CompanionRuntime(Protocol):
    def execute_client_action(
        self,
        *,
        client_id: str,
        session_id: str,
        action: Dict[str, Any],
    ) -> Dict[str, Any]: ...

    def run_turn(
        self,
        *,
        client_id: str,
        session_id: str,
        user_message: str,
        turn_type: str,
        channel: str,
        active_skill: Optional[str] = None,
        response_delta_callback: Optional[Callable[[str], None]] = None,
        initial_tool_results: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]: ...

    def execute_signed_assessment_proposal(
        self,
        *,
        client_id: str,
        session_id: str,
        signed_assessment_ref: Dict[str, Any],
    ) -> Dict[str, Any]: ...

    def resume_turn(
        self,
        *,
        client_id: str,
        session_id: str,
        interrupted_state: Dict[str, Any],
        approval_decisions: List[Dict[str, Any]],
        channel: str,
        response_delta_callback: Optional[Callable[[str], None]] = None,
        active_skill: Optional[str] = None,
    ) -> Dict[str, Any]: ...


@dataclass(frozen=True)
class CompanionTurnRequest:
    client_id: str
    session_id: str
    user_message: str
    turn_type: str = "user_message"
    channel: str = "text"
    input_source: Any = None
    client_action: Optional[InvestmentAssessmentDecision] = None
    active_skill: Optional[str] = None
    persist: bool = True
    stream: bool = False
    trace_source_type: str = "companion_v1_compat"
    client_turn_id: Optional[str] = None


@dataclass(frozen=True)
class CompanionTurnCallbacks:
    response_delta: Optional[Callable[[str], None]] = None
    client_action_completed: Optional[Callable[[Dict[str, Any]], None]] = None


@dataclass(frozen=True)
class CompanionTurnDependencies:
    get_advisor_runtime: Callable[[], CompanionRuntime]
    db_store_companion_message: Callable[..., Any]
    db_store_companion_message_bubbles: Callable[..., Any]
    db_list_artifacts: Callable[..., List[Dict[str, Any]]]
    db_save_artifact: Callable[..., Any]
    db_update_artifact: Callable[..., Any]
    db_list_policies: Callable[..., List[Dict[str, Any]]]
    db_save_policy: Callable[..., Any]
    db_update_policy: Callable[..., Any]
    db_get_cashflow_analysis_snapshot: Callable[..., Any]
    db_get_latest_knowledge_snapshot: Callable[..., Any]
    db_save_asset_allocation_proposal_bundle: Optional[Callable[..., Any]] = None
    build_client_state_view: Optional[Callable[..., Dict[str, Any]]] = None
    persist_turn_trace: Callable[..., Dict[str, Any]] = persist_advisor_turn_trace
    schedule_conversation_compaction: Optional[Callable[..., None]] = None
    bind_fact_confirmation_prompt: Optional[Callable[..., Dict[str, Any]]] = None

    def __post_init__(self) -> None:
        optional = {
            "db_save_asset_allocation_proposal_bundle",
            "build_client_state_view",
            "schedule_conversation_compaction",
            "bind_fact_confirmation_prompt",
        }
        missing = [
            field.name
            for field in fields(self)
            if not (
                field.name in optional
                and getattr(self, field.name) is None
            )
            and not callable(getattr(self, field.name))
        ]
        if missing:
            raise TypeError(
                "Companion turn dependencies must be callable: "
                + ", ".join(sorted(missing))
            )


@dataclass(frozen=True)
class CompanionTurnOutcome:
    request: CompanionTurnRequest
    result: Dict[str, Any]
    assistant_message: str
    proposal_artifacts: List[Dict[str, Any]]
    projection_artifacts: List[Dict[str, Any]]
    user_message_id: Any
    assistant_message_id: Any
    trace_write: Dict[str, Any]
    http_status: int
    turn_receipt: Optional[Dict[str, Any]] = None

    @property
    def runtime_status(self) -> str:
        return str(self.result.get("status") or "completed")

    @property
    def success(self) -> bool:
        return self.http_status < 400

    def canonical_payload(self) -> Dict[str, Any]:
        result = self.result
        return {
            "success": self.success,
            "session_id": self.request.session_id,
            "status": self.runtime_status,
            "runtime": "advisor_runtime",
            "trace_id": result.get("trace_id")
            or self.trace_write.get("trace_id"),
            "turn_id": (
                self.turn_receipt.get("turn_id")
                if self.turn_receipt
                else result.get("turn_id") or self.trace_write.get("turn_id")
            ),
            "server_turn_id": self.turn_receipt.get("turn_id") if self.turn_receipt else None,
            "client_turn_id": self.request.client_turn_id,
            "advisor_turn_id": result.get("turn_id") or self.trace_write.get("turn_id"),
            "timing": result.get("timing", {}),
            "llm_calls": result.get("llm_calls", []),
            "selected_skill": result.get("selected_skill"),
            "active_skills": result.get("active_skills", {}),
            "skill_candidates": result.get("skill_candidates", {}),
            "request_mode": result.get("request_mode"),
            "no_model_required": result.get("no_model_required", False),
            "capability_plan": result.get("capability_plan", []),
            "routing_clarification": result.get("routing_clarification"),
            "pending_intent": result.get("pending_intent"),
            "interrupted_state": result.get("interrupted_state"),
            "interruptions": result.get("interruptions", []),
            "artifact_context": result.get("artifact_context"),
            "requested_capabilities": result.get("requested_capabilities", []),
            "cashflow_decision": result.get("cashflow_decision"),
            "active_objective": result.get("active_objective"),
            "planned_actions": result.get("planned_actions", []),
            "tool_results": result.get("tool_results", []),
            "client_action_result": result.get("client_action_result"),
            "subagent_artifacts": result.get("subagent_artifacts", []),
            "proposal_artifacts": self.proposal_artifacts,
            "projection_artifacts": self.projection_artifacts,
            "conclusion_validation": result.get("conclusion_validation"),
            "quantitative_response": result.get("quantitative_response"),
            "recommendation_policy": result.get("recommendation_policy"),
            "errors": result.get("errors", []),
            "trace_events": result.get("trace_events", []),
            "trace_timeline": result.get("trace_timeline", ""),
            "trace_write": self.trace_write,
            "turn_type": result.get("turn_type", self.request.turn_type),
            "channel": result.get("channel", self.request.channel),
            "memory_context": result.get("memory_context", {}),
            "persisted": {
                "user_message_id": self.user_message_id,
                "assistant_message_id": self.assistant_message_id,
                "status": self.runtime_status,
            },
        }


class CompanionTurnService:
    def __init__(
        self,
        dependencies: CompanionTurnDependencies,
        turn_runs: Optional[CompanionTurnRunService] = None,
    ) -> None:
        self._deps = dependencies
        self._turn_runs = turn_runs

    @property
    def durable_turns_enabled(self) -> bool:
        return self._turn_runs is not None

    def accept_turn(self, request: CompanionTurnRequest) -> Optional[Dict[str, Any]]:
        if self._turn_runs is None or not request.client_turn_id:
            return None
        return self._turn_runs.accept(
            client_id=request.client_id,
            companion_session_id=request.session_id,
            client_turn_id=request.client_turn_id,
            turn_type=request.turn_type,
            user_message=request.user_message,
            client_action=request.client_action.to_dict() if request.client_action else None,
            input_source=request.input_source,
            channel=request.channel,
        )

    def get_turn(self, *, client_id: str, session_id: str, turn_id: str) -> Optional[Dict[str, Any]]:
        if self._turn_runs is None:
            return None
        return self._turn_runs.get(
            turn_id=turn_id,
            client_id=client_id,
            companion_session_id=session_id,
        )

    def list_turns(self, *, client_id: str, session_id: str, active_only: bool, limit: int) -> List[Dict[str, Any]]:
        if self._turn_runs is None:
            return []
        return self._turn_runs.list(
            client_id=client_id,
            companion_session_id=session_id,
            active_only=active_only,
            limit=limit,
        )

    def run_turn(
        self,
        request: CompanionTurnRequest,
        callbacks: Optional[CompanionTurnCallbacks] = None,
        accepted_turn: Optional[Dict[str, Any]] = None,
    ) -> CompanionTurnOutcome:
        callbacks = callbacks or CompanionTurnCallbacks()
        deps = self._deps
        api_started = time.perf_counter()
        turn_type = request.turn_type or (
            "user_message" if request.user_message else "app_entry"
        )
        channel = request.channel if request.channel in {"text", "voice"} else "text"

        if accepted_turn is None:
            accepted_turn = self.accept_turn(request)
        user_message_id = accepted_turn.get("user_message_id") if accepted_turn else None
        user_persist_started_at = None
        user_persist_elapsed_ms = None
        if request.persist and request.user_message and accepted_turn is None:
            user_persist_started_at = time.time()
            user_persist_started = time.perf_counter()
            user_message_id = deps.db_store_companion_message(
                session_id=request.session_id,
                client_id=request.client_id,
                role="user",
                content=request.user_message,
                metadata=(
                    {
                        "source": request.input_source,
                        "runtime": "advisor_runtime",
                    }
                    if request.input_source
                    else {"runtime": "advisor_runtime"}
                ),
            )
            user_persist_elapsed_ms = round(
                (time.perf_counter() - user_persist_started) * 1000,
                3,
            )

        if accepted_turn and self._turn_runs is not None:
            self._turn_runs.update(
                accepted_turn["turn_id"],
                client_id=request.client_id,
                status="running",
                stage="main_advisor",
            )
        runtime = deps.get_advisor_runtime()
        client_action_result: Optional[Dict[str, Any]] = None
        client_action_tool_results: List[Dict[str, Any]] = []
        allocation_result: Optional[Dict[str, Any]] = None
        existing_action_proposals: List[Dict[str, Any]] = []
        is_assessment_decision = bool(
            request.client_action
            and request.client_action.type
            == "investment_assessment_decision"
        )
        if is_assessment_decision:
            client_action_result = runtime.execute_client_action(
                client_id=request.client_id,
                session_id=request.session_id,
                action=request.client_action.to_dict(),
            )
            client_action_tool_results.append(client_action_result)
            if callbacks.client_action_completed is not None:
                callbacks.client_action_completed(client_action_result)
            if (
                client_action_result.get("ok") is True
                and str(
                    request.client_action.decision
                ).strip().lower()
                == "agree"
            ):
                existing_action_proposals = ready_proposals_for_assessment(
                    deps=deps,
                    client_id=request.client_id,
                    assessment_id=str(
                        request.client_action.assessment_id
                    ),
                )
                if not existing_action_proposals:
                    signed_assessment_ref = client_action_result.get(
                        "signed_assessment_ref"
                    )
                    if isinstance(signed_assessment_ref, dict):
                        try:
                            allocation_result = (
                                runtime.execute_signed_assessment_proposal(
                                    client_id=request.client_id,
                                    session_id=request.session_id,
                                    signed_assessment_ref=signed_assessment_ref,
                                )
                            )
                        except Exception as exc:
                            allocation_result = {
                                "tool": "run_asset_allocation",
                                "ok": False,
                                "error": type(exc).__name__,
                                "details": str(exc),
                            }
                        client_action_tool_results.append(allocation_result)

        run_turn_kwargs: Dict[str, Any] = {
            "client_id": request.client_id,
            "session_id": request.session_id,
            "user_message": request.user_message,
            "turn_type": turn_type,
            "channel": channel,
        }
        if request.active_skill:
            run_turn_kwargs["active_skill"] = request.active_skill
        supports_delta_callback = (
            callbacks.response_delta is not None
            and request.stream
            and not is_assessment_decision
        )
        if supports_delta_callback:
            run_turn_kwargs["response_delta_callback"] = callbacks.response_delta

        if is_assessment_decision:
            # Sign-off succeeded deterministically. Pre-activate investment-consult
            # so the Agent immediately has consult_investment_solution_specialist
            # available, and provide the signoff result so it appears in the turn.
            run_turn_kwargs["active_skill"] = "investment-consult"
            run_turn_kwargs["initial_tool_results"] = client_action_tool_results
            try:
                result = runtime.run_turn(**run_turn_kwargs)
            except Exception as exc:
                result = {
                    "status": "failed",
                    "response_text": "",
                    "tool_results": client_action_tool_results,
                    "subagent_artifacts": [],
                    "trace_events": [],
                    "errors": [
                        {"type": type(exc).__name__, "message": str(exc)}
                    ],
                    "timing": {},
                }
        else:
            try:
                result = runtime.run_turn(**run_turn_kwargs)
            except Exception as exc:  # pylint: disable=broad-except
                result = {
                    "status": "failed",
                    "response_text": "",
                    "tool_results": [],
                    "subagent_artifacts": [],
                    "trace_events": [],
                    "errors": [
                        {
                            "type": type(exc).__name__,
                            "message": str(exc),
                        }
                    ],
                    "timing": {},
                }
        if client_action_result is not None:
            result = dict(result)
            result["client_action_result"] = client_action_result

        resume_guard = 0
        while (
            str(result.get("status") or "") == "interrupted"
            and isinstance(result.get("interrupted_state"), dict)
            and resume_guard < 3
        ):
            interruptions = list(result.get("interruptions") or [])
            if not interruptions:
                break
            if any(
                str(item.get("tool_name") or "").strip()
                == "record_assessment_signoff"
                for item in interruptions
                if isinstance(item, dict)
            ):
                break
            resume_guard += 1
            result = runtime.resume_turn(
                client_id=request.client_id,
                session_id=request.session_id,
                interrupted_state=result["interrupted_state"],
                approval_decisions=[
                    {
                        "index": int(item.get("index", index)),
                        "action": "approve",
                    }
                    for index, item in enumerate(interruptions)
                    if isinstance(item, dict)
                ],
                channel=channel,
                response_delta_callback=(
                    callbacks.response_delta
                    if supports_delta_callback and not is_assessment_decision
                    else None
                ),
                active_skill=run_turn_kwargs.get("active_skill"),
            )
        if (
            callbacks.response_delta is not None
            and not supports_delta_callback
            and not is_assessment_decision
            and result.get("response_text")
        ):
            callbacks.response_delta(str(result["response_text"]))

        result = dict(result)
        runtime_status = str(result.get("status") or "completed")
        runtime_success = runtime_status not in {"failed", "error"}
        materialize_started = time.perf_counter()
        if runtime_success:
            proposal_result = materialize_proposal_artifacts(
                deps=deps,
                client_id=request.client_id,
                session_id=request.session_id,
                result=result,
            )
            proposal_artifacts = proposal_result.records
            proposal_persist_outcomes = proposal_result.outcomes
            if not proposal_artifacts and existing_action_proposals:
                proposal_artifacts = existing_action_proposals
            projection_result = materialize_projection_artifacts(
                deps=deps,
                client_id=request.client_id,
                session_id=request.session_id,
                result=result,
            )
            projection_artifacts = projection_result.records
            projection_persist_outcomes = projection_result.outcomes
        else:
            proposal_artifacts = []
            proposal_persist_outcomes = []
            projection_artifacts = []
            projection_persist_outcomes = []
        materialize_elapsed_ms = round(
            (time.perf_counter() - materialize_started) * 1000,
            3,
        )
        _append_materialization_errors(
            result,
            "proposal_artifact",
            proposal_persist_outcomes,
        )
        _append_materialization_errors(
            result,
            "projection_artifact",
            projection_persist_outcomes,
        )

        assistant_message = str(result.get("response_text") or "").strip()
        if is_assessment_decision and client_action_result:
            decision = str(
                request.client_action.decision
            ).strip().lower()
            if decision == "agree" and client_action_result.get("ok") is True:
                assistant_message = (
                    "Your assessment is signed off and the proposal is ready "
                    "to review. Nothing has been invested yet — open it to "
                    "review the allocation, return, and risk, and ask me "
                    "anything before you decide."
                    if proposal_artifacts
                    else (
                        "Your sign-off is saved, but I couldn't finish the "
                        "proposal just now. Please try again."
                    )
                )
            elif decision == "cancel" and client_action_result.get("ok") is True:
                assistant_message = (
                    "Understood. I haven't proceeded with this investment "
                    "proposal."
                )
            else:
                assistant_message = (
                    "I couldn't record that decision. Please refresh the "
                    "assessment and try again."
                )
        if not assistant_message:
            assistant_message = fallback_assistant_message_for_artifacts(
                proposal_artifacts=proposal_artifacts,
                subagent_artifacts=result.get("subagent_artifacts", []),
            )

        assistant_message_id = None
        assistant_persist_started_at = None
        assistant_persist_elapsed_ms = None
        if request.persist and runtime_success:
            assistant_persist_started_at = time.time()
            assistant_persist_started = time.perf_counter()
            confirmation_presentation = _confirmation_presentation(result)
            message_metadata: Dict[str, Any] = {
                "runtime": "advisor_runtime",
                "status": runtime_status,
                "user_message_id": user_message_id,
                "selected_skill": result.get("selected_skill"),
                "stream": request.stream,
                "routing_clarification": result.get(
                    "routing_clarification"
                ),
                "pending_intent": result.get("pending_intent"),
                "artifact_context": result.get("artifact_context"),
                **confirmation_presentation,
            }
            if is_assessment_decision and proposal_artifacts:
                ui_artifacts = _build_agree_ui_artifacts(
                    request=request,
                    proposal_artifacts=proposal_artifacts,
                )
                if ui_artifacts:
                    message_metadata["ui_artifacts"] = ui_artifacts
            assistant_message_id = deps.db_store_companion_message_bubbles(
                session_id=request.session_id,
                client_id=request.client_id,
                role="assistant",
                content=assistant_message,
                metadata=message_metadata,
            )
            assistant_persist_elapsed_ms = round(
                (time.perf_counter() - assistant_persist_started) * 1000,
                3,
            )
            if (
                channel == "text"
                and deps.schedule_conversation_compaction is not None
            ):
                try:
                    deps.schedule_conversation_compaction(
                        client_id=request.client_id,
                        session_id=request.session_id,
                    )
                except Exception:  # pragma: no cover - maintenance isolation
                    logger.exception(
                        "Failed to schedule conversation compaction",
                        extra={
                            "client_id": request.client_id,
                            "session_id": request.session_id,
                            "trigger": "schedule",
                        },
                    )
            if (
                assistant_message_id
                and confirmation_presentation.get("confirmation_set_id")
                and deps.bind_fact_confirmation_prompt is not None
            ):
                try:
                    deps.bind_fact_confirmation_prompt(
                        set_id=confirmation_presentation["confirmation_set_id"],
                        client_id=request.client_id,
                        companion_session_id=request.session_id,
                        prompt_message_id=str(assistant_message_id),
                        presented_item_ids=confirmation_presentation["presented_item_ids"],
                    )
                except Exception as exc:
                    runtime_success = False
                    result.setdefault("errors", []).append(
                        {"type": "confirmation_prompt_bind_failed", "message": str(exc)}
                    )

        trace_events = list(result.get("trace_events") or [])
        trace_id = str(result.get("trace_id") or "")
        turn_id = str(result.get("turn_id") or "")
        root_span_id = str(result.get("root_span_id") or "")
        for kind, outcomes in (
            ("proposal", proposal_persist_outcomes),
            ("projection", projection_persist_outcomes),
        ):
            for outcome in outcomes:
                trace_events = _append_materialization_trace(
                    trace_events=trace_events,
                    kind=kind,
                    outcome=outcome,
                    trace_id=trace_id,
                    turn_id=turn_id,
                    root_span_id=root_span_id,
                )
        if user_message_id is not None:
            trace_events = append_trace(
                trace_events,
                event="message_persisted",
                node="persist_user_message",
                summary=(
                    "Persisted the user's final text/voice message before "
                    "agent execution."
                ),
                trace_id=trace_id or None,
                turn_id=turn_id or None,
                parent_span_id=root_span_id or None,
                started_at=_trace_started_at(user_persist_started_at),
                duration_ms=user_persist_elapsed_ms,
                input_data={
                    "session_id": request.session_id,
                    "role": "user",
                    "content": request.user_message,
                    "source": request.input_source,
                },
                output_data={
                    "message_id": user_message_id,
                    "status": "persisted",
                },
                data={
                    "kind": "db_write",
                    "record": "companion_message",
                    "message_id": user_message_id,
                    "source": request.input_source,
                },
            )
        if assistant_message_id is not None:
            trace_events = append_trace(
                trace_events,
                event="message_persisted",
                node="persist_assistant_message",
                summary=(
                    "Persisted the completed AWM response after agent execution."
                ),
                trace_id=trace_id or None,
                turn_id=turn_id or None,
                parent_span_id=root_span_id or None,
                started_at=_trace_started_at(assistant_persist_started_at),
                duration_ms=assistant_persist_elapsed_ms,
                input_data={
                    "session_id": request.session_id,
                    "role": "assistant",
                    "content": assistant_message,
                    "user_message_id": user_message_id,
                },
                output_data={
                    "message_id": assistant_message_id,
                    "status": "persisted",
                },
                data={
                    "kind": "db_write",
                    "record": "companion_message",
                    "message_id": assistant_message_id,
                    "linked_user_message_id": user_message_id,
                },
            )

        timing = dict(result.get("timing") or {})
        timing["proposal_artifact_persist_elapsed_ms"] = materialize_elapsed_ms
        timing["api_total_elapsed_ms"] = round(
            (time.perf_counter() - api_started) * 1000,
            3,
        )
        result["timing"] = timing
        result["trace_events"] = trace_events
        result["response_text"] = assistant_message
        result["trace_timeline"] = format_trace_timeline(
            user_message=request.user_message,
            response_text=assistant_message,
            trace_events=trace_events,
        )
        trace_write = deps.persist_turn_trace(
            client_id=request.client_id,
            session_id=request.session_id,
            user_message=request.user_message,
            result=result,
            channel=channel,
            turn_type=turn_type,
            source_type=request.trace_source_type,
        )
        outcome = CompanionTurnOutcome(
            request=request,
            result=result,
            assistant_message=assistant_message,
            proposal_artifacts=proposal_artifacts,
            projection_artifacts=projection_artifacts,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            trace_write=trace_write,
            http_status=200 if runtime_success else 500,
            turn_receipt=accepted_turn,
        )
        if accepted_turn and self._turn_runs is not None:
            artifact_ids = [
                str(item.get("id") or item.get("artifact_id"))
                for item in [*proposal_artifacts, *projection_artifacts]
                if item.get("id") or item.get("artifact_id")
            ]
            self._turn_runs.update(
                accepted_turn["turn_id"],
                client_id=request.client_id,
                status="done" if runtime_success else "failed",
                stage="completed" if runtime_success else "failed",
                payload_patch={
                    "assistant_message_id": assistant_message_id,
                    "assistant_message": assistant_message,
                    "trace_id": result.get("trace_id") or trace_write.get("trace_id"),
                    "advisor_turn_id": result.get("turn_id") or trace_write.get("turn_id"),
                    "artifact_ids": artifact_ids,
                    "retryable": not runtime_success,
                    "retry_stage": "main_advisor" if not runtime_success else None,
                },
                error=(
                    str((result.get("errors") or ["Advisor runtime failed"])[0])
                    if not runtime_success
                    else None
                ),
            )
        return outcome


def _build_agree_ui_artifacts(
    request: CompanionTurnRequest,
    proposal_artifacts: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build assessment_card + proposal_link metadata for Agree responses."""
    if not request.client_action:
        return []
    assessment_id = str(request.client_action.assessment_id or "")
    assessment_version = request.client_action.assessment_version
    consultation_id = str(
        request.client_action.investment_consultation_id or ""
    )
    money_pool_id = str(request.client_action.money_pool_id or "")
    ui_artifacts: List[Dict[str, Any]] = []
    for artifact in proposal_artifacts:
        if not isinstance(artifact, dict):
            continue
        proposal_id = str(
            artifact.get("id")
            or artifact.get("artifact_id")
            or ""
        ).strip()
        if not proposal_id:
            continue
        ui_artifacts.append(
            {
                "assessment_card": {
                    "artifact_type": "investment_assessment_card",
                    "assessment_id": assessment_id,
                    "assessment_version": assessment_version,
                    "investment_consultation_id": consultation_id,
                    "money_pool_id": money_pool_id,
                    "decision": "signed_off",
                    "title": "Investment Consultation Summary",
                    "subtitle": "For your sign-off",
                },
                "proposal_link": {
                    "artifact_type": "proposal_link",
                    "proposal_id": proposal_id,
                    "assessment_id": assessment_id,
                    "assessment_version": assessment_version,
                    "money_pool_id": money_pool_id,
                    "title": "Check the proposal",
                },
            }
        )
    return ui_artifacts


def fallback_assistant_message_for_artifacts(
    *,
    proposal_artifacts: List[Dict[str, Any]],
    subagent_artifacts: Any,
) -> str:
    if proposal_artifacts:
        return "I've prepared the investment proposal for your review."
    artifacts = subagent_artifacts if isinstance(subagent_artifacts, list) else []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        payload = (
            artifact.get("payload")
            if isinstance(artifact.get("payload"), dict)
            else {}
        )
        if payload.get("artifact_type") == "investment_assessment":
            return "Here's the investment consultation summary for your sign-off."
    return ""


def _append_materialization_errors(
    result: Dict[str, Any],
    prefix: str,
    outcomes: List[Dict[str, Any]],
) -> None:
    errors = [
        str(outcome.get("error"))
        for outcome in outcomes
        if outcome.get("status") == "error"
    ]
    if errors:
        result["errors"] = [
            *list(result.get("errors") or []),
            *[f"{prefix}: {error}" for error in errors],
        ]


def _append_materialization_trace(
    *,
    trace_events: List[Dict[str, Any]],
    kind: str,
    outcome: Dict[str, Any],
    trace_id: str,
    turn_id: str,
    root_span_id: str,
) -> List[Dict[str, Any]]:
    succeeded = outcome.get("status") == "success"
    return append_trace(
        trace_events,
        event=(
            f"{kind}_artifact_persisted"
            if succeeded
            else f"{kind}_artifact_persistence_failed"
        ),
        node=f"persist_{kind}_artifact",
        summary=(
            f"Persisted the advisor result as an APP {kind} artifact."
            if succeeded
            else f"Failed to persist the advisor result as an APP {kind} artifact."
        ),
        trace_id=trace_id or None,
        turn_id=turn_id or None,
        parent_span_id=root_span_id or None,
        duration_ms=outcome.get("duration_ms"),
        input_data={
            "source_advisor_artifact_id": outcome.get(
                "source_advisor_artifact_id"
            ),
            "source_cashflow_analysis_id": outcome.get("analysis_id"),
            "money_pool_label": outcome.get("money_pool_label"),
        },
        output_data={
            "artifact_id": outcome.get("artifact_id"),
            "operation": outcome.get("operation"),
            "status": outcome.get("status"),
        },
        data=outcome,
    )


def _trace_started_at(started_at: Optional[float]) -> Optional[str]:
    if started_at is None:
        return None
    return time.strftime(
        "%Y-%m-%dT%H:%M:%S+00:00",
        time.gmtime(started_at),
    )


def _confirmation_presentation(result: Dict[str, Any]) -> Dict[str, Any]:
    for tool_result in result.get("tool_results") or []:
        if not isinstance(tool_result, dict) or tool_result.get("tool") != "present_fact_confirmation" or tool_result.get("ok") is not True:
            continue
        confirmation_set = tool_result.get("confirmation_set") if isinstance(tool_result.get("confirmation_set"), dict) else {}
        set_id = str(confirmation_set.get("confirmation_set_id") or "")
        item_ids = [str(item) for item in tool_result.get("presented_item_ids") or [] if item]
        if set_id and item_ids:
            return {"confirmation_set_id": set_id, "presented_item_ids": item_ids}
    return {}


__all__ = [
    "CompanionRuntime",
    "CompanionTurnCallbacks",
    "CompanionTurnDependencies",
    "CompanionTurnOutcome",
    "CompanionTurnRequest",
    "CompanionTurnService",
    "fallback_assistant_message_for_artifacts",
]
