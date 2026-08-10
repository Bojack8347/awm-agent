"""Conversation-memory compaction orchestration."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from advisor.agents.runtime.token_budget import (
    chat_record_compact_fraction,
    chat_record_soft_pct,
    chat_record_token_budget,
    compact_safety_gap,
    conversation_memory_enabled,
    count_chat_record_tokens,
    count_message_tokens,
    count_text_tokens,
    summary_recompact_pct,
)
from api.persistence.companion import iter_companion_messages_since
from api.persistence.events import create_business_event
from api.persistence.conversation_memory import (
    conversation_memory_transaction,
    insert_summary,
    list_active_summaries,
    supersede_summaries,
    validate_session_ownership,
)


PROMPT_VERSION = "awm.conversation_summary.v1"
COMPACTION_COMPLETED_EVENT_TYPE = "conversation.compaction.completed"
COMPACTION_FAILED_EVENT_TYPE = "conversation.compaction.failed"
COMPACTION_AGGREGATE_TYPE = "conversation_memory"
logger = logging.getLogger(__name__)
_ANALYSIS_REFERENCE_RE = re.compile(
    r"\b(?P<prefix>cashflow|allocation)[_-][A-Za-z0-9][A-Za-z0-9_-]{2,159}\b",
    re.IGNORECASE,
)


def _summary_block_text(summaries: Sequence[Dict[str, Any]]) -> str:
    return json.dumps(
        [summary.get("summary") or {} for summary in summaries],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _source_hash(values: Iterable[Any]) -> str:
    encoded = json.dumps(
        list(values),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _has_unresolved_pending_intent(row: Dict[str, Any]) -> bool:
    metadata = row.get("metadata")
    return bool(isinstance(metadata, dict) and metadata.get("pending_intent"))


def select_tier_one_range(
    rows: Sequence[Dict[str, Any]],
    *,
    compact_fraction: Optional[float] = None,
    safety_gap: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Select the oldest token fraction, ending at a safe assistant boundary."""
    fraction = compact_fraction or chat_record_compact_fraction()
    gap = compact_safety_gap() if safety_gap is None else max(0, safety_gap)
    eligible = list(rows[:-gap] if gap else rows)
    if len(eligible) < 2:
        return []
    total_tokens = sum(
        count_message_tokens(
            {
                "role": str(row.get("role") or ""),
                "content": str(row.get("content") or ""),
            }
        )
        for row in rows
    )
    target = max(1, int(total_tokens * fraction))
    running = 0
    safe_boundaries: List[tuple[int, int]] = []
    for index, row in enumerate(eligible):
        running += count_message_tokens(
            {
                "role": str(row.get("role") or ""),
                "content": str(row.get("content") or ""),
            }
        )
        if str(
            row.get("role") or ""
        ).strip().lower() == "assistant" and not _has_unresolved_pending_intent(row):
            safe_boundaries.append((index, running))
    if not safe_boundaries:
        return []
    boundary = next(
        (index for index, token_count in safe_boundaries if token_count >= target),
        safe_boundaries[-1][0],
    )
    return eligible[: boundary + 1]


