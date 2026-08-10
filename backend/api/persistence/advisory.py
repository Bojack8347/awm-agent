"""Persistence helpers for domain-neutral advisory records.

The existing investment journey path still writes `journey_runs.solution_output`
for backward compatibility. These helpers create the new advisory records in
parallel so the product can trace the durable chain:

    client state -> engine run -> advisory plan -> artifact -> decision/event
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from .core import _get_pool, _pg_json, _safe_getconn


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value else None


def _coerce_domain(journey_type: str) -> str:
    value = str(journey_type or "").strip().lower()
    if value == "investment":
        return "investment"
    return value or "advisory"


def _extract_title_and_summary(solution_output: Dict[str, Any]) -> tuple[str, str]:
    menu = solution_output.get("menu") if isinstance(solution_output, dict) else {}
    detail = solution_output.get("detail") if isinstance(solution_output, dict) else {}
    title = ""
    summary = ""
    if isinstance(menu, dict):
        title = str(menu.get("title") or "").strip()
        summary = str(menu.get("summary") or "").strip()
    if isinstance(detail, dict):
        if not title:
            title = str(detail.get("title") or "").strip()
        if not summary:
            summary = str(detail.get("summary") or detail.get("rationale") or "").strip()
    return title, summary


def ensure_advisory_plan_from_journey(
    *,
    journey_id: str,
    client_id: str,
    journey_type: str,
    solution_output: Dict[str, Any],
    collected_fields: Optional[Dict[str, Any]] = None,
    baseline_snapshot_version: Optional[int] = None,
    generated_at: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Create the advisory plan/artifact mirror for a completed journey.

    The operation is idempotent per `journey_id`. If a plan already exists,
    the existing plan is returned without creating a duplicate artifact.
    """
    pool = _get_pool()
    if pool is None:
        return None
    if not isinstance(solution_output, dict) or not solution_output:
        return None

    domain = _coerce_domain(journey_type)
    title, summary = _extract_title_and_summary(solution_output)
    collected = collected_fields if isinstance(collected_fields, dict) else {}
    input_summary = {
        "journey_id": journey_id,
        "journey_type": journey_type,
        "collected_fields": collected,
        "baseline_snapshot_version": baseline_snapshot_version,
    }
    output_summary = {
        "title": title,
        "summary": summary,
        "has_menu": isinstance(solution_output.get("menu"), dict),
        "has_detail": isinstance(solution_output.get("detail"), dict),
    }

    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT p.id, p.current_artifact_id, p.engine_run_id,
                           p.status, p.created_at, p.updated_at
                    FROM advisory_plans p
                    WHERE p.journey_id = %s
                    """,
                    (journey_id,),
                )
                existing = cur.fetchone()
                if existing:
                    conn.commit()
                    return {
                        "id": str(existing[0]),
                        "advisory_plan_id": str(existing[0]),
                        "current_artifact_id": str(existing[1]) if existing[1] else None,
                        "engine_run_id": str(existing[2]) if existing[2] else None,
                        "status": existing[3],
                        "created_at": _iso(existing[4]),
                        "updated_at": _iso(existing[5]),
                        "created": False,
                    }

                engine_run_id = str(uuid.uuid4())
                plan_id = str(uuid.uuid4())
                artifact_id = str(uuid.uuid4())
                cur.execute(
                    """
                    INSERT INTO engine_runs
                        (id, client_id, domain, engine_name, engine_version, status,
                         journey_id, input_summary, output_summary,
                         based_on_client_state_version,
                         based_on_client_state_as_of_time,
                         completed_at)
                    VALUES
                        (%s, %s, %s, %s, %s, 'completed',
                         %s, %s::jsonb, %s::jsonb,
                         %s, %s, COALESCE(%s::timestamptz, NOW()))
                    """,
                    (
                        engine_run_id,
                        client_id,
                        domain,
                        "journey_completion_pipeline",
                        "v1",
                        journey_id,
                        _pg_json(input_summary),
                        _pg_json(output_summary),
                        baseline_snapshot_version,
                        generated_at,
                        generated_at,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO advisory_plans
                        (id, client_id, domain, source_type, journey_id,
                         engine_run_id, status, title, summary,
                         based_on_client_state_version,
                         based_on_client_state_as_of_time,
                         generated_at, metadata)
                    VALUES
                        (%s, %s, %s, 'engine', %s,
                         %s, 'proposed', %s, %s,
                         %s, %s, COALESCE(%s::timestamptz, NOW()), %s::jsonb)
                    """,
                    (
                        plan_id,
                        client_id,
                        domain,
                        journey_id,
                        engine_run_id,
                        title,
                        summary,
                        baseline_snapshot_version,
                        generated_at,
                        generated_at,
                        _pg_json({
                            "source": "journey_completion",
                            "request_id": request_id,
                        }),
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO advisory_artifacts
                        (id, advisory_plan_id, artifact_type, version, payload,
                         source_journey_id, engine_run_id, created_at)
                    VALUES
                        (%s, %s, 'plan_solution', 1, %s::jsonb,
                         %s, %s, COALESCE(%s::timestamptz, NOW()))
                    """,
                    (
                        artifact_id,
                        plan_id,
                        _pg_json(solution_output),
                        journey_id,
                        engine_run_id,
                        generated_at,
                    ),
                )
                cur.execute(
                    """
                    UPDATE advisory_plans
                       SET current_artifact_id = %s,
                           updated_at = NOW()
                     WHERE id = %s
                    """,
                    (artifact_id, plan_id),
                )
            conn.commit()
            return {
                "id": plan_id,
                "advisory_plan_id": plan_id,
                "current_artifact_id": artifact_id,
                "engine_run_id": engine_run_id,
                "status": "proposed",
                "created": True,
            }
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] ensure_advisory_plan_from_journey failed: {exc}", flush=True)
        return None


def mark_advisory_plan_status_for_journey(
    *,
    journey_id: str,
    status: str,
    decision: Optional[str] = None,
    decision_payload: Optional[Dict[str, Any]] = None,
    decided_at: Optional[str] = None,
) -> bool:
    """Update the advisory plan mirrored from a journey and optionally record a decision."""
    pool = _get_pool()
    if pool is None:
        return False
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE advisory_plans
                       SET status = %s,
                           updated_at = NOW()
                     WHERE journey_id = %s
                     RETURNING id, client_id
                    """,
                    (status, journey_id),
                )
                row = cur.fetchone()
                if row is None:
                    conn.commit()
                    return False
                plan_id, client_id = str(row[0]), row[1]
                if decision:
                    cur.execute(
                        """
                        INSERT INTO advisory_decisions
                            (client_id, advisory_plan_id, decision, payload, decided_at)
                        VALUES (%s, %s, %s, %s::jsonb, COALESCE(%s::timestamptz, NOW()))
                        """,
                        (
                            client_id,
                            plan_id,
                            decision,
                            _pg_json(decision_payload or {}),
                            decided_at,
                        ),
                    )
            conn.commit()
            return True
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] mark_advisory_plan_status_for_journey failed: {exc}", flush=True)
        return False


