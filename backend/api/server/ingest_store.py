"""Consultation ingest cache and disk fallback."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from . import state


def _load_ingests_from_disk() -> None:
    """Hydrate in-memory ingest cache from NDJSON store."""
    if not state._INGEST_STORE_PATH.exists():
        return

    loaded_rows: Dict[str, Dict[str, Any]] = {}
    try:
        for raw_line in state._INGEST_STORE_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            ingest_id = str(row.get("ingest_id", "") or "").strip()
            if ingest_id:
                loaded_rows[ingest_id] = row
    except OSError:
        # Non-fatal: API still works with in-memory ingest cache.
        pass

    if loaded_rows:
        with state._INGEST_LOCK:
            state._CONSULTATION_INGESTS.update(loaded_rows)


def _append_ingest_to_disk(ingest_payload: Dict[str, Any]) -> None:
    """Append ingest payload to NDJSON store for restart resilience."""
    state._INGEST_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = json.dumps(ingest_payload, ensure_ascii=True)
    with state._INGEST_LOCK:
        with state._INGEST_STORE_PATH.open("a", encoding="utf-8") as handle:
            handle.write(record + "\n")


def _get_ingested_consultation(ingest_id: str) -> Optional[Dict[str, Any]]:
    """Fetch consultation ingest payload by ID."""
    if not ingest_id:
        return None
    with state._INGEST_LOCK:
        return state._CONSULTATION_INGESTS.get(ingest_id)


_load_ingests_from_disk()
