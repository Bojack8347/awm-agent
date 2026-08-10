"""
SessionRuntime: Redis-backed per-session context cache for the companion endpoint.

Eliminates repeated DB reads for stable per-session data (knowledge snapshot,
diagnosis snapshot, recent turns, dismissed facts) that are fetched fresh from
Postgres on every request in the baseline implementation.

Redis key: awm:session:{session_id}
TTL: SESSION_TTL_SECONDS (default 4 h)

Design principles:
- Completely optional: if Redis is unavailable or REDIS_URL is unset, every
  method degrades silently and the caller falls back to direct DB reads.
- Explicit invalidation: snapshots are not expired by TTL alone. When truth
  changes within a request (fact committed, diagnosis refreshed), the caller
  marks the affected cache slot as invalid so the next request re-fetches.
- Future-ready key structure: awm:session:{session_id} partitions naturally by
  session. Adding user-level keys (awm:user:{user_id}:session:{session_id}) is
  a drop-in extension when multi-user support is needed.

NOTE (Apr 2026): Cloud Memorystore Redis instance was deleted to reduce GCP costs
(was ~$74/month). The system falls back to direct PostgreSQL reads with no
functional impact — only a minor per-message latency increase (~10–50ms), which
is imperceptible given AI pipeline latency (1–5s).

To re-enable Redis for performance optimisation:
  1. Create a new Cloud Memorystore for Redis instance (GCP Console → Memorystore)
  2. Set REDIS_URL env var in Cloud Run: redis://<host>:<port>
  3. No code changes needed — this module picks it up automatically.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

RECENT_TURNS_LIMIT = 12
SESSION_TTL_SECONDS = 4 * 3600   # 4 hours

@dataclass
class CompanionSessionCache:
    """In-memory representation of one session's cached context.

    knowledge_snapshot / diagnosis_snapshot / dismissed_facts each carry a
    *_valid flag.  When False the caller must re-fetch from DB before use and
    then set the flag back to True.
    """

    knowledge_snapshot: Optional[Dict[str, Any]]
    knowledge_snapshot_valid: bool

    diagnosis_snapshot: Optional[Dict[str, Any]]
    diagnosis_snapshot_valid: bool

    dismissed_facts: List[Dict[str, Any]]
    dismissed_facts_valid: bool

    # last RECENT_TURNS_LIMIT messages (role + content only; no DB ids)
    recent_turns: List[Dict[str, Any]] = field(default_factory=list)

    # OpenAI Responses API anchor — threaded across turns
    previous_response_id: Optional[str] = None


def _record_version(record: Optional[Dict[str, Any]]) -> int:
    """Return the stable version number carried by a snapshot-like record."""
    if not isinstance(record, dict):
        return 0
    for key in ("version", "snapshot_version", "diagnosis_snapshot_version"):
        value = record.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return 0


def companion_layer2_anchor_changed(
    cache: CompanionSessionCache,
    *,
    latest_knowledge_snapshot: Optional[Dict[str, Any]],
    latest_diagnosis_snapshot: Optional[Dict[str, Any]],
) -> bool:
    """Return True when a stored Responses anchor no longer matches Layer 2.

    The OpenAI `previous_response_id` anchor is only valid while the large
    stable Layer 2 context is unchanged. If the session cache says either
    snapshot slot is invalid, or if the live snapshot versions differ from
    the cached versions, the next companion turn must send the full prompt
    and clear the anchor.
    """
    if not cache.knowledge_snapshot_valid or not cache.diagnosis_snapshot_valid:
        return True
    return (
        _record_version(cache.knowledge_snapshot)
        != _record_version(latest_knowledge_snapshot)
        or _record_version(cache.diagnosis_snapshot)
        != _record_version(latest_diagnosis_snapshot)
    )


class SessionRuntime:
    """Load, mutate, and persist a CompanionSessionCache via Redis."""

    KEY_PREFIX = "awm:session:"

    def __init__(self, redis_url: str) -> None:
        import redis  # optional dependency
        self._r = redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=2)

    # ------------------------------------------------------------------
    # Load / save
    # ------------------------------------------------------------------

    def load(self, session_id: str) -> Optional[CompanionSessionCache]:
        """Return cached session state, or None on cache miss / Redis error."""
        try:
            raw = self._r.get(self.KEY_PREFIX + session_id)
            if not raw:
                return None
            data = json.loads(raw)
            return CompanionSessionCache(
                knowledge_snapshot=data.get("knowledge_snapshot"),
                knowledge_snapshot_valid=bool(data.get("knowledge_snapshot_valid", False)),
                diagnosis_snapshot=data.get("diagnosis_snapshot"),
                diagnosis_snapshot_valid=bool(data.get("diagnosis_snapshot_valid", False)),
                dismissed_facts=data.get("dismissed_facts") or [],
                dismissed_facts_valid=bool(data.get("dismissed_facts_valid", False)),
                recent_turns=data.get("recent_turns") or [],
                previous_response_id=data.get("previous_response_id"),
            )
        except Exception as exc:
            print(f"[session_runtime] load failed session={session_id}: {exc}", flush=True)
            return None

    def save(self, session_id: str, cache: CompanionSessionCache) -> None:
        """Persist cache to Redis with TTL refresh.  Fails silently."""
        try:
            self._r.setex(
                self.KEY_PREFIX + session_id,
                SESSION_TTL_SECONDS,
                json.dumps(asdict(cache)),
            )
        except Exception as exc:
            print(f"[session_runtime] save failed session={session_id}: {exc}", flush=True)

    def delete(self, session_id: str) -> None:
        """Remove session cache (e.g. on explicit session reset)."""
        try:
            self._r.delete(self.KEY_PREFIX + session_id)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Mutations (operate on the in-memory cache object; caller calls save)
    # ------------------------------------------------------------------

    def append_turn(
        self,
        cache: CompanionSessionCache,
        user_message: str,
        assistant_message: str,
        response_id: Optional[str],
    ) -> None:
        """Add current turn to the sliding recent-turns window."""
        cache.recent_turns.append({"role": "user", "content": user_message, "metadata": {}})
        cache.recent_turns.append({"role": "assistant", "content": assistant_message, "metadata": {}})
        if len(cache.recent_turns) > RECENT_TURNS_LIMIT:
            cache.recent_turns = cache.recent_turns[-RECENT_TURNS_LIMIT:]
        cache.previous_response_id = response_id

    def invalidate_knowledge(self, cache: CompanionSessionCache) -> None:
        """Mark knowledge + diagnosis snapshots as stale.

        Call this when the current request committed new facts so the next
        request re-fetches both snapshots from Postgres.
        """
        cache.knowledge_snapshot_valid = False
        cache.diagnosis_snapshot_valid = False

    def invalidate_dismissed_facts(self, cache: CompanionSessionCache) -> None:
        """Mark dismissed facts as stale (call when a fact is dismissed)."""
        cache.dismissed_facts_valid = False

def build_session_runtime() -> Optional[SessionRuntime]:
    """Construct a SessionRuntime from REDIS_URL env var.

    Returns None (with a warning) if REDIS_URL is not set or Redis is
    unreachable — callers then fall back to per-request DB reads.
    """
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        return None
    try:
        rt = SessionRuntime(redis_url)
        rt._r.ping()
        return rt
    except Exception as exc:
        print(f"[session_runtime] Redis unavailable ({exc}); session caching disabled.", flush=True)
        return None