def update_advisory_plan_status(
    *,
    advisory_plan_id: str,
    client_id: Optional[str] = None,
    status: str,
    metadata_patch: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Update an advisory plan status without going through legacy Journey."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                params: List[Any] = [status, _pg_json(metadata_patch or {}), advisory_plan_id]
                client_clause = ""
                if client_id:
                    client_clause = "AND client_id = %s"
                    params.append(client_id)
                cur.execute(
                    f"""
                    UPDATE advisory_plans
                       SET status = %s,
                           metadata = metadata || %s::jsonb,
                           updated_at = NOW()
                     WHERE id = %s {client_clause}
                     RETURNING id, client_id, domain, source_type, journey_id,
                               engine_run_id, current_artifact_id, status,
                               title, summary, based_on_client_state_version,
                               based_on_client_state_as_of_time, generated_at,
                               effective_at, valid_from, valid_to,
                               metadata, created_at, updated_at
                    """,
                    tuple(params),
                )
                row = cur.fetchone()
            conn.commit()
            return _plan_row(row) if row else None
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] update_advisory_plan_status failed: {exc}", flush=True)
        return None


def get_advisory_plan_for_journey(journey_id: str) -> Optional[Dict[str, Any]]:
    """Fetch the advisory mirror for a journey, including current artifact payload."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT p.id, p.client_id, p.domain, p.status, p.title, p.summary,
                           p.journey_id, p.engine_run_id, p.current_artifact_id,
                           p.based_on_client_state_version,
                           p.based_on_client_state_as_of_time,
                           p.generated_at, p.created_at, p.updated_at,
                           a.version, a.payload
                    FROM advisory_plans p
                    LEFT JOIN advisory_artifacts a ON a.id = p.current_artifact_id
                    WHERE p.journey_id = %s
                    """,
                    (journey_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return {
                    "id": str(row[0]),
                    "client_id": row[1],
                    "domain": row[2],
                    "status": row[3],
                    "title": row[4],
                    "summary": row[5],
                    "journey_id": str(row[6]) if row[6] else None,
                    "engine_run_id": str(row[7]) if row[7] else None,
                    "current_artifact_id": str(row[8]) if row[8] else None,
                    "based_on_client_state_version": row[9],
                    "based_on_client_state_as_of_time": _iso(row[10]),
                    "generated_at": _iso(row[11]),
                    "created_at": _iso(row[12]),
                    "updated_at": _iso(row[13]),
                    "artifact_version": row[14],
                    "artifact_payload": row[15] or {},
                }
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] get_advisory_plan_for_journey failed: {exc}", flush=True)
        return None


