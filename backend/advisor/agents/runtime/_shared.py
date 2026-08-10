from __future__ import annotations

import json
from typing import Any, Dict, Optional


def _runtime_number(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _subagent_artifact_key(artifact: Dict[str, Any]) -> str:
    payload = artifact.get("payload") if isinstance(artifact.get("payload"), dict) else {}
    source_assessment = payload.get("source_assessment") if isinstance(payload.get("source_assessment"), dict) else {}
    identity = (
        payload.get("id")
        or payload.get("proposal_id")
        or payload.get("assessment_id")
        or source_assessment.get("assessment_id")
        or json.dumps(payload, sort_keys=True, default=str)[:200]
    )
    return "|".join(
        str(part or "")
        for part in (artifact.get("artifact_type"), artifact.get("writeback_target"), identity)
    )
