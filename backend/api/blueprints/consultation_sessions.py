"""Consultation session HTTP routes."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Tuple

from flask import Blueprint, jsonify, request

from api.services.companion_turn import CompanionTurnRequest


def create_consultation_sessions_blueprint(
    *,
    user_auth_decorator: Callable[[Any], Any],
    deps: Any,
) -> Blueprint:
    """Create consultation session routes with app-level dependency hooks."""
    bp = Blueprint("consultation_sessions", __name__)

    @bp.route("/api/v1/consultations/history", methods=["GET"])
    @user_auth_decorator
    def consultation_history(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        """Full cross-session thread for the single window, so a returning client
        sees their prior conversation — text turns plus the event and section
        cards that surfaced — rather than a blank window."""
        from api.persistence import (  # local import: same pattern as consultation_complete
            get_client_conversation_history,
            get_thread_annotations,
            list_companion_turns_for_client,
        )
        try:
            limit = int(request.args.get("limit", 200))
        except (TypeError, ValueError):
            limit = 200
        limit = max(1, min(limit, 500))
        client_id = auth_session["client_id"]
        turns = get_client_conversation_history(client_id, max_turns=limit)
        companion_turns = list_companion_turns_for_client(client_id, max_turns=limit)
        items = [{"kind": "turn", "role": t["role"], "text": t["text"], "ts": t["ts"]} for t in turns]
        # Dedup incremental turn-annotations against the legacy transcript turns
        # so a completed session's turns aren't shown twice.
        seen_turns = {(t["role"], (t["text"] or "").strip()) for t in turns}
        for turn in companion_turns:
            text = (turn.get("text") or "").strip()
            key = (turn.get("role"), text)
            if not text or key in seen_turns:
                continue
            seen_turns.add(key)
            items.append(
                {
                    "kind": "turn",
                    "role": turn["role"],
                    "text": text,
                    "ts": turn["ts"],
                }
            )
        for ann in get_thread_annotations(client_id, limit=limit):
            if ann["kind"] == "turn":
                text = (ann.get("text") or "").strip()
                artifact_type = str(ann.get("artifactType") or "").strip()
                proposal_id = str(ann.get("proposalId") or ann.get("artifactId") or "").strip()
                # Proposal/assessment cards may be persisted as turn annotations with
                # an artifactType; keep them as structured cards instead of plain text.
                if artifact_type in {
                    "proposal_link",
                    "proposal_card",
                    "assessment_card",
                    "investment_assessment_card",
                }:
                    items.append({
                        "kind": "turn",
                        "role": ann["role"],
                        "ts": ann["ts"],
                        "title": ann.get("title") or "",
                        "summary": ann.get("summary") or text,
                        "text": text,
                        "artifactType": artifact_type,
                        "artifactId": ann.get("artifactId") or proposal_id,
                        "proposalId": proposal_id,
                        "assessmentId": ann.get("assessmentId") or "",
                        "assessmentVersion": ann.get("assessmentVersion"),
                        "investmentConsultationId": ann.get("investmentConsultationId") or "",
                        "moneyPoolId": ann.get("moneyPoolId") or "",
                        "poolLabel": ann.get("poolLabel") or "",
                        "subtitle": ann.get("subtitle") or "",
                        "paragraphs": ann.get("paragraphs") if isinstance(ann.get("paragraphs"), list) else [],
                        "decision": ann.get("decision") or "",
                    })
                    continue
                key = (ann["role"], text)
                if not text or key in seen_turns:
                    continue
                seen_turns.add(key)
                items.append({"kind": "turn", "role": ann["role"], "text": text, "ts": ann["ts"]})
                continue
            if ann["kind"] == "voice_ended":
                items.append({
                    "kind": "voice_ended",
                    "role": ann["role"],
                    "ts": ann["ts"],
                    "durationSeconds": int(ann.get("durationSeconds") or 0),
                    "title": ann.get("title") or "Voice Ended",
                })
                continue
            items.append({
                "kind": ann["kind"],            # 'event' | 'section'
                "role": ann["role"],
                "ts": ann["ts"],
                "title": ann["title"],
                "summary": ann["summary"],
                "artifactType": ann.get("artifactType") or ("advisory event" if ann["kind"] == "event" else "proposal"),
                "artifactId": ann.get("artifactId") or "",
                "proposalId": ann.get("proposalId") or "",
                "assessmentId": ann.get("assessmentId") or "",
                "assessmentVersion": ann.get("assessmentVersion"),
                "investmentConsultationId": ann.get("investmentConsultationId") or "",
                "moneyPoolId": ann.get("moneyPoolId") or "",
                "poolLabel": ann.get("poolLabel") or "",
                "subtitle": ann.get("subtitle") or "",
                "paragraphs": ann.get("paragraphs") if isinstance(ann.get("paragraphs"), list) else [],
                "decision": ann.get("decision") or "",
            })
        items.sort(key=lambda i: i["ts"])
        if len(items) > limit:
            items = items[-limit:]
        return jsonify({"success": True, "items": items}), 200

    @bp.route("/api/v1/consultations/thread-annotation", methods=["POST"])
    @user_auth_decorator
    def add_consultation_thread_annotation(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        """Persist a non-text card (raised event / pulled section) into the thread."""
        from api.persistence import add_thread_annotation
        body = request.get_json(silent=True) or {}
        kind = str(body.get("kind") or "").strip()
        if kind not in ("event", "section", "turn", "voice_ended"):
            return jsonify({"success": False, "error": "kind must be 'event', 'section', 'turn', or 'voice_ended'"}), 400
        # Allow proposal/assessment cards via turn annotations so journey-created
        # proposals can rehydrate as tappable history cards.
        duration_seconds = body.get("durationSeconds")
        try:
            duration_seconds_i = int(duration_seconds) if duration_seconds is not None else 0
        except (TypeError, ValueError):
            duration_seconds_i = 0
        ok = add_thread_annotation(
            client_id=auth_session["client_id"],
            kind=kind,
            payload={
                "title": str(body.get("title") or ""),
                "summary": str(body.get("summary") or ""),
                "artifactType": str(body.get("artifactType") or ""),
                "text": str(body.get("text") or ""),
                "artifactId": str(body.get("artifactId") or ""),
                "assessmentId": str(body.get("assessmentId") or ""),
                "assessmentVersion": body.get("assessmentVersion"),
                "investmentConsultationId": str(body.get("investmentConsultationId") or ""),
                "moneyPoolId": str(body.get("moneyPoolId") or ""),
                "poolLabel": str(body.get("poolLabel") or ""),
                "subtitle": str(body.get("subtitle") or ""),
                "paragraphs": body.get("paragraphs") if isinstance(body.get("paragraphs"), list) else [],
                "decision": str(body.get("decision") or ""),
                "proposalId": str(body.get("proposalId") or ""),
                "durationSeconds": max(0, duration_seconds_i),
            },
            client_ts=int(body.get("ts") or 0),
            role=str(body.get("role") or "assistant"),
        )
        return jsonify({"success": ok}), (200 if ok else 500)

    @bp.route("/api/v1/consultations/session-token", methods=["POST"])
    @user_auth_decorator
    def consultation_session_token(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        client_id = auth_session["client_id"]
        session_type = body.get("session_type")

        if session_type not in ("onboarding_understanding", "journey_preparation"):
            return jsonify({"success": False, "error": "Invalid session_type"}), 400

        journey_type = body.get("journey_type")
        journey_id = body.get("journey_id")
        trigger_source = body.get("trigger_source")
        baseline_snapshot_version = body.get("baseline_snapshot_version")
        companion_session_id = body.get("companion_session_id")

        if baseline_snapshot_version is None:
            baseline_snapshot_version = deps.db_get_current_snapshot_version(client_id)

        lifecycle = getattr(deps, "consultation_lifecycle_service", None)
        if lifecycle is not None:
            client_request_id = str(body.get("client_request_id") or "").strip()
            client_interaction_id = str(body.get("client_interaction_id") or "").strip()
            channel = str(body.get("channel") or "voice").strip()
            if not client_request_id or not client_interaction_id:
                return jsonify({"success": False, "error": "client_request_id and client_interaction_id are required"}), 400
            try:
                uuid.UUID(client_request_id)
                uuid.UUID(client_interaction_id)
                engagement = lifecycle.ensure_open(
                    auth_session=auth_session,
                    session_type=session_type,
                    journey_type=journey_type,
                    journey_id=journey_id,
                    trigger_source=trigger_source,
                    baseline_snapshot_version=baseline_snapshot_version,
                    companion_session_id=str(companion_session_id or ""),
                    client_request_id=client_request_id,
                )
                interaction = lifecycle.begin_or_renew(
                    auth_session=auth_session,
                    engagement_id=engagement["consultation_engagement_id"],
                    client_interaction_id=client_interaction_id,
                    companion_session_id=str(companion_session_id or ""),
                    channel=channel,
                )
            except PermissionError as exc:
                return jsonify({"success": False, "error": str(exc)}), 403
            except (ValueError, LookupError) as exc:
                return jsonify({"success": False, "error": str(exc)}), 409
            return jsonify({
                "success": True,
                "consultation_engagement_id": engagement["consultation_engagement_id"],
                "session_id": engagement["consultation_engagement_id"],
                "session_type": session_type,
                "journey_type": journey_type,
                "status": interaction["engagement"]["status"],
                "disposition": engagement["disposition"],
                "created": engagement["created"],
                "resumed": engagement["resumed"],
                "lifecycle_version": interaction["engagement"]["lifecycle_version"],
                "interaction_id": interaction["interaction_id"],
                "interaction_version": interaction["interaction_version"],
                "interaction_lease_expires_at": interaction["lease_expires_at"],
                "baseline_snapshot_version": baseline_snapshot_version,
            }), 200

        session_id = deps.db_create_consultation_session(
            client_id=client_id,
            session_type=session_type,
            journey_type=journey_type,
            journey_id=journey_id,
            trigger_source=trigger_source,
            baseline_snapshot_version=baseline_snapshot_version,
            companion_session_id=companion_session_id,
        )

        if not session_id:
            return jsonify({"success": False, "error": "Failed to create session"}), 500

        if session_type == "onboarding_understanding" and getattr(
            deps, "get_planning_refresh_coordinator", None
        ) is not None:
            deps.get_planning_refresh_coordinator().consultation_state_changed(
                client_id=client_id,
                active=True,
            )

        return jsonify({
            "success": True,
            "session_id": session_id,
            "session_type": session_type,
            "journey_type": journey_type,
            "baseline_snapshot_version": baseline_snapshot_version,
        }), 200

    @bp.route("/api/v1/consultations/active", methods=["GET"])
    @user_auth_decorator
    def consultation_active(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        lifecycle = getattr(deps, "consultation_lifecycle_service", None)
        if lifecycle is None:
            return jsonify({"success": False, "error": "Consultation lifecycle unavailable"}), 503
        session_type = str(request.args.get("session_type") or "onboarding_understanding")
        if session_type not in {"onboarding_understanding", "journey_preparation"}:
            return jsonify({"success": False, "error": "Invalid session_type"}), 400
        engagement = lifecycle.get_active(
            auth_session=auth_session,
            session_type=session_type,
            journey_id=request.args.get("journey_id"),
        )
        return jsonify({"success": True, "engagement": engagement}), 200

    @bp.route("/api/v1/consultations/<session_id>/interactions", methods=["POST"])
    @user_auth_decorator
    def consultation_interaction_begin(session_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        lifecycle = getattr(deps, "consultation_lifecycle_service", None)
        try:
            result = lifecycle.begin_or_renew(
                auth_session=auth_session, engagement_id=session_id,
                client_interaction_id=str(body.get("client_interaction_id") or ""),
                companion_session_id=str(body.get("companion_session_id") or ""),
                channel=str(body.get("channel") or "text"),
            )
            return jsonify({"success": True, **result}), 200
        except PermissionError as exc:
            return jsonify({"success": False, "error": str(exc)}), 403
        except LookupError as exc:
            return jsonify({"success": False, "error": str(exc)}), 404
        except (ValueError, AttributeError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 409

    @bp.route("/api/v1/consultations/<session_id>/interactions/<interaction_id>/heartbeat", methods=["POST"])
    @user_auth_decorator
    def consultation_interaction_heartbeat(session_id: str, interaction_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        try:
            result = deps.consultation_lifecycle_service.heartbeat(
                auth_session=auth_session, engagement_id=session_id,
                interaction_id=interaction_id,
                expected_version=int(body.get("expected_version") or 0),
            )
            return jsonify({"success": True, **result}), 200
        except LookupError as exc:
            return jsonify({"success": False, "error": str(exc)}), 404
        except (ValueError, AttributeError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 409

    @bp.route("/api/v1/consultations/<session_id>/interactions/<interaction_id>/end", methods=["POST"])
    @user_auth_decorator
    def consultation_interaction_end(session_id: str, interaction_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        try:
            result = deps.consultation_lifecycle_service.end(
                auth_session=auth_session, engagement_id=session_id,
                interaction_id=interaction_id,
                end_reason=str(body.get("reason") or "client_ended"),
                expected_version=int(body["expected_version"]) if body.get("expected_version") is not None else None,
            )
            return jsonify({"success": True, **result}), 200
        except LookupError as exc:
            return jsonify({"success": False, "error": str(exc)}), 404
        except (ValueError, AttributeError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 409

    @bp.route("/api/v1/consultations/<session_id>/checkpoint", methods=["POST"])
    @user_auth_decorator
    def consultation_checkpoint(session_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        interaction_id = str(body.get("interaction_id") or "")
        client_turn_id = str(body.get("client_turn_id") or "")
        transcript = body.get("transcript") if isinstance(body.get("transcript"), dict) else {}
        try:
            checkpoint = deps.consultation_lifecycle_service.checkpoint(
                auth_session=auth_session, engagement_id=session_id,
                interaction_id=interaction_id, client_turn_id=client_turn_id,
                reason=str(body.get("reason") or "checkpoint"), transcript=transcript,
            )
            transcript_text = _consultation_transcript_text(transcript)
            turn_receipt = None
            if transcript_text:
                turn_request = CompanionTurnRequest(
                    client_id=str(auth_session["client_id"]),
                    session_id=str(checkpoint["companion_session_id"]),
                    user_message=transcript_text, channel="voice",
                    client_turn_id=client_turn_id,
                    input_source={"consultation_engagement_id": session_id, "interaction_id": interaction_id},
                )
                accepted = deps.companion_turn_service.accept_turn(turn_request)
                outcome = deps.companion_turn_service.run_turn(turn_request, accepted_turn=accepted)
                turn_receipt = outcome.turn_receipt
            ended = deps.consultation_lifecycle_service.end(
                auth_session=auth_session, engagement_id=session_id,
                interaction_id=interaction_id, end_reason=str(body.get("reason") or "checkpoint"),
            )
            return jsonify({"success": True, "consultation_engagement_id": session_id, "checkpoint": checkpoint, "interaction": ended, "turn_receipt": turn_receipt}), 200
        except LookupError as exc:
            return jsonify({"success": False, "error": str(exc)}), 404
        except (ValueError, AttributeError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 409

    @bp.route("/api/v1/consultations/<session_id>/complete", methods=["POST"])
    @user_auth_decorator
    def consultation_complete(session_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        transcript = body.get("transcript", {})

        if getattr(deps, "consultation_lifecycle_service", None) is not None:
            return jsonify({
                "success": False,
                "error": "voice completion moved to the interaction checkpoint endpoint",
                "consultation_engagement_id": session_id,
            }), 409

        session = deps.db_get_consultation_session(session_id)
        if not session:
            return jsonify({"success": False, "error": "Session not found"}), 404
        if session["client_id"] != auth_session["client_id"]:
            return jsonify({"success": False, "error": "Forbidden"}), 403

        deps.db_complete_consultation_session(session_id, transcript)
        client_id = session["client_id"]
        advisor_voice_result: Dict[str, Any] | None = None
        advisor_voice_error: str | None = None

        if getattr(deps, "get_advisor_runtime", None) is not None:
            try:
                transcript_text = _consultation_transcript_text(transcript)
                if transcript_text:
                    companion_session_id = (
                        str(session.get("companion_session_id") or "").strip()
                        or f"voice-consultation-{session_id}"
                    )
                    advisor_voice_result = deps.get_advisor_runtime().run_turn(
                        client_id=client_id,
                        session_id=companion_session_id,
                        user_message=transcript_text,
                        turn_type="user_message",
                        channel="voice",
                    )
            except Exception as exc:
                print(f"[consultation_complete] advisor voice handoff failed: {exc}", flush=True)
                advisor_voice_error = str(exc)

        if (
            session.get("session_type") == "onboarding_understanding"
            and getattr(deps, "get_planning_refresh_coordinator", None) is not None
        ):
            planning_refresh = deps.get_planning_refresh_coordinator().consultation_state_changed(
                client_id=client_id,
                active=False,
            )
        else:
            planning_refresh = None

        if advisor_voice_error is not None:
            if getattr(deps, "db_finalize_consultation_session", None) is not None:
                deps.db_finalize_consultation_session(
                    session_id,
                    status="failed",
                    metadata={"error": advisor_voice_error, "processing_path": "canonical_advisor_runtime"},
                )
            with deps.task_lock():
                deps.consultation_tasks()[session_id] = {
                    "status": "failed",
                    "error": advisor_voice_error,
                    "client_id": client_id,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
            return jsonify({
                "success": False,
                "status": "failed",
                "session_id": session_id,
                "error": advisor_voice_error,
            }), 500

        onboarding_lifecycle = None
        if (
            session.get("session_type") == "onboarding_understanding"
            and getattr(deps, "db_transition_advisor_onboarding_status", None) is not None
        ):
            onboarding_lifecycle = deps.db_transition_advisor_onboarding_status(
                client_id=client_id,
                target_status="complete",
            )

        metadata = {
            "processing_path": "canonical_advisor_runtime",
            "planning_refresh": planning_refresh,
            "onboarding_lifecycle": onboarding_lifecycle,
        }
        if getattr(deps, "db_finalize_consultation_session", None) is not None:
            deps.db_finalize_consultation_session(
                session_id,
                status="completed",
                metadata=metadata,
            )

        with deps.task_lock():
            deps.consultation_tasks()[session_id] = {
                "status": "complete",
                "diagnosis_status": "planning_refresh",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "client_id": client_id,
                "processing_path": "canonical_advisor_runtime",
                "planning_refresh": planning_refresh,
                "onboarding_lifecycle": onboarding_lifecycle,
            }

        with deps.task_lock():
            task = dict(deps.consultation_tasks().get(session_id) or {})

        return jsonify({
            "success": True,
            "status": task.get("status", "complete"),
            "session_id": session_id,
            "next_action": "companion_home",
            "knowledge_snapshot_version": task.get("knowledge_snapshot_version"),
            "diagnosis_snapshot_version": task.get("diagnosis_snapshot_version"),
            "diagnosis_status": task.get("diagnosis_status"),
            "candidate_facts_extracted": task.get("candidate_facts_extracted"),
            "facts_committed": task.get("facts_committed"),
            "pending_confirmations": task.get("pending_confirmations"),
            "graph_sync": task.get("graph_sync"),
            "advisor_voice": _advisor_voice_summary(advisor_voice_result),
        }), 200

    @bp.route("/api/v1/consultations/<session_id>/status", methods=["GET"])
    @user_auth_decorator
    def consultation_status(session_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        with deps.task_lock():
            task = deps.consultation_tasks().get(session_id)

        if not task:
            session = deps.db_get_consultation_session(session_id)
            if session and session.get("status") == "completed":
                snapshot = deps.db_get_latest_knowledge_snapshot(session["client_id"])
                metadata = session.get("metadata") or {}
                return jsonify({
                    "success": True,
                    "status": "complete",
                    "knowledge_snapshot_version": (
                        metadata.get("knowledge_snapshot_version")
                        or (snapshot or {}).get("version", 0)
                    ),
                    "candidate_facts_extracted": metadata.get("candidate_facts_extracted"),
                    "facts_committed": metadata.get("facts_committed"),
                    "graph_sync": metadata.get("graph_sync"),
                }), 200
            if session and session.get("status") == "processing":
                metadata = session.get("metadata") or {}
                return jsonify({
                    "success": True,
                    "status": "processing",
                    "progress_label": metadata.get("progress_label"),
                    "knowledge_snapshot_version": 0,
                }), 200
            if session and session.get("status") == "failed":
                metadata = session.get("metadata") or {}
                return jsonify({
                    "success": True,
                    "status": "failed",
                    "error": metadata.get("error"),
                }), 200
            return jsonify({"success": False, "error": "No processing task found"}), 404

        diagnosis_status = task.get("diagnosis_status")
        diagnosis_snapshot_version = task.get("diagnosis_snapshot_version")
        if diagnosis_status in {"pending_refresh", "running", "superseded"}:
            refresh_state = deps.get_diagnosis_refresh_payload(task.get("client_id", auth_session["client_id"]))
            diagnosis_status = refresh_state.get("status", diagnosis_status)
            if refresh_state.get("latest_diagnosis_version"):
                diagnosis_snapshot_version = refresh_state.get("latest_diagnosis_version")
            requested_version = int(task.get("diagnosis_requested_snapshot_version") or 0)
            completed_version = int(refresh_state.get("latest_completed_knowledge_snapshot_version") or 0)
            if requested_version and completed_version >= requested_version and diagnosis_status == "complete":
                with deps.task_lock():
                    live_task = deps.consultation_tasks().get(session_id)
                    if live_task is not None:
                        live_task["diagnosis_status"] = "complete"
                        live_task["diagnosis_snapshot_version"] = diagnosis_snapshot_version

        return jsonify({
            "success": True,
            "status": task.get("status", "processing"),
            "progress_label": task.get("progress_label"),
            "knowledge_snapshot_version": task.get("knowledge_snapshot_version"),
            "diagnosis_snapshot_version": diagnosis_snapshot_version,
            "diagnosis_status": diagnosis_status,
            "candidate_facts_extracted": task.get("candidate_facts_extracted"),
            "facts_committed": task.get("facts_committed"),
            "graph_sync": task.get("graph_sync"),
            "error": task.get("error"),
        }), 200

    return bp


def _consultation_transcript_text(transcript: Any) -> str:
    if isinstance(transcript, str):
        return transcript.strip()
    if isinstance(transcript, list):
        parts = []
        has_client_turn = False
        for item in transcript:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or item.get("speaker") or "").strip()
            text = str(item.get("text") or item.get("content") or item.get("transcript") or "").strip()
            if text:
                parts.append(f"{role}: {text}" if role else text)
                has_client_turn = has_client_turn or not role or role.lower() in {
                    "client",
                    "user",
                }
        return "\n".join(parts).strip() if has_client_turn else ""
    if isinstance(transcript, dict):
        # Prefer an explicit turns/messages list even when it is empty. An empty
        # list is falsy in Python; using `or` would fall through to json.dumps
        # and post '{"turns": []}' as a companion user message.
        if "turns" in transcript:
            turns = transcript.get("turns")
        elif "messages" in transcript:
            turns = transcript.get("messages")
        elif "items" in transcript:
            turns = transcript.get("items")
        else:
            turns = None
        if isinstance(turns, list):
            return _consultation_transcript_text(turns)
        for key in ("text", "content", "transcript"):
            value = transcript.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    return ""


def _advisor_voice_summary(result: Dict[str, Any] | None) -> Dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    return {
        "runtime": "advisor_runtime",
        "channel": "voice",
        "selected_skill": result.get("selected_skill"),
        "active_objective": result.get("active_objective"),
        "errors": result.get("errors", []),
    }


__all__ = ["create_consultation_sessions_blueprint"]
