"""Persisted advisory/policy lifecycle transitions.

This module keeps the database state guard that used to live under the
root-level ``journeys`` package. The OpenAI Agents SDK now owns advisory
conversation flow selection, so this file intentionally does not define a
separate journey runtime, event router, or specialist-agent registry.

It only protects persisted ``journey_runs.state`` transitions that still back
policy activation, orphan cleanup, and auditability.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, Mapping, Optional


STATES: FrozenSet[str] = frozenset(
    {
        "collecting",
        "ready",
        "running",
        "completed",
        "activated",
        "abandoned",
        "declined",
    }
)


ALLOWED_TRANSITIONS: Dict[str, FrozenSet[str]] = {
    "collecting": frozenset({"ready", "abandoned"}),
    "ready": frozenset({"running", "abandoned"}),
    "running": frozenset({"completed", "abandoned"}),
    "completed": frozenset({"activated", "declined"}),
    "activated": frozenset(),
    "abandoned": frozenset(),
    "declined": frozenset(),
}


class InvalidStateTransition(Exception):
    """Raised when a persisted lifecycle transition is not allowed."""


class StaleJourneyLease(Exception):
    """Raised when a worker tries to write after losing its lease token."""


def is_valid_transition(current: str, new: str) -> bool:
    """Return True iff ``current -> new`` is an allowed persisted transition."""
    if current not in ALLOWED_TRANSITIONS:
        return False
    return new in ALLOWED_TRANSITIONS[current]


def transition_journey_state(
    journey_id: str,
    new_state: str,
    *,
    reason: str,
    extra_cols: Optional[Mapping[str, Any]] = None,
    expected_lease_owner_token: Optional[str] = None,
) -> None:
    """Move a ``journey_runs`` row to a new lifecycle state.

    The name keeps the table language for compatibility with existing storage,
    but the responsibility is now lifecycle persistence rather than agent-flow
    orchestration.
    """
    if new_state not in STATES:
        raise ValueError(f"Unknown state: {new_state!r}")
    if not reason or not reason.strip():
        raise ValueError("transition_journey_state requires a non-empty reason")

    from api.persistence import _get_pool, _pg_json, _safe_getconn  # type: ignore

    pool = _get_pool()
    if pool is None:
        print(
            f"[journey_lifecycle] (no-db) {journey_id} -> {new_state} "
            f"(reason={reason})",
            flush=True,
        )
        return

    extra: Dict[str, Any] = dict(extra_cols or {})

    conn = _safe_getconn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, lease_owner_token FROM journey_runs WHERE id = %s FOR UPDATE",
                (journey_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError(f"journey row {journey_id} not found")
            current = row[0]
            current_lease_owner_token = row[1]

            if (
                expected_lease_owner_token is not None
                and current_lease_owner_token != expected_lease_owner_token
            ):
                raise StaleJourneyLease(
                    f"journey {journey_id} lease owner mismatch "
                    f"(reason={reason!r})"
                )

            if not is_valid_transition(current, new_state):
                raise InvalidStateTransition(
                    f"Disallowed transition {current!r} -> {new_state!r} "
                    f"(reason={reason!r}). Allowed from {current!r}: "
                    f"{sorted(ALLOWED_TRANSITIONS.get(current, set()))}"
                )

            cols = ["state = %s"]
            values = [new_state]
            for col, val in extra.items():
                cols.append(f"{col} = %s")
                values.append(_pg_json(val) if isinstance(val, (dict, list)) else val)
            values.append(journey_id)
            cur.execute(
                f"UPDATE journey_runs SET {', '.join(cols)} WHERE id = %s",
                values,
            )
            conn.commit()
        print(
            f"[journey_lifecycle] {journey_id} {current} -> {new_state} "
            f"(reason={reason})",
            flush=True,
        )
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            pool.putconn(conn)
        except Exception:
            pass


__all__ = [
    "ALLOWED_TRANSITIONS",
    "InvalidStateTransition",
    "StaleJourneyLease",
    "STATES",
    "is_valid_transition",
    "transition_journey_state",
]
