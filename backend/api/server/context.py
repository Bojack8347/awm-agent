"""Context builders for companion requests."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from advisor.runtime.run_context import AgentRunContext
from .deps import get_companion_service


def _build_companion_context(
    *,
    client_id: str,
    session_id: str,
    user_message: str,
    knowledge_snapshot: Optional[Dict[str, Any]],
    diagnosis_snapshot: Optional[Dict[str, Any]],
    pending_confirmations: List[Dict[str, Any]],
    cached_recent_turns: Optional[List[Dict[str, Any]]] = None,
    ctx: Optional[AgentRunContext] = None,
) -> Dict[str, Any]:
    return get_companion_service().build_context(
        client_id=client_id,
        session_id=session_id,
        user_message=user_message,
        knowledge_snapshot=knowledge_snapshot,
        diagnosis_snapshot=diagnosis_snapshot,
        pending_confirmations=pending_confirmations,
        cached_recent_turns=cached_recent_turns,
        ctx=ctx,
    )