def _coverage_for_rows(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    first = rows[0]
    last = rows[-1]
    return {
        "from_message_id": str(first["id"]),
        "through_message_id": str(last["id"]),
        "from_date": str(first.get("created_at") or "")[:10],
        "through_date": str(last.get("created_at") or "")[:10],
        "message_count": len(rows),
    }


def _artifact_references(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
    references: List[Dict[str, str]] = []

    def add(domain: Any, analysis_id: Any, source_tool: Any) -> None:
        normalized_domain = str(domain or "").strip().lower()
        if normalized_domain == "allocation":
            normalized_domain = "asset_allocation"
        normalized_id = str(analysis_id or "").strip()
        if (
            normalized_domain not in {"cashflow", "asset_allocation"}
            or not normalized_id
        ):
            return
        references[:] = [
            item
            for item in references
            if not (
                item["domain"] == normalized_domain
                and item["analysis_id"] == normalized_id
            )
        ]
        references.append(
            {
                "domain": normalized_domain,
                "analysis_id": normalized_id,
                "source_tool": str(source_tool or "conversation_memory"),
            }
        )

    for row in rows:
        metadata = row.get("metadata")
        artifact_context = (
            metadata.get("artifact_context") if isinstance(metadata, dict) else None
        )
        if isinstance(artifact_context, dict):
            for item in artifact_context.get("references") or []:
                if isinstance(item, dict):
                    add(
                        item.get("domain"),
                        item.get("analysis_id"),
                        item.get("source_tool"),
                    )
        for match in _ANALYSIS_REFERENCE_RE.finditer(str(row.get("content") or "")):
            add(match.group("prefix"), match.group(0), "conversation_text")
    return references


def _summaries_are_contiguous(
    summaries: Sequence[Dict[str, Any]],
    all_rows: Sequence[Dict[str, Any]],
) -> bool:
    positions = {str(row.get("id")): index for index, row in enumerate(all_rows)}
    for previous, current in zip(summaries, summaries[1:]):
        previous_end = positions.get(str(previous.get("covered_through_message_id")))
        current_start = positions.get(str(current.get("covered_from_message_id")))
        if previous_end is None or current_start != previous_end + 1:
            return False
    return True


class ConversationMemoryService:
    def __init__(
        self,
        *,
        compactor_getter: Callable[[], Any],
        history_reader: Callable[
            ..., Iterable[Dict[str, Any]]
        ] = iter_companion_messages_since,
        event_writer: Callable[..., Any] = create_business_event,
    ) -> None:
        self._compactor_getter = compactor_getter
        self._history_reader = history_reader
        self._event_writer = event_writer

    def active_summaries(self, client_id: str, session_id: str) -> List[Dict[str, Any]]:
        return list_active_summaries(client_id, session_id)

    def compact_if_needed(
        self,
        *,
        client_id: str,
        session_id: str,
        force: bool = False,
        hard_only: bool = False,
        trigger: str = "manual",
    ) -> Optional[Dict[str, Any]]:
        if not conversation_memory_enabled():
            return None
        normalized_trigger = (
            trigger
            if trigger in {"background", "inline_hard", "manual"}
            else "manual"
        )
        stage = "validation"
        ownership_validated = False
        committed_summaries: List[Dict[str, Any]] = []
        inserted: Optional[Dict[str, Any]] = None
        try:
            with conversation_memory_transaction(session_id) as connection:
                if not validate_session_ownership(
                    client_id,
                    session_id,
                    connection=connection,
                ):
                    raise PermissionError(
                        "conversation session does not belong to client"
                    )
                ownership_validated = True
                active = list_active_summaries(
                    client_id,
                    session_id,
                    connection=connection,
                )
                boundary = (
                    active[-1]["covered_through_message_id"] if active else None
                )
                stage = "history_read"
                raw_rows = list(
                    self._history_reader(
                        session_id,
                        after_message_id=boundary,
                        page_size=500,
                        client_id=client_id,
                    )
                )
                total_tokens = count_chat_record_tokens(
                    raw_rows,
                    current_user_message="",
                    summary_block=_summary_block_text(active) if active else "",
                )
                threshold = (
                    chat_record_token_budget()
                    if hard_only
                    else int(chat_record_token_budget() * chat_record_soft_pct())
                )
                if not force and total_tokens <= threshold:
                    return None

                selected = select_tier_one_range(raw_rows)
                if not selected:
                    return None
                coverage = _coverage_for_rows(selected)
                stage = "tier_1"
                compacted = self._compactor_getter().compact(
                    selected,
                    1,
                    coverage=coverage,
                )
                inserted = insert_summary(
                    client_id=client_id,
                    session_id=session_id,
                    tier=1,
                    covered_from_message_id=coverage["from_message_id"],
                    covered_through_message_id=coverage["through_message_id"],
                    covered_from_created_at=str(selected[0]["created_at"]),
                    covered_through_created_at=str(selected[-1]["created_at"]),
                    source_message_count=len(selected),
                    source_hash=_source_hash(
                        (row.get("id"), row.get("role"), row.get("content"))
                        for row in selected
                    ),
                    source_summary_ids=[],
                    summary=compacted["summary"],
                    carried_artifact_references=_artifact_references(selected),
                    model=compacted.get("model_used"),
                    prompt_version=PROMPT_VERSION,
                    connection=connection,
                )
                committed_summaries.append(inserted)
                stage = "tier_2"
                replacement = self._recompact_summaries_if_needed(
                    client_id=client_id,
                    session_id=session_id,
                    connection=connection,
                )
                if replacement is not None:
                    committed_summaries.append(replacement)
        except Exception as exc:
            if ownership_validated:
                self._record_compaction_event(
                    client_id=client_id,
                    session_id=session_id,
                    trigger=normalized_trigger,
                    outcome="failed",
                    stage=stage,
                    error=exc,
                )
            raise

        self._record_compaction_event(
            client_id=client_id,
            session_id=session_id,
            trigger=normalized_trigger,
            outcome="compacted",
            stage="committed",
            summaries=committed_summaries,
        )
        return inserted

    def _record_compaction_event(
        self,
        *,
        client_id: str,
        session_id: str,
        trigger: str,
        outcome: str,
        stage: str,
        summaries: Sequence[Dict[str, Any]] = (),
        error: Optional[Exception] = None,
    ) -> None:
        event_type = (
            COMPACTION_COMPLETED_EVENT_TYPE
            if outcome == "compacted"
            else COMPACTION_FAILED_EVENT_TYPE
        )
        summary_payload = [
            {
                "tier": int(item["tier"]),
                "source_hash": str(item["source_hash"]),
                "covered_from_message_id": str(item["covered_from_message_id"]),
                "covered_through_message_id": str(
                    item["covered_through_message_id"]
                ),
                "source_message_count": int(item["source_message_count"]),
            }
            for item in summaries
        ]
        payload: Dict[str, Any] = {
            "outcome": outcome,
            "trigger": str(trigger or "manual"),
            "stage": stage,
        }
        event_key = None
        if outcome == "compacted":
            source_hashes = [item["source_hash"] for item in summary_payload]
            payload.update(
                {
                    "tiers": [item["tier"] for item in summary_payload],
                    "summaries": summary_payload,
                }
            )
            committed_hash = (
                source_hashes[0]
                if len(source_hashes) == 1
                else _source_hash(source_hashes)
            )
            event_key = (
                f"conv-compact:{client_id}:{session_id}:{committed_hash}"
            )
        elif error is not None:
            payload.update(
                {
                    "error_type": type(error).__name__,
                    "error": f"Conversation compaction failed during {stage}",
                }
            )
        try:
            written = self._event_writer(
                event_type=event_type,
                client_id=client_id,
                aggregate_type=COMPACTION_AGGREGATE_TYPE,
                aggregate_id=f"{client_id}:{session_id}",
                event_source="conversation_memory",
                event_key=event_key,
                status="consumed",
                payload=payload,
            )
            if written is None:
                logger.warning(
                    "Conversation compaction event could not be persisted",
                    extra={
                        "client_id": client_id,
                        "session_id": session_id,
                        "trigger": trigger,
                        "outcome": outcome,
                    },
                )
        except Exception:
            logger.exception(
                "Conversation compaction event write failed",
                extra={
                    "client_id": client_id,
                    "session_id": session_id,
                    "trigger": trigger,
                    "outcome": outcome,
                },
            )

    def _recompact_summaries_if_needed(
        self,
        *,
        client_id: str,
        session_id: str,
        connection: Any,
    ) -> Optional[Dict[str, Any]]:
        active = list_active_summaries(
            client_id,
            session_id,
            connection=connection,
        )
        if len(active) < 2:
            return None
        summary_tokens = count_text_tokens(_summary_block_text(active))
        threshold = int(chat_record_token_budget() * summary_recompact_pct())
        if summary_tokens < threshold:
            return None

        all_rows = list(
            self._history_reader(
                session_id,
                after_message_id=None,
                page_size=500,
                client_id=client_id,
            )
        )
        if not _summaries_are_contiguous(active, all_rows):
            raise ValueError(
                "tier-2 summaries must cover adjacent, non-overlapping ranges"
            )

        first = active[0]
        last = active[-1]
        coverage = {
            "from_message_id": first["covered_from_message_id"],
            "through_message_id": last["covered_through_message_id"],
            "from_date": str(first["covered_from_created_at"])[:10],
            "through_date": str(last["covered_through_created_at"])[:10],
            "message_count": sum(int(item["source_message_count"]) for item in active),
        }
        source_ids = [item["id"] for item in active]
        compacted = self._compactor_getter().compact(
            [{"id": item["id"], **dict(item.get("summary") or {})} for item in active],
            2,
            coverage=coverage,
            source_summary_ids=source_ids,
        )
        carried: List[Dict[str, Any]] = []
        for item in active:
            for reference in item.get("carried_artifact_references") or []:
                if reference not in carried:
                    carried.append(reference)
        replacement = insert_summary(
            client_id=client_id,
            session_id=session_id,
            tier=2,
            covered_from_message_id=coverage["from_message_id"],
            covered_through_message_id=coverage["through_message_id"],
            covered_from_created_at=first["covered_from_created_at"],
            covered_through_created_at=last["covered_through_created_at"],
            source_message_count=coverage["message_count"],
            source_hash=_source_hash(
                (item["id"], item["source_hash"]) for item in active
            ),
            source_summary_ids=source_ids,
            summary=compacted["summary"],
            carried_artifact_references=carried,
            model=compacted.get("model_used"),
            prompt_version=PROMPT_VERSION,
            connection=connection,
        )
        updated = supersede_summaries(
            client_id=client_id,
            session_id=session_id,
            summary_ids=source_ids,
            superseded_by=replacement["id"],
            connection=connection,
        )
        if updated != len(source_ids):
            raise RuntimeError(
                "tier-2 supersession did not update every source summary"
            )
        return replacement
