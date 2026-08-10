"""Latency instrumentation helpers for HTTP handlers."""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from typing import Any, Dict, Generator


_perf_log = logging.getLogger("awm.perf")
_perf_log.setLevel(logging.INFO)
if not _perf_log.handlers:
    _perf_handler = logging.StreamHandler()
    _perf_handler.setFormatter(logging.Formatter("%(message)s"))
    _perf_log.addHandler(_perf_handler)


@contextmanager
def timed(endpoint: str, stage: str) -> Generator[Dict[str, Any], None, None]:
    """Context manager that logs stage duration and collects Server-Timing entries."""
    ctx: Dict[str, Any] = {}
    t0 = time.perf_counter()
    try:
        yield ctx
    finally:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        ctx["duration_ms"] = round(elapsed_ms, 1)
        _perf_log.info(
            json.dumps({"endpoint": endpoint, "stage": stage, "duration_ms": ctx["duration_ms"]})
        )


def collect_server_timing(timings: list) -> str:
    """Build Server-Timing header value from (name, duration_ms) pairs."""
    return ", ".join(f"{name};dur={dur:.1f}" for name, dur in timings)


__all__ = ["timed", "collect_server_timing"]
