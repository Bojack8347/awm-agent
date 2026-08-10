"""Consultation ingest and pipeline-run HTTP routes."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, MutableMapping, Tuple

from flask import Blueprint, jsonify, request


def create_consultation_blueprint(
    *,
    api_key_auth_decorator: Callable[[Any], Any],
    ingest_lock: Any,
    in_memory_ingests: MutableMapping[str, Dict[str, Any]],
    append_ingest_to_disk: Callable[[Dict[str, Any]], None],
    get_ingested_consultation: Callable[[str], Any],
    db_available_factory: Callable[[], bool],
    store_ingest: Callable[[Dict[str, Any]], Any],
    get_latest_ingest: Callable[[], Any],
    get_ingest: Callable[[str], Any],
    get_pipeline_run: Callable[[str], Any],
    get_latest_pipeline_run: Callable[[Any], Any],
) -> Blueprint:
    """Create consultation/pipeline routes with app-level dependency hooks."""
    bp = Blueprint("consultation", __name__)

    @bp.route("/advisor/api/v1/consultation-ingest", methods=["POST"])
    @api_key_auth_decorator
    def consultation_ingest() -> Tuple[Any, int]:
        body = request.get_json() or {}
        if not isinstance(body, dict):
            return jsonify({"success": False, "error": "Request JSON body is required"}), 400

        session_id = str(body.get("session_id", "") or "").strip()
        client_id = str(body.get("client_id", "") or "").strip()
        turns = body.get("turns")
        language = str(body.get("language", "en") or "en").strip()

        if not session_id:
            return jsonify({"success": False, "error": "session_id is required"}), 400
        if not client_id:
            return jsonify({"success": False, "error": "client_id is required"}), 400
        if not isinstance(turns, list):
            return jsonify({"success": False, "error": "turns must be a list"}), 400

        normalized_turns = []
        for idx, turn in enumerate(turns):
            if not isinstance(turn, dict):
                return jsonify({"success": False, "error": f"turns[{idx}] must be an object"}), 400

            speaker = str(turn.get("speaker", "") or "").strip()
            text = str(turn.get("text", "") or "").strip()
            ts_start_ms = turn.get("ts_start_ms")

            if speaker not in {"agent", "client", "system"}:
                return jsonify({"success": False, "error": f"turns[{idx}].speaker is invalid"}), 400
            if not text:
                continue
            if not isinstance(ts_start_ms, (int, float)):
                return jsonify({"success": False, "error": f"turns[{idx}].ts_start_ms must be numeric"}), 400

            normalized_turns.append(
                {
                    "speaker": speaker,
                    "text": text,
                    "ts_start_ms": int(ts_start_ms),
                    "ts_end_ms": (
                        int(turn.get("ts_end_ms"))
                        if isinstance(turn.get("ts_end_ms"), (int, float))
                        else None
                    ),
                }
            )

        agent_utterances = [t["text"] for t in normalized_turns if t["speaker"] == "agent"]
        ingest_id = str(uuid.uuid4())
        ingest_payload = {
            "ingest_id": ingest_id,
            "session_id": session_id,
            "client_id": client_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "transcript": {
                "session_id": session_id,
                "started_at": body.get("started_at"),
                "ended_at": body.get("ended_at"),
                "completion_reason": body.get("completion_reason"),
                "turns": normalized_turns,
                "metadata": body.get("metadata", {}),
                "language": language,
            },
            "agent_preview": {
                "last_agent_message": agent_utterances[-1] if agent_utterances else "",
            },
        }
        with ingest_lock:
            in_memory_ingests[ingest_id] = ingest_payload
        try:
            append_ingest_to_disk(ingest_payload)
        except OSError:
            pass

        store_ingest(ingest_payload)
        return jsonify({"success": True, "ingest_id": ingest_id}), 200

    @bp.route("/advisor/api/v1/consultation-ingest/latest", methods=["GET"])
    @api_key_auth_decorator
    def consultation_ingest_latest() -> Tuple[Any, int]:
        latest = get_latest_ingest() if db_available_factory() else None

        if not latest:
            with ingest_lock:
                ingests = list(in_memory_ingests.values())
            if ingests:
                latest = max(
                    ingests,
                    key=lambda row: str(row.get("created_at", "") or ""),
                )

        if not latest:
            return jsonify({"success": False, "error": "No consultation ingests found"}), 404

        return jsonify({"success": True, "consultation_ingest": latest}), 200

    @bp.route("/advisor/api/v1/consultation-ingest/<ingest_id>", methods=["GET"])
    @api_key_auth_decorator
    def consultation_ingest_get(ingest_id: str) -> Tuple[Any, int]:
        ingest_id = str(ingest_id or "").strip()
        if not ingest_id:
            return jsonify({"success": False, "error": "ingest_id is required"}), 400

        payload = get_ingest(ingest_id) if db_available_factory() else None
        if not payload:
            payload = get_ingested_consultation(ingest_id)
        if not payload:
            return jsonify({"success": False, "error": "consultation ingest not found"}), 404

        return jsonify({"success": True, "consultation_ingest": payload}), 200

    @bp.route("/advisor/api/v1/pipeline-run/<run_id>", methods=["GET"])
    @api_key_auth_decorator
    def get_pipeline_run_endpoint(run_id: str) -> Tuple[Any, int]:
        run = get_pipeline_run(run_id)
        if not run:
            return jsonify({"success": False, "error": "Pipeline run not found"}), 404
        return jsonify({"success": True, "pipeline_run": run}), 200

    @bp.route("/advisor/api/v1/pipeline-run/latest", methods=["GET"])
    @api_key_auth_decorator
    def get_latest_pipeline_run_endpoint() -> Tuple[Any, int]:
        session_id = request.args.get("session_id")
        run = get_latest_pipeline_run(session_id)
        if not run:
            return jsonify({"success": False, "error": "No pipeline runs found"}), 404
        return jsonify({"success": True, "pipeline_run": run}), 200

    return bp


__all__ = ["create_consultation_blueprint"]
