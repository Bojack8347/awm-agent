"""Lazy dependency factories for API handlers and background work."""

from __future__ import annotations

import logging
from typing import Optional

from advisor.llm.config import ToolLoopConfig
from advisor.runtime.task_definition import get_task_profile, list_task_profiles
from advisor.runtime.session_runtime import SessionRuntime, build_session_runtime
from advisor.tasks.client_profile.agent import ClientProfileExtractor
from advisor.tasks.knowledge_updater.agent import KnowledgeUpdaterAgent
from advisor.tasks.activation_mutation import ActivationMutator
from advisor.tasks.message_composer.agent import MessageComposerAgent
from advisor.tasks.conversation_compactor.agent import ConversationCompactorAgent
from advisor.instructions import task_prompt_dir
from api.services.diagnosis import DiagnosisService
from api.services.knowledge import KnowledgeService, CATEGORY_TO_SECTION as _CATEGORY_TO_SECTION, SECTION_TITLES as _SECTION_TITLES
from api.services.companion import CompanionService
from api.services.conversation_memory import ConversationMemoryService
from api.services.proactive import ProactiveService
from api.services.business_events import BusinessEventWorker

from .persistence import db_get_active_client_ids

_CLIENT_PROFILE_EXTRACTOR: Optional[ClientProfileExtractor] = None
_KNOWLEDGE_UPDATER: Optional[KnowledgeUpdaterAgent] = None
_ACTIVATION_MUTATOR: Optional[ActivationMutator] = None
_DIAGNOSIS_SERVICE: Optional[DiagnosisService] = None
_KNOWLEDGE_SERVICE: Optional[KnowledgeService] = None
_COMPANION_SERVICE: Optional[CompanionService] = None
_MESSAGE_COMPOSER: Optional[MessageComposerAgent] = None
_CONVERSATION_COMPACTOR: Optional[ConversationCompactorAgent] = None
_CONVERSATION_MEMORY_SERVICE: Optional[ConversationMemoryService] = None
_PROACTIVE_SERVICE: Optional[ProactiveService] = None
_BUSINESS_EVENT_WORKER: Optional[BusinessEventWorker] = None
_PLANNING_REFRESH_COORDINATOR: Optional[Any] = None
_SESSION_RUNTIME: Optional[SessionRuntime] = None
_SESSION_RUNTIME_INITIALIZED: bool = False
_ADVISOR_RUNTIME: Optional[object] = None
logger = logging.getLogger(__name__)


def get_client_profile_extractor() -> ClientProfileExtractor:
    """Lazily initialize the client profile extractor (knowledge layer)."""
    global _CLIENT_PROFILE_EXTRACTOR
    if _CLIENT_PROFILE_EXTRACTOR is None:
        config = ToolLoopConfig.from_env()
        profile = get_task_profile("client_profile")
        _CLIENT_PROFILE_EXTRACTOR = ClientProfileExtractor(
            llm_api_key=config.llm_api_key,
            model_chain=list(profile.chain),
            prompts_dir=task_prompt_dir("client_profile"),
            llm_timeout_ms=profile.llm_timeout_ms,
            temperature=profile.temperature,
            reasoning_effort=profile.reasoning_effort,
        )
    return _CLIENT_PROFILE_EXTRACTOR


def get_knowledge_updater() -> KnowledgeUpdaterAgent:
    """Lazily initialize the knowledge updater agent."""
    global _KNOWLEDGE_UPDATER
    if _KNOWLEDGE_UPDATER is None:
        config = ToolLoopConfig.from_env()
        profile = get_task_profile("knowledge_updater")
        cashflow_profile = get_task_profile("cashflow_mapper")
        _KNOWLEDGE_UPDATER = KnowledgeUpdaterAgent(
            llm_api_key=config.llm_api_key,
            model_chain=list(profile.chain),
            prompts_dir=task_prompt_dir("knowledge_updater"),
            llm_timeout_ms=profile.llm_timeout_ms,
            cashflow_prompts_dir=task_prompt_dir("cashflow_mapper"),
            cashflow_timeout_ms=cashflow_profile.llm_timeout_ms,
            cashflow_model_chain=list(cashflow_profile.chain),
            temperature=profile.temperature,
            cashflow_temperature=cashflow_profile.temperature,
            reasoning_effort=profile.reasoning_effort,
            cashflow_reasoning_effort=cashflow_profile.reasoning_effort,
        )
    return _KNOWLEDGE_UPDATER


def get_activation_mutator() -> ActivationMutator:
    """Lazily initialize the activation mutator (deriver + validator)."""
    global _ACTIVATION_MUTATOR
    if _ACTIVATION_MUTATOR is None:
        config = ToolLoopConfig.from_env()
        profile = get_task_profile("activation_mutation")
        _ACTIVATION_MUTATOR = ActivationMutator(
            llm_api_key=config.llm_api_key,
            model_chain=list(profile.chain),
            prompts_dir=task_prompt_dir("activation_mutation"),
            llm_timeout_ms=profile.llm_timeout_ms,
            temperature=profile.temperature,
            reasoning_effort=profile.reasoning_effort,
        )
    return _ACTIVATION_MUTATOR


def get_diagnosis_service() -> DiagnosisService:
    """Lazily initialize the SDK-backed diagnosis service."""
    global _DIAGNOSIS_SERVICE
    if _DIAGNOSIS_SERVICE is None:
        from advisor.agents.diagnosis import build_production_diagnosis_runner

        _DIAGNOSIS_SERVICE = DiagnosisService(build_production_diagnosis_runner())
    return _DIAGNOSIS_SERVICE


