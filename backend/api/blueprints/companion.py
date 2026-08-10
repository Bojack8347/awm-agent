"""Companion HTTP transport adapters."""

from __future__ import annotations

import io
import json
import os
import queue
import threading
import time
import uuid
from datetime import date, datetime
from typing import Any, Callable, Dict, Optional, Tuple

from flask import (
    Blueprint,
    Response,
    copy_current_request_context,
    jsonify,
    request,
    stream_with_context,
)

from api.services.companion_turn import (
    CompanionTurnCallbacks,
    CompanionTurnOutcome,
    CompanionTurnRequest,
    CompanionTurnService,
)
from api.services.companion_actions import parse_client_action
from api.services.companion_sessions import CompanionSessionService


_STT_DOMAIN_PROMPT = (
    "Transcribe only clearly audible speech from this recording. "
    "If there is no discernible speech, return an empty transcript. "
    "Financial terms that may appear include: 401(k), Roth IRA, traditional IRA, "
    "taxable brokerage, index fund, ETF, mutual fund, S&P 500, "
    "dollar-cost averaging, asset allocation, rebalancing, emergency fund, "
    "debt-to-income ratio, investment diagnosis, financial journey, "
    "risk tolerance, time horizon, compound interest, capital gains."
)
_STT_MAX_BYTES = 10 * 1024 * 1024


