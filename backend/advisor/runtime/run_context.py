"""AgentRunContext — typed context bag threaded through every agent boundary.


Today, ad-hoc parameters like `client_id` travel along every call signature.
This module replaces that pattern with a single dataclass passed by reference.
Future additions (request_id, abort signals, feature flags) become free.

Phase 1 scope is intentionally narrow — the per-phase exit-criterion checklist
keeps only the fields that have a current consumer:

    client_id, session_id, journey_id, request_id, actor, started_at, db,
    lease_owner_token

`feature_flags` and `abort_signal` from the §5.4 superset are deferred until a
concrete consumer exists (Phase 5+ for cooperative cancel; Phase 6+ for flags).

This module has zero side effects on import and no behavior change in Phase 1
beyond log enrichment — `request_id` is propagated to `prompt_logging` so log
lines now carry it. Existing flows must remain green.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional


Actor = Literal["companion", "voice_agent", "internal", "scheduled"]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _new_request_id() -> str:
    """Short opaque correlation id for log enrichment.

    Not a security boundary; just enough to grep one turn out of an NDJSON.
    """
    return uuid.uuid4().hex[:12]


@dataclass
class AgentRunContext:
    """Single bag carrying request-scoped identity through the agent stack.

    Instances are constructed at HTTP-boundary handlers (companion endpoint,
    journey endpoint, voice token endpoint, internal task workers) and passed
    by reference into services and agents.

    The `db` field defaults to None for tests / call sites that don't carry a
    handle today. Phase 1 does not require threading a real handle everywhere
    — the helpers in `backend/api/persistence/` are still module-level. This field
    is reserved for future testability work without forcing a wide refactor
    in this phase.
    """

    client_id: str
    actor: Actor = "internal"
    session_id: Optional[str] = None
    journey_id: Optional[str] = None
    request_id: str = field(default_factory=_new_request_id)
    started_at: datetime = field(default_factory=_now_utc)
    db: Any = None
    lease_owner_token: Optional[str] = None

    def child(
        self,
        *,
        journey_id: Optional[str] = None,
        actor: Optional[Actor] = None,
    ) -> "AgentRunContext":
        """Return a copy with selected fields overridden, keeping correlation.

        `request_id` is intentionally preserved so a single user turn that
        spawns multiple sub-runtimes (e.g. companion -> financial_planning)
        appears as one correlated unit in logs.
        """
        return AgentRunContext(
            client_id=self.client_id,
            actor=actor if actor is not None else self.actor,
            session_id=self.session_id,
            journey_id=journey_id if journey_id is not None else self.journey_id,
            request_id=self.request_id,
            started_at=self.started_at,
            db=self.db,
            lease_owner_token=self.lease_owner_token,
        )

    def log_fields(self) -> dict:
        """Compact correlation dict for log enrichment.

        Used by `prompt_logging.append_prompt_log(extra=ctx.log_fields())`.
        Only includes non-empty fields to keep log lines small.
        """
        out: dict = {
            "request_id": self.request_id,
            "client_id": self.client_id,
            "actor": self.actor,
        }
        if self.session_id:
            out["session_id"] = self.session_id
        if self.journey_id:
            out["journey_id"] = self.journey_id
        return out