def list_advisory_plans(
    *,
    client_id: str,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """List native advisory plans for one client."""
    pool = _get_pool()
    if pool is None:
        return []
    limit = max(1, min(int(limit or 50), 100))
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                if status:
                    cur.execute(
                        """
                        SELECT p.id, p.client_id, p.domain, p.source_type, p.journey_id,
                               p.engine_run_id, p.current_artifact_id, p.status,
                               p.title, p.summary, p.based_on_client_state_version,
                               p.based_on_client_state_as_of_time, p.generated_at,
                               p.effective_at, p.valid_from, p.valid_to,
                               p.metadata, p.created_at, p.updated_at
                        FROM advisory_plans p
                        WHERE p.client_id = %s AND p.status = %s
                        ORDER BY p.generated_at DESC, p.created_at DESC
                        LIMIT %s
                        """,
                        (client_id, status, limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT p.id, p.client_id, p.domain, p.source_type, p.journey_id,
                               p.engine_run_id, p.current_artifact_id, p.status,
                               p.title, p.summary, p.based_on_client_state_version,
                               p.based_on_client_state_as_of_time, p.generated_at,
                               p.effective_at, p.valid_from, p.valid_to,
                               p.metadata, p.created_at, p.updated_at
                        FROM advisory_plans p
                        WHERE p.client_id = %s
                        ORDER BY p.generated_at DESC, p.created_at DESC
                        LIMIT %s
                        """,
                        (client_id, limit),
                    )
                return [_plan_row(row) for row in cur.fetchall()]
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] list_advisory_plans failed: {exc}", flush=True)
        return []


def get_advisory_plan(
    *,
    advisory_plan_id: str,
    client_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch one advisory plan with its current artifact payload."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                params: List[Any] = [advisory_plan_id]
                client_clause = ""
                if client_id:
                    client_clause = "AND p.client_id = %s"
                    params.append(client_id)
                cur.execute(
                    f"""
                    SELECT p.id, p.client_id, p.domain, p.source_type, p.journey_id,
                           p.engine_run_id, p.current_artifact_id, p.status,
                           p.title, p.summary, p.based_on_client_state_version,
                           p.based_on_client_state_as_of_time, p.generated_at,
                           p.effective_at, p.valid_from, p.valid_to,
                           p.metadata, p.created_at, p.updated_at,
                           a.version, a.payload
                    FROM advisory_plans p
                    LEFT JOIN advisory_artifacts a ON a.id = p.current_artifact_id
                    WHERE p.id = %s {client_clause}
                    """,
                    tuple(params),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                plan = _plan_row(row[:19])
                plan["artifact_version"] = row[19]
                plan["artifact_payload"] = row[20] or {}
                return plan
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] get_advisory_plan failed: {exc}", flush=True)
        return None


def list_advisory_artifacts(
    *,
    advisory_plan_id: str,
    client_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List artifacts for a plan, newest version first."""
    pool = _get_pool()
    if pool is None:
        return []
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                params: List[Any] = [advisory_plan_id]
                client_clause = ""
                if client_id:
                    client_clause = "AND p.client_id = %s"
                    params.append(client_id)
                cur.execute(
                    f"""
                    SELECT a.id, a.advisory_plan_id, a.artifact_type, a.version,
                           a.payload, a.source_journey_id, a.engine_run_id,
                           a.effective_at, a.valid_from, a.valid_to, a.created_at
                    FROM advisory_artifacts a
                    JOIN advisory_plans p ON p.id = a.advisory_plan_id
                    WHERE a.advisory_plan_id = %s {client_clause}
                    ORDER BY a.version DESC, a.created_at DESC
                    """,
                    tuple(params),
                )
                return [_artifact_row(row) for row in cur.fetchall()]
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] list_advisory_artifacts failed: {exc}", flush=True)
        return []


def record_advisory_decision(
    *,
    advisory_plan_id: str,
    client_id: str,
    decision: str,
    reason: Optional[str] = None,
    reason_code: Optional[str] = None,
    free_text_reason: Optional[str] = None,
    revisit_at: Optional[str] = None,
    source_type: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    decided_at: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Record a user's decision and update the plan status."""
    pool = _get_pool()
    if pool is None:
        return None
    normalized = str(decision or "").strip().lower()
    if not normalized:
        return None
    status_map = {
        "accept": "accepted",
        "accepted": "accepted",
        "activate": "accepted",
        "activated": "accepted",
        "decline": "declined",
        "declined": "declined",
        "defer": "deferred",
        "deferred": "deferred",
        "cancel": "cancelled",
        "cancelled": "cancelled",
        "review": "review_requested",
        "review_requested": "review_requested",
    }
    plan_status = status_map.get(normalized, normalized)
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE advisory_plans
                       SET status = %s,
                           updated_at = NOW()
                     WHERE id = %s AND client_id = %s
                     RETURNING id
                    """,
                    (plan_status, advisory_plan_id, client_id),
                )
                if cur.fetchone() is None:
                    conn.commit()
                    return None
                cur.execute(
                    """
                    INSERT INTO advisory_decisions
                        (client_id, advisory_plan_id, decision, reason,
                         reason_code, free_text_reason, revisit_at, source_type,
                         payload, decided_at)
                    VALUES
                        (%s, %s, %s, %s,
                         %s, %s, %s::timestamptz, %s,
                         %s::jsonb, COALESCE(%s::timestamptz, NOW()))
                    RETURNING id, client_id, advisory_plan_id, decision, reason,
                              reason_code, free_text_reason, revisit_at,
                              source_type, payload, decided_at, created_at
                    """,
                    (
                        client_id,
                        advisory_plan_id,
                        normalized,
                        reason,
                        reason_code,
                        free_text_reason,
                        revisit_at,
                        source_type,
                        _pg_json(payload or {}),
                        decided_at,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
            return _decision_row(row)
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] record_advisory_decision failed: {exc}", flush=True)
        return None


def list_advisory_policies(
    *,
    client_id: str,
    statuses: List[str],
) -> List[Dict[str, Any]]:
    """Return advisory-backed policies in the legacy mobile policy shape.

    The mobile app currently opens proposal/detail screens with `journey_id`.
    For journey-backed advisory plans we therefore keep `id = journey_id` and
    attach advisory IDs as metadata. Future non-journey plans should get their
    own endpoint before being exposed to this legacy route.
    """
    pool = _get_pool()
    if pool is None:
        return []
    normalized = [str(s).strip() for s in statuses if str(s).strip()]
    if not normalized:
        return []
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT p.id, p.client_id, p.domain, p.status, p.journey_id,
                           p.engine_run_id, p.current_artifact_id, p.title, p.summary,
                           p.based_on_client_state_version,
                           p.based_on_client_state_as_of_time,
                           p.generated_at, p.created_at, p.updated_at,
                           a.version, a.payload,
                           j.state, j.activated_at, j.activation_snapshot_version,
                           j.created_at, j.completed_at
                    FROM advisory_plans p
                    JOIN advisory_artifacts a ON a.id = p.current_artifact_id
                    LEFT JOIN journey_runs j ON j.id = p.journey_id
                    WHERE p.client_id = %s
                      AND p.status = ANY(%s)
                      AND p.journey_id IS NOT NULL
                    ORDER BY COALESCE(j.activated_at, j.completed_at, p.generated_at, p.created_at) DESC
                    """,
                    (client_id, normalized),
                )
                rows = cur.fetchall()
                policies: List[Dict[str, Any]] = []
                for r in rows:
                    advisory_status = r[3]
                    journey_state = r[16] or (
                        "activated" if advisory_status in ("accepted", "activated") else "completed"
                    )
                    policy_status = (
                        "activated"
                        if advisory_status in ("accepted", "activated")
                        else "proposed"
                    )
                    journey_id = str(r[4]) if r[4] else str(r[0])
                    policies.append({
                        "id": journey_id,
                        "client_id": r[1],
                        "journey_type": r[2],
                        "state": journey_state,
                        "status": journey_state,
                        "policy_status": policy_status,
                        "solution_output": r[15] or {},
                        "activated_at": _iso(r[17]),
                        "activation_snapshot_version": r[18],
                        "created_at": _iso(r[19]) or _iso(r[12]),
                        "completed_at": _iso(r[20]) or _iso(r[11]),
                        "advisory_plan_id": str(r[0]),
                        "advisory_artifact_id": str(r[6]) if r[6] else None,
                        "engine_run_id": str(r[5]) if r[5] else None,
                        "advisory_status": advisory_status,
                        "artifact_version": r[14],
                        "based_on_client_state_version": r[9],
                        "based_on_client_state_as_of_time": _iso(r[10]),
                    })
                return policies
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] list_advisory_policies failed: {exc}", flush=True)
        return []


def create_advisory_event(
    *,
    client_id: str,
    event_type: str,
    source_type: str,
    advisory_plan_id: Optional[str] = None,
    source_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    status: str = "open",
    event_time: Optional[str] = None,
    detected_at: Optional[str] = None,
    effective_at: Optional[str] = None,
    dedupe_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Create an advisory event from RM action, system detection, or plan change."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO advisory_events
                        (client_id, advisory_plan_id, event_type, source_type,
                         source_id, status, event_time, detected_at,
                         effective_at, payload, dedupe_key)
                    VALUES
                        (%s, %s, %s, %s,
                         %s, %s, COALESCE(%s::timestamptz, NOW()),
                         COALESCE(%s::timestamptz, NOW()), %s::timestamptz,
                         %s::jsonb, %s)
                    ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL
                    DO UPDATE SET detected_at = advisory_events.detected_at
                    RETURNING id, client_id, advisory_plan_id, event_type,
                              source_type, source_id, status, event_time,
                              detected_at, effective_at, payload, created_at
                    """,
                    (
                        client_id,
                        advisory_plan_id,
                        event_type,
                        source_type,
                        source_id,
                        status,
                        event_time,
                        detected_at,
                        effective_at,
                        _pg_json(payload or {}),
                        dedupe_key,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
            return _event_row(row)
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] create_advisory_event failed: {exc}", flush=True)
        return None


def create_advisory_holding(
    *,
    client_id: str,
    advisory_plan_id: Optional[str] = None,
    domain: str = "investment",
    holding_type: str = "external",
    status: str = "active",
    title: Optional[str] = None,
    institution: Optional[str] = None,
    symbol: Optional[str] = None,
    quantity: Optional[Any] = None,
    market_value: Optional[Any] = None,
    currency: str = "USD",
    acquisition_source: Optional[str] = None,
    acquired_at: Optional[str] = None,
    effective_at: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Create a first-class holding, including imported external positions."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO advisory_holdings
                        (client_id, advisory_plan_id, domain, holding_type, status,
                         title, institution, symbol, quantity, market_value,
                         currency, acquisition_source, acquired_at, effective_at,
                         metadata)
                    VALUES
                        (%s, %s, %s, %s, %s,
                         %s, %s, %s, %s, %s,
                         %s, %s, %s::timestamptz, %s::timestamptz,
                         %s::jsonb)
                    RETURNING id, client_id, advisory_plan_id, domain, holding_type,
                              status, title, institution, symbol, quantity,
                              market_value, currency, acquisition_source,
                              acquired_at, effective_at, metadata, created_at,
                              updated_at
                    """,
                    (
                        client_id,
                        advisory_plan_id,
                        domain,
                        holding_type,
                        status,
                        title,
                        institution,
                        symbol,
                        quantity,
                        market_value,
                        currency,
                        acquisition_source,
                        acquired_at,
                        effective_at,
                        _pg_json(metadata or {}),
                    ),
                )
                row = cur.fetchone()
            conn.commit()
            return _holding_row(row)
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] create_advisory_holding failed: {exc}", flush=True)
        return None