def create_companion_blueprint(
    *,
    user_auth_decorator: Callable[[Any], Any],
    turn_service: CompanionTurnService,
    expected_companion_session_id: Callable[[Dict[str, Any]], str],
    db_get_companion_messages: Callable[..., Any],
    db_count_companion_messages: Callable[..., int],
    session_service: CompanionSessionService | None = None,
) -> Blueprint:
    """Create the legacy Companion HTTP routes over the canonical turn service."""

    bp = Blueprint("companion", __name__)

    @bp.route("/api/v1/companion/sessions", methods=["POST"])
    @user_auth_decorator
    def create_session(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        if session_service is None:
            return jsonify({"success": False, "error": "session_registry_unavailable"}), 503
        body = request.get_json(silent=True) or {}
        launch_id = str(body.get("launch_id") or "").strip()
        origin = str(body.get("origin") or "cold_launch").strip()
        if not launch_id:
            return jsonify({"success": False, "error": "launch_id is required"}), 400
        if origin not in {"cold_launch", "explicit_new"}:
            return jsonify({"success": False, "error": "invalid origin"}), 400
        try:
            row = session_service.create(
                auth_session=auth_session,
                launch_id=launch_id,
                origin=origin,
            )
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 409
        return jsonify({"success": True, "session": row}), 201

    @bp.route("/api/v1/companion/sessions", methods=["GET"])
    @user_auth_decorator
    def list_sessions(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        if session_service is None:
            return jsonify({"success": False, "error": "session_registry_unavailable"}), 503
        try:
            limit = int(request.args.get("limit", "50"))
        except (TypeError, ValueError):
            limit = 50
        return jsonify({"success": True, "sessions": session_service.list(auth_session=auth_session, limit=limit)}), 200

    @bp.route("/api/v1/companion/sessions/<session_id>", methods=["GET"])
    @user_auth_decorator
    def get_session(session_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        if session_service is None:
            return jsonify({"success": False, "error": "session_registry_unavailable"}), 503
        row = session_service.get_owned(auth_session=auth_session, session_id=session_id)
        if row is None:
            return jsonify({"success": False, "error": "Not found"}), 404
        return jsonify({"success": True, "session": row}), 200

    @bp.route("/api/v1/companion/sessions/<session_id>/continue", methods=["POST"])
    @user_auth_decorator
    def continue_session(session_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        if session_service is None:
            return jsonify({"success": False, "error": "session_registry_unavailable"}), 503
        body = request.get_json(silent=True) or {}
        request_id = str(body.get("continuation_request_id") or "").strip()
        if not request_id:
            return jsonify({"success": False, "error": "continuation_request_id is required"}), 400
        try:
            row = session_service.continue_from(
                auth_session=auth_session,
                previous_session_id=session_id,
                request_id=request_id,
            )
        except LookupError:
            return jsonify({"success": False, "error": "Not found"}), 404
        except PermissionError:
            return jsonify({"success": False, "error": "Forbidden"}), 403
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 409
        return jsonify({"success": True, "session": row}), 201

    @bp.route("/api/v1/companion/sessions/<session_id>/close", methods=["POST"])
    @user_auth_decorator
    def close_session(session_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        if session_service is None:
            return jsonify({"success": False, "error": "session_registry_unavailable"}), 503
        body = request.get_json(silent=True) or {}
        try:
            expected_version = int(body.get("expected_lifecycle_version"))
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "expected_lifecycle_version is required"}), 400
        try:
            row = session_service.close(
                auth_session=auth_session,
                session_id=session_id,
                expected_version=expected_version,
            )
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 409
        if row is None:
            return jsonify({"success": False, "error": "Not found"}), 404
        return jsonify({"success": True, "session": row}), 200

    @bp.route("/api/v1/companion/sessions/<session_id>/history", methods=["GET"])
    @user_auth_decorator
    def session_history(session_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        invalid = _authorize_session(session_id, auth_session, session_service, expected_companion_session_id, require_active=False)
        if invalid is not None:
            return invalid
        try:
            limit = max(1, min(int(request.args.get("limit", "100")), 500))
        except (TypeError, ValueError):
            limit = 100
        before_cursor = request.args.get("before") or None
        messages = db_get_companion_messages(session_id, limit=limit, before_cursor=before_cursor)
        total = db_count_companion_messages(session_id)
        return jsonify({
            "success": True,
            "session_id": session_id,
            "messages": messages,
            "total": total,
            "has_more": total > len(messages) if not before_cursor else len(messages) == limit,
            "limit": limit,
        }), 200

    @bp.route("/api/v1/ai-companion/chat", methods=["POST"])
    @user_auth_decorator
    def ai_companion_chat(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        messages = body.get("messages", [])
        session_id = (
            str(body.get("session_id", "") or "").strip() or str(uuid.uuid4())
        )
        if not isinstance(messages, list):
            return jsonify(
                {"success": False, "error": "messages array is required"}
            ), 400
        latest_user_msg = ""
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                latest_user_msg = str(message.get("content", "") or "").strip()
                break
        outcome = turn_service.run_turn(
            CompanionTurnRequest(
                client_id=auth_session["client_id"],
                session_id=session_id,
                user_message=latest_user_msg,
                channel="text",
                persist=bool(latest_user_msg),
            )
        )
        return jsonify(_v1_payload(outcome)), outcome.http_status

    @bp.route(
        "/api/v1/ai-companion/history/<companion_session_id>",
        methods=["GET"],
    )
    @user_auth_decorator
    def ai_companion_history(
        companion_session_id: str,
        auth_session: Dict[str, Any],
    ) -> Tuple[Any, int]:
        invalid = _authorize_session(companion_session_id, auth_session, session_service, expected_companion_session_id, require_active=False)
        if invalid is not None:
            return invalid
        try:
            limit = int(request.args.get("limit", "100"))
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 500))
        before_cursor = request.args.get("before") or None
        messages = db_get_companion_messages(
            companion_session_id,
            limit=limit,
            before_cursor=before_cursor,
        )
        total = db_count_companion_messages(companion_session_id)
        has_more = (
            total > len(messages) if not before_cursor else len(messages) == limit
        )
        return jsonify(
            {
                "success": True,
                "session_id": companion_session_id,
                "messages": messages,
                "total": total,
                "has_more": has_more,
                "limit": limit,
            }
        ), 200

    @bp.route("/api/v1/companion/transcribe", methods=["POST"])
    @user_auth_decorator
    def companion_transcribe(_auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        if "audio" not in request.files:
            return jsonify(
                {"success": False, "error": "audio file is required"}
            ), 400
        audio_file = request.files["audio"]
        audio_bytes = audio_file.read()
        if len(audio_bytes) > _STT_MAX_BYTES:
            return jsonify(
                {
                    "success": False,
                    "error": (
                        f"Audio file too large ({len(audio_bytes)} bytes). "
                        f"Max {_STT_MAX_BYTES} bytes."
                    ),
                }
            ), 413
        if not audio_bytes:
            return jsonify({"success": False, "error": "Audio file is empty"}), 400
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            return jsonify(
                {"success": False, "error": "Transcription service unavailable"}
            ), 503
        try:
            import httpx
            from openai import OpenAI

            client = OpenAI(
                api_key=api_key,
                timeout=60.0,
                max_retries=1,
                http_client=httpx.Client(timeout=60.0, trust_env=False),
            )
            filename = audio_file.filename or "voice.m4a"
            stt_model = os.getenv(
                "COMPANION_STT_MODEL",
                "gpt-4o-mini-transcribe",
            )
            last_exc: Exception | None = None
            for attempt in range(1, 4):
                try:
                    result = client.audio.transcriptions.create(
                        model=stt_model,
                        file=(filename, io.BytesIO(audio_bytes)),
                        prompt=_STT_DOMAIN_PROMPT,
                    )
                    transcript = result.text.strip() if result.text else ""
                    return jsonify(
                        {"success": True, "transcript": transcript}
                    ), 200
                except Exception as exc:  # pylint: disable=broad-except
                    last_exc = exc
                    if attempt < 3:
                        time.sleep(0.75 * attempt)
            raise last_exc or RuntimeError("unknown transcription failure")
        except Exception as exc:  # pylint: disable=broad-except
            print(
                f"[transcribe] Failed after retries: {type(exc).__name__}: {exc}",
                flush=True,
            )
            return jsonify(
                {"success": False, "error": "Transcription failed"}
            ), 503

    @bp.route(
        "/api/v1/companion/sessions/<session_id>/message",
        methods=["POST"],
    )
    @user_auth_decorator
    def companion_message(
        session_id: str,
        auth_session: Dict[str, Any],
    ) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        invalid = _validate_session_and_body(
            session_id,
            auth_session,
            body,
            expected_companion_session_id,
            session_service,
        )
        if invalid is not None:
            return invalid
        turn_request = _request_from_v1_body(
                client_id=auth_session["client_id"],
                session_id=session_id,
                body=body,
                stream=False,
            )
        try:
            receipt = turn_service.accept_turn(turn_request)
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 409
        if receipt and not receipt.get("created"):
            return jsonify({"success": True, "turn": receipt}), 202
        outcome = turn_service.run_turn(turn_request, accepted_turn=receipt)
        return jsonify(_v1_payload(outcome)), outcome.http_status

    @bp.route(
        "/api/v1/companion/sessions/<session_id>/entry",
        methods=["POST"],
    )
    @user_auth_decorator
    def companion_entry(
        session_id: str,
        auth_session: Dict[str, Any],
    ) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        invalid = _authorize_session(session_id, auth_session, session_service, expected_companion_session_id)
        if invalid is not None:
            return invalid
        turn_request = CompanionTurnRequest(
                client_id=auth_session["client_id"],
                session_id=session_id,
                user_message="",
                turn_type="app_entry",
                channel=str(body.get("channel") or "text"),
                input_source=body.get("input_source") or "app_entry",
                persist=True,
                client_turn_id=str(body.get("client_turn_id") or "").strip() or None,
            )
        try:
            receipt = turn_service.accept_turn(turn_request)
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 409
        if receipt and not receipt.get("created"):
            return jsonify({"success": True, "turn": receipt}), 202
        outcome = turn_service.run_turn(turn_request, accepted_turn=receipt)
        return jsonify(_v1_payload(outcome)), outcome.http_status

    @bp.route(
        "/api/v1/companion/sessions/<session_id>/message-stream",
        methods=["POST"],
    )
    @user_auth_decorator
    def companion_message_stream(
        session_id: str,
        auth_session: Dict[str, Any],
    ) -> Any:
        body = request.get_json(silent=True) or {}
        invalid = _validate_session_and_body(
            session_id,
            auth_session,
            body,
            expected_companion_session_id,
            session_service,
        )
        if invalid is not None:
            return invalid
        turn_request = _request_from_v1_body(
            client_id=auth_session["client_id"],
            session_id=session_id,
            body=body,
            stream=True,
        )
        try:
            receipt = turn_service.accept_turn(turn_request)
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 409
        event_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        request_id = receipt["turn_id"] if receipt else f"stream_{uuid.uuid4().hex[:12]}"

        @copy_current_request_context
        def run_turn_in_background() -> None:
            emitted_delta = False

            def on_delta(delta: str) -> None:
                nonlocal emitted_delta
                if delta:
                    emitted_delta = True
                    event_queue.put(("response.delta", {"delta": delta}))

            try:
                outcome = turn_service.run_turn(
                    turn_request,
                    CompanionTurnCallbacks(
                        response_delta=on_delta,
                        client_action_completed=lambda result: event_queue.put(
                            ("client_action.completed", {"result": result})
                        ),
                    ),
                    accepted_turn=receipt,
                )
                payload = _v1_payload(outcome)
                if not outcome.success:
                    event_queue.put(
                        (
                            "response.error",
                            {
                                "success": False,
                                "http_status": outcome.http_status,
                                "status": payload.get("status") or "failed",
                                "error": payload.get("error")
                                or "Advisor runtime failed",
                                "trace_id": payload.get("trace_id"),
                                "turn_id": payload.get("turn_id"),
                                "timing": payload.get("timing", {}),
                                "errors": payload.get("errors", []),
                            },
                        )
                    )
                    return
                if outcome.assistant_message and not emitted_delta:
                    event_queue.put(
                        (
                            "response.delta",
                            {"delta": outcome.assistant_message},
                        )
                    )
                event_queue.put(
                    (
                        "response.completed",
                        {
                            "http_status": outcome.http_status,
                            "payload": payload,
                        },
                    )
                )
            except Exception as exc:  # pylint: disable=broad-except
                print(
                    f"[companion/stream] advisor SSE generation failed: {exc}",
                    flush=True,
                )
                event_queue.put(
                    (
                        "response.error",
                        {
                            "success": False,
                            "error": "Stream processing failed",
                        },
                    )
                )
            finally:
                event_queue.put(("done", {"done": True}))

        if not receipt or receipt.get("created"):
            threading.Thread(
                target=run_turn_in_background,
                name=f"awm-companion-stream-{request_id}",
                daemon=True,
            ).start()

        def generate_sse():
            yield _sse_event(
                "accepted",
                (
                    receipt
                    if receipt
                    else {"request_id": request_id, "session_id": session_id}
                ),
            )
            if receipt and not receipt.get("created"):
                yield _sse_event("turn.status", receipt)
                yield _sse_event("done", {"done": True})
                return
            while True:
                try:
                    event_name, event_payload = event_queue.get(timeout=10.0)
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    continue
                yield _sse_event(event_name, event_payload)
                if event_name == "done":
                    return

        return Response(
            stream_with_context(generate_sse()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @bp.route(
        "/api/v1/companion/sessions/<session_id>/turns/<turn_id>",
        methods=["GET"],
    )
    @user_auth_decorator
    def get_turn_status(
        session_id: str,
        turn_id: str,
        auth_session: Dict[str, Any],
    ) -> Tuple[Any, int]:
        invalid = _authorize_session(
            session_id,
            auth_session,
            session_service,
            expected_companion_session_id,
            require_active=False,
        )
        if invalid is not None:
            return invalid
        turn = turn_service.get_turn(
            client_id=auth_session["client_id"],
            session_id=session_id,
            turn_id=turn_id,
        )
        if turn is None:
            return jsonify({"success": False, "error": "Not found"}), 404
        return jsonify({"success": True, "turn": turn}), 200

    @bp.route(
        "/api/v1/companion/sessions/<session_id>/turns",
        methods=["GET"],
    )
    @user_auth_decorator
    def list_turn_statuses(
        session_id: str,
        auth_session: Dict[str, Any],
    ) -> Tuple[Any, int]:
        invalid = _authorize_session(
            session_id,
            auth_session,
            session_service,
            expected_companion_session_id,
            require_active=False,
        )
        if invalid is not None:
            return invalid
        try:
            limit = int(request.args.get("limit", "20"))
        except (TypeError, ValueError):
            limit = 20
        active_only = request.args.get("status", "active") == "active"
        turns = turn_service.list_turns(
            client_id=auth_session["client_id"],
            session_id=session_id,
            active_only=active_only,
            limit=limit,
        )
        return jsonify({"success": True, "turns": turns}), 200

    return bp


def _request_from_v1_body(
    *,
    client_id: str,
    session_id: str,
    body: Dict[str, Any],
    stream: bool,
) -> CompanionTurnRequest:
    input_source = body.get("input_source")
    channel = (
        "voice"
        if input_source == "voice_message"
        else str(body.get("channel") or "text")
    )
    return CompanionTurnRequest(
        client_id=client_id,
        session_id=session_id,
        user_message=str(body.get("message") or "").strip(),
        turn_type=(
            "client_action"
            if parse_client_action(body.get("client_action")) is not None
            and not str(body.get("message") or "").strip()
            else "user_message"
        ),
        channel=channel,
        input_source=input_source,
        client_action=parse_client_action(body.get("client_action")),
        active_skill=_client_active_skill(body),
        persist=True,
        stream=stream,
        client_turn_id=str(body.get("client_turn_id") or "").strip() or None,
    )


def _validate_session_and_body(
    session_id: str,
    auth_session: Dict[str, Any],
    body: Dict[str, Any],
    expected_companion_session_id: Callable[[Dict[str, Any]], str],
    session_service: CompanionSessionService | None = None,
) -> Optional[Tuple[Any, int]]:
    invalid = _authorize_session(session_id, auth_session, session_service, expected_companion_session_id)
    if invalid is not None:
        return invalid
    parsed_action = parse_client_action(body.get("client_action"))
    if not str(body.get("message") or "").strip() and parsed_action is None:
        return jsonify({"success": False, "error": "message or client_action is required"}), 400
    if (
        body.get("client_action") is not None
        and parse_client_action(body.get("client_action")) is None
    ):
        return jsonify({"success": False, "error": "invalid client_action"}), 400
    if (
        session_service is not None
        and not str(body.get("client_turn_id") or "").strip()
        and os.getenv("AWM_ALLOW_LEGACY_COMPANION_WRITES", "false").strip().lower()
        not in {"1", "true", "yes"}
    ):
        return jsonify({"success": False, "error": "client_turn_id is required"}), 400
    return None


def _authorize_session(
    session_id: str,
    auth_session: Dict[str, Any],
    session_service: CompanionSessionService | None,
    expected_companion_session_id: Callable[[Dict[str, Any]], str],
    *,
    require_active: bool = True,
) -> Optional[Tuple[Any, int]]:
    if session_service is None:
        if session_id == expected_companion_session_id(auth_session):
            return None
        return jsonify({"success": False, "error": "Forbidden"}), 403
    row = session_service.get_owned(auth_session=auth_session, session_id=session_id)
    if row is None:
        if require_active and session_service.authorize_write(auth_session=auth_session, session_id=session_id) == "ok":
            return None
        return jsonify({"success": False, "error": "Forbidden"}), 403
    if require_active and row["status"] != "active":
        return jsonify({"success": False, "error": "session_closed"}), 409
    return None


def _v1_payload(outcome: CompanionTurnOutcome) -> Dict[str, Any]:
    payload = outcome.canonical_payload()
    payload.update(
        {
            "response": outcome.assistant_message,
            "message": outcome.assistant_message,
            "assistant_message": outcome.assistant_message,
            "action_type": "chat",
            "error": _first_error_message(payload.get("errors", []))
            if not outcome.success
            else None,
        }
    )
    return payload


def _first_error_message(errors: Any) -> Optional[str]:
    if not isinstance(errors, list) or not errors:
        return None
    first = errors[0]
    if isinstance(first, dict):
        return str(first.get("message") or first.get("error") or "") or None
    return str(first)


def _client_active_skill(body: Dict[str, Any]) -> str | None:
    requested = str(body.get("active_skill") or "").strip()
    return requested if requested == "investment-consult" else None


def _json_default(value: Any) -> str:
    """Serialize payload values that json.dumps cannot handle natively.

    The non-streaming routes go through Flask's ``jsonify``, which already
    tolerates datetimes. Without this the stream generator raises mid-response
    and the client never receives ``response.completed``.
    """
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _sse_event(event_name: str, payload: Dict[str, Any]) -> str:
    return (
        f"event: {event_name}\n"
        f"data: {json.dumps(payload, ensure_ascii=True, default=_json_default)}\n\n"
    )


__all__ = ["create_companion_blueprint"]
