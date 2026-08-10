"""Execution boundaries for AWM agent v2 actions."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import threading
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Protocol, Tuple

from client_file.interfaces import ClientFileReader, ClientFileWriter
from client_file.fact_vocabulary import canonical_fact_name
from client_file.lifecycle import (
    FactWriteValidationError,
    build_assessment_signoff_payload,
    build_commit_facts_payload,
    build_draft_fact_payload,
    build_save_fact_payload,
    draft_identity,
    normalize_fact_keys,
)
from advisor.services.deterministic import (
    DeterministicServiceAdapterRegistry,
    DeterministicServiceRequest,
    build_production_service_adapter_registry,
)
from advisor.tools.subagent_tools.assessment_revalidation_specialist.agent import AssessmentRevalidationAgentV2
from advisor.tools.subagent_tools.common.interfaces import SubAgentArtifact
from advisor.tools.subagent_tools.financial_planning_specialist.agent import FinancialPlanningAgentV2
from advisor.tools.subagent_tools.investment_solution_specialist.agent import InvestmentSolutionAgentV2
from advisor.tools.deterministic_tools.persistence import V2PersistentToolHandlers
from advisor.tools.deterministic_tools.registry import AgentToolDefinition, ToolRegistry, build_default_tool_registry


class ToolExecutor(Protocol):
    """Executes deterministic tools selected by the Main Agent."""

    def execute(
        self,
        *,
        client_id: str,
        session_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute one tool and return a normalized result."""


class SubAgentDispatcher(Protocol):
    """Dispatches silent specialist work and returns artifacts."""

    def dispatch(
        self,
        *,
        client_id: str,
        objective: Dict[str, Any],
        client_file: Dict[str, Any],
        subagent: str,
    ) -> SubAgentArtifact:
        """Run a silent sub-agent."""


class ArtifactWritingSubAgentDispatcher:
    """Decorator that persists silent sub-agent artifacts to Client File."""

    def __init__(
        self,
        *,
        inner: SubAgentDispatcher,
        client_file_writer: ClientFileWriter,
    ) -> None:
        self.inner = inner
        self.client_file_writer = client_file_writer

    def dispatch(
        self,
        *,
        client_id: str,
        objective: Dict[str, Any],
        client_file: Dict[str, Any],
        subagent: str,
    ) -> SubAgentArtifact:
        artifact = self.inner.dispatch(
            client_id=client_id,
            objective=objective,
            client_file=client_file,
            subagent=subagent,
        )
        write_result = self.client_file_writer.write_event(
            client_id,
            event_type="subagent_artifact",
            payload=artifact.model_dump(),
            source={
                "source": "advisor_subagent_dispatcher",
                "subagent": subagent,
                "objective_id": objective.get("id"),
            },
        )
        payload = dict(artifact.payload)
        payload["write_result"] = write_result
        return artifact.model_copy(update={"payload": payload})