def get_knowledge_service() -> KnowledgeService:
    """Lazily initialize the knowledge service."""
    global _KNOWLEDGE_SERVICE
    if _KNOWLEDGE_SERVICE is None:
        _KNOWLEDGE_SERVICE = KnowledgeService(get_knowledge_updater)
    return _KNOWLEDGE_SERVICE


def get_companion_service() -> CompanionService:
    """Lazily initialize the companion service."""
    global _COMPANION_SERVICE
    if _COMPANION_SERVICE is None:
        from .operations import _commit_confirmed_pending
        _COMPANION_SERVICE = CompanionService(_commit_confirmed_pending)
    return _COMPANION_SERVICE


def get_message_composer() -> MessageComposerAgent:
    """Lazily initialize the message composer agent (proactive outbound messages)."""
    global _MESSAGE_COMPOSER
    if _MESSAGE_COMPOSER is None:
        config = ToolLoopConfig.from_env()
        profile = get_task_profile("message_composer")
        _MESSAGE_COMPOSER = MessageComposerAgent(
            llm_api_key=config.llm_api_key,
            model_chain=list(profile.chain),
            prompts_dir=task_prompt_dir("message_composer"),
            llm_timeout_ms=profile.llm_timeout_ms,
            temperature=profile.temperature,
            reasoning_effort=profile.reasoning_effort,
        )
    return _MESSAGE_COMPOSER


def get_conversation_compactor() -> ConversationCompactorAgent:
    """Lazily initialize the tool-free conversation compactor task."""
    global _CONVERSATION_COMPACTOR
    if _CONVERSATION_COMPACTOR is None:
        config = ToolLoopConfig.from_env()
        profile = get_task_profile("conversation_compactor")
        _CONVERSATION_COMPACTOR = ConversationCompactorAgent(
            llm_api_key=config.llm_api_key,
            model_chain=list(profile.chain),
            prompts_dir=task_prompt_dir("conversation_compactor"),
            llm_timeout_ms=profile.llm_timeout_ms,
            temperature=profile.temperature,
            reasoning_effort=profile.reasoning_effort,
        )
    return _CONVERSATION_COMPACTOR


def get_conversation_memory_service() -> ConversationMemoryService:
    """Lazily initialize durable conversation-memory orchestration."""
    global _CONVERSATION_MEMORY_SERVICE
    if _CONVERSATION_MEMORY_SERVICE is None:
        _CONVERSATION_MEMORY_SERVICE = ConversationMemoryService(
            compactor_getter=get_conversation_compactor,
        )
    return _CONVERSATION_MEMORY_SERVICE


def schedule_conversation_compaction(*, client_id: str, session_id: str) -> None:
    """Schedule non-client-visible memory maintenance after a persisted text turn."""
    from .state import _BACKGROUND_EXECUTOR

    def _run() -> None:
        try:
            get_conversation_memory_service().compact_if_needed(
                client_id=client_id,
                session_id=session_id,
                trigger="background",
            )
        except Exception:  # pragma: no cover - background safety boundary
            logger.exception(
                "Background conversation compaction failed",
                extra={
                    "client_id": client_id,
                    "session_id": session_id,
                    "trigger": "background",
                },
            )

    _BACKGROUND_EXECUTOR.submit(_run)


def get_proactive_service() -> ProactiveService:
    """Lazily initialize the proactive engagement service.

    Injected with the message composer getter and the active-client-ids
    provider. Orchestrates ProactivePlanner (deterministic triggers) →
    MessageComposer (LLM) → persistence (outreach log, outbound queue).
    """
    global _PROACTIVE_SERVICE
    if _PROACTIVE_SERVICE is None:
        _PROACTIVE_SERVICE = ProactiveService(
            get_composer_fn=get_message_composer,
            get_active_client_ids_fn=db_get_active_client_ids,
        )
    return _PROACTIVE_SERVICE


def get_business_event_worker() -> BusinessEventWorker:
    """Lazily initialize the business event outbox worker."""
    global _BUSINESS_EVENT_WORKER
    if _BUSINESS_EVENT_WORKER is None:
        _BUSINESS_EVENT_WORKER = BusinessEventWorker(
            diagnosis_service_factory=get_diagnosis_service,
            proactive_service_factory=get_proactive_service,
            planning_coordinator_factory=get_planning_refresh_coordinator,
        )
    return _BUSINESS_EVENT_WORKER


def get_planning_refresh_coordinator() -> Any:
    global _PLANNING_REFRESH_COORDINATOR
    if _PLANNING_REFRESH_COORDINATOR is None:
        from api.services.planning_refresh import PlanningRefreshCoordinator
        from client_file.interfaces import ClientStateViewReader

        _PLANNING_REFRESH_COORDINATOR = PlanningRefreshCoordinator(
            client_file_reader=ClientStateViewReader()
        )
    return _PLANNING_REFRESH_COORDINATOR


_SESSION_RUNTIME: Optional[SessionRuntime] = None
_SESSION_RUNTIME_INITIALIZED = False


def get_session_runtime() -> Optional[SessionRuntime]:
    """Lazily initialize the Redis-backed session runtime (optional).

    Returns None when REDIS_URL is unset or Redis is unreachable —
    the companion endpoint falls back to per-request DB reads.
    """
    global _SESSION_RUNTIME, _SESSION_RUNTIME_INITIALIZED
    if not _SESSION_RUNTIME_INITIALIZED:
        _SESSION_RUNTIME = build_session_runtime()
        _SESSION_RUNTIME_INITIALIZED = True
    return _SESSION_RUNTIME


def get_advisor_runtime() -> object:
    """Lazily initialize the advisor runtime."""

    global _ADVISOR_RUNTIME
    if _ADVISOR_RUNTIME is None:
        from advisor.runtime.service import build_production_advisor_runtime

        _ADVISOR_RUNTIME = build_production_advisor_runtime()
    return _ADVISOR_RUNTIME
