"""Process-local API server state and task trackers."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Set

from advisor.agents.background_jobs import set_specialist_job_executor

from .bootstrap import _SERVICE_DIR

_CONSULTATION_TASKS: Dict[str, Dict[str, Any]] = {}
_TASK_LOCK = threading.Lock()


def _background_max_workers() -> int:
    raw = os.getenv("AWM_BACKGROUND_MAX_WORKERS", "4")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 4


_BACKGROUND_EXECUTOR = ThreadPoolExecutor(
    max_workers=_background_max_workers(),
    thread_name_prefix="awm-background",
)
set_specialist_job_executor(_BACKGROUND_EXECUTOR)

_DIAGNOSIS_REFRESH_LOCK = threading.Lock()
_DIAGNOSIS_REFRESH_RUNNING: Set[str] = set()

_CONSULTATION_INGESTS: Dict[str, Dict[str, Any]] = {}
_INGEST_LOCK = threading.Lock()
_INGEST_STORE_PATH = Path(
    os.getenv(
        "ADVISOR_INGEST_STORE_PATH",
        str(_SERVICE_DIR / "logs" / "consultation_ingests.ndjson"),
    )
)