class FinancialPlanningQueryService(Protocol):
    """Answers wealth-data questions without letting the Main Agent calculate."""

    def answer(
        self,
        *,
        client_id: str,
        session_id: str,
        question: str,
        question_type: str,
        client_file: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Return a structured Financial Planning answer."""

    def analyze_scenario(
        self,
        *,
        client_id: str,
        session_id: str,
        question: str,
        scenario: Dict[str, Any],
        client_file: Dict[str, Any],
        mortgage_defaults_authorized: bool,
        monte_carlo_paths: Optional[int],
        detail_report_groups: Optional[List[str]],
        authorized_public_model_inputs: Optional[List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """Validate and run a structured cash-flow scenario."""


class PublicFinancialFactResearchService(Protocol):
    """Returns a validated public fact without receiving client data."""

    def collect_session_fact(
        self,
        request: Any,
        *,
        session_scope_sha256: str,
        retrieved_at: datetime | None = None,
    ) -> Any:
        """Return a server-bound ephemeral fact for the current session."""


class PublicFinancialFactPromotionService(Protocol):
    """Applies mechanical durable-reuse checks after an agent assessment."""

    def examine_and_promote(
        self,
        fact: Any,
        *,
        assessment: Any,
        examined_at: datetime | None = None,
    ) -> Any:
        """Return one server-owned durable promotion examination."""

@dataclass(frozen=True)
class DeterministicServiceDecision:
    """Result of checking a guarded deterministic service action."""

    allowed: bool
    service_name: str
    reason: str
    requires_explicit_consent: bool = True


class DeterministicServiceGate:
    """Guard irreversible or regulated services outside the LLM."""

    guarded_keywords = {
        "account_opening": ("open account", "kyc", "verify identity"),
        "execution": ("execute", "buy ", "sell ", "trade", "transfer"),
        "policy_exit": ("exit policy", "close policy", "liquidate"),
        "settlement": ("settle", "settlement"),
        "holdings_ingestion": ("connect account", "link account", "import holdings"),
    }

    def check(self, *, user_message: str, explicit_consent: bool = False) -> Optional[DeterministicServiceDecision]:
        text = user_message.lower()
        for service_name, keywords in self.guarded_keywords.items():
            if any(keyword in text for keyword in keywords):
                if explicit_consent:
                    return DeterministicServiceDecision(
                        allowed=True,
                        service_name=service_name,
                        reason="Explicit consent is present for the guarded deterministic service.",
                    )
                return DeterministicServiceDecision(
                    allowed=False,
                    service_name=service_name,
                    reason="Guarded deterministic service requires explicit consent before execution.",
                )
        return None


def _merge_fact_commit_client_files(
    persisted: Dict[str, Any],
    in_process: Dict[str, Any],
    injected: Dict[str, Any],
) -> Dict[str, Any]:
    """Merge fact views while preserving same-turn drafts by stable identity."""

    merged = {**persisted, **in_process, **injected}
    terminal_items: set[tuple[str, str]] = set()
    terminal_drafts: set[str] = set()
    for source in (persisted, in_process, injected):
        decisions = source.get("confirmation_decisions") if isinstance(source.get("confirmation_decisions"), list) else []
        recent = source.get("recent_writebacks") if isinstance(source.get("recent_writebacks"), list) else []
        for decision in [*decisions, *recent]:
            if not isinstance(decision, dict):
                continue
            values = decision.get("values") if isinstance(decision.get("values"), dict) else decision
            status = str(values.get("decision") or values.get("status") or "").strip().lower()
            if status not in {"confirmed", "corrected", "rejected", "superseded"}:
                continue
            draft_id = str(values.get("draft_id") or "").strip()
            field = str(values.get("field") or "").strip()
            if draft_id and field:
                terminal_items.add((draft_id, field))
            elif draft_id:
                terminal_drafts.add(draft_id)
    drafts: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for source in (injected, in_process, persisted):
        source_drafts = (
            source.get("draft_facts")
            if isinstance(source.get("draft_facts"), list)
            else []
        )
        for item in source_drafts:
            if not isinstance(item, dict):
                continue
            identity = str(
                item.get("draft_id")
                or item.get("source_event_id")
                or ""
            )
            if not identity:
                identity = json.dumps(
                    {
                        "fact_type": item.get("fact_type"),
                        "facts": item.get("facts"),
                    },
                    sort_keys=True,
                    default=str,
                )
            if identity in seen:
                continue
            if identity in terminal_drafts:
                continue
            facts = item.get("facts") if isinstance(item.get("facts"), dict) else {}
            retained_facts = {
                field: value
                for field, value in facts.items()
                if (identity, str(canonical_fact_name(str(field)) or field)) not in terminal_items
            }
            if facts and not retained_facts:
                continue
            seen.add(identity)
            drafts.append({**item, "facts": retained_facts} if facts != retained_facts else item)
    merged["draft_facts"] = drafts
    return merged


def _commit_confirmation_audit(
    client_file: Dict[str, Any],
    payload: Dict[str, Any],
) -> tuple[str, str, Dict[str, Any]]:
    """Classify an explicit commit as confirmation or correction of its drafts."""

    resolved_ids = {
        str(item)
        for item in payload.get("resolved_draft_ids", [])
        if item
    }
    committed = payload.get("facts") if isinstance(payload.get("facts"), dict) else {}
    corrected_proposals: Dict[str, Any] = {}
    drafts = (
        client_file.get("draft_facts")
        if isinstance(client_file.get("draft_facts"), list)
        else []
    )
    for index, item in enumerate(drafts):
        if not isinstance(item, dict) or draft_identity(item, index=index) not in resolved_ids:
            continue
        proposed = normalize_fact_keys(
            item.get("facts") if isinstance(item.get("facts"), dict) else {}
        )
        for field, value in proposed.items():
            if field in committed and committed[field] != value:
                corrected_proposals[field] = value

    if corrected_proposals:
        return (
            "corrected",
            ",".join(sorted(corrected_proposals)),
            corrected_proposals,
        )
    return (
        "confirmed",
        ",".join(sorted(committed)),
        committed,
    )


def _fact_tool_argument_shape_errors(
    tool_name: str,
    arguments: Dict[str, Any],
) -> list[Dict[str, Any]]:
    allowed = {
        "draft_fact": {"fact_type", "facts", "entities", "confidence", "metadata"},
        "save_fact": {"fact_type", "facts", "entities", "confidence", "metadata"},
        "commit_facts": {
            "fact_type",
            "facts",
            "entities",
            "fact_ids",
            "confirmation_action_id",
            "confirmation_text",
            "user_message",  # server-injected authenticated current turn
            "post_commit_action",
            "confidence",
            "metadata",
            "client_file",  # server-injected current-turn context
        },
    }.get(tool_name)
    if allowed is None:
        return []
    errors: list[Dict[str, Any]] = []
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        errors.append(
            {
                "reason": "unrecognized_top_level_fields",
                "fields": unknown,
            }
        )
    metadata = arguments.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        errors.append({"reason": "metadata_must_be_an_object"})
    elif isinstance(metadata, dict):
        unknown_metadata = sorted(
            set(metadata)
            - {"source", "source_message_id", "observed_at", "note"}
        )
        if unknown_metadata:
            errors.append(
                {
                    "reason": "unrecognized_metadata_fields",
                    "fields": unknown_metadata,
                }
            )
        invalid_metadata = sorted(
            key
            for key, value in metadata.items()
            if key in {"source", "source_message_id", "observed_at", "note"}
            and not isinstance(value, str)
        )
        if invalid_metadata:
            errors.append(
                {
                    "reason": "invalid_metadata_field_shape",
                    "fields": invalid_metadata,
                }
            )
    if tool_name == "commit_facts":
        post_commit_action = arguments.get("post_commit_action")
        if post_commit_action is not None and post_commit_action != "cashflow_projection":
            errors.append(
                {
                    "reason": "invalid_post_commit_action",
                    "allowed_values": ["cashflow_projection"],
                }
            )
    return errors


class RegistryToolExecutor:
    """Tool executor that enforces v2 tool contracts before writeback."""

    def __init__(
        self,
        *,
        tool_registry: Optional[ToolRegistry] = None,
        client_file_writer: Optional[ClientFileWriter] = None,
        financial_planning_query_service: Optional[FinancialPlanningQueryService] = None,
        asset_allocation_config: Optional[Any] = None,
        asset_allocation_http_session: Optional[Any] = None,
        asset_allocation_request_timeout_seconds: Optional[int] = None,
        wolfram_alpha_http_session: Optional[Any] = None,
        wolfram_alpha_env_getter: Optional[Any] = None,
        public_fact_research_service: Optional[
            PublicFinancialFactResearchService
        ] = None,
        public_fact_promotion_service: Optional[
            PublicFinancialFactPromotionService
        ] = None,
        client_file_reader: Optional[ClientFileReader] = None,
        investment_assessment_store: Optional[Any] = None,
        cashflow_analysis_store: Optional[Any] = None,
        cashflow_analysis_reader: Optional[Any] = None,
        asset_allocation_analysis_store: Optional[Any] = None,
        asset_allocation_analysis_reader: Optional[Any] = None,
        conversation_history_reader: Optional[Any] = None,
        fact_confirmation_repository: Optional[Any] = None,
        client_file: Optional[Dict[str, Any]] = None,
    ):
        self.tool_registry = tool_registry or build_default_tool_registry()
        self.client_file_writer = client_file_writer
        self.financial_planning_query_service = (
            financial_planning_query_service or ContractOnlyFinancialPlanningQueryService()
        )
        self.asset_allocation_config = asset_allocation_config
        self.asset_allocation_http_session = asset_allocation_http_session
        self.asset_allocation_request_timeout_seconds = asset_allocation_request_timeout_seconds
        self.wolfram_alpha_http_session = wolfram_alpha_http_session
        self.wolfram_alpha_env_getter = wolfram_alpha_env_getter
        self.public_fact_research_service = public_fact_research_service
        self.public_fact_promotion_service = public_fact_promotion_service
        self._session_public_facts: OrderedDict[
            Tuple[str, str], Dict[str, Any]
        ] = OrderedDict()
        self._session_public_facts_lock = threading.RLock()
        self._asset_allocation_state = _EngineToolState()
        self.client_file_reader = client_file_reader
        self.investment_assessment_store = investment_assessment_store
        self.cashflow_analysis_store = cashflow_analysis_store
        self.cashflow_analysis_reader = cashflow_analysis_reader
        self._cashflow_analysis_snapshots: Dict[str, Dict[str, Any]] = {}
        self.asset_allocation_analysis_store = asset_allocation_analysis_store
        self.asset_allocation_analysis_reader = asset_allocation_analysis_reader
        self.conversation_history_reader = conversation_history_reader
        self.fact_confirmation_repository = fact_confirmation_repository
        self._asset_allocation_analysis_snapshots: Dict[str, Dict[str, Any]] = {}
        self.client_file = client_file or {}

    def execute(
        self,
        *,
        client_id: str,
        session_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        arguments = dict(arguments)
        authenticated_calculation_context = any(
            key in arguments
            for key in (
                "_arc_authenticated_user_message",
                "_arc_companion_turn_id",
            )
        )
        companion_turn_id = str(
            arguments.pop("_arc_companion_turn_id", "")
            or arguments.get("source_turn_id")
            or session_id
        )
        authenticated_user_message = str(
            arguments.pop("_arc_authenticated_user_message", "") or ""
        )
        tool = self.tool_registry.get(tool_name)
        result = {
            "tool": tool_name,
            "ok": True,
            "read_only": tool.read_only,
            "writeback_target": tool.writeback_target,
            "arguments": arguments,
        }
        if tool_name == "present_fact_confirmation":
            return self._present_fact_confirmation(
                client_id=client_id,
                session_id=session_id,
                source_turn_id=companion_turn_id,
                arguments=arguments,
                result=result,
            )
        if tool_name == "resolve_fact_confirmation":
            return self._resolve_fact_confirmation(
                client_id=client_id,
                session_id=session_id,
                arguments=arguments,
                result=result,
            )
        if tool_name == "record_confirmation_decision":
            decision = str(arguments.get("decision") or "").strip().lower()
            required = {
                "draft_id",
                "field",
                "proposed_value",
                "rationale",
                "database_action",
                "client_message",
            }
            if decision not in {"confirmed", "rejected", "corrected", "ambiguous"} or any(
                key not in arguments for key in required
            ):
                return {
                    **result,
                    "ok": False,
                    "error": "confirmation_decision_invalid",
                }
            if not self.client_file_writer:
                return {**result, "ok": False, "error": "client_file_writer_missing"}
            payload = {
                "draft_id": arguments["draft_id"],
                "field": arguments["field"],
                "proposed_value": arguments["proposed_value"],
                "client_message": arguments["client_message"],
                "decision": decision,
                "rationale": str(arguments["rationale"]),
                "database_action": dict(arguments["database_action"]),
                "session_id": session_id,
                "turn_id": str(arguments.get("turn_id") or session_id),
                "model_version": str(arguments.get("model_version") or os.getenv("OPENAI_MODEL") or "unknown"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            write_result = self.client_file_writer.write_event(
                client_id,
                event_type="confirmation_decision",
                payload=payload,
                source={"session_id": session_id, "source": "advisor_semantic_decision"},
            )
            return {
                **result,
                "ok": bool(write_result.get("ok")),
                "payload": payload,
                "write_result": write_result,
            }
        if tool_name == "retrieve_conversation_history":
            if self.conversation_history_reader is None:
                return {
                    **result,
                    "ok": False,
                    "error": "conversation_history_unavailable",
                    "detail": "Conversation-history storage is unavailable.",
                }
            try:
                retrieval = self.conversation_history_reader(
                    client_id,
                    session_id,
                    message_ids=arguments.get("message_ids"),
                    from_message_id=arguments.get("from_message_id"),
                    through_message_id=arguments.get("through_message_id"),
                    query=arguments.get("query"),
                    max_results=arguments.get("max_results", 20),
                    context_window=arguments.get("context_window", 2),
                )
            except PermissionError as exc:
                return {
                    **result,
                    "ok": False,
                    "error": "conversation_history_access_denied",
                    "detail": str(exc),
                }
            except ValueError as exc:
                return {
                    **result,
                    "ok": False,
                    "error": "conversation_history_query_invalid",
                    "detail": str(exc),
                }
            except Exception as exc:  # pragma: no cover - production adapter safety
                return {
                    **result,
                    "ok": False,
                    "error": "conversation_history_retrieval_failed",
                    "detail": str(exc),
                }
            return {**result, **retrieval}
        if tool_name == "cancel_specialist_job":
            from advisor.agents.background_jobs import cancel_specialist_job

            job_id = str(arguments.get("job_id") or "").strip()
            reason = str(arguments.get("reason") or "").strip()
            if not job_id or not reason:
                return {
                    **result,
                    "ok": False,
                    "error": "specialist_job_control_invalid",
                    "detail": "job_id and reason are required",
                }
            event = cancel_specialist_job(
                job_id,
                reason=reason,
                client_id=client_id,
            )
            if event is None:
                return {
                    **result,
                    "ok": False,
                    "error": "specialist_job_not_found",
                    "detail": "No specialist job with that id belongs to the current client.",
                }
            return {
                **result,
                "cancelled": str(event.get("status") or "") == "cancelled",
                "job_id": job_id,
                "status": event.get("status"),
            }
        if tool_name == "report_calculation_capability_gap":
            from advisor.tools.deterministic_tools.report_calculation_capability_gap.execution import (
                build_calculation_capability_gap,
            )

            gap = build_calculation_capability_gap(arguments)
            result.update(
                {
                    "full_result": gap,
                    "terminal": True,
                    "retry_allowed": False,
                    "client_message": gap["client_message"],
                    "note": (
                        "The calculation toolkit reached a terminal capability "
                        "boundary. Do not call another tool in this turn."
                    ),
                }
            )
            return result
        if tool_name == "research_public_financial_fact":
            from advisor.assumptions.research import (
                ResearchRequest,
                SessionPublicFact,
                ResearchSpecialistError,
                session_public_fact_integrity_valid,
            )

            if self.public_fact_research_service is None:
                return {
                    **result,
                    "ok": False,
                    "error": "research_disabled",
                    "retry_allowed": False,
                    "note": (
                        "Session public-data research is unavailable. Do not invent "
                        "the missing fact or retry this call unchanged."
                    ),
                }
            if (
                not str(client_id or "").strip()
                or not str(session_id or "").strip()
            ):
                return {
                    **result,
                    "ok": False,
                    "error": "research_session_required",
                    "retry_allowed": False,
                }
            session_scope = _session_public_fact_scope(
                client_id=client_id,
                session_id=session_id,
            )
            try:
                fact = self.public_fact_research_service.collect_session_fact(
                    ResearchRequest(
                        variable_key=arguments.get("variable_key"),
                        effective_year=arguments.get("effective_year"),
                    ),
                    session_scope_sha256=session_scope,
                )
                fact_data = fact.model_dump(mode="json")
            except ResearchSpecialistError as exc:
                return {
                    **result,
                    "ok": False,
                    "error": exc.code.value,
                    "research_attempted": exc.attempted,
                    "retry_allowed": False,
                }
            except Exception:
                return {
                    **result,
                    "ok": False,
                    "error": "research_gateway_unavailable",
                    "retry_allowed": False,
                }
            try:
                fact_model = SessionPublicFact.model_validate(fact_data)
                if not session_public_fact_integrity_valid(fact_model):
                    raise ValueError("session public fact integrity mismatch")
                fact_data = fact_model.model_dump(mode="json")
            except Exception:
                return {
                    **result,
                    "ok": False,
                    "error": "research_output_invalid",
                    "retry_allowed": False,
                }
            if fact_data.get("session_scope_sha256") != session_scope:
                return {
                    **result,
                    "ok": False,
                    "error": "research_output_invalid",
                    "retry_allowed": False,
                }

            session_fact_id = str(fact_data["fact_id"])
            if not self._store_session_public_fact(
                session_scope=session_scope,
                fact_data=fact_data,
            ):
                return {
                    **result,
                    "ok": False,
                    "error": "research_output_stale",
                    "retry_allowed": False,
                }
            promotion_receipt = {
                "schema_version": "awm.durable_fact_promotion_receipt.v1",
                "status": "session_only",
                "reason_codes": ["agent_reuse_review_not_completed"],
                "examination_id": None,
                "durable_assumption_id": None,
                "durable_version": None,
                "supersedes_artifact_id": None,
                "granted_uses": [],
                "agent_assessment": None,
                "verification": None,
                "policy": None,
            }
            return {
                **result,
                "ok": True,
                "retry_allowed": False,
                "full_result": {
                    "schema_version": (
                        "awm.session_public_fact_authorization.v1"
                    ),
                    "session_fact_id": session_fact_id,
                    "authorization": {
                        "scope": "current_financial_planning_session",
                        "session_scope_sha256": session_scope,
                        "expires_at": fact_data.get("expires_at"),
                        "human_review_required": False,
                        "durable": False,
                        "reporting_allowed": True,
                        "session_calculation_allowed": fact_data.get(
                            "session_calculation_allowed"
                        )
                        is True,
                        "durable_model_input_allowed": False,
                        "recommendation_allowed": False,
                    },
                    "fact": {
                        key: copy.deepcopy(fact_data.get(key))
                        for key in (
                            "variable_key",
                            "effective_year",
                            "value",
                            "unit",
                            "jurisdiction",
                            "content_sha256",
                            "retrieved_at",
                            "origin",
                        )
                    },
                    "sources": copy.deepcopy(fact_data.get("sources") or []),
                    "durable_promotion": promotion_receipt,
                    "disclosure": (
                        "The server validated and session-bound the researched fact. "
                        "It is available for immediate conversational use when the "
                        "Financial Planning agent selects it. Durable storage and reuse "
                        "are governed by that agent's separate reuse decision."
                    ),
                },
                "note": (
                    "The Financial Planning agent requested this public fact. The "
                    "server validated and session-bound it without human approval. "
                    "Call review_public_fact_reuse only after making the separate "
                    "agent decision about durable storage and reuse."
                ),
            }
        if tool_name == "review_public_fact_reuse":
            from advisor.assumptions.contracts import (
                DurableFactPromotionExamination,
            )
            from advisor.assumptions.promotion import (
                build_durable_fact_agent_assessment,
            )
            from advisor.assumptions.research import (
                SessionPublicFact,
                session_public_fact_integrity_valid,
            )

            fact_data, fact_error = self._load_session_public_fact(
                client_id=client_id,
                session_id=session_id,
                session_fact_id=str(arguments.get("session_fact_id") or ""),
            )
            if fact_error is not None or fact_data is None:
                return {
                    **result,
                    "ok": False,
                    "error": fact_error or "session_public_fact_unavailable",
                    "retry_allowed": False,
                }
            try:
                fact_model = SessionPublicFact.model_validate(fact_data)
                if not session_public_fact_integrity_valid(fact_model):
                    raise ValueError("session public fact integrity mismatch")
                assessment = build_durable_fact_agent_assessment(
                    fact_model,
                    decision=arguments.get("decision"),
                    reason_code=arguments.get("reason_code"),
                )
            except Exception:
                return {
                    **result,
                    "ok": False,
                    "error": "public_fact_reuse_review_invalid",
                    "retry_allowed": False,
                }

            default_reason = (
                "agent_assessment_kept_session_only"
                if assessment.decision == "keep_session_only"
                else "durable_promotion_unavailable"
            )
            promotion_receipt = {
                "schema_version": "awm.durable_fact_promotion_receipt.v1",
                "status": "session_only",
                "reason_codes": [default_reason],
                "examination_id": None,
                "durable_assumption_id": None,
                "durable_version": None,
                "supersedes_artifact_id": None,
                "granted_uses": [],
                "agent_assessment": {
                    "assessment_id": assessment.assessment_id,
                    "assessed_by": assessment.assessed_by,
                    "decision": assessment.decision,
                    "reason_code": assessment.reason_code,
                },
                "verification": None,
                "policy": None,
            }
            if (
                self.public_fact_promotion_service is not None
                and assessment.decision == "authorize_durable_reuse"
            ):
                try:
                    examination = DurableFactPromotionExamination.model_validate(
                        self.public_fact_promotion_service.examine_and_promote(
                            fact_model,
                            assessment=assessment,
                        )
                    )
                    if (
                        examination.schema_version
                        != "awm.durable_fact_promotion.v2"
                        or examination.agent_assessment != assessment
                    ):
                        raise ValueError(
                            "promotion examination does not match agent assessment"
                        )
                    promotion_receipt = _durable_fact_promotion_receipt(
                        examination,
                        fact_data=fact_data,
                    )
                except Exception:
                    promotion_receipt = {
                        **promotion_receipt,
                        "reason_codes": ["durable_promotion_failed"],
                    }

            projection_input_ref = (
                {
                    "variable_key": "social_security_taxable_maximum",
                    "session_fact_id": fact_model.fact_id,
                }
                if fact_model.variable_key
                == "social_security_taxable_maximum"
                else None
            )
            return {
                **result,
                "ok": True,
                "retry_allowed": False,
                "full_result": {
                    "schema_version": "awm.public_fact_reuse_review.v1",
                    "session_fact_id": fact_model.fact_id,
                    "agent_assessment": promotion_receipt[
                        "agent_assessment"
                    ],
                    "durable_promotion": promotion_receipt,
                    "projection_input_ref": projection_input_ref,
                    "immediate_session_use": {
                        "human_review_required": False,
                        "reporting_allowed": fact_model.reporting_allowed,
                        "session_calculation_allowed": (
                            fact_model.session_calculation_allowed
                        ),
                        "projection_model_input_supported": (
                            projection_input_ref is not None
                        ),
                    },
                },
                "note": (
                    "The Financial Planning agent made the semantic reuse decision. "
                    "The server only validated and applied its mechanical storage "
                    "and model-input contracts."
                ),
            }
        if tool_name == "get_cashflow_analysis":
            return self._get_cashflow_analysis(
                client_id=client_id,
                session_id=session_id,
                analysis_id=arguments.get("analysis_id"),
                detail_query=arguments,
                result=result,
            )
        if tool_name == "audit_cashflow_analysis":
            lookup = self._get_cashflow_analysis(
                client_id=client_id,
                session_id=session_id,
                analysis_id=arguments.get("analysis_id"),
                result={
                    "tool": "get_cashflow_analysis",
                    "ok": True,
                    "read_only": True,
                    "writeback_target": "none",
                    "arguments": {
                        "analysis_id": arguments.get("analysis_id"),
                        "calendar_years": None,
                        "detail_columns": None,
                    },
                },
            )
            if lookup.get("ok") is not True:
                result.update(
                    {
                        "ok": False,
                        "error": "cashflow_analysis_audit_source_unavailable",
                        "details": lookup.get("error"),
                        "analysis_id": lookup.get("analysis_id"),
                        "requires_rerun": lookup.get("requires_rerun"),
                    }
                )
                return result
            resolved_id = str(lookup.get("analysis_id") or "").strip()
            artifact = None
            if self.cashflow_analysis_reader is not None:
                artifact = self.cashflow_analysis_reader(
                    client_id=client_id,
                    analysis_id=resolved_id,
                    session_id=session_id,
                )
            snapshot = (
                artifact.get("payload")
                if isinstance(artifact, dict)
                and isinstance(artifact.get("payload"), dict)
                else artifact
            )
            if not isinstance(snapshot, dict):
                snapshot = self._cashflow_analysis_snapshots.get(resolved_id)
            if (
                not isinstance(snapshot, dict)
                or snapshot.get("client_id") != client_id
                or snapshot.get("analysis_id") != resolved_id
            ):
                result.update(
                    {
                        "ok": False,
                        "error": "cashflow_analysis_audit_snapshot_invalid",
                        "analysis_id": resolved_id or None,
                    }
                )
                return result
            from advisor.tools.deterministic_tools.audit_cashflow_analysis.execution import (
                audit_cashflow_snapshot,
            )

            audited = audit_cashflow_snapshot(snapshot)
            result["ok"] = audited.get("success") is True
            result["full_result"] = audited.get("full_result")
            result["analysis_id"] = resolved_id
            result["input_fingerprint"] = snapshot.get("input_fingerprint")
            result["retrieved_without_rerun"] = True
            result["requires_rerun"] = bool(
                isinstance(result.get("full_result"), dict)
                and result["full_result"].get("requires_source_rerun")
            )
            result["note"] = (
                "The immutable stored snapshot was audited programmatically; "
                "LifeModel was not rerun and no financial recommendation was produced."
            )
            return result
        if tool_name == "get_asset_allocation_analysis":
            return self._get_asset_allocation_analysis(
                client_id=client_id,
                session_id=session_id,
                analysis_id=arguments.get("analysis_id"),
                result=result,
            )
        if tool_name == "compare_quant_analyses":
            domain = str(arguments.get("domain") or "").strip()
            getter = (
                self._get_cashflow_analysis
                if domain == "cashflow"
                else self._get_asset_allocation_analysis
                if domain == "asset_allocation"
                else None
            )
            if getter is None:
                result.update({"ok": False, "error": "comparison_domain_invalid"})
                return result
            base_id = str(arguments.get("base_analysis_id") or "").strip()
            comparison_id = str(
                arguments.get("comparison_analysis_id") or ""
            ).strip()
            base = getter(
                client_id=client_id,
                session_id=session_id,
                analysis_id=base_id,
                result={
                    "tool": (
                        "get_cashflow_analysis"
                        if domain == "cashflow"
                        else "get_asset_allocation_analysis"
                    ),
                    "ok": True,
                    "read_only": True,
                    "writeback_target": "none",
                    "arguments": {"analysis_id": base_id},
                },
            )
            comparison = getter(
                client_id=client_id,
                session_id=session_id,
                analysis_id=comparison_id,
                result={
                    "tool": (
                        "get_cashflow_analysis"
                        if domain == "cashflow"
                        else "get_asset_allocation_analysis"
                    ),
                    "ok": True,
                    "read_only": True,
                    "writeback_target": "none",
                    "arguments": {"analysis_id": comparison_id},
                },
            )
            unavailable = [
                item
                for item in (base, comparison)
                if item.get("ok") is not True
            ]
            if unavailable:
                result.update(
                    {
                        "ok": False,
                        "error": "comparison_analysis_unavailable",
                        "requires_rerun": True,
                        "lookup_errors": [
                            {
                                "analysis_id": item.get("analysis_id"),
                                "error": item.get("error"),
                            }
                            for item in unavailable
                        ],
                    }
                )
                return result
            from advisor.tools.deterministic_tools.compare_quant_analyses.execution import (
                compare_quant_analysis_results,
            )

            compared = compare_quant_analysis_results(
                domain=domain,
                base=base,
                comparison=comparison,
                metric_keys=arguments.get("metric_keys"),
            )
            result["ok"] = compared.get("success") is True
            result["full_result"] = compared.get("full_result")
            if compared.get("error"):
                result["error"] = compared["error"]
            result["base_analysis_id"] = base_id
            result["comparison_analysis_id"] = comparison_id
            result["retrieved_without_rerun"] = True
            result["note"] = (
                "Exact arithmetic deltas compare two fresh immutable analyses; "
                "they do not establish causation."
            )
            return result
        if tool_name == "calculate_cashflow_metrics":
            lookup = self._get_cashflow_analysis(
                client_id=client_id,
                session_id=session_id,
                analysis_id=arguments.get("analysis_id"),
                result={
                    "tool": "get_cashflow_analysis",
                    "ok": True,
                    "read_only": True,
                    "writeback_target": "none",
                    "arguments": {
                        "analysis_id": arguments.get("analysis_id"),
                        "calendar_years": None,
                        "detail_columns": None,
                    },
                },
            )
            if lookup.get("ok") is not True:
                result.update(
                    {
                        "ok": False,
                        "error": "cashflow_calculation_source_unavailable",
                        "details": lookup.get("error"),
                        "analysis_id": lookup.get("analysis_id"),
                        "requires_rerun": lookup.get("requires_rerun"),
                    }
                )
                return result
            from advisor.tools.deterministic_tools.calculate_cashflow_metrics.execution import (
                calculate_cashflow_metrics,
            )

            calculated = calculate_cashflow_metrics(
                analysis_id=str(lookup.get("analysis_id") or ""),
                recommendation_evidence=(
                    lookup.get("recommendation_evidence")
                    if isinstance(lookup.get("recommendation_evidence"), dict)
                    else {}
                ),
                arguments=arguments,
            )
            result["ok"] = calculated.get("success") is True
            result["full_result"] = calculated.get("full_result")
            result["analysis_id"] = lookup.get("analysis_id")
            result["input_fingerprint"] = lookup.get("input_fingerprint")
            result["retrieved_without_rerun"] = True
            result["requires_rerun"] = False
            if calculated.get("error"):
                result["error"] = calculated["error"]
            result["note"] = (
                "Typed operands were resolved from one immutable cash-flow analysis "
                "and calculated without rerunning LifeModel."
            )
            return result
        if tool_name == "calculate_financial_math":
            from advisor.tools.deterministic_tools.calculate_financial_math.execution import (
                calculate_financial_math,
            )

            if (
                authenticated_calculation_context
                and arguments.get("schema_version") != "awm.financial_math.v2"
            ):
                return {
                    **result,
                    "ok": False,
                    "error": "financial_math_agent_plan_schema_required",
                }
            try:
                current_client_file = (
                    self.client_file_reader.read(client_id).payload
                    if self.client_file_reader is not None
                    else self.client_file
                )
            except Exception as exc:
                return {**result, "ok": False, "error": "client_file_read_failed", "details": str(exc)}
            plan_arguments, authentication_error = _prepare_financial_math_arguments(
                arguments,
                companion_turn_id=companion_turn_id,
                authenticated_user_message=authenticated_user_message,
                current_client_file=(
                    current_client_file
                    if isinstance(current_client_file, dict)
                    else {}
                ),
            )
            if authentication_error:
                return {
                    **result,
                    "ok": False,
                    "error": authentication_error,
                }
            calculated = calculate_financial_math(
                plan_arguments,
                client_id=client_id,
                companion_turn_id=companion_turn_id,
                client_file=current_client_file if isinstance(current_client_file, dict) else {},
                calculation_result_reader=lambda *, source: (
                    self._resolve_financial_math_source(
                        client_id=client_id,
                        session_id=session_id,
                        source=source,
                    )
                ),
            )
            result["ok"] = calculated.get("success") is True
            result["full_result"] = calculated.get("full_result")
            if calculated.get("error"):
                result["error"] = calculated["error"]
            result["note"] = (
                "The result is deterministic arithmetic over authenticated inputs. "
                "Stored cash-flow evidence, when referenced, was ownership- and "
                "freshness-checked without rerunning the model."
            )
            return result
        if tool_name == "query_wolfram_alpha":
            from advisor.tools.deterministic_tools.query_wolfram_alpha.execution import (
                query_wolfram_alpha,
            )

            raw_query = str(arguments.get("query") or "")
            result["arguments"] = {
                "query_sha256": (
                    f"sha256:{hashlib.sha256(raw_query.encode('utf-8')).hexdigest()}"
                    if raw_query
                    else ""
                ),
                "query_length": len(raw_query),
                "expected_unit": arguments.get("expected_unit"),
            }
            adapter_options: Dict[str, Any] = {}
            if self.wolfram_alpha_http_session is not None:
                adapter_options["http_session"] = self.wolfram_alpha_http_session
            if self.wolfram_alpha_env_getter is not None:
                adapter_options["env_getter"] = self.wolfram_alpha_env_getter
            computed = query_wolfram_alpha(arguments, **adapter_options)
            result["ok"] = computed.get("success") is True
            result["full_result"] = computed.get("full_result")
            result["retry_allowed"] = False
            if computed.get("error"):
                result["error"] = computed["error"]
            if computed.get("query_hash"):
                result["query_hash"] = computed["query_hash"]
            result["note"] = (
                "The Financial Planning agent selected one external pure-math "
                "fallback. The adapter received only a privacy-screened query, never "
                "Client File or analysis objects, and accepts only one unambiguous "
                "finite scalar for reporting."
            )
            return result
        if tool_name in {"analyze_portfolio_risk", "analyze_asset_location"}:
            allocation_analysis_id = str(
                arguments.get("allocation_analysis_id") or ""
            ).strip()
            allocation_lookup = self._get_asset_allocation_analysis(
                client_id=client_id,
                session_id=session_id,
                analysis_id=allocation_analysis_id,
                result={
                    "tool": "get_asset_allocation_analysis",
                    "ok": True,
                    "read_only": True,
                    "writeback_target": "none",
                    "arguments": {"analysis_id": allocation_analysis_id},
                },
            )
            if allocation_lookup.get("ok") is not True:
                result.update(
                    {
                        "ok": False,
                        "error": "allocation_analysis_unavailable",
                        "details": allocation_lookup.get("error"),
                        "requires_rerun": True,
                    }
                )
                return result
            allocation_view = (
                allocation_lookup.get("asset_allocation_agent_view")
                if isinstance(
                    allocation_lookup.get("asset_allocation_agent_view"), dict
                )
                else {}
            )
            allocation_payload = (
                allocation_view.get("allocation")
                if isinstance(allocation_view.get("allocation"), dict)
                else {}
            )
            weights = (
                allocation_payload.get("weights")
                if isinstance(allocation_payload.get("weights"), dict)
                else {}
            )
            if tool_name == "analyze_portfolio_risk":
                from advisor.tools.deterministic_tools.analyze_portfolio_risk.execution import (
                    run_portfolio_risk_analysis,
                )

                analyzed = run_portfolio_risk_analysis(
                    allocation=weights,
                    stress_scenarios=arguments.get("stress_scenarios"),
                    drawdown_config=arguments.get("drawdown_config"),
                    fee_drag_config=arguments.get("fee_drag_config"),
                    initial_investment=(
                        allocation_view.get("mandate", {}).get("total_investment")
                        if isinstance(allocation_view.get("mandate"), dict)
                        else None
                    ),
                )
            else:
                try:
                    current_client_file = (
                        self.client_file_reader.read(client_id).payload
                        if self.client_file_reader is not None
                        else self.client_file
                    )
                    modeled = build_cashflow_payload_from_client_file(
                        current_client_file
                    )
                except Exception as exc:
                    result.update(
                        {
                            "ok": False,
                            "error": "client_file_read_failed",
                            "details": str(exc),
                        }
                    )
                    return result
                accounts = (
                    modeled.get("accounts")
                    if isinstance(modeled.get("accounts"), dict)
                    else {}
                )

                def account_total(name: str) -> float:
                    pool = accounts.get(name)
                    if not isinstance(pool, list):
                        return 0.0
                    return sum(
                        float(item.get("balance") or 0.0)
                        for item in pool
                        if isinstance(item, dict)
                    )

                from advisor.tools.deterministic_tools.analyze_asset_location.execution import (
                    run_asset_location_analysis,
                )

                analyzed = run_asset_location_analysis(
                    allocation=weights,
                    taxable_balance=account_total("brokerage"),
                    retirement_balance=account_total("retirement"),
                )
            result["ok"] = analyzed.get("success") is True
            result["full_result"] = analyzed.get("full_result")
            result["analysis_id"] = allocation_analysis_id
            result["source_allocation_analysis_id"] = allocation_analysis_id
            if analyzed.get("error"):
                result["error"] = analyzed["error"]
            result["note"] = (
                "This is a separate reporting-only validated capability; it "
                "does not alter the allocation, save a proposal, or execute trades."
            )
            return result
        if tool_name == "solve_cashflow_contribution":
            try:
                current_client_file = (
                    self.client_file_reader.read(client_id).payload
                    if self.client_file_reader is not None
                    else self.client_file
                )
            except Exception as exc:
                result.update(
                    {
                        "ok": False,
                        "error": "client_file_read_failed",
                        "details": str(exc),
                    }
                )
                return result
            if not isinstance(current_client_file, dict):
                result.update(
                    {"ok": False, "error": "client_file_read_invalid"}
                )
                return result

            allocation_analysis_id = str(
                arguments.get("allocation_analysis_id") or ""
            ).strip() or None
            effective_client_file = current_client_file
            if allocation_analysis_id is not None:
                allocation_lookup = self._get_asset_allocation_analysis(
                    client_id=client_id,
                    session_id=session_id,
                    analysis_id=allocation_analysis_id,
                    result={
                        "tool": "get_asset_allocation_analysis",
                        "ok": True,
                        "read_only": True,
                        "writeback_target": "none",
                        "arguments": {"analysis_id": allocation_analysis_id},
                    },
                )
                if allocation_lookup.get("ok") is not True:
                    result.update(
                        {
                            "ok": False,
                            "error": "cashflow_allocation_analysis_unavailable",
                            "details": allocation_lookup.get("error"),
                            "requires_rerun": True,
                        }
                    )
                    return result
                effective_client_file = _client_file_with_linked_allocation(
                    current_client_file,
                    allocation_lookup,
                )

            start_horizon_years = int(arguments.get("start_horizon_years") or 0)
            duration_years = int(arguments.get("duration_years") or 0)
            question = str(arguments.get("question") or "")
            monte_carlo_paths = arguments.get("monte_carlo_paths")

            def evaluate(monthly_contribution: float) -> Dict[str, Any]:
                changes = []
                if monthly_contribution > 0:
                    changes.append(
                        {
                            "kind": "recurring_investment_contribution",
                            "amount": monthly_contribution * 12.0,
                            "horizon_years": start_horizon_years,
                            "duration_years": duration_years,
                            "person": "primary",
                            "label": "Contribution solver candidate",
                            "unit": "USD",
                        }
                    )
                return self.financial_planning_query_service.analyze_scenario(
                    client_id=client_id,
                    session_id=session_id,
                    question=question,
                    scenario={
                        "requested": True,
                        "action": "run_cashflow_model",
                        "confidence": 1.0,
                        "source": "deterministic_contribution_solver",
                        "reason": (
                            "The deterministic contribution solver is testing "
                            "an explicit bounded candidate."
                        ),
                        "evidence": [],
                        "requested_metrics": [
                            "success_probability",
                            "shortfall",
                            "minimum_liquidity",
                            "terminal_value_percentiles",
                        ],
                        "scenario_changes": changes,
                        "negated": False,
                    },
                    client_file=effective_client_file,
                    mortgage_defaults_authorized=bool(
                        arguments.get("mortgage_defaults_authorized")
                    ),
                    monte_carlo_paths=monte_carlo_paths,
                )

            from advisor.tools.deterministic_tools.solve_cashflow_contribution.execution import (
                solve_monthly_contribution,
            )

            solved = solve_monthly_contribution(
                evaluate=evaluate,
                objective=str(arguments.get("objective") or ""),
                target_terminal_value=arguments.get("target_terminal_value"),
                minimum_success_probability=float(
                    arguments.get("minimum_success_probability")
                ),
                minimum_p10_liquidity=float(
                    arguments.get("minimum_p10_liquidity")
                ),
                maximum_monthly_contribution=float(
                    arguments.get("maximum_monthly_contribution")
                ),
                monthly_tolerance=float(arguments.get("monthly_tolerance")),
            )
            result["ok"] = solved.get("success") is True
            result["execution_ok"] = result["ok"]
            result["valid_for_reporting"] = result["ok"]
            result["valid_for_conclusion"] = result["ok"]
            result["valid_for_recommendation"] = False
            result["full_result"] = solved.get("full_result")
            result["source_allocation_analysis_id"] = allocation_analysis_id
            if solved.get("error"):
                result["error"] = solved["error"]
                result["tested_points"] = solved.get("tested_points")
            selected_monthly = solved.get("selected_monthly_contribution")
            if solved.get("success") is True and selected_monthly is not None:
                selected_scenario_changes = []
                if float(selected_monthly) > 0:
                    selected_scenario_changes.append(
                        {
                            "kind": "recurring_investment_contribution",
                            "amount": float(selected_monthly) * 12.0,
                            "horizon_years": start_horizon_years,
                            "duration_years": duration_years,
                            "person": "primary",
                            "label": "Selected contribution solver scenario",
                            "unit": "USD",
                        }
                    )
                selected_run = self.execute(
                    client_id=client_id,
                    session_id=session_id,
                    tool_name="run_cashflow_projection",
                    arguments={
                        "question": (
                            f"{question} Validate the selected bounded solver "
                            f"scenario at ${float(selected_monthly):,.2f} per month."
                        ),
                        "mortgage_defaults_authorized": bool(
                            arguments.get("mortgage_defaults_authorized")
                        ),
                        "allocation_analysis_id": allocation_analysis_id,
                        "monte_carlo_paths": monte_carlo_paths,
                        "scenario": {
                            "requested": True,
                            "action": "run_cashflow_model",
                            "confidence": 1.0,
                            "source": "deterministic_contribution_solver",
                            "reason": "Persist the selected bounded solver scenario.",
                            "evidence": [],
                            "requested_metrics": [
                                "success_probability",
                                "shortfall",
                                "minimum_liquidity",
                                "terminal_value_percentiles",
                            ],
                            "scenario_changes": selected_scenario_changes,
                            "negated": False,
                        },
                    },
                )
                result["selected_cashflow_analysis_id"] = selected_run.get(
                    "analysis_id"
                )
                result["selected_cashflow_agent_view"] = selected_run.get(
                    "cashflow_agent_view"
                )
                result["selected_cashflow_valid_for_reporting"] = selected_run.get(
                    "valid_for_reporting"
                )
                if isinstance(result.get("full_result"), dict):
                    result["full_result"]["selected_cashflow_analysis_id"] = (
                        selected_run.get("analysis_id")
                    )
            result["note"] = (
                "The solver reports a bounded numerical result under explicit "
                "constraints. It does not save a proposal, execute a trade, or "
                "enable recommendation narration."
            )
            return result
        if tool_name == "run_cashflow_projection":
            authorized_public_model_inputs, public_input_error = (
                self._authorized_public_model_inputs(
                    client_id=client_id,
                    session_id=session_id,
                    references=arguments.get("public_fact_refs"),
                )
            )
            if public_input_error is not None:
                result.update(
                    {
                        "ok": False,
                        "execution_ok": False,
                        "valid_for_reporting": False,
                        "valid_for_conclusion": False,
                        "valid_for_recommendation": False,
                        "error": public_input_error,
                        "analysis": {
                            "schema_version": "awm.cashflow_result.v2",
                            "status": {
                                "execution": "not_run",
                                "validation": "invalid_request",
                                "analysis_grade": "not_run",
                                "valid_for_recommendation": False,
                                "warnings": [],
                                "error": public_input_error,
                                "missing_required_metrics": [],
                                "normalization_errors": [],
                            },
                            "metrics": {},
                            "missing_data": ["authorized_public_model_input"],
                        },
                    }
                )
                return result
            try:
                current_client_file = (
                    self.client_file_reader.read(client_id).payload
                    if self.client_file_reader is not None
                    else self.client_file
                )
            except Exception as exc:
                result.update(
                    {
                        "ok": False,
                        "error": "client_file_read_failed",
                        "details": str(exc),
                        "analysis": {
                            "schema_version": "awm.cashflow_result.v2",
                            "status": {
                                "execution": "not_run",
                                "validation": "failed",
                                "valid_for_recommendation": False,
                                "warnings": [],
                                "error": "client_file_read_failed",
                            },
                            "metrics": {},
                            "missing_data": ["authoritative_client_file"],
                        },
                    }
                )
                return result
            if not isinstance(current_client_file, dict):
                result.update(
                    {
                        "ok": False,
                        "error": "client_file_read_invalid",
                        "analysis": {
                            "schema_version": "awm.cashflow_result.v2",
                            "status": {
                                "execution": "not_run",
                                "validation": "failed",
                                "valid_for_recommendation": False,
                                "warnings": [],
                                "error": "client_file_read_invalid",
                            },
                            "metrics": {},
                            "missing_data": ["authoritative_client_file"],
                        },
                    }
                )
                return result
            allocation_analysis_id = str(
                arguments.get("allocation_analysis_id") or ""
            ).strip() or None
            allocation_analysis_ids = [
                str(item).strip()
                for item in arguments.get("allocation_analysis_ids") or []
                if str(item).strip()
            ]
            if allocation_analysis_id is not None:
                allocation_analysis_ids = [allocation_analysis_id]
            linked_allocations: List[Dict[str, Any]] = []
            effective_client_file = current_client_file
            for linked_analysis_id in allocation_analysis_ids:
                allocation_lookup = self._get_asset_allocation_analysis(
                    client_id=client_id,
                    session_id=session_id,
                    analysis_id=linked_analysis_id,
                    result={
                        "tool": "get_asset_allocation_analysis",
                        "ok": True,
                        "read_only": True,
                        "writeback_target": "none",
                        "arguments": {"analysis_id": linked_analysis_id},
                    },
                )
                if allocation_lookup.get("ok") is not True:
                    result.update(
                        {
                            "ok": False,
                            "execution_ok": False,
                            "valid_for_reporting": False,
                            "valid_for_conclusion": False,
                            "valid_for_recommendation": False,
                            "error": "cashflow_allocation_analysis_unavailable",
                            "details": allocation_lookup.get("error"),
                            "allocation_analysis_id": linked_analysis_id,
                            "allocation_analysis_ids": allocation_analysis_ids,
                            "allocation_lookup": allocation_lookup,
                            "analysis": {
                                "schema_version": "awm.cashflow_result.v2",
                                "status": {
                                    "execution": "not_run",
                                    "validation": "failed",
                                    "analysis_grade": "not_run",
                                    "valid_for_recommendation": False,
                                    "warnings": [
                                        "The requested allocation analysis could not be "
                                        "validated for this cash-flow projection."
                                    ],
                                    "error": "cashflow_allocation_analysis_unavailable",
                                    "missing_required_metrics": [],
                                    "normalization_errors": [],
                                },
                                "metrics": {},
                                "missing_data": ["valid_allocation_analysis"],
                            },
                        }
                    )
                    return result
                linked_allocations.append(allocation_lookup)
            if linked_allocations:
                try:
                    effective_client_file = _client_file_with_linked_allocations(
                        current_client_file,
                        linked_allocations,
                    )
                except ValueError as exc:
                    result.update(
                        {
                            "ok": False,
                            "execution_ok": False,
                            "valid_for_reporting": False,
                            "valid_for_conclusion": False,
                            "valid_for_recommendation": False,
                            "error": "cashflow_allocation_mapping_invalid",
                            "details": str(exc),
                            "allocation_analysis_ids": allocation_analysis_ids,
                            "analysis": {
                                "schema_version": "awm.cashflow_result.v2",
                                "status": {
                                    "execution": "not_run",
                                    "validation": "failed",
                                    "analysis_grade": "not_run",
                                    "valid_for_recommendation": False,
                                    "warnings": [str(exc)],
                                    "error": "cashflow_allocation_mapping_invalid",
                                    "missing_required_metrics": [],
                                    "normalization_errors": [],
                                },
                                "metrics": {},
                                "missing_data": [
                                    "confirmed_account_to_money_pool_mapping"
                                ],
                            },
                        }
                    )
                    return result
            analysis = self.financial_planning_query_service.analyze_scenario(
                client_id=client_id,
                session_id=session_id,
                question=str(arguments.get("question") or ""),
                scenario=arguments.get("scenario")
                if isinstance(arguments.get("scenario"), dict)
                else {},
                client_file=effective_client_file,
                mortgage_defaults_authorized=bool(
                    arguments.get("mortgage_defaults_authorized")
                ),
                monte_carlo_paths=arguments.get("monte_carlo_paths"),
                detail_report_groups=arguments.get("detail_report_groups"),
                authorized_public_model_inputs=authorized_public_model_inputs,
            )
            projection_source = (
                analysis.pop("_projection_source", None)
                if isinstance(analysis, dict)
                else None
            )
            detail_error = _cashflow_analysis_detail_excerpt(
                analysis,
                {
                    "calendar_years": arguments.get("calendar_years"),
                    "detail_columns": arguments.get("detail_columns"),
                },
            )
            status = analysis.get("status") if isinstance(analysis.get("status"), dict) else {}
            result["analysis"] = analysis
            result["execution_ok"] = status.get("execution") == "succeeded"
            result["valid_for_reporting"] = bool(
                result["execution_ok"]
                and status.get("validation")
                not in {"failed", "invalid_request", "missing_data"}
            )
            result["ok"] = result["valid_for_reporting"]
            result["valid_for_conclusion"] = bool(
                result["valid_for_reporting"]
                and (
                    status.get("valid_for_conclusion") is True
                    or status.get("valid_for_recommendation") is True
                )
            )
            result["valid_for_recommendation"] = bool(
                result["valid_for_reporting"]
                and status.get("valid_for_recommendation") is True
            )
            if detail_error is not None:
                result.update(detail_error)
                result["ok"] = False
                result["valid_for_reporting"] = False
                result["valid_for_conclusion"] = False
                result["valid_for_recommendation"] = False
            result["missing_data"] = analysis.get("missing_data") or []
            source_allocations = [
                reference
                for allocation in linked_allocations
                if (
                    reference := _cashflow_source_allocation_reference(allocation)
                )
                is not None
            ]
            if source_allocations:
                result["source_allocations"] = source_allocations
                if len(source_allocations) == 1:
                    result["source_allocation"] = source_allocations[0]
            result["note"] = (
                "Cash-flow scenario handled by Financial Planning boundary; "
                "Main Agent must narrate only the validated analysis."
            )
            try:
                from advisor.agents.quant_contracts import attach_quant_evidence
                from advisor.tools.deterministic_tools.cashflow_analysis_snapshot import (
                    build_cashflow_analysis_snapshot,
                    canonical_fingerprint,
                )

                evidenced = attach_quant_evidence("run_cashflow_projection", result)
                if evidenced.get("valid_for_reporting") is True:
                    modeled_client_file = build_cashflow_payload_from_client_file(
                        current_client_file
                    )
                    snapshot = build_cashflow_analysis_snapshot(
                        client_id=client_id,
                        session_id=session_id,
                        analysis=analysis,
                        recommendation_evidence=evidenced["recommendation_evidence"],
                        client_file_fingerprint=canonical_fingerprint(modeled_client_file),
                        source_allocation=(
                            source_allocations[0]
                            if len(source_allocations) == 1
                            else None
                        ),
                        source_allocations=source_allocations,
                        projection_source=projection_source,
                    )
                    analysis_id = str(snapshot["analysis_id"])
                    self._cashflow_analysis_snapshots[analysis_id] = snapshot
                    durable_result = None
                    if self.cashflow_analysis_store is not None:
                        durable_result = self.cashflow_analysis_store(
                            client_id=client_id,
                            payload=snapshot,
                        )
                    result["analysis_id"] = analysis_id
                    result["cashflow_agent_view"] = snapshot["cashflow_agent_view"]
                    result["analysis_persistence"] = {
                        "stored": (
                            durable_result.get("ok") is True
                            if isinstance(durable_result, dict)
                            else self.cashflow_analysis_store is None
                        ),
                        "durable": self.cashflow_analysis_store is not None,
                        "idempotent_replay": bool(
                            isinstance(durable_result, dict)
                            and durable_result.get("idempotent_replay")
                        ),
                    }
            except Exception as exc:  # pragma: no cover - analysis remains usable this turn
                result["analysis_persistence"] = {
                    "stored": False,
                    "durable": self.cashflow_analysis_store is not None,
                    "error": "cashflow_analysis_persistence_failed",
                    "detail": str(exc),
                }
            return result
        if tool_name in {"estimateAllocationRiskReturn", "lookupRiskReturnFrontier"}:
            if tool_name == "estimateAllocationRiskReturn":
                from advisor.tools.deterministic_tools.estimate_allocation_risk_return.execution import (
                    run_estimate_allocation_risk_return,
                )

                capital = run_estimate_allocation_risk_return(arguments)
            else:
                from advisor.tools.deterministic_tools.lookup_risk_return_frontier.execution import (
                    run_lookup_risk_return_frontier,
                )

                capital = run_lookup_risk_return_frontier(arguments)
            result["ok"] = bool(capital.get("success"))
            result["full_result"] = capital.get("full_result")
            if capital.get("error"):
                result["error"] = capital.get("error")
            result["note"] = "Capital markets tool executed through its deterministic tool package."
            return result
        if tool_name == "create_investment_assessment":
            from advisor.tools.deterministic_tools.create_investment_assessment.execution import (
                prepare_investment_assessment,
            )

            if not self.client_file_writer:
                result["ok"] = False
                result["error"] = "client_file_writer_missing"
                return result
            try:
                current_client_file = (
                    self.client_file_reader.read(client_id).payload
                    if self.client_file_reader is not None
                    else self.client_file
                )
            except Exception as exc:
                result.update(
                    {
                        "ok": False,
                        "error": "client_file_read_failed",
                        "details": str(exc),
                    }
                )
                return result
            prepared = prepare_investment_assessment(
                arguments,
                current_client_file,
                client_id=client_id,
                session_id=session_id,
            )
            if prepared.get("ok") is not True:
                result.update(
                    {
                        "ok": False,
                        "error": prepared.get("error") or "assessment_creation_blocked",
                        "details": prepared.get("details"),
                        "missing_data": prepared.get("missing_data") or [],
                    }
                )
                return result
            pending_payload = prepared["payload"]
            idempotent_replay = prepared.get("idempotent_replay") is True
            if idempotent_replay:
                write_result = {"ok": True, "idempotent_replay": True}
            else:
                write_result = self.client_file_writer.write_event(
                    client_id,
                    event_type="create_investment_assessment",
                    payload=pending_payload,
                    source={
                        "session_id": session_id,
                        "source": "advisor_investment_assessment_gate",
                    },
                )
            durable_result: Optional[Dict[str, Any]] = None
            if self.investment_assessment_store is not None:
                durable_result = self.investment_assessment_store(
                    client_id=client_id,
                    payload=pending_payload,
                )
            durable_ok = (
                bool(durable_result.get("ok"))
                if isinstance(durable_result, dict)
                else True
            )
            result["ok"] = bool(write_result.get("ok")) and durable_ok
            result["write_result"] = write_result
            if durable_result is not None:
                result["durable_result"] = durable_result
            result["idempotent_replay"] = idempotent_replay or bool(
                isinstance(durable_result, dict)
                and durable_result.get("idempotent_replay")
            )
            result["pending_assessment_ref"] = {
                "assessment_id": pending_payload.get("assessment_id"),
                "assessment_version": pending_payload.get("assessment_version"),
                "money_pool_id": pending_payload.get("money_pool_id"),
                "assessed_at": pending_payload.get("assessed_at"),
                "valid_until": pending_payload.get("valid_until"),
                "content_fingerprint": pending_payload.get("content_fingerprint"),
            }
            # Keep both keys: UI/runtime card builders historically read `payload`,
            # while agent-visible notes use the top-level `assessment` object.
            result["assessment"] = pending_payload
            result["payload"] = pending_payload
            if not durable_ok:
                result["error"] = "assessment_durable_persistence_failed"
            result["note"] = (
                "The server resolved money-pool facts from the current Client File and "
                "persisted a pending assessment; it is not signed and cannot authorize allocation."
            )
            return result
        if tool_name == "run_asset_allocation":
            from advisor.tools.deterministic_tools.run_asset_allocation.execution import (
                resolve_authoritative_asset_allocation_arguments,
                run_asset_allocation_tool,
            )

            try:
                current_client_file = (
                    self.client_file_reader.read(client_id).payload
                    if self.client_file_reader is not None
                    else self.client_file
                )
            except Exception as exc:
                result.update(
                    {
                        "ok": False,
                        "error": "client_file_read_failed",
                        "details": str(exc),
                        "valid_for_recommendation": False,
                    }
                )
                return result
            authorization = resolve_authoritative_asset_allocation_arguments(
                arguments,
                current_client_file,
                client_id=client_id,
            )
            if authorization.get("success") is not True:
                result.update(
                    {
                        "ok": False,
                        "error": authorization.get("error") or "allocation_not_authorized",
                        "details": authorization.get("details"),
                        "missing_data": authorization.get("missing_data") or [],
                        "status": authorization.get("status"),
                        "valid_for_recommendation": False,
                        "constraint_checks": {
                            "signed_assessment": {
                                "passed": False,
                                "error": authorization.get("error"),
                            }
                        },
                        "warnings": [
                            "Asset allocation was blocked before model execution."
                        ],
                    }
                )
                return result

            allocation = run_asset_allocation_tool(
                authorization["arguments"],
                self._asset_allocation_state,
                config=self.asset_allocation_config or _load_engine_config(),
                http_session=self.asset_allocation_http_session or _engine_http_session(),
                request_timeout_seconds=(
                    self.asset_allocation_request_timeout_seconds
                    or _engine_request_timeout_seconds("AWM_ASSET_ALLOCATION_MODEL_TIMEOUT_SECONDS", 60)
                ),
                log_debug=lambda _message: None,
                authorization=authorization,
            )
            result["execution_ok"] = bool(allocation.get("success"))
            result["ok"] = bool(
                result["execution_ok"]
                and allocation.get("valid_for_recommendation")
            )
            result["full_result"] = allocation.get("full_result")
            result["status"] = allocation.get("status")
            result["valid_for_recommendation"] = bool(
                allocation.get("valid_for_recommendation")
            )
            result["constraint_checks"] = allocation.get("constraint_checks") or {}
            result["warnings"] = allocation.get("warnings") or []
            result["allocation_id"] = allocation.get("allocation_id")
            result["source_assessment"] = authorization.get("assessment_ref")
            result["authoritative_inputs"] = authorization.get("arguments")
            if allocation.get("error"):
                result["error"] = allocation.get("error")
            if allocation.get("details"):
                result["details"] = allocation.get("details")
            result["note"] = (
                "Asset allocation was computed read-only from the durable signed "
                "assessment; no proposal or policy was persisted."
            )
            try:
                from advisor.agents.quant_contracts import attach_quant_evidence
                from advisor.tools.deterministic_tools.asset_allocation_analysis_snapshot import (
                    build_asset_allocation_agent_view,
                    build_asset_allocation_analysis_snapshot,
                )

                full_result = (
                    result.get("full_result")
                    if isinstance(result.get("full_result"), dict)
                    else {}
                )
                agent_view = build_asset_allocation_agent_view(
                    allocation_id=result.get("allocation_id"),
                    full_result=full_result,
                    source_assessment=authorization.get("assessment_ref") or {},
                    authoritative_inputs=authorization.get("arguments") or {},
                    authorization=authorization,
                )
                result["asset_allocation_agent_view"] = agent_view
                evidenced = attach_quant_evidence("run_asset_allocation", result)
                if evidenced.get("valid_for_reporting") is True:
                    snapshot = build_asset_allocation_analysis_snapshot(
                        client_id=client_id,
                        session_id=session_id,
                        full_result=full_result,
                        recommendation_evidence=evidenced["recommendation_evidence"],
                        source_assessment=authorization.get("assessment_ref") or {},
                        authoritative_inputs=authorization.get("arguments") or {},
                        authorization=authorization,
                        allocation_id=result.get("allocation_id"),
                        agent_view=agent_view,
                    )
                    analysis_id = str(snapshot["analysis_id"])
                    self._asset_allocation_analysis_snapshots[analysis_id] = snapshot
                    durable_result = None
                    if self.asset_allocation_analysis_store is not None:
                        durable_result = self.asset_allocation_analysis_store(
                            client_id=client_id,
                            payload=snapshot,
                        )
                    result["analysis_id"] = analysis_id
                    result["asset_allocation_agent_view"] = snapshot[
                        "asset_allocation_agent_view"
                    ]
                    result["analysis_persistence"] = {
                        "stored": (
                            durable_result.get("ok") is True
                            if isinstance(durable_result, dict)
                            else self.asset_allocation_analysis_store is None
                        ),
                        "durable": self.asset_allocation_analysis_store is not None,
                        "idempotent_replay": bool(
                            isinstance(durable_result, dict)
                            and durable_result.get("idempotent_replay")
                        ),
                    }
            except Exception as exc:  # pragma: no cover - allocation remains usable this turn
                result["analysis_persistence"] = {
                    "stored": False,
                    "durable": self.asset_allocation_analysis_store is not None,
                    "error": "asset_allocation_analysis_persistence_failed",
                    "detail": str(exc),
                }
            return result
        if tool_name == "record_assessment_signoff":
            from advisor.tools.deterministic_tools.record_assessment_signoff.execution import (
                prepare_assessment_signoff,
            )

            if not self.client_file_writer:
                result["ok"] = False
                result["error"] = "client_file_writer_missing"
                return result
            try:
                current_client_file = (
                    self.client_file_reader.read(client_id).payload
                    if self.client_file_reader is not None
                    else self.client_file
                )
            except Exception as exc:
                result.update(
                    {
                        "ok": False,
                        "error": "client_file_read_failed",
                        "details": str(exc),
                    }
                )
                return result
            prepared = prepare_assessment_signoff(arguments, current_client_file)
            if prepared.get("ok") is not True:
                result.update(
                    {
                        "ok": False,
                        "error": prepared.get("error") or "assessment_signoff_blocked",
                        "details": prepared.get("details"),
                    }
                )
                return result
            signed_payload = prepared["payload"]
            idempotent_replay = prepared.get("idempotent_replay") is True
            if idempotent_replay:
                write_result = {"ok": True, "idempotent_replay": True}
            else:
                write_result = self.client_file_writer.write_event(
                    client_id,
                    event_type="record_assessment_signoff",
                    payload=signed_payload,
                    source={
                        "session_id": session_id,
                        "source": "advisor_signed_assessment_gate",
                    },
                )
            durable_result: Optional[Dict[str, Any]] = None
            if self.investment_assessment_store is not None:
                durable_result = self.investment_assessment_store(
                    client_id=client_id,
                    payload=signed_payload,
                )
            durable_ok = (
                bool(durable_result.get("ok"))
                if isinstance(durable_result, dict)
                else True
            )
            result["ok"] = bool(write_result.get("ok")) and durable_ok
            result["write_result"] = write_result
            if durable_result is not None:
                result["durable_result"] = durable_result
            result["idempotent_replay"] = idempotent_replay or bool(
                isinstance(durable_result, dict)
                and durable_result.get("idempotent_replay")
            )
            if not durable_ok:
                result["error"] = "assessment_durable_persistence_failed"
            result["signed_assessment_ref"] = {
                "assessment_id": signed_payload.get("assessment_id"),
                "assessment_version": signed_payload.get("assessment_version"),
                "money_pool_id": signed_payload.get("money_pool_id"),
                "signed_off_at": signed_payload.get("signed_off_at"),
                "valid_until": signed_payload.get("valid_until"),
                "content_fingerprint": signed_payload.get("content_fingerprint"),
            }
            result["note"] = (
                "The server matched the supplied consultation basis and assessment to "
                "the exact pending version, then stamped the decision time server-side."
            )
            return result
        if tool_name == "record_deterministic_service_outcome":
            if not self.client_file_writer:
                result["ok"] = False
                result["error"] = "client_file_writer_missing"
                return result
            write_result = self.client_file_writer.write_event(
                client_id,
                event_type="deterministic_service_outcome",
                payload=arguments,
                source={"session_id": session_id, "source": "advisor_deterministic_service"},
            )
            result["write_result"] = write_result
            result["note"] = "Deterministic service workflow recorded in Client File."
            return result
        if tool.read_only:
            result["note"] = "Read-only tool boundary verified; real adapter can be attached later."
            return result
        if not self.client_file_writer:
            result["ok"] = False
            result["error"] = "client_file_writer_missing"
            return result

        fact_argument_errors = _fact_tool_argument_shape_errors(tool_name, arguments)
        if fact_argument_errors:
            result.update(
                {
                    "ok": False,
                    "error": "tool_arguments_invalid",
                    "validation_errors": fact_argument_errors,
                }
            )
            return result

        payload = arguments
        try:
            if tool_name == "draft_fact":
                payload = build_draft_fact_payload(
                    fact_type=str(arguments.get("fact_type") or "captured_fact"),
                    facts=arguments.get("facts") if isinstance(arguments.get("facts"), dict) else {},
                    confidence=str(arguments.get("confidence") or "medium"),
                    metadata=arguments.get("metadata") or {},
                    entities=arguments.get("entities") if isinstance(arguments.get("entities"), list) else None,
                    client_id=client_id,
                )
            elif tool_name == "save_fact":
                payload = build_save_fact_payload(
                    fact_type=str(arguments.get("fact_type") or "captured_fact"),
                    facts=arguments.get("facts") if isinstance(arguments.get("facts"), dict) else {},
                    confidence=str(arguments.get("confidence") or "medium"),
                    metadata=arguments.get("metadata") or {},
                    entities=arguments.get("entities") if isinstance(arguments.get("entities"), list) else None,
                )
            elif tool_name == "commit_facts":
                try:
                    persisted_client_file = (
                        self.client_file_reader.read(client_id).payload
                        if self.client_file_reader is not None
                        else {}
                    )
                except Exception as exc:
                    result.update(
                        {
                            "ok": False,
                            "error": "client_file_read_failed",
                            "details": str(exc),
                        }
                    )
                    return result
                injected_client_file = (
                    arguments.get("client_file")
                    if isinstance(arguments.get("client_file"), dict)
                    else {}
                )
                client_file = _merge_fact_commit_client_files(
                    persisted_client_file
                    if isinstance(persisted_client_file, dict)
                    else {},
                    self.client_file if isinstance(self.client_file, dict) else {},
                    injected_client_file,
                )
                payload = build_commit_facts_payload(
                    client_file=client_file if isinstance(client_file, dict) else {},
                    confirmation_text=str(arguments.get("confirmation_text") or ""),
                    facts=arguments.get("facts") if isinstance(arguments.get("facts"), dict) else None,
                    fact_ids=arguments.get("fact_ids") if isinstance(arguments.get("fact_ids"), list) else None,
                    fact_type=str(arguments.get("fact_type") or "captured_fact"),
                    confidence=str(arguments.get("confidence") or "medium"),
                    metadata=arguments.get("metadata") or {},
                    entities=arguments.get("entities") if isinstance(arguments.get("entities"), list) else None,
                    confirmation_action_id=str(arguments.get("confirmation_action_id") or "") or None,
                )
        except FactWriteValidationError as exc:
            result.update(
                {
                    "ok": False,
                    "error": exc.code,
                    "validation_errors": exc.details,
                    "facts_unrecognized": exc.unrecognized,
                }
            )
            return result

        if tool_name == "record_assessment_signoff":
            source_user_message = str(arguments.get("user_message") or "")
            if arguments.get("signed_off") is True and not _has_explicit_assessment_signoff(source_user_message):
                result["ok"] = False
                result["error"] = "explicit_assessment_signoff_required"
                result["note"] = (
                    "record_assessment_signoff(signed_off=true) requires an explicit client Agree/sign-off "
                    "message or authenticated assessment-card action."
                )
                return result
            arguments = {key: value for key, value in arguments.items() if key != "user_message"}
            payload = build_assessment_signoff_payload(
                arguments,
                client_file=self.client_file if isinstance(self.client_file, dict) else {},
            )

        write_result = self.client_file_writer.write_event(
            client_id,
            event_type=tool.name,
            payload=payload,
            source={
                "session_id": session_id,
                "source": "advisor_tool_executor",
            },
        )
        result["write_result"] = write_result
        if isinstance(write_result.get("onboarding_lifecycle"), dict):
            lifecycle = write_result["onboarding_lifecycle"]
            result["onboarding_lifecycle"] = lifecycle
            if lifecycle.get("ok") is False:
                result["ok"] = False
                result["error"] = lifecycle.get("error")
        payload_metadata = (
            payload.get("metadata")
            if isinstance(payload, dict) and isinstance(payload.get("metadata"), dict)
            else {}
        )
        if payload_metadata.get("facts_unrecognized"):
            result["facts_unrecognized"] = payload_metadata["facts_unrecognized"]
        if tool_name in {"draft_fact", "save_fact", "commit_facts"}:
            result["ok"] = bool(write_result.get("ok"))
        if tool_name == "commit_facts" and result.get("ok"):
            if arguments.get("post_commit_action") == "cashflow_projection":
                result["post_commit_action"] = "cashflow_projection"
            audit_decision, audit_field, audit_proposed_value = (
                _commit_confirmation_audit(client_file, payload)
            )
            audit_payload = {
                "draft_id": ",".join(str(item) for item in payload.get("resolved_draft_ids", [])) or "current_turn",
                "field": audit_field,
                "proposed_value": audit_proposed_value,
                "client_message": str(arguments.get("user_message") or arguments.get("confirmation_text") or ""),
                "decision": audit_decision,
                "rationale": str((arguments.get("metadata") or {}).get("decision_rationale") or "Model selected commit_facts for the pending values."),
                "database_action": {
                    "operation": "commit_facts",
                    "ok": True,
                    "result_id": str((write_result.get("event") or {}).get("id") or ""),
                },
                "session_id": session_id,
                "turn_id": session_id,
                "model_version": str(os.getenv("OPENAI_MODEL") or "unknown"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            result["confirmation_audit"] = self.client_file_writer.write_event(
                client_id,
                event_type="confirmation_decision",
                payload=audit_payload,
                source={"session_id": session_id, "source": "advisor_semantic_decision"},
            )
        if tool_name in {"draft_fact", "save_fact", "commit_facts", "record_assessment_signoff"}:
            result["payload"] = payload
            if tool_name in {"save_fact", "commit_facts"} and isinstance(payload.get("facts"), dict):
                # Keep in-memory executor state coherent for later tools in this process.
                existing = (
                    client_file
                    if tool_name == "commit_facts" and isinstance(client_file, dict)
                    else self.client_file if isinstance(self.client_file, dict) else {}
                )
                facts = dict(existing.get("facts") or {})
                facts.update(payload["facts"])
                remaining_drafts = list(existing.get("draft_facts") or [])
                if tool_name == "commit_facts":
                    resolved_ids = {
                        str(item)
                        for item in payload.get("resolved_draft_ids", [])
                        if item
                    }
                    remaining_drafts = [
                        item
                        for index, item in enumerate(remaining_drafts)
                        if not isinstance(item, dict)
                        or draft_identity(item, index=index)
                        not in resolved_ids
                    ]
                    resolved_items = {
                        (str(item.get("draft_id") or ""), str(item.get("field") or ""))
                        for item in payload.get("resolved_draft_items", [])
                        if isinstance(item, dict)
                    }
                    if resolved_items:
                        next_drafts = []
                        for index, item in enumerate(remaining_drafts):
                            if not isinstance(item, dict):
                                next_drafts.append(item)
                                continue
                            identity = draft_identity(item, index=index)
                            facts_for_draft = item.get("facts") if isinstance(item.get("facts"), dict) else {}
                            retained = {
                                field: value
                                for field, value in facts_for_draft.items()
                                if (identity, str(canonical_fact_name(str(field)) or field)) not in resolved_items
                            }
                            if retained:
                                next_drafts.append({**item, "facts": retained})
                        remaining_drafts = next_drafts
                self.client_file = {
                    **existing,
                    "facts": facts,
                    "structured_facts": {
                        **(existing.get("structured_facts") or {}),
                        **(payload.get("structured_facts") or {}),
                    },
                    "draft_facts": remaining_drafts,
                }
            elif tool_name == "record_assessment_signoff" and isinstance(payload, dict):
                existing = self.client_file if isinstance(self.client_file, dict) else {}
                assessments = list(existing.get("investment_assessments") or [])
                assessments = [item for item in assessments if isinstance(item, dict)]
                assessments.insert(0, payload)
                recent = list(existing.get("recent_writebacks") or [])
                recent = [item for item in recent if isinstance(item, dict)]
                recent.insert(
                    0,
                    {
                        "record": "client_file.plans",
                        "operation": "record_assessment_signoff",
                        "subject": payload.get("pool_label") or payload.get("assessment_id"),
                        "values": payload,
                    },
                )
                self.client_file = {
                    **existing,
                    "investment_assessments": assessments,
                    "recent_writebacks": recent,
                }
        return result

    def _present_fact_confirmation(
        self,
        *,
        client_id: str,
        session_id: str,
        source_turn_id: str,
        arguments: Dict[str, Any],
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        repository = self.fact_confirmation_repository
        if repository is None:
            return {**result, "ok": False, "error": "fact_confirmation_repository_missing"}
        try:
            persisted = self.client_file_reader.read(client_id).payload if self.client_file_reader else {}
            client_file = _merge_fact_commit_client_files(
                persisted if isinstance(persisted, dict) else {},
                self.client_file if isinstance(self.client_file, dict) else {},
                {},
            )
            drafts = {
                draft_identity(item, index=index): item
                for index, item in enumerate(client_file.get("draft_facts") or [])
                if isinstance(item, dict)
            }
            items = []
            for requested in arguments.get("items") or []:
                draft_id = str(requested.get("draft_id") or "")
                requested_field = str(requested.get("field") or "")
                draft = drafts.get(draft_id)
                normalized = normalize_fact_keys(draft.get("facts") or {}) if draft else {}
                entities = {
                    str(entity.get("entity_id") or ""): entity
                    for entity in (draft.get("entities") or [])
                    if isinstance(entity, dict) and entity.get("entity_id")
                } if draft else {}
                field = str(canonical_fact_name(requested_field) or requested_field)
                if not draft or (field not in normalized and field not in entities):
                    return {**result, "ok": False, "error": "confirmation_item_not_pending"}
                items.append({
                    **requested,
                    "draft_id": draft_id,
                    "field": field,
                    "value": normalized[field] if field in normalized else entities[field],
                })
            confirmation_set = repository.create(
                client_id=client_id,
                companion_session_id=session_id,
                source_turn_id=source_turn_id,
                client_file_version=int(client_file.get("client_file_version") or 0),
                items=items,
            )
            return {
                **result,
                "confirmation_set": confirmation_set,
                "presented_item_ids": [item["confirmation_item_id"] for item in confirmation_set["items"]],
            }
        except (ValueError, LookupError) as exc:
            return {**result, "ok": False, "error": str(exc)}

    def _resolve_fact_confirmation(
        self,
        *,
        client_id: str,
        session_id: str,
        arguments: Dict[str, Any],
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        repository = self.fact_confirmation_repository
        if repository is None or self.client_file_writer is None:
            return {**result, "ok": False, "error": "fact_confirmation_dependencies_missing"}
        confirmation_set = repository.get(
            set_id=str(arguments.get("confirmation_set_id") or ""),
            client_id=client_id,
        )
        if not confirmation_set or confirmation_set.get("companion_session_id") != session_id:
            return {**result, "ok": False, "error": "confirmation_set_not_found"}
        if confirmation_set.get("status") in {"resolved", "partially_resolved"} and confirmation_set.get("resolution"):
            return {**result, "confirmation_set": confirmation_set, "idempotent_replay": True}
        if confirmation_set.get("status") != "pending":
            return {**result, "ok": False, "error": "confirmation_set_terminal"}
        if confirmation_set.get("prompt_message_id") != str(arguments.get("prompt_message_id") or ""):
            return {**result, "ok": False, "error": "confirmation_prompt_mismatch"}
        items_by_id = {item["confirmation_item_id"]: item for item in confirmation_set.get("items") or []}
        decisions = arguments.get("decisions") or []
        if any(str(item.get("confirmation_item_id") or "") not in items_by_id for item in decisions):
            return {**result, "ok": False, "error": "confirmation_item_out_of_set"}
        decision_ids = {str(item.get("confirmation_item_id") or "") for item in decisions}
        atomic_groups: Dict[str, set[str]] = {}
        for item in items_by_id.values():
            if item.get("atomic_group_id") and item.get("resolution_mode") == "all_or_none":
                atomic_groups.setdefault(str(item["atomic_group_id"]), set()).add(item["confirmation_item_id"])
        if any(decision_ids.intersection(group) and not group.issubset(decision_ids) for group in atomic_groups.values()):
            return {**result, "ok": False, "error": "confirmation_atomic_group_incomplete"}
        corrected_facts: Dict[str, Any] = {}
        entity_item_ids = {
            item["field"]
            for item in items_by_id.values()
            if isinstance(item.get("value"), dict)
            and item["value"].get("entity_id") == item["field"]
        }
        for decision in decisions:
            item = items_by_id[str(decision["confirmation_item_id"])]
            outcome = str(decision.get("decision") or "")
            if outcome == "corrected":
                if item["field"] in entity_item_ids:
                    return {**result, "ok": False, "error": "confirmation_entity_correction_requires_redraft"}
                corrected_facts[item["field"]] = decision.get("corrected_value")
        try:
            persisted = self.client_file_reader.read(client_id).payload if self.client_file_reader else self.client_file
            current_drafts = {}
            for index, draft in enumerate((persisted or {}).get("draft_facts") or []):
                if not isinstance(draft, dict):
                    continue
                selectors = normalize_fact_keys(draft.get("facts") or {})
                selectors.update({
                    str(entity.get("entity_id")): entity
                    for entity in (draft.get("entities") or [])
                    if isinstance(entity, dict) and entity.get("entity_id")
                })
                current_drafts[draft_identity(draft, index=index)] = selectors
            committed_facts = normalize_fact_keys((persisted or {}).get("facts") or {})
            commit_items = []
            for item_id in decision_ids:
                item = items_by_id[item_id]
                draft_matches = (
                    current_drafts.get(item["draft_id"], {}).get(item["field"])
                    == item.get("value")
                )
                already_committed = (
                    item["field"] in committed_facts
                    and committed_facts[item["field"]] == item.get("value")
                )
                decision = next(
                    decision
                    for decision in decisions
                    if str(decision.get("confirmation_item_id") or "") == item_id
                )
                if not draft_matches and not (
                    str(decision.get("decision") or "") == "confirmed"
                    and already_committed
                ):
                    return {**result, "ok": False, "error": "confirmation_set_stale"}
                if draft_matches and str(decision.get("decision") or "") == "confirmed":
                    commit_items.append(
                        {"draft_id": item["draft_id"], "field": item["field"]}
                    )
            payload = build_commit_facts_payload(
                client_file=persisted if isinstance(persisted, dict) else {},
                confirmation_text=str(arguments.get("client_message") or ""),
                facts=corrected_facts or None,
                draft_items=commit_items,
            ) if (commit_items or corrected_facts) else {
                "fact_type": "captured_fact", "facts": {}, "structured_facts": {},
                "status": "no_fact_mutation", "resolved_draft_ids": [], "resolved_draft_items": [],
            }
            write_result = None
            if payload.get("facts") or payload.get("entities"):
                write_result = self.client_file_writer.write_event(
                    client_id,
                    event_type="commit_facts",
                    payload=payload,
                    source={"session_id": session_id, "confirmation_set_id": confirmation_set["confirmation_set_id"]},
                )
                if not write_result.get("ok"):
                    return {**result, "ok": False, "error": "confirmation_commit_failed"}
            decision_write = self.client_file_writer.write_event(
                client_id,
                event_type="confirmation_decision",
                payload={
                    "confirmation_set_id": confirmation_set["confirmation_set_id"],
                    "decisions": [
                        {
                            "draft_id": items_by_id[str(decision["confirmation_item_id"])]["draft_id"],
                            "field": items_by_id[str(decision["confirmation_item_id"])]["field"],
                            "decision": str(decision.get("decision") or ""),
                        }
                        for decision in decisions
                    ],
                    "database_action": {
                        "ok": write_result is None or write_result.get("ok") is True,
                    },
                },
                source={
                    "session_id": session_id,
                    "confirmation_set_id": confirmation_set["confirmation_set_id"],
                },
            )
            if not decision_write.get("ok"):
                return {**result, "ok": False, "error": "confirmation_decision_audit_failed"}
            resolved = repository.resolve(
                set_id=confirmation_set["confirmation_set_id"],
                client_id=client_id,
                companion_session_id=session_id,
                prompt_message_id=str(arguments.get("prompt_message_id") or ""),
                decisions=decisions,
                resolution_result={"write_result": write_result, "payload": payload},
            )
            return {
                **result,
                "payload": payload,
                "write_result": write_result,
                "decision_write": decision_write,
                "confirmation_set": resolved,
            }
        except (ValueError, LookupError, FactWriteValidationError) as exc:
            return {**result, "ok": False, "error": getattr(exc, "code", str(exc))}

    def _get_cashflow_analysis(
        self,
        *,
        client_id: str,
        session_id: str,
        analysis_id: Any,
        result: Dict[str, Any],
        detail_query: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Retrieve client-owned evidence and reject it if modeled inputs changed."""

        requested_id = str(analysis_id or "").strip() or None
        artifact = None
        if self.cashflow_analysis_reader is not None:
            artifact = self.cashflow_analysis_reader(
                client_id=client_id,
                analysis_id=requested_id,
                session_id=session_id,
            )
        snapshot = (
            artifact.get("payload")
            if isinstance(artifact, dict) and isinstance(artifact.get("payload"), dict)
            else artifact
        )
        if not isinstance(snapshot, dict):
            candidates = [
                item
                for item in self._cashflow_analysis_snapshots.values()
                if item.get("client_id") == client_id
                and (requested_id is None or item.get("analysis_id") == requested_id)
                and (requested_id is not None or item.get("session_id") == session_id)
            ]
            snapshot = candidates[-1] if candidates else None
        if not isinstance(snapshot, dict):
            result.update(
                {
                    "ok": False,
                    "error": "cashflow_analysis_not_found",
                    "analysis_id": requested_id,
                    "requires_rerun": True,
                    "note": "No completed cash-flow analysis is available for this client and conversation.",
                }
            )
            return result
        if snapshot.get("client_id") != client_id:
            result.update({"ok": False, "error": "cashflow_analysis_not_found"})
            return result
        try:
            current_client_file = (
                self.client_file_reader.read(client_id).payload
                if self.client_file_reader is not None
                else self.client_file
            )
            from advisor.tools.deterministic_tools.cashflow_analysis_snapshot import (
                canonical_fingerprint,
            )

            current_fingerprint = canonical_fingerprint(
                build_cashflow_payload_from_client_file(current_client_file)
            )
        except Exception as exc:
            result.update(
                {
                    "ok": False,
                    "error": "cashflow_analysis_freshness_check_failed",
                    "detail": str(exc),
                    "requires_rerun": True,
                }
            )
            return result
        if current_fingerprint != snapshot.get("client_file_fingerprint"):
            result.update(
                {
                    "ok": False,
                    "error": "cashflow_analysis_stale",
                    "analysis_id": snapshot.get("analysis_id"),
                    "requires_rerun": True,
                    "note": "Relevant modeled Client File inputs changed after this analysis.",
                }
            )
            return result
        source_allocation = snapshot.get("source_allocation")
        source_allocations = [
            item
            for item in snapshot.get("source_allocations") or []
            if isinstance(item, dict)
        ]
        if not source_allocations and isinstance(source_allocation, dict):
            source_allocations = [source_allocation]
        for linked_source_allocation in source_allocations:
            linked_analysis_id = str(
                linked_source_allocation.get("analysis_id") or ""
            ).strip()
            allocation_lookup = self._get_asset_allocation_analysis(
                client_id=client_id,
                session_id=session_id,
                analysis_id=linked_analysis_id,
                result={
                    "tool": "get_asset_allocation_analysis",
                    "ok": True,
                    "read_only": True,
                    "writeback_target": "none",
                    "arguments": {"analysis_id": linked_analysis_id},
                },
            )
            expected_fingerprint = linked_source_allocation.get("input_fingerprint")
            if (
                not linked_analysis_id
                or allocation_lookup.get("ok") is not True
                or (
                    expected_fingerprint
                    and allocation_lookup.get("input_fingerprint")
                    != expected_fingerprint
                )
            ):
                result.update(
                    {
                        "ok": False,
                        "error": "cashflow_linked_allocation_stale",
                        "analysis_id": snapshot.get("analysis_id"),
                        "allocation_analysis_id": linked_analysis_id or None,
                        "requires_rerun": True,
                        "note": (
                            "The allocation used by this cash-flow analysis is no "
                            "longer available under the same signed inputs."
                        ),
                    }
                )
                return result
        analysis = copy.deepcopy(snapshot.get("analysis"))
        if not isinstance(analysis, dict):
            result.update(
                {
                    "ok": False,
                    "error": "cashflow_analysis_snapshot_invalid",
                    "requires_rerun": True,
                }
            )
            return result
        detail_error = _cashflow_analysis_detail_excerpt(
            analysis,
            detail_query or {},
        )
        if detail_error is not None:
            result.update(
                {
                    "ok": False,
                    "analysis_id": snapshot.get("analysis_id"),
                    **detail_error,
                }
            )
            return result
        available_detail_columns = sorted(
            str(item)
            for item in (analysis.get("detail_series") or {})
            if str(item).strip()
        )
        recommendation_evidence = (
            snapshot.get("recommendation_evidence")
            if isinstance(snapshot.get("recommendation_evidence"), dict)
            else {}
        )
        # The snapshot remains immutable, while this deterministic semantic
        # view is regenerated on read so wording and limitation fixes apply to
        # analyses saved by older app versions.
        from advisor.tools.deterministic_tools.cashflow_analysis_snapshot import (
            build_cashflow_agent_view,
        )

        cashflow_agent_view = build_cashflow_agent_view(
            analysis_id=str(snapshot.get("analysis_id") or ""),
            analysis=analysis,
            recommendation_evidence=recommendation_evidence,
            created_at=snapshot.get("created_at"),
            source_allocation=(
                source_allocation if isinstance(source_allocation, dict) else None
            ),
            source_allocations=source_allocations,
        )
        # The full annual series stays inside the immutable snapshot. Only a
        # bounded, typed excerpt is returned to the agent/tool trace.
        analysis.pop("detail_series", None)
        result.update(
            {
                "ok": True,
                "analysis_id": snapshot.get("analysis_id"),
                "input_fingerprint": snapshot.get("input_fingerprint"),
                "analysis": analysis,
                "cashflow_agent_view": cashflow_agent_view,
                "source_allocation": source_allocation or None,
                "source_allocations": source_allocations,
                "available_detail_columns": available_detail_columns,
                "recommendation_evidence": recommendation_evidence,
                "retrieved_without_rerun": True,
                "requires_rerun": False,
                "note": (
                    "A completed cash-flow analysis was retrieved without model execution; "
                    "use only its typed evidence and precomputed interpretations."
                ),
            }
        )
        return result

    def _load_session_public_fact(
        self,
        *,
        client_id: str,
        session_id: str,
        session_fact_id: str,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Load one unexpired fact from the authenticated session only."""

        session_fact_id = str(session_fact_id or "").strip()
        if not session_fact_id or not str(client_id or "").strip() or not str(
            session_id or ""
        ).strip():
            return None, "session_public_fact_unavailable"
        session_scope = _session_public_fact_scope(
            client_id=client_id,
            session_id=session_id,
        )
        key = (session_scope, session_fact_id)
        now = datetime.now(timezone.utc)
        with self._session_public_facts_lock:
            fact_data = self._session_public_facts.get(key)
            if fact_data is None:
                self._prune_session_public_facts(now=now)
                return None, "session_public_fact_unavailable"
            expires_at = _parse_aware_timestamp(fact_data.get("expires_at"))
            if expires_at is None or expires_at <= now:
                self._session_public_facts.pop(key, None)
                return None, "session_public_fact_expired"
            if fact_data.get("session_scope_sha256") != session_scope:
                self._session_public_facts.pop(key, None)
                return None, "session_public_fact_unavailable"
            fact_data = copy.deepcopy(fact_data)
            self._session_public_facts.move_to_end(key)
        return fact_data, None

    def _store_session_public_fact(
        self,
        *,
        session_scope: str,
        fact_data: Dict[str, Any],
    ) -> bool:
        """Keep a bounded, process-local authorization for one session."""

        expires_at = _parse_aware_timestamp(fact_data.get("expires_at"))
        now = datetime.now(timezone.utc)
        if expires_at is None or expires_at <= now:
            return False
        session_fact_id = str(fact_data.get("fact_id") or "").strip()
        if not session_fact_id:
            return False
        key = (session_scope, session_fact_id)
        with self._session_public_facts_lock:
            self._prune_session_public_facts(now=now)
            self._session_public_facts[key] = copy.deepcopy(fact_data)
            self._session_public_facts.move_to_end(key)
            while len(self._session_public_facts) > 256:
                self._session_public_facts.popitem(last=False)
        return True

    def _prune_session_public_facts(self, *, now: datetime) -> None:
        expired = [
            key
            for key, fact in self._session_public_facts.items()
            if (
                (expires_at := _parse_aware_timestamp(fact.get("expires_at")))
                is None
                or expires_at <= now
            )
        ]
        for key in expired:
            self._session_public_facts.pop(key, None)

    def _authorized_public_model_inputs(
        self,
        *,
        client_id: str,
        session_id: str,
        references: Any,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Resolve agent-selected opaque references into one model input."""

        if references is None:
            return [], None
        if not isinstance(references, list) or len(references) > 1:
            return [], "public_fact_refs_invalid"
        if not references:
            return [], None
        reference = references[0]
        if not isinstance(reference, dict) or set(reference) != {
            "variable_key",
            "session_fact_id",
        }:
            return [], "public_fact_ref_fields_invalid"
        variable_key = str(reference.get("variable_key") or "").strip()
        if variable_key != "social_security_taxable_maximum":
            return [], "public_fact_projection_binding_unsupported"
        fact_data, fact_error = self._load_session_public_fact(
            client_id=client_id,
            session_id=session_id,
            session_fact_id=str(reference.get("session_fact_id") or ""),
        )
        if fact_error is not None or fact_data is None:
            return [], fact_error or "session_public_fact_unavailable"

        from advisor.assumptions.research import (
            SessionPublicFact,
            session_public_fact_integrity_valid,
        )

        try:
            fact = SessionPublicFact.model_validate(fact_data)
        except Exception:
            return [], "session_public_fact_invalid"
        if (
            not session_public_fact_integrity_valid(fact)
            or fact.variable_key != variable_key
            or fact.unit != "USD_annual"
            or fact.jurisdiction != "US"
            or fact.reporting_allowed is not True
            or fact.session_calculation_allowed is not True
            or fact.durable is not False
            or fact.origin not in {"live_research", "durable_registry"}
        ):
            return [], "session_public_fact_not_model_input_eligible"
        if (
            isinstance(fact.value, bool)
            or not isinstance(fact.value, (int, float))
            or not math.isfinite(float(fact.value))
            or float(fact.value) <= 0
        ):
            return [], "session_public_fact_value_invalid"
        sources = [source.model_dump(mode="json") for source in fact.sources]
        if not sources:
            return [], "session_public_fact_sources_missing"
        return [
            {
                "schema_version": "awm.authorized_public_model_input.v1",
                "variable_key": fact.variable_key,
                "value": fact.value,
                "unit": fact.unit,
                "jurisdiction": fact.jurisdiction,
                "effective_year": fact.effective_year,
                "session_fact_id": fact.fact_id,
                "content_sha256": fact.content_sha256,
                "expires_at": fact.expires_at.isoformat(),
                "sources": sources,
            }
        ], None

    def _resolve_financial_math_source(
        self,
        *,
        client_id: str,
        session_id: str,
        source: Dict[str, Any],
    ) -> Dict[str, Any]:
        if isinstance(source, dict) and source.get("kind") == "session_public_fact":
            return self._resolve_session_public_fact_source(
                client_id=client_id,
                session_id=session_id,
                source=source,
            )
        return self._resolve_financial_math_cashflow_source(
            client_id=client_id,
            session_id=session_id,
            source=source,
        )

    def _resolve_session_public_fact_source(
        self,
        *,
        client_id: str,
        session_id: str,
        source: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Resolve a scalar only from this authenticated session's fact store."""

        if set(source) != {"id", "kind", "session_fact_id"}:
            return {
                "success": False,
                "error": "session_public_fact_source_fields_invalid",
            }
        session_fact_id = str(source.get("session_fact_id") or "").strip()
        fact_data, fact_error = self._load_session_public_fact(
            client_id=client_id,
            session_id=session_id,
            session_fact_id=session_fact_id,
        )
        if fact_error is not None or fact_data is None:
            return {
                "success": False,
                "error": fact_error or "session_public_fact_unavailable",
            }
        session_scope = _session_public_fact_scope(
            client_id=client_id,
            session_id=session_id,
        )

        if (
            fact_data.get("schema_version") != "awm.session_public_fact.v1"
            or fact_data.get("session_scope_sha256") != session_scope
            or fact_data.get("session_calculation_allowed") is not True
            or fact_data.get("reporting_allowed") is not True
            or fact_data.get("durable") is not False
            or fact_data.get("recommendation_allowed") is not False
            or fact_data.get("origin") not in {
                "live_research",
                "durable_registry",
            }
        ):
            return {
                "success": False,
                "error": "session_public_fact_not_calculation_eligible",
            }
        raw_value = fact_data.get("value")
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            return {
                "success": False,
                "error": "session_public_fact_value_invalid",
            }
        try:
            value = Decimal(str(raw_value))
        except InvalidOperation:
            value = Decimal("NaN")
        if not value.is_finite():
            return {
                "success": False,
                "error": "session_public_fact_value_invalid",
            }
        unit = {
            "USD_annual": "money_per_year:USD",
            "USD_per_month": "money_per_month:USD",
            "percent": "percentage",
        }.get(str(fact_data.get("unit") or ""))
        if unit is None:
            return {
                "success": False,
                "error": "session_public_fact_unit_unsupported",
            }
        evidence_refs = sorted(
            {
                str(item.get("url") or "").strip()
                for item in fact_data.get("sources") or []
                if isinstance(item, dict) and str(item.get("url") or "").strip()
            }
        )
        if not evidence_refs:
            return {
                "success": False,
                "error": "session_public_fact_sources_missing",
            }
        variable_key = str(fact_data.get("variable_key") or "").strip()
        effective_year = fact_data.get("effective_year")
        return {
            "success": True,
            "value": format(value, "f"),
            "unit": unit,
            "session_fact_id": session_fact_id,
            "variable_key": variable_key,
            "effective_year": effective_year,
            "content_sha256": fact_data.get("content_sha256"),
            "origin": fact_data.get("origin"),
            "evidence_refs": evidence_refs,
            "source_path": "$.full_result.fact.value",
            "validation": {
                "ownership": "matched_authenticated_session",
                "freshness": "unexpired_session_authorization",
                "reporting_permission": "validated",
                "session_authorization": "server_validated",
            },
        }

    def _resolve_financial_math_cashflow_source(
        self,
        *,
        client_id: str,
        session_id: str,
        source: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Resolve one typed plan operand from a fresh client-owned analysis."""

        if not isinstance(source, dict):
            return {"success": False, "error": "financial_math_source_invalid"}
        kind = str(source.get("kind") or "").strip()
        analysis_id = str(source.get("analysis_id") or "").strip()
        if not analysis_id:
            return {
                "success": False,
                "error": "cashflow_analysis_id_required_for_calculation",
            }

        detail_query: Optional[Dict[str, Any]] = None
        if kind == "cashflow_series_value":
            year = source.get("calendar_year")
            column = str(source.get("column") or "").strip()
            percentile = str(source.get("percentile") or "").strip().lower()
            if (
                isinstance(year, bool)
                or not isinstance(year, int)
                or not column
                or percentile not in {"p10", "p50", "p90"}
            ):
                return {
                    "success": False,
                    "error": "cashflow_series_source_selector_invalid",
                }
            detail_query = {
                "calendar_years": [year],
                "detail_columns": [column],
            }
        elif kind != "cashflow_claim":
            return {
                "success": False,
                "error": "financial_math_source_kind_invalid",
            }

        lookup = self._get_cashflow_analysis(
            client_id=client_id,
            session_id=session_id,
            analysis_id=analysis_id,
            detail_query=detail_query,
            result={
                "tool": "get_cashflow_analysis",
                "ok": True,
                "read_only": True,
                "writeback_target": "none",
                "arguments": {"analysis_id": analysis_id, **(detail_query or {})},
            },
        )
        if lookup.get("ok") is not True:
            return {
                "success": False,
                "error": str(lookup.get("error") or "cashflow_calculation_source_unavailable"),
                "requires_rerun": lookup.get("requires_rerun") is True,
            }
        if str(lookup.get("analysis_id") or "") != analysis_id:
            return {
                "success": False,
                "error": "cashflow_analysis_identity_mismatch",
            }

        input_fingerprint = str(lookup.get("input_fingerprint") or "").strip()
        recommendation_evidence = (
            lookup.get("recommendation_evidence")
            if isinstance(lookup.get("recommendation_evidence"), dict)
            else {}
        )
        if recommendation_evidence.get("valid_for_reporting") is not True:
            return {
                "success": False,
                "error": "cashflow_analysis_not_reportable",
            }
        if kind == "cashflow_claim":
            from advisor.tools.deterministic_tools.calculate_cashflow_metrics.execution import (
                _resolve_metric,
            )

            try:
                resolved = _resolve_metric(
                    recommendation_evidence,
                    {
                        "metric_key": source.get("metric_key"),
                        "value_path": source.get("value_path"),
                    },
                )
                unit = _financial_math_unit_from_cashflow_unit(resolved.get("unit"))
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
            return {
                "success": True,
                "value": str(resolved["value"]),
                "unit": unit,
                "analysis_id": analysis_id,
                "input_fingerprint": input_fingerprint,
                "metric_key": resolved.get("metric_key"),
                "value_path": resolved.get("value_path"),
                "claim_id": resolved.get("claim_id"),
                "evidence_ref": resolved.get("evidence_ref"),
                "source_path": resolved.get("source_path"),
                "validation": {
                    "ownership": "matched_authenticated_client",
                    "freshness": "matched_current_modeled_inputs",
                    "reporting_permission": "validated",
                },
            }

        year = int(source["calendar_year"])
        column = str(source["column"]).strip()
        percentile = str(source["percentile"]).strip().lower()
        analysis = lookup.get("analysis") if isinstance(lookup.get("analysis"), dict) else {}
        metrics = analysis.get("metrics") if isinstance(analysis.get("metrics"), dict) else {}
        trajectory = (
            metrics.get("queried_percentile_trajectory")
            if isinstance(metrics.get("queried_percentile_trajectory"), dict)
            else {}
        )
        values = trajectory.get("value") if isinstance(trajectory.get("value"), dict) else {}
        try:
            raw_value = values[str(year)][column][percentile]
        except (KeyError, TypeError):
            return {
                "success": False,
                "error": "cashflow_series_source_value_unavailable",
            }
        if (
            isinstance(raw_value, bool)
            or not isinstance(raw_value, (int, float))
            or not math.isfinite(float(raw_value))
        ):
            return {
                "success": False,
                "error": "cashflow_series_source_value_invalid",
            }
        source_path = (
            "$.analysis.detail_series"
            f"[{json.dumps(column)}][{json.dumps(str(year))}]"
            f"[{json.dumps(percentile)}]"
        )
        try:
            series_unit = _financial_math_unit_from_cashflow_series(column)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        return {
            "success": True,
            "value": str(raw_value),
            "unit": series_unit,
            "analysis_id": analysis_id,
            "input_fingerprint": input_fingerprint,
            "calendar_year": year,
            "column": column,
            "percentile": percentile,
            "evidence_ref": f"run_cashflow_projection/{analysis_id}/{column}/{year}/{percentile}",
            "source_path": source_path,
            "validation": {
                "ownership": "matched_authenticated_client",
                "freshness": "matched_current_modeled_inputs",
                "reporting_permission": "validated",
                "detail_series": "retrieved_from_immutable_snapshot",
            },
        }


    def _get_asset_allocation_analysis(
        self,
        *,
        client_id: str,
        session_id: str,
        analysis_id: Any,
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Retrieve client-owned allocation evidence if its signed mandate is unchanged."""

        requested_id = str(analysis_id or "").strip() or None
        artifact = None
        if self.asset_allocation_analysis_reader is not None:
            artifact = self.asset_allocation_analysis_reader(
                client_id=client_id,
                analysis_id=requested_id,
                session_id=session_id,
            )
        snapshot = (
            artifact.get("payload")
            if isinstance(artifact, dict) and isinstance(artifact.get("payload"), dict)
            else artifact
        )
        if not isinstance(snapshot, dict):
            candidates = [
                item
                for item in self._asset_allocation_analysis_snapshots.values()
                if item.get("client_id") == client_id
                and (
                    requested_id is None
                    or _allocation_snapshot_matches_reference(item, requested_id)
                )
                and (requested_id is not None or item.get("session_id") == session_id)
            ]
            snapshot = candidates[-1] if candidates else None
        if not isinstance(snapshot, dict) or snapshot.get("client_id") != client_id:
            result.update(
                {
                    "ok": False,
                    "error": "asset_allocation_analysis_not_found",
                    "analysis_id": requested_id,
                    "requires_rerun": True,
                    "note": (
                        "No completed asset-allocation analysis is available for this "
                        "client and conversation."
                    ),
                }
            )
            return result
        assessment_ref = snapshot.get("assessment_ref")
        if not isinstance(assessment_ref, dict):
            result.update(
                {
                    "ok": False,
                    "error": "asset_allocation_analysis_snapshot_invalid",
                    "requires_rerun": True,
                }
            )
            return result
        try:
            current_client_file = (
                self.client_file_reader.read(client_id).payload
                if self.client_file_reader is not None
                else self.client_file
            )
            from advisor.tools.deterministic_tools.run_asset_allocation.execution import (
                resolve_authoritative_asset_allocation_arguments,
            )

            authorization = resolve_authoritative_asset_allocation_arguments(
                {"assessment_ref": assessment_ref},
                current_client_file,
                client_id=client_id,
            )
        except Exception as exc:
            result.update(
                {
                    "ok": False,
                    "error": "asset_allocation_analysis_freshness_check_failed",
                    "detail": str(exc),
                    "requires_rerun": True,
                }
            )
            return result
        if authorization.get("success") is not True:
            result.update(
                {
                    "ok": False,
                    "error": "asset_allocation_analysis_stale",
                    "stale_reason": authorization.get("error"),
                    "analysis_id": snapshot.get("analysis_id"),
                    "requires_rerun": True,
                    "note": "The signed assessment is no longer current and must be revalidated.",
                }
            )
            return result
        if (
            authorization.get("assessment_fingerprint")
            != snapshot.get("assessment_fingerprint")
        ):
            result.update(
                {
                    "ok": False,
                    "error": "asset_allocation_analysis_stale",
                    "stale_reason": "signed_assessment_content_changed",
                    "analysis_id": snapshot.get("analysis_id"),
                    "requires_rerun": True,
                    "note": "The signed assessment content changed after this allocation.",
                }
            )
            return result
        full_result = snapshot.get("full_result")
        if not isinstance(full_result, dict):
            result.update(
                {
                    "ok": False,
                    "error": "asset_allocation_analysis_snapshot_invalid",
                    "requires_rerun": True,
                }
            )
            return result
        evidence = snapshot.get("recommendation_evidence")
        if not isinstance(evidence, dict) or evidence.get("valid_for_reporting") is not True:
            result.update(
                {
                    "ok": False,
                    "error": "asset_allocation_analysis_snapshot_invalid",
                    "requires_rerun": True,
                }
            )
            return result
        result.update(
            {
                "ok": True,
                "execution_ok": True,
                "valid_for_reporting": evidence.get("valid_for_reporting") is True,
                "valid_for_conclusion": evidence.get("valid_for_conclusion") is True,
                "valid_for_recommendation": (
                    evidence.get("valid_for_recommendation") is True
                ),
                "analysis_id": snapshot.get("analysis_id"),
                "allocation_id": snapshot.get("allocation_id"),
                "input_fingerprint": snapshot.get("input_fingerprint"),
                "source_assessment": assessment_ref,
                "full_result": full_result,
                "constraint_checks": full_result.get("constraint_checks") or {},
                "asset_allocation_agent_view": snapshot.get(
                    "asset_allocation_agent_view"
                )
                or {},
                "recommendation_evidence": evidence,
                "retrieved_without_rerun": True,
                "requires_rerun": False,
                "note": (
                    "A completed allocation analysis was retrieved without optimizer "
                    "execution; use only its typed evidence and agent view."
                ),
            }
        )
        return result


def _has_explicit_assessment_signoff(user_message: str) -> bool:
    text = " ".join(str(user_message or "").lower().replace("\u2019", "'").replace("\u2018", "'").split())
    if not text:
        return False
    if "authenticated app button tap" in text and '"decision": "agree"' in text:
        return True
    if re.search(r"\b(i\s+)?(agree|approve|approved|accepted|confirm|confirmed)\b", text):
        return True
    if re.search(r"\b(i\s+)?sign(?:ed)?\s*off\b", text):
        return True
    if re.fullmatch(r"\s*(yes|yep|yeah|ok|okay|agree)\.?\s*", text):
        return True
    return False


class V2PersistentToolExecutor:
    """Production persistence adapter for v2 tools."""

    def __init__(
        self,
        *,
        fallback: Optional[ToolExecutor] = None,
        persistent_handlers: Optional[V2PersistentToolHandlers] = None,
    ) -> None:
        self.fallback = fallback or RegistryToolExecutor()
        self.persistent_handlers = persistent_handlers or V2PersistentToolHandlers()

    def execute(
        self,
        *,
        client_id: str,
        session_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        if tool_name in {"upsert_money_pool", "record_policy_review_outcome"}:
            try:
                result = self.persistent_handlers.execute(
                    client_id=client_id,
                    session_id=session_id,
                    tool_name=tool_name,
                    arguments=arguments,
                )
                result["tool"] = tool_name
                result["adapter"] = "v2_persistent_tool_executor"
                return result
            except Exception as exc:  # pragma: no cover - production adapter safety
                return {
                    "tool": tool_name,
                    "ok": False,
                    "adapter": "v2_persistent_tool_executor",
                    "error": "dispatch_failed",
                    "detail": str(exc),
                }
        return self.fallback.execute(
            client_id=client_id,
            session_id=session_id,
            tool_name=tool_name,
            arguments=arguments,
        )


class V2SubAgentDispatcher:
    """Dispatcher for v2 silent specialists without legacy agent code."""

    def __init__(
        self,
        *,
        financial_planning_agent: Optional[FinancialPlanningAgentV2] = None,
        investment_solution_agent: Optional[InvestmentSolutionAgentV2] = None,
        assessment_revalidation_agent: Optional[AssessmentRevalidationAgentV2] = None,
    ) -> None:
        self.financial_planning_agent = financial_planning_agent or FinancialPlanningAgentV2()
        self.investment_solution_agent = investment_solution_agent or InvestmentSolutionAgentV2()
        self.assessment_revalidation_agent = assessment_revalidation_agent or AssessmentRevalidationAgentV2()

    def dispatch(
        self,
        *,
        client_id: str,
        objective: Dict[str, Any],
        client_file: Dict[str, Any],
        subagent: str,
    ) -> SubAgentArtifact:
        if subagent == "financial_planning":
            # Phase 5a: an objective that carries a concrete investment request runs
            # the silent best-interest assessment (aligned/misaligned + sign-off),
            # not the generic FP analysis.
            request = objective.get("investment_request") if isinstance(objective, dict) else None
            if isinstance(request, dict) and request:
                return self.financial_planning_agent.assess_investment_request(
                    client_id=client_id,
                    session_id=str(objective.get("session_id") or ""),
                    request=request,
                    client_file=client_file,
                )
            return self.financial_planning_agent.build_artifact(
                client_id=client_id,
                objective=objective,
                client_file=client_file,
            )
        if subagent == "investment_solution":
            return self.investment_solution_agent.build_artifact(
                client_id=client_id,
                objective=objective,
                client_file=client_file,
            )
        if subagent == "assessment_revalidation":
            return self.assessment_revalidation_agent.run(
                client_id=client_id,
                objective=objective,
                client_file=client_file,
                change_hint=str(objective.get("change_hint") or ""),
            )
        writeback_target = "client_file.plans"
        return SubAgentArtifact(
            artifact_type=f"{subagent}_placeholder_artifact",
            payload={
                "client_id": client_id,
                "objective": objective,
                "client_file_summary": client_file.get("summary", {}),
                "status": "contract_only",
                "note": "Silent sub-agent boundary is wired; real specialist implementation is not attached yet.",
            },
            writeback_target=writeback_target,
        )


class ContractOnlyFinancialPlanningQueryService:
    """Financial Planning query service for cashflow projection requests."""

    def __init__(self, *, financial_planning_agent: Optional[FinancialPlanningAgentV2] = None) -> None:
        self.financial_planning_agent = financial_planning_agent or FinancialPlanningAgentV2()

    def analyze_scenario(
        self,
        *,
        client_id: str,
        session_id: str,
        question: str,
        scenario: Dict[str, Any],
        client_file: Dict[str, Any],
        mortgage_defaults_authorized: bool = False,
        monte_carlo_paths: Optional[int] = None,
        detail_report_groups: Optional[List[str]] = None,
        authorized_public_model_inputs: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        return self.financial_planning_agent.run_cashflow_projection(
            client_id=client_id,
            session_id=session_id,
            question=question,
            scenario=scenario,
            client_file=client_file,
            mortgage_defaults_authorized=mortgage_defaults_authorized,
            monte_carlo_paths=monte_carlo_paths,
            detail_report_groups=detail_report_groups,
            authorized_public_model_inputs=authorized_public_model_inputs,
        )


def allowed_tool_names_for_action(tool: AgentToolDefinition) -> List[str]:
    """Return a stable one-item list for trace formatting and tests."""

    return [tool.name]


_CONSENT_PATTERNS = (
    r"\bi approve\b",
    r"\bi consent\b",
    r"\bexplicit consent\b",
    r"\byes,? proceed\b",
    r"\bgo ahead\b",
)


def explicit_consent_in_message(message: str) -> bool:
    """Detect explicit consent phrases in user text."""
    text = message.lower()
    return any(re.search(pattern, text) for pattern in _CONSENT_PATTERNS)


@dataclass(frozen=True)
class DeterministicWorkflowOutcome:
    """Result of a guarded deterministic service workflow."""

    service_name: str
    status: str
    reason: str
    writeback: Optional[Dict[str, Any]] = None


class DeterministicWorkflowRunner:
    """Run regulated service workflows outside the LLM."""

    service_contracts = {
        "account_opening": {
            "requires_human_approval": True,
            "next_status": "kyc_pending",
            "adapter": "account_opening_adapter_pending",
        },
        "execution": {
            "requires_human_approval": True,
            "next_status": "execution_pending_external_adapter",
            "adapter": "execution_adapter_pending",
        },
        "policy_exit": {
            "requires_human_approval": True,
            "next_status": "exit_pending_settlement",
            "adapter": "policy_exit_adapter_pending",
        },
        "settlement": {
            "requires_human_approval": True,
            "next_status": "settlement_pending_external_adapter",
            "adapter": "settlement_adapter_pending",
        },
        "holdings_ingestion": {
            "requires_human_approval": False,
            "next_status": "holdings_connection_pending",
            "adapter": "holdings_ingestion_adapter_pending",
        },
    }

    def __init__(self, *, adapter_registry: Optional[DeterministicServiceAdapterRegistry] = None) -> None:
        self.adapter_registry = adapter_registry or build_production_service_adapter_registry(self.service_contracts)

    def run(
        self,
        *,
        service_name: str,
        client_id: str,
        session_id: str,
        user_message: str,
        explicit_consent: bool,
        context: Optional[Dict[str, Any]] = None,
    ) -> DeterministicWorkflowOutcome:
        context = context if isinstance(context, dict) else {}
        contract = self.service_contracts.get(
            service_name,
            {
                "requires_human_approval": True,
                "next_status": "adapter_pending",
                "adapter": "unknown_service_adapter_pending",
            },
        )
        status = "initiated" if explicit_consent else "blocked"
        adapter_result = None
        if explicit_consent:
            adapter_result = self.adapter_registry.initiate(
                DeterministicServiceRequest(
                    service_name=service_name,
                    client_id=client_id,
                    session_id=session_id,
                    user_message=user_message,
                    context=context,
                )
            )
        writeback_values = {
            "client_id": client_id,
            "session_id": session_id,
            "service_name": service_name,
            "status": status,
            "next_status": adapter_result.next_status if adapter_result else "awaiting_explicit_consent",
            "adapter": adapter_result.adapter_name if adapter_result else contract["adapter"],
            "adapter_status": adapter_result.adapter_status if adapter_result else "not_called",
            "external_reference": adapter_result.external_reference if adapter_result else None,
            "adapter_payload": adapter_result.payload if adapter_result else {},
            "requires_human_approval": bool(contract["requires_human_approval"]),
            "consent_source": "user_message" if explicit_consent else None,
            "consent_text": user_message if explicit_consent else "",
            "selected_skill": context.get("selected_skill"),
            "policy": {
                "llm_may_execute": False,
                "external_adapter_required": True,
            },
        }
        return DeterministicWorkflowOutcome(
            service_name=service_name,
            status=status,
            reason=f"Deterministic {service_name} workflow {status}.",
            writeback={
                "record": "client_file.services",
                "values": writeback_values,
            },
        )


class _EngineToolState:
    """Minimal adapter state for financial model tool calls."""

    def __init__(self) -> None:
        self._tool_result_cache: Dict[str, Any] = OrderedDict()
        self._tool_cache_lock = threading.RLock()
        self._tool_inflight: Dict[str, Future] = {}
        self._tool_result_cache_max_entries = max(
            1,
            int(os.getenv("AWM_TOOL_RESULT_CACHE_MAX_ENTRIES", "64") or 64),
        )
        self.latest_cashflow_full: Optional[Dict[str, Any]] = None
        self.latest_asset_allocation_full: Optional[Dict[str, Any]] = None
        self.latest_asset_allocation: Optional[Dict[str, Any]] = None


def _cashflow_source_allocation_reference(
    allocation_result: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Return the immutable lineage needed to audit a cross-model projection."""

    if not isinstance(allocation_result, dict):
        return None
    view = (
        allocation_result.get("asset_allocation_agent_view")
        if isinstance(allocation_result.get("asset_allocation_agent_view"), dict)
        else {}
    )
    portfolio = view.get("portfolio") if isinstance(view.get("portfolio"), dict) else {}
    allocation = view.get("allocation") if isinstance(view.get("allocation"), dict) else {}
    return {
        "analysis_id": allocation_result.get("analysis_id"),
        "allocation_id": allocation_result.get("allocation_id"),
        "source_assessment": copy.deepcopy(
            allocation_result.get("source_assessment") or {}
        ),
        "input_fingerprint": allocation_result.get("input_fingerprint"),
        "target_allocation": copy.deepcopy(allocation.get("weights") or {}),
        "allocation_expected_return_annual_decimal": portfolio.get(
            "expected_return_annual_decimal"
        ),
        "allocation_expected_volatility_annual_decimal": portfolio.get(
            "expected_volatility_annual_decimal"
        ),
        "cashflow_return_policy": (
            "LifeModel uses the linked target weights with its own configured "
            "per-asset returns, volatilities, and correlations; allocation-model "
            "expected return and volatility are retained only as lineage."
        ),
    }


def _cashflow_analysis_detail_excerpt(
    analysis: Dict[str, Any],
    query: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Attach a bounded exact-year excerpt from already collected annual bands."""

    raw_years = query.get("calendar_years")
    raw_columns = query.get("detail_columns")
    if raw_years is None and raw_columns is None:
        return None
    if raw_years is None:
        return {
            "error": "cashflow_detail_query_requires_calendar_years",
            "requires_rerun": False,
            "note": "detail_columns require one or more explicit calendar_years.",
        }
    if (
        not isinstance(raw_years, list)
        or not raw_years
        or len(raw_years) > 12
        or any(isinstance(year, bool) or not isinstance(year, int) for year in raw_years)
        or len(set(raw_years)) != len(raw_years)
    ):
        return {
            "error": "cashflow_detail_calendar_years_invalid",
            "requires_rerun": False,
        }
    detail_series = (
        analysis.get("detail_series")
        if isinstance(analysis.get("detail_series"), dict)
        else {}
    )
    default_columns = [
        "Net Worth",
        "Cashflow Shortfall Debt",
        "Bank Balance",
    ]
    columns = raw_columns if raw_columns is not None else default_columns
    if (
        not isinstance(columns, list)
        or not columns
        or len(columns) > 20
        or any(not isinstance(column, str) or not column.strip() for column in columns)
        or len(set(columns)) != len(columns)
    ):
        return {
            "error": "cashflow_detail_columns_invalid",
            "requires_rerun": False,
        }
    normalized_columns = [str(column).strip() for column in columns]
    missing_columns = [
        column for column in normalized_columns if column not in detail_series
    ]
    if missing_columns:
        return {
            "error": "cashflow_detail_not_collected",
            "missing_detail_columns": missing_columns,
            "available_detail_columns": sorted(detail_series),
            "requires_rerun": True,
            "note": (
                "Rerun with the matching detail_report_groups; the optimized "
                "default projection intentionally collects only three columns."
            ),
        }
    requested_years = [str(year) for year in raw_years]
    available_years = sorted(
        {
            year
            for column in normalized_columns
            for year in detail_series.get(column, {})
        },
        key=lambda value: int(value),
    )
    missing_years = [
        int(year)
        for year in requested_years
        if any(year not in detail_series[column] for column in normalized_columns)
    ]
    if missing_years:
        return {
            "error": "cashflow_detail_year_outside_projection",
            "missing_calendar_years": missing_years,
            "available_calendar_year_range": (
                [int(available_years[0]), int(available_years[-1])]
                if available_years
                else []
            ),
            "requires_rerun": False,
        }
    excerpt: Dict[str, Dict[str, Dict[str, float]]] = {}
    for year in requested_years:
        excerpt[year] = {
            column: copy.deepcopy(detail_series[column][year])
            for column in normalized_columns
        }
    metrics = (
        analysis.setdefault("metrics", {})
        if isinstance(analysis.get("metrics"), dict)
        else {}
    )
    if not isinstance(analysis.get("metrics"), dict):
        analysis["metrics"] = metrics
    metrics["queried_percentile_trajectory"] = {
        "value": excerpt,
        "unit": "LifeModel_column_value_by_calendar_year_and_percentile",
        "source_path": "$.detail_series[selected_columns][selected_calendar_years]",
        "provenance": {
            "source_path": "$.detail_series[selected_columns][selected_calendar_years]",
            "derivation": (
                "exact_bounded_excerpt_from_stored_annual_percentile_series_without_rerun"
            ),
        },
    }
    return None


def _financial_math_unit_from_cashflow_unit(value: Any) -> str:
    """Translate evidence units into the calculation engine's dimensional units."""

    unit = str(value or "").strip()
    aliases = {
        "USD": "money:USD",
        "USD_per_year": "money_per_year:USD",
        "USD_per_month": "money_per_month:USD",
        "probability_0_to_1": "probability_0_to_1",
        "decimal_0_to_1": "probability_0_to_1",
        "share": "probability_0_to_1",
        "decimal": "decimal",
        "ratio": "decimal",
        "decimal_change": "decimal",
        "annual_decimal": "decimal",
        "percentage": "percentage",
        "years": "years",
        "months": "months",
        "count": "count",
    }
    normalized = aliases.get(unit)
    if normalized is None:
        raise ValueError("cashflow_metric_unit_unsupported_for_financial_math")
    return normalized


def _durable_fact_promotion_receipt(
    examination: Any,
    *,
    fact_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Expose only a non-replayable summary after matching server-owned lineage."""

    candidate = examination.candidate
    expected_value_hash = "sha256:" + hashlib.sha256(
        json.dumps(
            fact_data.get("value"),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    if (
        candidate.variable_key != fact_data.get("variable_key")
        or candidate.effective_year != fact_data.get("effective_year")
        or candidate.unit != fact_data.get("unit")
        or candidate.jurisdiction != fact_data.get("jurisdiction")
        or candidate.finding_content_sha256 != fact_data.get("content_sha256")
        or candidate.canonical_value_sha256 != expected_value_hash
    ):
        raise ValueError("durable promotion lineage does not match session fact")
    decision = examination.decision
    policy = examination.policy
    verification = examination.verification
    assessment = examination.agent_assessment
    return {
        "schema_version": "awm.durable_fact_promotion_receipt.v1",
        "status": decision.status,
        "reason_codes": list(decision.reason_codes),
        "examination_id": examination.examination_id,
        "durable_assumption_id": decision.approved_artifact_id,
        "durable_version": decision.approved_version,
        "supersedes_artifact_id": decision.supersedes_artifact_id,
        "granted_uses": (
            [use.value for use in policy.granted_uses] if policy else []
        ),
        "agent_assessment": (
            {
                "assessment_id": assessment.assessment_id,
                "assessed_by": assessment.assessed_by,
                "decision": assessment.decision,
                "reason_code": assessment.reason_code,
            }
            if assessment is not None
            else None
        ),
        "verification": (
            {
                "profile": verification.profile,
                "provider_id": verification.provider_id,
                "verifier_version": verification.verifier_version,
                "source_snapshot_sha256": verification.source_snapshot_sha256,
            }
            if verification is not None
            else None
        ),
        "policy": (
            {
                "policy_id": policy.policy_id,
                "policy_version": policy.policy_version,
            }
            if policy is not None
            else None
        ),
    }


def _session_public_fact_scope(*, client_id: str, session_id: str) -> str:
    """Derive an opaque scope from authenticated server context only."""

    client = str(client_id or "").strip()
    session = str(session_id or "").strip()
    if not client or not session:
        raise ValueError("session public fact scope requires client and session")
    digest = hashlib.sha256(f"{client}\0{session}".encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _parse_aware_timestamp(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


_CASHFLOW_BALANCE_DETAIL_COLUMNS = frozenset(
    {
        "Net Worth",
        "Cashflow Shortfall Debt",
        "Bank Balance",
        "Brokerage Balance",
        "Investment Balance",
        "401k Balance",
        "Traditional IRA Balance",
        "Roth IRA Balance",
        "Total Assets",
        "Total Liabilities",
        "Mortgage Balance",
    }
)


def _financial_math_unit_from_cashflow_series(column: str) -> str:
    from advisor.tools.deterministic_tools.run_cashflow_projection.tool import (
        DETAIL_REPORT_COLUMNS,
    )

    if column not in DETAIL_REPORT_COLUMNS:
        raise ValueError("cashflow_series_column_unit_unsupported")
    return (
        "money:USD"
        if column in _CASHFLOW_BALANCE_DETAIL_COLUMNS
        else "money_per_year:USD"
    )


_AUTHENTICATED_LITERAL_RE = re.compile(
    r"(?<![\w.])"
    r"([+-]?(?:(?:\d{1,3}(?:,\d{3})+)|\d+)(?:\.\d+)?"
    r"(?:[eE][+-]?\d+)?)"
    r"\s*(%|percent(?:age)?|[kKmM])?(?![%A-Za-z0-9_]|\.\d)",
    re.IGNORECASE,
)


def _prepare_financial_math_arguments(
    arguments: Dict[str, Any],
    *,
    companion_turn_id: str,
    authenticated_user_message: str,
    current_client_file: Dict[str, Any],
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Inject server-owned freshness and authenticate any v2 literal sources."""

    prepared = copy.deepcopy(arguments)
    if prepared.get("schema_version") != "awm.financial_math.v2":
        return prepared, None
    try:
        prepared["client_file_version"] = int(
            current_client_file.get("client_file_version") or 0
        )
    except (TypeError, ValueError):
        return prepared, "client_file_version_invalid"
    sources = prepared.get("sources")
    if not isinstance(sources, list):
        return prepared, None
    for source in sources:
        if not isinstance(source, dict) or source.get("kind") != "literal":
            continue
        if not _literal_is_authenticated_by_user_message(
            source.get("value"),
            unit=source.get("unit"),
            user_message=authenticated_user_message,
        ):
            return prepared, "literal_source_not_authenticated"
        source["source_message_id"] = companion_turn_id
    return prepared, None


def _literal_is_authenticated_by_user_message(
    value: Any,
    *,
    unit: Any,
    user_message: str,
) -> bool:
    if not isinstance(value, str) or not user_message.strip():
        return False
    try:
        target = Decimal(value)
    except InvalidOperation:
        return False
    if not target.is_finite():
        return False
    unit_text = str(unit or "")
    for match in _AUTHENTICATED_LITERAL_RE.finditer(user_message):
        try:
            candidate = Decimal(match.group(1).replace(",", ""))
        except InvalidOperation:
            continue
        suffix = str(match.group(2) or "").lower()
        context = _literal_unit_context(
            user_message,
            start=match.start(),
            end=match.end(),
        )
        if suffix in {"%", "percent", "percentage"}:
            if unit_text == "percentage":
                pass
            elif unit_text in {"decimal", "probability_0_to_1", "unitless"}:
                candidate = candidate / Decimal(100)
            else:
                continue
        elif suffix in {"k", "m"} and unit_text.startswith("money"):
            multiplier = Decimal(1000 if suffix == "k" else 1_000_000)
            candidate *= multiplier
        elif suffix in {"k", "m"}:
            continue
        if not _literal_unit_matches_context(
            unit_text,
            suffix=suffix,
            context=context,
        ):
            continue
        if target == candidate:
            return True
    return False


def _literal_unit_context(
    user_message: str,
    *,
    start: int,
    end: int,
) -> Dict[str, Any]:
    prefix = user_message[max(0, start - 32) : start]
    suffix = user_message[end : end + 32]
    currency_code = ""
    currency_patterns = (
        ("USD", r"(?:\$\s*|\busd\s*)$", r"^\s*(?:usd|dollars?)\b"),
        ("EUR", r"(?:\u20ac\s*|\beur\s*)$", r"^\s*(?:eur|euros?)\b"),
        ("GBP", r"(?:\u00a3\s*|\bgbp\s*)$", r"^\s*(?:gbp|pounds?)\b"),
        ("JPY", r"(?:\u00a5\s*|\bjpy\s*)$", r"^\s*(?:jpy|yen)\b"),
    )
    currency_codes = set()
    for code, before_pattern, after_pattern in currency_patterns:
        if re.search(before_pattern, prefix, re.IGNORECASE) or re.search(
            after_pattern,
            suffix,
            re.IGNORECASE,
        ):
            currency_codes.add(code)
    currency_conflict = len(currency_codes) > 1
    if len(currency_codes) == 1:
        currency_code = next(iter(currency_codes))
    suffix_without_currency = re.sub(
        r"^\s*(?:usd|dollars?|eur|euros?|gbp|pounds?|jpy|yen)\b",
        "",
        suffix,
        count=1,
        flags=re.IGNORECASE,
    )
    duration_year = bool(
        re.match(
            r"\s*(?:-\s*)?years?\b",
            suffix_without_currency,
            re.IGNORECASE,
        )
    )
    duration_month = bool(
        re.match(
            r"\s*(?:-\s*)?months?\b",
            suffix_without_currency,
            re.IGNORECASE,
        )
    )
    cadence_year = bool(
        re.match(
            r"\s*(?:(?:/|per|a)\s*)years?\b|\s*(?:annual(?:ly)?|yearly)\b",
            suffix_without_currency,
            re.IGNORECASE,
        )
        or (
            bool(currency_code)
            and re.search(
                r"\b(?:annual(?:ly)?|yearly)\b[^,.;\d]{0,24}(?:\$|[A-Z]{3}\s*)?$",
                prefix,
                re.IGNORECASE,
            )
        )
    )
    cadence_month = bool(
        re.match(
            r"\s*(?:(?:/|per|a)\s*)months?\b|\s*monthly\b",
            suffix_without_currency,
            re.IGNORECASE,
        )
        or (
            bool(currency_code)
            and re.search(
                r"\bmonthly\b[^,.;\d]{0,24}(?:\$|[A-Z]{3}\s*)?$",
                prefix,
                re.IGNORECASE,
            )
        )
    )
    probability = bool(
        re.match(r"\s*probabilit(?:y|ies)\b", suffix, re.IGNORECASE)
        or re.search(r"\bprobabilit(?:y|ies)\s*$", prefix, re.IGNORECASE)
    )
    rate = bool(
        re.match(
            r"\s*(?:(?:annual(?:ly)?|yearly|monthly)\s+)?rate\b",
            suffix_without_currency,
            re.IGNORECASE,
        )
        or re.search(
            r"\b(?:rate|rate\s+of)\s*$",
            prefix,
            re.IGNORECASE,
        )
    )
    return {
        "currency_code": currency_code,
        "currency_conflict": currency_conflict,
        "duration_year": duration_year,
        "duration_month": duration_month,
        "cadence_year": cadence_year,
        "cadence_month": cadence_month,
        "probability": probability,
        "rate": rate,
    }


def _literal_unit_matches_context(
    unit: str,
    *,
    suffix: str,
    context: Mapping[str, Any],
) -> bool:
    currency_match = re.fullmatch(
        r"money(?P<cadence>_per_year|_per_month)?:(?P<currency>[A-Z]{3})",
        unit,
    )
    currency_code = str(context.get("currency_code") or "")
    duration_year = context.get("duration_year") is True
    duration_month = context.get("duration_month") is True
    cadence_year = context.get("cadence_year") is True
    cadence_month = context.get("cadence_month") is True
    is_percent = suffix in {"%", "percent", "percentage"}
    if context.get("currency_conflict") is True:
        return False
    if currency_match:
        if currency_code != currency_match.group("currency") or is_percent:
            return False
        cadence = str(currency_match.group("cadence") or "")
        if cadence == "_per_year":
            return cadence_year and not cadence_month
        if cadence == "_per_month":
            return cadence_month and not cadence_year
        return not cadence_year and not cadence_month
    if currency_code:
        return False
    if is_percent:
        return unit in {
            "decimal",
            "percentage",
            "probability_0_to_1",
            "unitless",
        }
    if unit == "years":
        return duration_year and not duration_month
    if unit == "months":
        return duration_month and not duration_year
    if duration_year or duration_month:
        return False
    if unit == "percentage":
        return False
    if unit == "probability_0_to_1":
        return context.get("probability") is True
    if unit in {"decimal", "unitless"}:
        return not (cadence_year or cadence_month) or context.get("rate") is True
    if unit == "count":
        return True
    return False


def _client_file_with_linked_allocation(
    client_file: Dict[str, Any],
    allocation_result: Dict[str, Any],
) -> Dict[str, Any]:
    """Compatibility wrapper for one validated allocation."""

    return _client_file_with_linked_allocations(client_file, [allocation_result])


def _client_file_with_linked_allocations(
    client_file: Dict[str, Any],
    allocation_results: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Expose validated, non-overlapping allocations to the cash-flow mapper.

    This is a read-only, turn-scoped overlay. It does not create a proposal,
    activate a policy, or write to the Client File.
    """

    if not allocation_results:
        return copy.deepcopy(client_file)
    from advisor.tools.deterministic_tools.run_cashflow_projection.scenarios import (
        CashflowClientInput,
    )

    base_accounts = CashflowClientInput.from_client_file(
        client_file
    ).to_engine_payload().get("accounts", {})
    if not isinstance(base_accounts, dict):
        raise ValueError("Modeled account balances are unavailable for allocation mapping")

    artifacts: List[Dict[str, Any]] = []
    seen_analysis_ids: set[str] = set()
    seen_money_pool_ids: set[str] = set()
    requested_by_kind: Dict[str, float] = {}
    capacity_by_kind = {
        kind: sum(
            max(0.0, _engine_number(item.get("balance")) or 0.0)
            for item in entries
            if isinstance(item, dict)
        )
        for kind, entries in base_accounts.items()
        if isinstance(entries, list)
    }
    for allocation_result in allocation_results:
        linked = _cashflow_source_allocation_reference(allocation_result) or {}
        analysis_id = str(linked.get("analysis_id") or "").strip()
        if not analysis_id or analysis_id in seen_analysis_ids:
            raise ValueError(
                "Each linked allocation must have a unique immutable analysis_id"
            )
        seen_analysis_ids.add(analysis_id)
        weights = linked.get("target_allocation")
        if not isinstance(weights, dict) or not weights:
            raise ValueError(
                f"Linked allocation {analysis_id} has no target weights"
            )
        view = (
            allocation_result.get("asset_allocation_agent_view")
            if isinstance(allocation_result.get("asset_allocation_agent_view"), dict)
            else {}
        )
        mandate = view.get("mandate") if isinstance(view.get("mandate"), dict) else {}
        source_assessment = (
            allocation_result.get("source_assessment")
            if isinstance(allocation_result.get("source_assessment"), dict)
            else {}
        )
        money_pool_id = str(source_assessment.get("money_pool_id") or "").strip()
        if not money_pool_id or money_pool_id in seen_money_pool_ids:
            raise ValueError(
                "Each linked allocation must reference a distinct confirmed money pool"
            )
        seen_money_pool_ids.add(money_pool_id)
        money_pool = copy.deepcopy(_find_money_pool(client_file, money_pool_id) or {})
        if not money_pool:
            raise ValueError(
                f"Confirmed money pool {money_pool_id} is unavailable"
            )
        funding_source = str(money_pool.get("funding_source") or "").strip()
        account_kind = _confirmed_money_pool_account_kind(
            funding_source,
            base_accounts,
        )
        if account_kind is None:
            raise ValueError(
                f"Money pool {money_pool_id} requires an unambiguous confirmed funding_source"
            )
        pool_amount = _engine_number(money_pool.get("amount"))
        mandate_amount = _engine_number(mandate.get("total_investment"))
        if pool_amount is None or pool_amount <= 0 or mandate_amount is None:
            raise ValueError(
                f"Money pool {money_pool_id} and signed mandate require positive amounts"
            )
        if not math.isclose(pool_amount, mandate_amount, rel_tol=0.0, abs_tol=0.01):
            raise ValueError(
                f"Money pool {money_pool_id} amount does not match its signed allocation mandate"
            )
        requested_by_kind[account_kind] = (
            requested_by_kind.get(account_kind, 0.0) + pool_amount
        )
        capacity = capacity_by_kind.get(account_kind, 0.0)
        if requested_by_kind[account_kind] > capacity + 0.01:
            raise ValueError(
                f"Linked {account_kind} money pools exceed the confirmed modeled account capacity"
            )
        artifact = {
            "id": analysis_id,
            "proposal_id": analysis_id,
            "awm_explicit_cashflow_allocation": True,
            "money_pool": money_pool,
            "target_allocation": copy.deepcopy(weights),
            "engine_run": {"engine_name": "asset_allocation_model"},
            "portfolio_analytics": {
                "expected_return": linked.get(
                    "allocation_expected_return_annual_decimal"
                ),
                "expected_volatility": linked.get(
                    "allocation_expected_volatility_annual_decimal"
                ),
                "source": "asset_allocation_model",
            },
            "cross_model_lineage": copy.deepcopy(linked),
        }
        artifacts.append(artifact)

    overlaid = copy.deepcopy(client_file)
    current_turn = overlaid.get("_current_turn_subagent_artifacts")
    existing_artifacts = list(current_turn) if isinstance(current_turn, list) else []
    overlaid["_current_turn_subagent_artifacts"] = [
        *artifacts,
        *existing_artifacts,
    ]
    return overlaid


def _confirmed_money_pool_account_kind(
    funding_source: str,
    accounts: Dict[str, Any],
) -> Optional[str]:
    """Resolve only an explicit funding-source mapping, never a purpose fallback."""

    funding = " ".join(str(funding_source or "").lower().split())
    candidates: List[str] = []
    if any(marker in funding for marker in ("529", "education")):
        candidates.append("education")
    if any(
        marker in funding
        for marker in ("401k", "401(k)", "ira", "retirement account", "pension")
    ):
        candidates.append("retirement")
    if any(
        marker in funding
        for marker in ("brokerage", "taxable", "rsu", "company stock")
    ):
        candidates.append("brokerage")
    if any(marker in funding for marker in ("bank", "cash", "checking", "savings")):
        candidates.append("bank")
    candidates = list(dict.fromkeys(candidates))
    if len(candidates) != 1:
        return None
    kind = candidates[0]
    return kind if accounts.get(kind) else None


def build_cashflow_payload_from_client_file(
    client_file: Dict[str, Any],
    *,
    client_input: Optional[Any] = None,
    mortgage_defaults_authorized: bool = False,
) -> Dict[str, Any]:
    """Build the engine payload from the same typed input used for readiness."""

    from advisor.tools.deterministic_tools.run_cashflow_projection.scenarios import (
        CashflowClientInput,
    )

    canonical = client_input or CashflowClientInput.from_client_file(client_file)
    payload = canonical.to_engine_payload(
        allow_mortgage_defaults=bool(mortgage_defaults_authorized)
    )
    modeled = _apply_asset_allocations_to_cashflow_payload(payload, client_file)
    return _reconcile_cashflow_input_support(modeled)


def _merge_cashflow_payloads(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-merge fresh Client File facts over a mapped cashflow_state."""

    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_cashflow_payloads(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _apply_asset_allocations_to_cashflow_payload(
    payload: Dict[str, Any],
    client_file: Dict[str, Any],
) -> Dict[str, Any]:
    """Apply persisted NEO/asset-allocation policies to modeled account sleeves.

    LifeModel owns stochastic return generation. The asset-allocation engine
    supplies target weights for the funded sleeve; expected return/volatility are
    retained as provenance only, not used as untraceable scalar overrides.
    """

    from advisor.tools.deterministic_tools.run_cashflow_projection.scenarios import (
        _canonicalize_stated_asset_allocation,
    )

    policies = _asset_allocation_policies(client_file)
    accounts = payload.get("accounts")
    if not policies or not isinstance(accounts, dict):
        return payload

    modeled = copy.deepcopy(payload)
    modeled_accounts = copy.deepcopy(accounts)
    provenance: List[Dict[str, Any]] = []
    policies_by_kind: Dict[str, List[Dict[str, Any]]] = {}
    for policy in policies:
        kind = _allocation_account_kind(policy, modeled_accounts)
        if kind:
            policies_by_kind.setdefault(kind, []).append(policy)

    for kind, kind_policies in policies_by_kind.items():
        explicit_policies = [
            policy
            for policy in kind_policies
            if policy.get("explicit_cashflow_allocation") is True
        ]
        if explicit_policies:
            # An explicitly addressed immutable analysis owns this funded sleeve.
            # Do not double-apply a separately persisted proposal for the same
            # account kind.
            kind_policies = explicit_policies
        original_entries = modeled_accounts.get(kind)
        if not isinstance(original_entries, list):
            continue
        original_entries = [
            copy.deepcopy(item) for item in original_entries if isinstance(item, dict)
        ]
        original_balance = sum(
            max(0.0, _engine_number(item.get("balance")) or 0.0)
            for item in original_entries
        )
        if original_balance <= 0:
            continue

        available_balance = original_balance
        sleeves: List[Dict[str, Any]] = []
        for policy in kind_policies:
            requested_amount = _engine_number(policy.get("amount"))
            if requested_amount is None:
                requested_amount = available_balance if len(kind_policies) == 1 else 0.0
            applied_balance = min(available_balance, max(0.0, requested_amount))
            if applied_balance <= 0:
                continue
            allocation = _canonicalize_stated_asset_allocation(
                policy["target_allocation"]
            )
            engine_name = str(policy.get("engine_name") or "asset_allocation_model")
            sleeves.append(
                {
                    "label": str(policy.get("pool_label") or "Asset allocation modeled sleeve"),
                    "balance": applied_balance,
                    "asset_allocation": copy.deepcopy(allocation),
                    "expected_return": policy.get("expected_return"),
                    "expected_volatility": policy.get("expected_volatility"),
                    "allocation_source": engine_name,
                    "allocation_proposal_id": policy.get("proposal_id"),
                }
            )
            available_balance -= applied_balance
            provenance.append(
                {
                    "proposal_id": policy.get("proposal_id"),
                    "pool_label": policy.get("pool_label"),
                    "account_kind": kind,
                    "applied_balance": applied_balance,
                    "target_allocation": copy.deepcopy(allocation),
                    "expected_return": policy.get("expected_return"),
                    "expected_volatility": policy.get("expected_volatility"),
                    "engine_name": engine_name,
                    "source": "asset_allocation_policy",
                }
            )

        if not sleeves:
            continue
        residual_scale = available_balance / original_balance
        residual_entries: List[Dict[str, Any]] = []
        for entry in original_entries:
            residual = copy.deepcopy(entry)
            residual["balance"] = max(
                0.0,
                (_engine_number(entry.get("balance")) or 0.0) * residual_scale,
            )
            if residual["balance"] > 0:
                residual_entries.append(residual)
        modeled_accounts[kind] = [*residual_entries, *sleeves]

    if not provenance:
        return payload
    modeled["accounts"] = modeled_accounts
    modeled["allocation_provenance"] = {
        "source": "asset_allocation_policy",
        "return_policy": (
            "LifeModel derives stochastic returns from supplied target weights; "
            "asset-allocation expected return and volatility are retained for audit."
        ),
        "policies": provenance,
    }
    return modeled


def _reconcile_cashflow_input_support(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Clear unsupported markers only when every funded sleeve is fully sourced."""

    from advisor.tools.deterministic_tools.run_cashflow_projection.scenarios import (
        cashflow_asset_allocation_is_exact,
    )

    contract = payload.get("awm_input_contract")
    accounts = payload.get("accounts")
    if not isinstance(contract, dict) or not isinstance(accounts, dict):
        return payload
    unsupported = _cashflow_string_list(contract.get("unsupported_inputs"))
    retained: List[str] = []
    for marker in unsupported:
        if not marker.startswith("missing_asset_allocation:"):
            retained.append(marker)
            continue
        account_kind = marker.split(":", 1)[1]
        pool = accounts.get(account_kind)
        funded = [
            item
            for item in pool
            if isinstance(item, dict) and (_engine_number(item.get("balance")) or 0.0) > 0
        ] if isinstance(pool, list) else []
        fully_sourced = bool(funded) and all(
            cashflow_asset_allocation_is_exact(
                item.get("asset_allocation")
                if isinstance(item.get("asset_allocation"), dict)
                else item.get("allocation")
            )
            and any(
                _engine_number(item.get(key)) is not None
                for key in ("expected_return", "growth_rate", "average_growth")
            )
            for item in funded
        )
        if not fully_sourced:
            retained.append(marker)
    contract["unsupported_inputs"] = retained
    return payload


def _asset_allocation_policies(client_file: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []

    def extend(value: Any) -> None:
        if isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, dict))

    extend(client_file.get("_current_turn_subagent_artifacts"))
    for key in ("proposals", "policy_artifacts"):
        extend(client_file.get(key))
    policies = client_file.get("policies")
    if isinstance(policies, dict):
        for key in ("writebacks", "proposed", "active", "mvp"):
            extend(policies.get(key))
    artifacts = client_file.get("artifacts")
    if isinstance(artifacts, dict):
        for key in ("proposals", "policies"):
            extend(artifacts.get(key))

    output: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        wrapper = candidate.get("values") if isinstance(candidate.get("values"), dict) else candidate
        payload = (
            wrapper.get("payload")
            if isinstance(wrapper.get("payload"), dict)
            else wrapper
        )
        policy = payload.get("policy") if isinstance(payload.get("policy"), dict) else {}
        allocation = (
            policy.get("target_allocation")
            if isinstance(policy.get("target_allocation"), dict)
            else payload.get("target_allocation")
        )
        if not allocation:
            allocation = _allocation_from_artifact_sections(payload)
        allocation = _positive_normalized_allocation(allocation)
        if not allocation:
            continue

        analytics = (
            payload.get("portfolio_analytics")
            if isinstance(payload.get("portfolio_analytics"), dict)
            else {}
        )
        if not analytics:
            analytics = _analytics_from_artifact_sections(payload)
        engine_run = (
            payload.get("engine_run")
            if isinstance(payload.get("engine_run"), dict)
            else {}
        )
        engine_name = str(engine_run.get("engine_name") or analytics.get("source") or "").lower()
        if engine_name not in {"neo", "asset_allocation_model", "real_asset_allocation_model"}:
            continue

        pool = payload.get("money_pool") if isinstance(payload.get("money_pool"), dict) else {}
        proposal_id = str(
            payload.get("id")
            or payload.get("proposal_id")
            or wrapper.get("id")
            or wrapper.get("proposal_id")
            or ""
        ).strip()
        dedupe_key = proposal_id or json.dumps(
            [pool.get("label"), pool.get("amount"), allocation],
            sort_keys=True,
            default=str,
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        output.append(
            {
                "proposal_id": proposal_id or None,
                "pool_label": pool.get("label") or policy.get("title") or payload.get("title"),
                "purpose_type": pool.get("purpose_type"),
                "funding_source": pool.get("funding_source"),
                "amount": pool.get("amount"),
                "target_allocation": allocation,
                "expected_return": analytics.get("expected_return"),
                "expected_volatility": analytics.get("expected_volatility"),
                "engine_name": engine_run.get("engine_name") or analytics.get("source") or "asset_allocation_model",
                "explicit_cashflow_allocation": (
                    payload.get("awm_explicit_cashflow_allocation") is True
                ),
            }
        )
    return output


def _allocation_from_artifact_sections(payload: Dict[str, Any]) -> Dict[str, float]:
    section = _artifact_section(payload, "allocation")
    chart = section.get("chart") if isinstance(section.get("chart"), list) else []
    allocation: Dict[str, float] = {}
    for row in chart:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or "").strip()
        value = _engine_number(row.get("value"))
        if label and value is not None and value > 0:
            allocation[label] = value / 100.0 if value > 1.0 else value
    return allocation


def _analytics_from_artifact_sections(payload: Dict[str, Any]) -> Dict[str, Any]:
    section = _artifact_section(payload, "portfolio_analytics")
    return {
        "expected_return": section.get("expected_return"),
        "expected_volatility": section.get("expected_volatility"),
        "source": section.get("source") or "asset_allocation_model",
    }


def _artifact_section(payload: Dict[str, Any], section_id: str) -> Dict[str, Any]:
    sections = payload.get("sections") if isinstance(payload.get("sections"), list) else []
    for section in sections:
        if not isinstance(section, dict) or section.get("section_id") != section_id:
            continue
        section_payload = section.get("payload")
        return section_payload if isinstance(section_payload, dict) else {}
    return {}


def _positive_normalized_allocation(value: Any) -> Dict[str, float]:
    if not isinstance(value, dict):
        return {}
    allocation = {
        str(name): numeric
        for name, weight in value.items()
        if (numeric := _engine_number(weight)) is not None and numeric > 0
    }
    total = sum(allocation.values())
    if total <= 0:
        return {}
    return {name: weight / total for name, weight in allocation.items()}


def _allocation_account_kind(
    policy: Dict[str, Any],
    accounts: Dict[str, Any],
) -> Optional[str]:
    funding = str(policy.get("funding_source") or "").lower()
    purpose = str(policy.get("purpose_type") or "").lower()
    if any(marker in funding for marker in ("529", "education")):
        return "education" if accounts.get("education") else None
    if any(marker in funding for marker in ("401k", "401(k)", "ira", "retirement account")):
        return "retirement" if accounts.get("retirement") else None
    if any(marker in funding for marker in ("brokerage", "taxable", "rsu", "company stock")):
        return "brokerage" if accounts.get("brokerage") else None
    if purpose in {"education", "college"} and accounts.get("education"):
        return "education"
    if purpose == "retirement" and accounts.get("retirement"):
        return "retirement"
    if accounts.get("brokerage"):
        return "brokerage"
    if accounts.get("retirement"):
        return "retirement"
    return None


@dataclass(frozen=True)
class CashflowEngineClient:
    """Adapter around the cashflow simulation engine."""

    config: Any
    enabled: bool = False
    request_timeout_seconds: int = 90
    recommendation_policy: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "_state", _EngineToolState())

    def analyze_scenario(
        self,
        *,
        client_file: Dict[str, Any],
        scenario: Dict[str, Any],
        question: str = "",
        mortgage_defaults_authorized: bool = False,
        monte_carlo_paths: Optional[int] = None,
        detail_report_groups: Optional[List[str]] = None,
        authorized_public_model_inputs: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Compile, execute, validate, and normalize one cash-flow scenario."""

        from advisor.tools.deterministic_tools.run_cashflow_projection.scenarios import (
            CashflowCapabilityDecision,
            CashflowClientInput,
            cashflow_input_readiness,
            compile_cashflow_scenario,
        )

        decision = CashflowCapabilityDecision.from_dict(scenario)
        canonical_input = CashflowClientInput.from_client_file(client_file)
        base_payload = build_cashflow_payload_from_client_file(
            client_file,
            client_input=canonical_input,
            mortgage_defaults_authorized=mortgage_defaults_authorized,
        )
        readiness = cashflow_input_readiness(
            client_file,
            decision=decision,
            client_input=canonical_input,
            engine_payload=base_payload,
            monte_carlo_paths=monte_carlo_paths,
        )
        if monte_carlo_paths is not None:
            if isinstance(monte_carlo_paths, bool) or not isinstance(
                monte_carlo_paths, int
            ):
                decision.validation_errors.append("monte_carlo_paths_must_be_integer")
            elif not 10 <= monte_carlo_paths <= 1000:
                decision.validation_errors.append("monte_carlo_paths_out_of_range")
        compiled = compile_cashflow_scenario(
            base_payload=base_payload,
            decision=decision,
            monte_carlo_paths=(
                monte_carlo_paths if not decision.validation_errors else None
            ),
        )
        if authorized_public_model_inputs:
            public_inputs = copy.deepcopy(authorized_public_model_inputs)
            compiled["effective_input"][
                "authorized_public_model_inputs"
            ] = public_inputs
            compiled["tool_args"].setdefault("payload_override", {})[
                "authorized_public_model_inputs"
            ] = public_inputs
        if detail_report_groups:
            supported_groups = {
                "income",
                "spending",
                "taxes",
                "withdrawals",
                "account_balances",
                "mortgage",
            }
            normalized_groups = [
                str(item).strip()
                for item in detail_report_groups
                if str(item).strip()
            ]
            if (
                len(normalized_groups) != len(detail_report_groups)
                or len(set(normalized_groups)) != len(normalized_groups)
                or any(item not in supported_groups for item in normalized_groups)
            ):
                decision.validation_errors.append(
                    "detail_report_groups_invalid"
                )
            else:
                compiled["effective_input"].setdefault("simulation_config", {})[
                    "detail_report_groups"
                ] = normalized_groups
                compiled["requested_input"]["detail_report_groups"] = normalized_groups
                compiled["tool_args"].setdefault("payload_override", {}).setdefault(
                    "simulation_config",
                    {},
                )["detail_report_groups"] = normalized_groups
        effective_input = compiled["effective_input"]
        call_id = _cashflow_call_id(effective_input)
        request = {
            "call_id": call_id,
            "scenario_label": (
                decision.scenario_summary or _cashflow_scenario_label(question)
            ),
            "requested_input": compiled["requested_input"],
            "effective_input": _cashflow_public_effective_input(effective_input),
            "applied_changes": compiled["applied_changes"],
            "unsupported_changes": compiled["unsupported_changes"],
        }

        if decision.validation_errors:
            return _cashflow_result_envelope(
                decision=decision,
                request=request,
                execution="not_run",
                validation="invalid_request",
                warnings=[*compiled["warnings"], *decision.validation_errors],
                valid_for_recommendation=False,
                error="invalid_request",
                missing_data=list(decision.validation_errors),
                analysis_grade="not_run",
            )
        if not readiness["ready"]:
            return _cashflow_result_envelope(
                decision=decision,
                request=request,
                execution="not_run",
                validation="missing_data",
                warnings=list(compiled["warnings"]),
                valid_for_recommendation=False,
                error="missing_required_inputs",
                missing_data=list(readiness["missing_required_inputs"]),
                analysis_grade="not_run",
            )
        if compiled["unsupported_changes"]:
            return _cashflow_result_envelope(
                decision=decision,
                request=request,
                execution="not_run",
                validation="failed",
                warnings=[
                    *compiled["warnings"],
                    "The requested scenario contains changes the current cash-flow engine cannot represent exactly.",
                ],
                valid_for_recommendation=False,
                error="unsupported_scenario_changes",
                analysis_grade="not_run",
            )
        if not self.enabled:
            return _cashflow_result_envelope(
                decision=decision,
                request=request,
                execution="unavailable",
                validation="failed",
                warnings=[*compiled["warnings"], "Cash-flow engine is not enabled."],
                valid_for_recommendation=False,
                error="engine_required_unavailable" if engines_required() else "engine_unavailable",
                analysis_grade="not_run",
            )

        from advisor.tools.deterministic_tools.run_cashflow_projection.execution import run_cashflow_model_tool

        result = run_cashflow_model_tool(
            compiled["tool_args"],
            base_payload,
            self._state,
            config=self.config,
            http_session=_engine_http_session(),
            request_timeout_seconds=self.request_timeout_seconds,
            resolve_cashflow_payload=lambda value: value if isinstance(value, dict) else {},
            log_debug=lambda _message: None,
        )
        adapter_request = result.get("request") if isinstance(result.get("request"), dict) else {}
        if adapter_request.get("call_id"):
            request["call_id"] = adapter_request["call_id"]
            effective_adapter_input = _cashflow_public_effective_input(
                adapter_request.get("effective_input")
                or request["effective_input"]
            )
            request["effective_input"] = effective_adapter_input
        full_result = result.get("full_result") if isinstance(result.get("full_result"), dict) else {}
        native_success = full_result.get("success") is True
        if not result.get("success") or not native_success:
            error = str(
                result.get("error")
                or full_result.get("error")
                or "Cash-flow engine returned an invalid or unsuccessful result."
            )
            return _cashflow_result_envelope(
                decision=decision,
                request=request,
                execution="failed",
                validation="failed",
                warnings=[*compiled["warnings"], error],
                valid_for_recommendation=False,
                error="engine_required_failed" if engines_required() else "engine_failed",
                analysis_grade="not_run",
            )

        normalized = (
            result.get("normalized_result")
            if isinstance(result.get("normalized_result"), dict)
            else {}
        )
        if not normalized:
            from advisor.tools.deterministic_tools.run_cashflow_projection.execution import (
                normalize_cashflow_engine_response,
            )

            normalized = normalize_cashflow_engine_response(full_result)
        metrics = normalized.get("metrics") if isinstance(normalized.get("metrics"), dict) else {}
        warnings = list(compiled["warnings"])
        warnings.extend(_cashflow_native_warning_messages(normalized.get("warnings")))
        normalization_errors = [
            str(item) for item in normalized.get("normalization_errors", []) if item
        ]
        if normalization_errors:
            warnings.append(
                "Cash-flow response normalization failed checks: "
                + ", ".join(normalization_errors)
            )
        missing_metrics = [
            metric for metric in decision.requested_metrics if metric not in metrics
        ]
        if missing_metrics:
            warnings.append(
                "Requested metrics unavailable from engine output: " + ", ".join(missing_metrics)
            )
        policy_eligible, policy_blockers, policy_metadata = _cashflow_recommendation_eligibility(
            policy=self.recommendation_policy,
            normalized_result=normalized,
            effective_input=request["effective_input"],
        )
        warnings.extend(policy_blockers)
        valid_for_recommendation = bool(
            not missing_metrics and not normalization_errors and policy_eligible
        )
        if missing_metrics:
            validation = "failed"
            error = "missing_required_metrics"
        elif normalization_errors:
            validation = "failed"
            error = "invalid_engine_output"
        elif not policy_eligible:
            validation = "estimate_only"
            error = "recommendation_policy_not_satisfied"
        else:
            validation = "warning" if warnings else "verified"
            error = None
        return _cashflow_result_envelope(
            decision=decision,
            request=request,
            execution="succeeded",
            validation=validation,
            warnings=warnings,
            valid_for_recommendation=valid_for_recommendation,
            metrics=metrics,
            native_result=full_result,
            normalized_result=normalized,
            error=error,
            missing_metrics=missing_metrics,
            analysis_grade=(
                "recommendation_grade" if valid_for_recommendation else "interactive_estimate"
            ),
            recommendation_policy=policy_metadata,
        )

    def evaluate_materiality(
        self,
        *,
        client_file: Dict[str, Any],
        change_hint: str = "",
    ) -> Dict[str, Any]:
        if not self.enabled:
            if engines_required():
                return _engine_required_unavailable("cashflow", "Cashflow engine is required but not enabled.")
            return {
                "material": False,
                "signal": "engine_unavailable",
                "reason": "Cashflow engine is not enabled.",
                "engine_policy": engine_policy(),
            }
        result = self._run_simulation(client_file)
        if not result.get("success"):
            if engines_required():
                return _engine_required_failed(
                    "cashflow",
                    str(result.get("error") or "Cashflow engine call failed."),
                )
            return {
                "material": False,
                "signal": "engine_unavailable",
                "reason": str(result.get("error") or "Cashflow engine call failed."),
                "engine_policy": engine_policy(),
            }
        full_result = result.get("full_result") if isinstance(result.get("full_result"), dict) else {}
        if full_result.get("requires_revalidation") or full_result.get("material_change"):
            return {
                "material": True,
                "signal": "cashflow_engine",
                "reason": "Cashflow engine reported a material planning change.",
                "source": "cashflow_engine",
            }
        return {
            "material": False,
            "signal": "cashflow_engine_clear",
            "reason": "Cashflow engine did not return an explicit material-change flag.",
            "source": "cashflow_engine",
        }

    def recommend_education_amount(self, client_file: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            if engines_required():
                return _engine_required_unavailable_payload(
                    "cashflow",
                    "Cashflow engine is required but not enabled.",
                )
            return None
        result = self._run_simulation(client_file)
        if not result.get("success"):
            if engines_required():
                return _engine_required_failed_payload(
                    "cashflow",
                    str(result.get("error") or "Cashflow engine call failed."),
                )
            return None
        from advisor.tools.deterministic_tools.run_cashflow_projection.scenarios import (
            CashflowClientInput,
        )

        canonical = CashflowClientInput.from_client_file(client_file)
        full_result = result.get("full_result") if isinstance(result.get("full_result"), dict) else {}
        projected = _first_engine_number(
            full_result.get("education_goal_amount"),
            full_result.get("college_projected_cost"),
            canonical.education_goal_amount,
        )
        existing_529 = canonical.education_balance
        missing_data = []
        if projected is None:
            missing_data.append("education_goal_amount")
        if existing_529 is None:
            missing_data.append("education_balance")
        if canonical.education_horizon_years is None:
            missing_data.append("education_goal_timing")
        if missing_data:
            return {
                "success": False,
                "valid_for_recommendation": False,
                "error": "missing_required_inputs",
                "missing_data": missing_data,
                "calculation_policy": (
                    "No education amount or timing was silently defaulted; complete Client File facts are required."
                ),
            }
        amount = max(0, int(projected - existing_529))
        return {
            "success": True,
            "valid_for_recommendation": False,
            "purpose_type": "education",
            "amount": amount,
            "currency": "USD",
            "method": "cashflow_engine_education_goal_minus_529",
            "components": {"projected_cost": projected, "existing_529": existing_529},
            "missing_data": [],
            "pool_arguments": {
                "amount": amount,
                "purpose_type": "education",
                "label": "College education",
                "horizon_text": f"{int(canonical.education_horizon_years)} years",
            },
            "calculation_policy": (
                "Estimate only; the amount is a subtraction of sourced Client File values, not an allocation recommendation."
            ),
        }

    def _run_simulation(self, client_file: Dict[str, Any]) -> Dict[str, Any]:
        from advisor.tools.deterministic_tools.run_cashflow_projection.execution import run_cashflow_model_tool

        payload = build_cashflow_payload_from_client_file(client_file)
        return run_cashflow_model_tool(
            {},
            payload,
            self._state,
            config=self.config,
            http_session=_engine_http_session(),
            request_timeout_seconds=self.request_timeout_seconds,
            resolve_cashflow_payload=lambda value: value if isinstance(value, dict) else {},
            log_debug=lambda _message: None,
        )


def _cashflow_call_id(effective_input: Dict[str, Any]) -> str:
    canonical = json.dumps(effective_input, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"cashflow:{digest}"


def _cashflow_public_effective_input(value: Any) -> Any:
    """Remove session capability data from public analysis request metadata."""

    sanitized = copy.deepcopy(value)
    if not isinstance(sanitized, dict):
        return sanitized
    for public_input in sanitized.get("authorized_public_model_inputs") or []:
        if isinstance(public_input, dict):
            public_input.pop("session_fact_id", None)
            public_input.pop("expires_at", None)
    return sanitized


def _cashflow_scenario_label(question: str) -> str:
    text = " ".join(str(question or "").split())
    return text[:160] if text else "cash-flow projection"


def _cashflow_native_warning_messages(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    messages: List[str] = []
    for warning in value:
        if isinstance(warning, dict):
            code = str(warning.get("code") or "NATIVE_WARNING")
            severity = str(warning.get("severity") or "warning")
            message = str(warning.get("message") or warning)
            messages.append(f"[life_model:{severity}:{code}] {message}")
        elif warning:
            messages.append(f"[life_model:warning] {warning}")
    return messages


def _cashflow_string_list(value: Any) -> List[str]:
    return [str(item) for item in value if item] if isinstance(value, list) else []


def _cashflow_recommendation_eligibility(
    *,
    policy: Optional[Dict[str, Any]],
    normalized_result: Dict[str, Any],
    effective_input: Dict[str, Any],
) -> Tuple[bool, List[str], Dict[str, Any]]:
    """Apply an explicit business-approved recommendation-grade policy."""

    if not isinstance(policy, dict):
        return (
            False,
            ["Cash-flow output is estimate-only; no approved recommendation policy was supplied."],
            {"policy_id": None, "approval_status": "not_supplied"},
        )

    required_policy_fields = (
        "policy_id",
        "approval_status",
        "approved_by",
        "approved_at",
        "minimum_simulations",
        "success_definition",
        "approved_wording_version",
    )
    blockers = [
        f"Recommendation policy is missing {field_name}."
        for field_name in required_policy_fields
        if policy.get(field_name) in (None, "", [])
    ]
    if str(policy.get("approval_status") or "").lower() != "approved":
        blockers.append("Recommendation policy is not approved.")
    run_metadata = (
        normalized_result.get("run_metadata")
        if isinstance(normalized_result.get("run_metadata"), dict)
        else {}
    )
    num_simulations = _engine_number(run_metadata.get("num_simulations"))
    minimum_simulations = _engine_number(policy.get("minimum_simulations"))
    if (
        num_simulations is None
        or minimum_simulations is None
        or num_simulations < minimum_simulations
    ):
        blockers.append("Engine run does not meet the approved simulation-count minimum.")
    if run_metadata.get("simulation_mode") == "monte_carlo":
        if str(policy.get("success_column") or "") != str(
            run_metadata.get("success_column") or ""
        ):
            blockers.append("Engine success column does not match the approved definition.")
        approved_threshold = _engine_number(policy.get("success_threshold"))
        actual_threshold = _engine_number(run_metadata.get("success_threshold"))
        if approved_threshold is None or actual_threshold != approved_threshold:
            blockers.append("Engine success threshold does not match the approved definition.")

    awm_contract = (
        effective_input.get("awm_input_contract")
        if isinstance(effective_input.get("awm_input_contract"), dict)
        else {}
    )
    if (
        awm_contract.get("all_inputs_client_confirmed") is False
        or (
            "all_inputs_client_confirmed" not in awm_contract
            and awm_contract.get("uses_draft_facts")
        )
    ):
        blockers.append("Recommendation-grade output cannot rely on unconfirmed draft facts.")
    unsupported_inputs = _cashflow_string_list(awm_contract.get("unsupported_inputs"))
    if unsupported_inputs:
        blockers.append("Unsupported cash-flow inputs remain: " + ", ".join(unsupported_inputs))

    native_context = run_metadata.get("input_context") if isinstance(run_metadata.get("input_context"), dict) else {}
    assumptions = {
        str(item)
        for item in [
            *_cashflow_string_list(awm_contract.get("assumptions")),
            *_cashflow_string_list(native_context.get("assumptions")),
        ]
        if item
    }
    approved_assumptions = {
        str(item) for item in _cashflow_string_list(policy.get("approved_assumptions"))
    }
    unapproved_assumptions = sorted(assumptions - approved_assumptions)
    if unapproved_assumptions:
        blockers.append(
            "Unapproved model assumptions remain: " + "; ".join(unapproved_assumptions)
        )
    native_unknowns = _cashflow_string_list(native_context.get("unknowns"))
    if native_unknowns:
        blockers.append("Native model input unknowns remain: " + ", ".join(native_unknowns))

    allowed_warning_codes = {
        str(item) for item in _cashflow_string_list(policy.get("allowed_warning_codes"))
    }
    native_warnings = normalized_result.get("warnings")
    for warning in native_warnings if isinstance(native_warnings, list) else []:
        if not isinstance(warning, dict):
            blockers.append("An untyped native warning is not approved for recommendation use.")
            continue
        code = str(warning.get("code") or "")
        severity = str(warning.get("severity") or "warning").lower()
        if severity == "warning" and code not in allowed_warning_codes:
            blockers.append(f"Native warning {code or '<missing code>'} is not policy-approved.")

    policy_metadata = {
        "policy_id": policy.get("policy_id"),
        "approval_status": policy.get("approval_status"),
        "approved_by": policy.get("approved_by"),
        "approved_at": policy.get("approved_at"),
        "approved_wording_version": policy.get("approved_wording_version"),
        "minimum_simulations": policy.get("minimum_simulations"),
    }
    return not blockers, blockers, policy_metadata


def _cashflow_result_envelope(
    *,
    decision: Any,
    request: Dict[str, Any],
    execution: str,
    validation: str,
    warnings: List[str],
    valid_for_recommendation: bool,
    metrics: Optional[Dict[str, Any]] = None,
    native_result: Optional[Dict[str, Any]] = None,
    normalized_result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    missing_data: Optional[List[str]] = None,
    missing_metrics: Optional[List[str]] = None,
    analysis_grade: str = "not_run",
    recommendation_policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    metrics = metrics or {}
    native_result = native_result or {}
    normalized_result = normalized_result or {}
    normalized_engine = (
        normalized_result.get("engine")
        if isinstance(normalized_result.get("engine"), dict)
        else {}
    )
    run_metadata = (
        normalized_result.get("run_metadata")
        if isinstance(normalized_result.get("run_metadata"), dict)
        else {}
    )
    native_input_context = (
        run_metadata.get("input_context")
        if isinstance(run_metadata.get("input_context"), dict)
        else {}
    )
    return {
        "schema_version": "awm.cashflow_result.v2",
        "model": {
            "name": "cashflow",
            "operation": "simulate",
            "model_version": (
                normalized_engine.get("version")
                or native_result.get("model_version")
                or None
            ),
            "transport_schema_version": normalized_result.get("transport_schema_version"),
        },
        "request": request,
        "status": {
            "execution": execution,
            "validation": validation,
            "analysis_grade": analysis_grade,
            "valid_for_recommendation": valid_for_recommendation,
            "warnings": warnings,
            "error": error,
            "missing_required_metrics": missing_metrics or [],
            "normalization_errors": normalized_result.get("normalization_errors", []),
        },
        "scenario": {
            "label": request.get("scenario_label"),
            "summary": decision.scenario_summary,
            "rationale": decision.scenario_rationale,
        },
        "headline": _cashflow_headline(
            metrics,
            execution=execution,
            valid=valid_for_recommendation,
            error=error,
        ),
        "metrics": metrics,
        "drivers": _cashflow_drivers(metrics),
        "assumptions": _cashflow_string_list(native_input_context.get("assumptions")),
        "resolved_assumptions": copy.deepcopy(
            native_input_context.get("resolved_assumptions")
            if isinstance(native_input_context.get("resolved_assumptions"), list)
            else []
        ),
        "missing_data": missing_data or [],
        "native_result_ref": request.get("call_id"),
        "native_result_metadata": {
            **run_metadata,
            "warnings": normalized_result.get("warnings", []),
        },
        "_projection_source": copy.deepcopy(native_result) if native_result else None,
        "detail_series": copy.deepcopy(
            normalized_result.get("detail_series")
            if isinstance(normalized_result.get("detail_series"), dict)
            else {}
        ),
        "recommendation_policy": recommendation_policy
        or {"policy_id": None, "approval_status": "not_supplied"},
        "calculation_policy": (
            "Main Agent did not calculate; the cash-flow engine result was validated "
            "and normalized by Financial Planning. Categorical feasibility wording is "
            "not generated without an approved recommendation policy."
        ),
    }


def _cashflow_metrics(full_result: Dict[str, Any]) -> Dict[str, Any]:
    from advisor.tools.deterministic_tools.run_cashflow_projection.execution import (
        normalize_cashflow_engine_response,
    )

    normalized = normalize_cashflow_engine_response(full_result)
    return normalized.get("metrics") if isinstance(normalized.get("metrics"), dict) else {}


def _minimum_liquidity_metric(details: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    trajectory = details.get("milestone_percentile_trajectory")
    if not isinstance(trajectory, list):
        return None
    candidates: List[Tuple[float, Any]] = []
    for item in trajectory:
        if not isinstance(item, dict):
            continue
        balances = item.get("balances") if isinstance(item.get("balances"), dict) else {}
        liquid = balances.get("liquid_assets") if isinstance(balances.get("liquid_assets"), dict) else {}
        value = _engine_number(liquid.get("p10"))
        if value is not None:
            candidates.append((value, item.get("year")))
    if not candidates:
        return None
    value, year = min(candidates, key=lambda pair: pair[0])
    return {
        "value": value,
        "unit": "USD",
        "year": year,
        "percentile": "p10",
        "source_path": "$.details.milestone_percentile_trajectory[*].balances.liquid_assets.p10",
    }


def _cashflow_headline(
    metrics: Dict[str, Any],
    *,
    execution: str,
    valid: bool,
    error: Optional[str],
) -> Dict[str, Any]:
    if execution != "succeeded" or error in {
        "missing_required_metrics",
        "invalid_engine_output",
    }:
        return {
            "conclusion": "analysis_unavailable",
            "summary": "The requested cash-flow scenario could not be validated for use.",
            "error": error,
        }
    if not valid:
        return {
            "conclusion": "estimate_complete",
            "summary": (
                "The cash-flow estimate completed, but it is not approved for a "
                "client-facing recommendation; review metrics, missing fields, and warnings."
            ),
            "error": error,
        }
    return {
        "conclusion": "analysis_complete",
        "summary": (
            "The recommendation-grade cash-flow analysis completed under the cited "
            "approved policy; review measured metrics and warnings."
        ),
    }


def _cashflow_drivers(metrics: Dict[str, Any]) -> List[str]:
    drivers: List[str] = []
    shortfall = metrics.get("shortfall") if isinstance(metrics.get("shortfall"), dict) else {}
    if (_engine_number(shortfall.get("value")) or 0.0) > 0:
        drivers.append("The modeled paths include an expected funding shortfall.")
    reserve = metrics.get("reserve_breach_probability") if isinstance(metrics.get("reserve_breach_probability"), dict) else {}
    reserve_value = _engine_number(reserve.get("value"))
    if reserve_value is not None and reserve_value > 0:
        drivers.append("The model reports a non-zero probability of breaching the reserve floor.")
    terminal = metrics.get("terminal_value_percentiles") if isinstance(metrics.get("terminal_value_percentiles"), dict) else {}
    terminal_values = terminal.get("value") if isinstance(terminal.get("value"), dict) else {}
    p10 = _engine_number(terminal_values.get("p10"))
    if p10 is not None and p10 < 0:
        drivers.append("The lower-decile terminal wealth outcome is negative.")
    return drivers


@dataclass(frozen=True)
class AssetAllocationModelClient:
    """Adapter around the asset allocation portfolio optimization model."""

    config: Any
    enabled: bool = False
    request_timeout_seconds: int = 60

    def __post_init__(self) -> None:
        object.__setattr__(self, "_state", _EngineToolState())

    def optimize_money_pool(self, pool: Dict[str, Any]) -> Dict[str, Any]:
        if not self.enabled:
            if engines_required():
                return _engine_required_unavailable_payload(
                    "asset_allocation_model",
                    "Asset allocation model is required but not enabled.",
                )
            return {"success": False, "error": "engine_disabled", "engine_policy": engine_policy()}
        return {
            "success": False,
            "valid_for_recommendation": False,
            "error": "signed_assessment_authorization_required",
            "status": {
                "execution": "blocked",
                "valid_for_recommendation": False,
                "contract_version": "asset_allocation_result.v2",
            },
            "reason": (
                "Direct money-pool optimization is deprecated. Use the RegistryToolExecutor "
                "signed-assessment reference boundary so mandate inputs are server-resolved."
            ),
        }



def _persist_asset_allocation_proposal(
    *,
    client_id: str,
    session_id: str,
    arguments: Dict[str, Any],
    allocation_result: Dict[str, Any],
    client_file: Dict[str, Any],
) -> Dict[str, Any]:
    """Persist a proposed policy artifact from a successful allocation run."""

    try:
        from api.persistence import save_asset_allocation_proposal_bundle
        from api.services.asset_allocation_artifact_adapter import proposal_artifact_from_asset_allocation

        full_result = allocation_result.get("full_result") or {}
        status = full_result.get("status") if isinstance(full_result.get("status"), dict) else {}
        if status.get("valid_for_recommendation") is not True:
            return {"ok": False, "error": "allocation_not_valid_for_persistence"}
        assessment_ref = (
            full_result.get("source_assessment")
            if isinstance(full_result.get("source_assessment"), dict)
            else {}
        )
        money_pool_id = str(assessment_ref.get("money_pool_id") or "") or None
        assessment_id = str(assessment_ref.get("assessment_id") or "") or None
        assessment_version = assessment_ref.get("assessment_version")
        allocation_id = str(full_result.get("allocation_id") or "")
        contract_version = str(status.get("contract_version") or "")
        if not assessment_id or not money_pool_id or not assessment_version or not allocation_id:
            return {"ok": False, "error": "allocation_persistence_lineage_missing"}
        idempotency_key = ":".join(
            [
                client_id,
                assessment_id,
                str(assessment_version),
                money_pool_id,
                allocation_id,
                contract_version,
            ]
        )
        money_pool = _find_money_pool(client_file, money_pool_id)
        collected_fields = {
            "amount": full_result.get("total_investment"),
            "objective": (money_pool or {}).get("objective") or (money_pool or {}).get("label") or "Investment policy proposal",
            "constraints": (money_pool or {}).get("constraints") or [],
        }
        journey = {
            "id": assessment_id or money_pool_id,
            "money_pool_id": money_pool_id,
            "assessment_id": assessment_id,
            "collected_fields": collected_fields,
        }
        artifact_payload = proposal_artifact_from_asset_allocation(
            full_result,
            journey=journey,
        )
        allocation_policy_fields = map_asset_allocation_result_to_policy_fields(allocation_result)
        if money_pool:
            artifact_payload["money_pool"] = money_pool
        if assessment_id:
            artifact_payload["assessment_id"] = assessment_id
        bundle = save_asset_allocation_proposal_bundle(
            client_id=client_id,
            idempotency_key=idempotency_key,
            artifact_title=str(artifact_payload.get("title") or "Investment Proposal"),
            artifact_payload={
                **artifact_payload,
                "source_session_id": session_id,
                "assessment_id": assessment_id,
                "assessment_version": assessment_version,
                "money_pool_id": money_pool_id,
            },
            policy_payload={
                "title": str(artifact_payload.get("title") or "Investment Proposal"),
                "money_pool": money_pool,
                "money_pool_id": money_pool_id,
                "assessment_id": assessment_id,
                "assessment_version": assessment_version,
                "source_session_id": session_id,
                "engine_run": artifact_payload.get("engine_run"),
                "policy": {
                    "target_allocation": allocation_policy_fields.get("target_allocation") or {},
                },
                "portfolio_analytics": {
                    "expected_return": allocation_policy_fields.get("expected_return"),
                    "expected_volatility": allocation_policy_fields.get("expected_volatility"),
                    "source": "asset_allocation_model",
                },
                "recommended_securities": allocation_policy_fields.get("recommended_securities") or [],
                "sections": artifact_payload.get("sections", []),
                "section_ids": artifact_payload.get("section_ids", []),
            },
            money_pool_id=money_pool_id,
        )
        return bundle
    except Exception as exc:  # pragma: no cover - persistence safety
        return {"ok": False, "error": "proposal_persistence_failed", "detail": str(exc)}


def _find_money_pool(client_file: Dict[str, Any], money_pool_id: Optional[str]) -> Optional[Dict[str, Any]]:
    pools = client_file.get("money_pools") if isinstance(client_file, dict) else None
    if not isinstance(pools, list):
        return None
    for pool in pools:
        if isinstance(pool, dict) and str(pool.get("id") or "") == str(money_pool_id or ""):
            return pool
    return None


def _allocation_snapshot_matches_reference(
    snapshot: Dict[str, Any],
    reference_id: str,
) -> bool:
    """Match an immutable analysis or one of its exact server-owned references."""

    assessment_ref = (
        snapshot.get("assessment_ref")
        if isinstance(snapshot.get("assessment_ref"), dict)
        else {}
    )
    candidate = str(reference_id or "").strip()
    return bool(
        candidate
        and candidate
        in {
            str(snapshot.get("analysis_id") or "").strip(),
            str(snapshot.get("allocation_id") or "").strip(),
            str(assessment_ref.get("assessment_id") or "").strip(),
            str(assessment_ref.get("money_pool_id") or "").strip(),
        }
    )
def _engine_required_unavailable(engine_name: str, reason: str) -> Dict[str, Any]:
    return {
        "material": False,
        "signal": "engine_required_unavailable",
        "reason": reason,
        "engine_name": engine_name,
        "engine_policy": "required",
    }


def _engine_required_failed(engine_name: str, reason: str) -> Dict[str, Any]:
    return {
        "material": False,
        "signal": "engine_required_failed",
        "reason": reason,
        "engine_name": engine_name,
        "engine_policy": "required",
    }


def _engine_required_unavailable_payload(engine_name: str, reason: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "error": "engine_required_unavailable",
        "reason": reason,
        "engine_name": engine_name,
        "engine_policy": "required",
    }


def _engine_required_failed_payload(engine_name: str, reason: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "error": "engine_required_failed",
        "reason": reason,
        "engine_name": engine_name,
        "engine_policy": "required",
    }


def map_asset_allocation_result_to_policy_fields(asset_allocation_result: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize asset allocation model output into IPS policy fields."""
    full_result = (
        asset_allocation_result.get("full_result")
        if isinstance(asset_allocation_result.get("full_result"), dict)
        else asset_allocation_result
    )
    from api.services.asset_allocation_artifact_adapter import normalize_asset_allocation_result

    full_result = normalize_asset_allocation_result(full_result)
    expected_return = _engine_number(full_result.get("portfolio_expected_return_pct"))
    expected_volatility = _engine_number(full_result.get("portfolio_expected_volatility_pct"))
    if expected_return is not None:
        expected_return = round(expected_return / 100.0 if abs(expected_return) > 1.0 else expected_return, 4)
    if expected_volatility is not None:
        expected_volatility = round(expected_volatility / 100.0 if abs(expected_volatility) > 1.0 else expected_volatility, 4)
    layers = full_result.get("layers") if isinstance(full_result.get("layers"), dict) else {}
    layer1 = layers.get("layer1") if isinstance(layers.get("layer1"), dict) else {}
    allocation = layer1.get("selected_weights") if isinstance(layer1.get("selected_weights"), dict) else {}
    if not allocation:
        by_asset_class = (
            (full_result.get("investment_allocations") or {}).get("by_asset_class") or {}
        )
        if isinstance(by_asset_class, dict):
            allocation = {
                str(label): payload.get("weight")
                for label, payload in by_asset_class.items()
                if isinstance(payload, dict) and payload.get("weight") is not None
            }
    securities = full_result.get("securities") if isinstance(full_result.get("securities"), list) else []
    return {
        "expected_return": expected_return,
        "expected_volatility": expected_volatility,
        "target_allocation": allocation,
        "recommended_securities": securities,
        "engine_run": {
            "engine_name": "asset_allocation_model",
            "status": "ready" if asset_allocation_result.get("success") else "failed",
            "inputs": {
                "total_investment": full_result.get("total_investment"),
            },
            "outputs": {
                "expected_return": expected_return,
                "expected_volatility": expected_volatility,
                "security_count": len(securities),
            },
        },
    }


def build_production_subagents() -> Tuple[FinancialPlanningAgentV2, InvestmentSolutionAgentV2, AssessmentRevalidationAgentV2]:
    """Build specialists with optional production model adapters."""
    cashflow_client: Optional[CashflowEngineClient] = None
    asset_allocation_client: Optional[AssetAllocationModelClient] = None
    if _engine_flag_enabled("AWM_CASHFLOW_MODEL_ENABLED"):
        cashflow_client = CashflowEngineClient(
            config=_load_engine_config(),
            enabled=True,
            request_timeout_seconds=_engine_request_timeout_seconds(
                "AWM_CASHFLOW_MODEL_TIMEOUT_SECONDS", 90
            ),
        )
    if _engine_flag_enabled("AWM_ASSET_ALLOCATION_MODEL_ENABLED"):
        asset_allocation_client = AssetAllocationModelClient(
            config=_load_engine_config(),
            enabled=True,
            request_timeout_seconds=_engine_request_timeout_seconds(
                "AWM_ASSET_ALLOCATION_MODEL_TIMEOUT_SECONDS", 60
            ),
        )
    fp_agent = FinancialPlanningAgentV2(cashflow_client=cashflow_client)
    ips_agent = InvestmentSolutionAgentV2(asset_allocation_client=asset_allocation_client)
    revalidation_agent = AssessmentRevalidationAgentV2(cashflow_client=cashflow_client)
    return fp_agent, ips_agent, revalidation_agent


def _engine_flag_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on", "required"}


def engine_policy(*, env_getter: Any = os.getenv) -> str:
    """How model-backed adapters behave when configured engines fail or are disabled."""
    return str(env_getter("AWM_ENGINE_POLICY", "graceful") or "graceful").strip().lower()


def engines_required(*, env_getter: Any = os.getenv) -> bool:
    return engine_policy(env_getter=env_getter) == "required"


def _engine_request_timeout_seconds(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _load_engine_config() -> Any:
    try:
        from advisor.llm.config import ToolLoopConfig

        return ToolLoopConfig.from_env()
    except Exception:  # pragma: no cover - env fallback
        from types import SimpleNamespace

        asset_allocation_model_url = os.getenv("ASSET_ALLOCATION_MODEL_URL", "http://localhost:8600")
        asset_allocation_model_api_key = os.getenv("ASSET_ALLOCATION_MODEL_API_KEY", "")
        asset_allocation_model_api_secret = (
            os.getenv("ASSET_ALLOCATION_MODEL_API_SECRET", "")
            or os.getenv("API_SECRET", "")
        )
        if (
            not asset_allocation_model_api_key
            and not asset_allocation_model_api_secret
            and (
                asset_allocation_model_url.startswith("http://localhost:")
                or asset_allocation_model_url.startswith("http://127.0.0.1:")
            )
        ):
            asset_allocation_model_api_secret = "local-dev-secret"
        if not asset_allocation_model_api_key and asset_allocation_model_api_secret:
            import hashlib
            import hmac

            asset_allocation_model_api_key = hmac.new(
                asset_allocation_model_api_secret.encode("utf-8"), b"api_key", hashlib.sha256
            ).hexdigest()

        return SimpleNamespace(
            cashflow_model_url=os.getenv("CASHFLOW_MODEL_URL", "http://localhost:8001"),
            cashflow_api_key=os.getenv("CASHFLOW_API_KEY", ""),
            asset_allocation_model_url=asset_allocation_model_url,
            asset_allocation_model_api_key=asset_allocation_model_api_key,
            asset_allocation_model_optimize_path=os.getenv("ASSET_ALLOCATION_MODEL_OPTIMIZE_PATH", "/asset-allocation/api/v1/optimize"),
        )


def _engine_http_session() -> Any:
    import requests

    return requests.Session()


def _merged_client_file_facts(client_file: Dict[str, Any]) -> Dict[str, Any]:
    """Merge draft then committed facts the same way cashflow readiness does."""

    if not isinstance(client_file, dict):
        return {}
    merged: Dict[str, Any] = {}
    draft_items = client_file.get("draft_facts") if isinstance(client_file.get("draft_facts"), list) else []
    for item in draft_items:
        if isinstance(item, dict) and isinstance(item.get("facts"), dict):
            merged.update(item["facts"])
    committed = client_file.get("facts") if isinstance(client_file.get("facts"), dict) else {}
    merged.update(committed)
    return merged


def _engine_number(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _first_engine_number(*values: Any) -> Optional[float]:
    return next(
        (number for number in (_engine_number(value) for value in values) if number is not None),
        None,
    )


def _education_horizon_from_facts(facts: Dict[str, Any]) -> Optional[str]:
    years = facts.get("college_years_until")
    if isinstance(years, (int, float)):
        return f"{int(years)} years"
    return None
