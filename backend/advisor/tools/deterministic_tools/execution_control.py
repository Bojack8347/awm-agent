"""Cooperative deadline control for detached read-only deterministic tools.

The Agents SDK can cancel an async tool invocation at its overall deadline, but
Python cannot forcibly stop a synchronous function already running in a worker
thread.  This module gives AWM's read-only model adapters a shared cancellation
signal so they can stop retries and refuse cache/state commits after the SDK has
returned a timeout result.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional, TypeVar


T = TypeVar("T")


@dataclass
class ReadOnlyToolExecutionControl:
    """One monotonic deadline and cancellation signal for a read-only tool call."""

    deadline_monotonic: float
    _cancelled: threading.Event = field(default_factory=threading.Event, repr=False)
    _commit_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def with_timeout(cls, timeout_seconds: float) -> "ReadOnlyToolExecutionControl":
        timeout = max(0.0, float(timeout_seconds))
        return cls(deadline_monotonic=time.monotonic() + timeout)

    def cancel(self) -> None:
        """Prevent all future cooperative retries and state commits."""

        with self._commit_lock:
            self._cancelled.set()

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_monotonic - time.monotonic())

    def is_cancelled(self) -> bool:
        if self._cancelled.is_set():
            return True
        if self.remaining_seconds() <= 0.0:
            self.cancel()
            return True
        return False

    def wait(self, delay_seconds: float) -> bool:
        """Wait interruptibly; return ``True`` when cancellation/deadline wins."""

        if self.is_cancelled():
            return True
        delay = min(max(0.0, float(delay_seconds)), self.remaining_seconds())
        if delay <= 0.0:
            self.cancel()
            return True
        if self._cancelled.wait(delay):
            return True
        return self.is_cancelled()

    def commit_if_active(self, callback: Callable[[], T]) -> tuple[bool, Optional[T]]:
        """Run a small state commit atomically with respect to cancellation."""

        with self._commit_lock:
            if self._cancelled.is_set() or time.monotonic() >= self.deadline_monotonic:
                self._cancelled.set()
                return False, None
            return True, callback()


_CURRENT_READ_ONLY_TOOL_CONTROL: ContextVar[Optional[ReadOnlyToolExecutionControl]] = (
    ContextVar("awm_current_read_only_tool_execution_control", default=None)
)


@contextmanager
def bind_read_only_tool_execution_control(
    control: ReadOnlyToolExecutionControl,
) -> Iterator[None]:
    """Expose one invocation's control to synchronous adapter code."""

    token = _CURRENT_READ_ONLY_TOOL_CONTROL.set(control)
    try:
        yield
    finally:
        _CURRENT_READ_ONLY_TOOL_CONTROL.reset(token)


def current_read_only_tool_execution_control() -> Optional[ReadOnlyToolExecutionControl]:
    return _CURRENT_READ_ONLY_TOOL_CONTROL.get()


def read_only_tool_execution_cancelled() -> bool:
    control = current_read_only_tool_execution_control()
    return bool(control and control.is_cancelled())


def effective_read_only_request_timeout(requested_seconds: float) -> float:
    """Cap one network attempt to the remaining overall tool deadline."""

    requested = max(0.001, float(requested_seconds))
    control = current_read_only_tool_execution_control()
    if control is None:
        return requested
    return max(0.001, min(requested, control.remaining_seconds()))


def wait_for_read_only_tool_cancellation(delay_seconds: float) -> bool:
    """Replace retry sleeps with an interruptible deadline-aware wait."""

    control = current_read_only_tool_execution_control()
    if control is None:
        time.sleep(max(0.0, float(delay_seconds)))
        return False
    return control.wait(delay_seconds)


def commit_read_only_tool_state(callback: Callable[[], T]) -> tuple[bool, Optional[T]]:
    """Commit adapter cache/state only while the SDK invocation is still active."""

    control = current_read_only_tool_execution_control()
    if control is None:
        return True, callback()
    return control.commit_if_active(callback)