def list_advisory_holdings(
    *,
    client_id: str,
    status: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """List holdings for a client."""
    pool = _get_pool()
    if pool is None:
        return []
    limit = max(1, min(int(limit or 100), 200))
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                if status:
                    cur.execute(
                        """
                        SELECT id, client_id, advisory_plan_id, domain, holding_type,
                               status, title, institution, symbol, quantity,
                               market_value, currency, acquisition_source,
                               acquired_at, effective_at, metadata, created_at,
                               updated_at
                        FROM advisory_holdings
                        WHERE client_id = %s AND status = %s
                        ORDER BY updated_at DESC, created_at DESC
                        LIMIT %s
                        """,
                        (client_id, status, limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, client_id, advisory_plan_id, domain, holding_type,
                               status, title, institution, symbol, quantity,
                               market_value, currency, acquisition_source,
                               acquired_at, effective_at, metadata, created_at,
                               updated_at
                        FROM advisory_holdings
                        WHERE client_id = %s
                        ORDER BY updated_at DESC, created_at DESC
                        LIMIT %s
                        """,
                        (client_id, limit),
                    )
                return [_holding_row(row) for row in cur.fetchall()]
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] list_advisory_holdings failed: {exc}", flush=True)
        return []


def update_advisory_holding_status(
    *,
    holding_id: str,
    client_id: Optional[str] = None,
    status: str,
    metadata_patch: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Update a holding lifecycle status."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                params: List[Any] = [status, _pg_json(metadata_patch or {}), holding_id]
                client_clause = ""
                if client_id:
                    client_clause = "AND client_id = %s"
                    params.append(client_id)
                cur.execute(
                    f"""
                    UPDATE advisory_holdings
                       SET status = %s,
                           metadata = metadata || %s::jsonb,
                           updated_at = NOW()
                     WHERE id = %s {client_clause}
                     RETURNING id, client_id, advisory_plan_id, domain, holding_type,
                               status, title, institution, symbol, quantity,
                               market_value, currency, acquisition_source,
                               acquired_at, effective_at, metadata, created_at,
                               updated_at
                    """,
                    tuple(params),
                )
                row = cur.fetchone()
            conn.commit()
            return _holding_row(row) if row else None
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] update_advisory_holding_status failed: {exc}", flush=True)
        return None


def create_expert_product(
    *,
    domain: str,
    name: str,
    status: str = "draft",
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Create an expert-maintained product shell."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO expert_products (domain, name, status, metadata)
                    VALUES (%s, %s, %s, %s::jsonb)
                    RETURNING id, domain, name, status, metadata, created_at, updated_at
                    """,
                    (domain, name, status, _pg_json(metadata or {})),
                )
                row = cur.fetchone()
            conn.commit()
            return _expert_product_row(row)
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] create_expert_product failed: {exc}", flush=True)
        return None


def list_expert_products(
    *,
    domain: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """List expert products for admin workflows."""
    pool = _get_pool()
    if pool is None:
        return []
    clauses: List[str] = []
    params: List[Any] = []
    if domain:
        clauses.append("domain = %s")
        params.append(domain)
    if status:
        clauses.append("status = %s")
        params.append(status)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    limit = max(1, min(int(limit or 100), 200))
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, domain, name, status, metadata, created_at, updated_at
                    FROM expert_products
                    {where}
                    ORDER BY updated_at DESC, created_at DESC
                    LIMIT %s
                    """,
                    (*params, limit),
                )
                return [_expert_product_row(row) for row in cur.fetchall()]
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] list_expert_products failed: {exc}", flush=True)
        return []


def create_expert_product_version(
    *,
    expert_product_id: str,
    version: Optional[int] = None,
    status: str = "draft",
    source_summary: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
    effective_at: Optional[str] = None,
    valid_from: Optional[str] = None,
    valid_to: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Create a versioned expert product payload."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                if version is None:
                    cur.execute(
                        """
                        SELECT COALESCE(MAX(version), 0) + 1
                        FROM expert_product_versions
                        WHERE expert_product_id = %s
                        """,
                        (expert_product_id,),
                    )
                    version = int(cur.fetchone()[0])
                cur.execute(
                    """
                    INSERT INTO expert_product_versions
                        (expert_product_id, version, status, source_summary,
                         payload, effective_at, valid_from, valid_to)
                    VALUES
                        (%s, %s, %s, %s::jsonb,
                         %s::jsonb, %s::timestamptz, %s::timestamptz, %s::timestamptz)
                    RETURNING id, expert_product_id, version, status,
                              source_summary, payload, effective_at, valid_from,
                              valid_to, created_at
                    """,
                    (
                        expert_product_id,
                        version,
                        status,
                        _pg_json(source_summary or {}),
                        _pg_json(payload or {}),
                        effective_at,
                        valid_from,
                        valid_to,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
            return _expert_product_version_row(row)
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] create_expert_product_version failed: {exc}", flush=True)
        return None


def list_expert_product_versions(
    *,
    expert_product_id: str,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """List versions for one expert product."""
    pool = _get_pool()
    if pool is None:
        return []
    limit = max(1, min(int(limit or 50), 100))
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                if status:
                    cur.execute(
                        """
                        SELECT id, expert_product_id, version, status,
                               source_summary, payload, effective_at, valid_from,
                               valid_to, created_at
                        FROM expert_product_versions
                        WHERE expert_product_id = %s AND status = %s
                        ORDER BY version DESC
                        LIMIT %s
                        """,
                        (expert_product_id, status, limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, expert_product_id, version, status,
                               source_summary, payload, effective_at, valid_from,
                               valid_to, created_at
                        FROM expert_product_versions
                        WHERE expert_product_id = %s
                        ORDER BY version DESC
                        LIMIT %s
                        """,
                        (expert_product_id, limit),
                    )
                return [_expert_product_version_row(row) for row in cur.fetchall()]
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] list_expert_product_versions failed: {exc}", flush=True)
        return []


