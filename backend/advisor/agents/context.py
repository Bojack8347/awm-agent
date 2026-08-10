"""Local dependencies and per-turn state for AWM SDK agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from advisor.tools.deterministic_tools.execution import ToolExecutor


@dataclass
class AwmAgentContext:
    """Data available to tools and hooks, but not sent automatically to the LLM."""

    client_id: str
    session_id: str
    user_message: str
    client_file: Dict[str, Any]
    tool_executor: ToolExecutor
    trace_id: str
    turn_id: str
    root_span_id: str
    channel: str = "text"
    allowed_tools: Optional[Set[str]] = None
    active_skills: Dict[str, str] = field(default_factory=dict)
    skill_candidates: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    artifact_context: Dict[str, Any] = field(default_factory=dict)
    trusted_action_context: Dict[str, Any] = field(default_factory=dict)
    background_jobs: List[Dict[str, Any]] = field(default_factory=list)
    conversation_summaries: List[Dict[str, Any]] = field(default_factory=list)
    tool_results: List[Dict[str, Any]] = field(default_factory=list)
    subagent_artifacts: List[Dict[str, Any]] = field(default_factory=list)
    trace_events: List[Dict[str, Any]] = field(default_factory=list)
    llm_calls: List[Dict[str, Any]] = field(default_factory=list)
    streamed_text_delta_emitted: bool = False
    streamed_text_buffer: str = ""
    streamed_output_text_done: bool = False
    proposal_claim_repair_attempted: bool = False
    assessment_claim_repair_attempted: bool = False
    cashflow_missing_input_repair_attempted: bool = False
    post_fact_continuation_action: Optional[str] = None
    post_fact_continuation_attempted: bool = False
    mid_turn_client_file_refresher: Optional[
        Callable[["AwmAgentContext", Dict[str, Any]], bool]
    ] = None
    mid_turn_commit_refresh_verified: Optional[bool] = None
    mid_turn_commit_refresh_reason: Optional[str] = None