def list_engine_runs(
    *,
    client_id: Optional[str] = None,
    journey_id: Optional[str] = None,
    expert_product_version_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """List engine runs for admin/debug lineage views."""
    pool = _get_pool()
    if pool is None:
        return []
    clauses: List[str] = []
    params: List[Any] = []
    if client_id:
        clauses.append("client_id = %s")
        params.append(client_id)
    if journey_id:
        clauses.append("journey_id = %s")
        params.append(journey_id)
    if expert_product_version_id:
        clauses.append("expert_product_version_id = %s")
        params.append(expert_product_version_id)
    if status:
        clauses.append("status = %s")
        params.append(status)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    limit = max(1, min(int(limit or 50), 100))
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, client_id, domain, engine_name, engine_version,
                           status, journey_id, expert_product_version_id,
                           input_summary, output_summary,
                           based_on_client_state_version,
                           based_on_client_state_as_of_time,
                           started_at, completed_at, created_at
                    FROM engine_runs
                    {where}
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (*params, limit),
                )
                return [_engine_run_row(row) for row in cur.fetchall()]
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] list_engine_runs failed: {exc}", flush=True)
        return []


def create_raw_external_event(
    *,
    source_system: str,
    event_type: str,
    source_event_id: Optional[str] = None,
    dedupe_key: Optional[str] = None,
    status: str = "received",
    occurred_at: Optional[str] = None,
    normalized_payload: Optional[Dict[str, Any]] = None,
    raw_payload: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Store an external event before deriving user-scoped advisory events."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO raw_external_events
                        (source_system, source_event_id, event_type, dedupe_key,
                         status, occurred_at, normalized_payload, raw_payload)
                    VALUES
                        (%s, %s, %s, %s,
                         %s, %s::timestamptz, %s::jsonb, %s::jsonb)
                    ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL
                    DO UPDATE SET status = raw_external_events.status
                    RETURNING id, source_system, source_event_id, event_type,
                              dedupe_key, status, occurred_at, received_at,
                              normalized_payload, raw_payload,
                              created_advisory_event_id, created_at
                    """,
                    (
                        source_system,
                        source_event_id,
                        event_type,
                        dedupe_key,
                        status,
                        occurred_at,
                        _pg_json(normalized_payload or {}),
                        _pg_json(raw_payload or {}),
                    ),
                )
                row = cur.fetchone()
            conn.commit()
            return _external_event_row(row)
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] create_raw_external_event failed: {exc}", flush=True)
        return None


def derive_advisory_events_from_external_event(
    *,
    raw_external_event_id: str,
    event_type: str = "holding.external_event_detected",
    status: str = "open",
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Match a raw external event to holdings by symbol and create advisory events.

    This is intentionally conservative and transparent: it only performs a
    symbol-based match against first-class holdings. More advanced matching can
    later use product identifiers, custodian IDs, or expert product exposure
    maps.
    """
    pool = _get_pool()
    if pool is None:
        return []
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, source_system, source_event_id, event_type,
                           dedupe_key, occurred_at, received_at,
                           normalized_payload, raw_payload
                    FROM raw_external_events
                    WHERE id = %s
                    """,
                    (raw_external_event_id,),
                )
                raw = cur.fetchone()
                if raw is None:
                    return []
                normalized_payload = raw[7] or {}
                raw_payload = raw[8] or {}
                symbol = str(
                    normalized_payload.get("symbol")
                    or raw_payload.get("symbol")
                    or raw_payload.get("ticker")
                    or ""
                ).strip().upper()
                if not symbol:
                    return []
                cur.execute(
                    """
                    SELECT id, client_id, advisory_plan_id, title, symbol,
                           market_value, metadata
                    FROM advisory_holdings
                    WHERE UPPER(symbol) = %s AND status = 'active'
                    ORDER BY updated_at DESC
                    LIMIT %s
                    """,
                    (symbol, max(1, min(int(limit or 100), 500))),
                )
                holdings = cur.fetchall()
                created: List[Dict[str, Any]] = []
                for holding in holdings:
                    holding_id = str(holding[0])
                    client_id = holding[1]
                    plan_id = str(holding[2]) if holding[2] else None
                    dedupe = f"external:{raw_external_event_id}:holding:{holding_id}"
                    payload = {
                        "raw_external_event_id": raw_external_event_id,
                        "source_system": raw[1],
                        "source_event_id": raw[2],
                        "source_event_type": raw[3],
                        "symbol": symbol,
                        "holding_id": holding_id,
                        "holding_title": holding[3],
                        "holding_market_value": float(holding[5]) if holding[5] is not None else None,
                        "external_payload": normalized_payload or raw_payload,
                    }
                    cur.execute(
                        """
                        INSERT INTO advisory_events
                            (client_id, advisory_plan_id, event_type, source_type,
                             source_id, status, event_time, detected_at,
                             effective_at, payload, dedupe_key)
                        VALUES
                            (%s, %s, %s, 'external_event',
                             %s, %s, COALESCE(%s::timestamptz, NOW()), NOW(),
                             %s::timestamptz, %s::jsonb, %s)
                        ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL
                        DO UPDATE SET status = advisory_events.status
                        RETURNING id, client_id, advisory_plan_id, event_type,
                                  source_type, source_id, status, event_time,
                                  detected_at, effective_at, payload, created_at
                        """,
                        (
                            client_id,
                            plan_id,
                            event_type,
                            raw_external_event_id,
                            status,
                            _iso(raw[5]),
                            _iso(raw[5]),
                            _pg_json(payload),
                            dedupe,
                        ),
                    )
                    created.append(_event_row(cur.fetchone()))
                if created:
                    cur.execute(
                        """
                        UPDATE raw_external_events
                           SET status = 'derived',
                               created_advisory_event_id = %s
                         WHERE id = %s
                        """,
                        (created[0]["id"], raw_external_event_id),
                    )
            conn.commit()
            return created
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] derive_advisory_events_from_external_event failed: {exc}", flush=True)
        return []


def get_advisory_events(
    *,
    client_id: str,
    status: Optional[str] = "open",
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """List advisory events for a client, newest first."""
    pool = _get_pool()
    if pool is None:
        return []
    limit = max(1, min(int(limit or 20), 100))
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                if status:
                    cur.execute(
                        """
                        SELECT id, client_id, advisory_plan_id, event_type,
                               source_type, source_id, status, event_time,
                               detected_at, effective_at, payload, created_at
                        FROM advisory_events
                        WHERE client_id = %s AND status = %s
                        ORDER BY event_time DESC, created_at DESC
                        LIMIT %s
                        """,
                        (client_id, status, limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, client_id, advisory_plan_id, event_type,
                               source_type, source_id, status, event_time,
                               detected_at, effective_at, payload, created_at
                        FROM advisory_events
                        WHERE client_id = %s
                        ORDER BY event_time DESC, created_at DESC
                        LIMIT %s
                        """,
                        (client_id, limit),
                    )
                return [_event_row(row) for row in cur.fetchall()]
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] get_advisory_events failed: {exc}", flush=True)
        return []


def update_advisory_event_status(
    *,
    event_id: str,
    client_id: Optional[str] = None,
    status: str,
) -> Optional[Dict[str, Any]]:
    """Update advisory event status, optionally enforcing client ownership."""
    pool = _get_pool()
    if pool is None:
        return None
    try:
        conn = _safe_getconn(pool)
        try:
            with conn.cursor() as cur:
                if client_id:
                    cur.execute(
                        """
                        UPDATE advisory_events
                           SET status = %s
                         WHERE id = %s AND client_id = %s
                         RETURNING id, client_id, advisory_plan_id, event_type,
                                   source_type, source_id, status, event_time,
                                   detected_at, effective_at, payload, created_at
                        """,
                        (status, event_id, client_id),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE advisory_events
                           SET status = %s
                         WHERE id = %s
                         RETURNING id, client_id, advisory_plan_id, event_type,
                                   source_type, source_id, status, event_time,
                                   detected_at, effective_at, payload, created_at
                        """,
                        (status, event_id),
                    )
                row = cur.fetchone()
            conn.commit()
            return _event_row(row) if row else None
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[db] update_advisory_event_status failed: {exc}", flush=True)
        return None


def _event_row(row: Any) -> Dict[str, Any]:
    return {
        "id": str(row[0]),
        "client_id": row[1],
        "advisory_plan_id": str(row[2]) if row[2] else None,
        "event_type": row[3],
        "source_type": row[4],
        "source_id": row[5],
        "status": row[6],
        "event_time": _iso(row[7]),
        "detected_at": _iso(row[8]),
        "effective_at": _iso(row[9]),
        "payload": row[10] or {},
        "created_at": _iso(row[11]),
    }


def _plan_row(row: Any) -> Dict[str, Any]:
    return {
        "id": str(row[0]),
        "client_id": row[1],
        "domain": row[2],
        "source_type": row[3],
        "journey_id": str(row[4]) if row[4] else None,
        "engine_run_id": str(row[5]) if row[5] else None,
        "current_artifact_id": str(row[6]) if row[6] else None,
        "status": row[7],
        "title": row[8],
        "summary": row[9],
        "based_on_client_state_version": row[10],
        "based_on_client_state_as_of_time": _iso(row[11]),
        "generated_at": _iso(row[12]),
        "effective_at": _iso(row[13]),
        "valid_from": _iso(row[14]),
        "valid_to": _iso(row[15]),
        "metadata": row[16] or {},
        "created_at": _iso(row[17]),
        "updated_at": _iso(row[18]),
    }


def _artifact_row(row: Any) -> Dict[str, Any]:
    return {
        "id": str(row[0]),
        "advisory_plan_id": str(row[1]),
        "artifact_type": row[2],
        "version": row[3],
        "payload": row[4] or {},
        "source_journey_id": str(row[5]) if row[5] else None,
        "engine_run_id": str(row[6]) if row[6] else None,
        "effective_at": _iso(row[7]),
        "valid_from": _iso(row[8]),
        "valid_to": _iso(row[9]),
        "created_at": _iso(row[10]),
    }


def _decision_row(row: Any) -> Dict[str, Any]:
    return {
        "id": str(row[0]),
        "client_id": row[1],
        "advisory_plan_id": str(row[2]),
        "decision": row[3],
        "reason": row[4],
        "reason_code": row[5],
        "free_text_reason": row[6],
        "revisit_at": _iso(row[7]),
        "source_type": row[8],
        "payload": row[9] or {},
        "decided_at": _iso(row[10]),
        "created_at": _iso(row[11]),
    }


def _holding_row(row: Any) -> Dict[str, Any]:
    return {
        "id": str(row[0]),
        "client_id": row[1],
        "advisory_plan_id": str(row[2]) if row[2] else None,
        "domain": row[3],
        "holding_type": row[4],
        "status": row[5],
        "title": row[6],
        "institution": row[7],
        "symbol": row[8],
        "quantity": float(row[9]) if row[9] is not None else None,
        "market_value": float(row[10]) if row[10] is not None else None,
        "currency": row[11],
        "acquisition_source": row[12],
        "acquired_at": _iso(row[13]),
        "effective_at": _iso(row[14]),
        "metadata": row[15] or {},
        "created_at": _iso(row[16]),
        "updated_at": _iso(row[17]),
    }


def _expert_product_row(row: Any) -> Dict[str, Any]:
    return {
        "id": str(row[0]),
        "domain": row[1],
        "name": row[2],
        "status": row[3],
        "metadata": row[4] or {},
        "created_at": _iso(row[5]),
        "updated_at": _iso(row[6]),
    }


def _expert_product_version_row(row: Any) -> Dict[str, Any]:
    return {
        "id": str(row[0]),
        "expert_product_id": str(row[1]),
        "version": row[2],
        "status": row[3],
        "source_summary": row[4] or {},
        "payload": row[5] or {},
        "effective_at": _iso(row[6]),
        "valid_from": _iso(row[7]),
        "valid_to": _iso(row[8]),
        "created_at": _iso(row[9]),
    }


def _engine_run_row(row: Any) -> Dict[str, Any]:
    return {
        "id": str(row[0]),
        "client_id": row[1],
        "domain": row[2],
        "engine_name": row[3],
        "engine_version": row[4],
        "status": row[5],
        "journey_id": str(row[6]) if row[6] else None,
        "expert_product_version_id": str(row[7]) if row[7] else None,
        "input_summary": row[8] or {},
        "output_summary": row[9] or {},
        "based_on_client_state_version": row[10],
        "based_on_client_state_as_of_time": _iso(row[11]),
        "started_at": _iso(row[12]),
        "completed_at": _iso(row[13]),
        "created_at": _iso(row[14]),
    }


def _external_event_row(row: Any) -> Dict[str, Any]:
    return {
        "id": str(row[0]),
        "source_system": row[1],
        "source_event_id": row[2],
        "event_type": row[3],
        "dedupe_key": row[4],
        "status": row[5],
        "occurred_at": _iso(row[6]),
        "received_at": _iso(row[7]),
        "normalized_payload": row[8] or {},
        "raw_payload": row[9] or {},
        "created_advisory_event_id": str(row[10]) if row[10] else None,
        "created_at": _iso(row[11]),
    }


__all__ = [
    "ensure_advisory_plan_from_journey",
    "create_advisory_holding",
    "create_advisory_event",
    "create_expert_product",
    "create_expert_product_version",
    "create_raw_external_event",
    "derive_advisory_events_from_external_event",
    "get_advisory_plan_for_journey",
    "get_advisory_plan",
    "get_advisory_events",
    "list_advisory_artifacts",
    "list_advisory_holdings",
    "list_advisory_plans",
    "list_expert_products",
    "list_expert_product_versions",
    "list_engine_runs",
    "list_advisory_policies",
    "mark_advisory_plan_status_for_journey",
    "record_advisory_decision",
    "update_advisory_holding_status",
    "update_advisory_plan_status",
    "update_advisory_event_status",
]
