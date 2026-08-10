"""ToolDefinition — per-tool metadata that drives concurrency decisions.


Today, `ToolLoopRunner._execute_tool_calls_batch()` contains hardcoded
checks against tool names and the `use_latest_asset_allocation` flag to
decide whether a batch of tool calls can run concurrently. That logic
forces the loop to know tool semantics — a brittle pattern that won't
scale to a third tool.

This module declares the same decision *on each tool*, then provides
`partition_tool_calls()` which separates a list of calls into
all-concurrent or one-serial batches based on each tool's declaration.
The loop then executes each batch (concurrent in a thread pool, serial
one-at-a-time) without knowing tool names.

Phase 2 is a structural refactor with no behavior change. The plan's
exit criterion explicitly verifies the `use_latest_asset_allocation=True`
case still serializes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Union

from advisor.tools.deterministic_tools.common.compactors import Compactor, ManifestExtras


ConcurrencySafe = Union[bool, Callable[[Dict[str, Any]], bool]]


@dataclass(frozen=True)
class ToolDefinition:
    """Per-tool metadata.

    Attributes:
        name: Canonical tool name (must match what `_canonical_tool_name`
            returns in `tool_loop.py`).
        description: Human-readable; used in registries / docs only.
        is_concurrent_safe: Either a static `bool` or a callable that takes
            the call's arguments dict and returns a bool. The callable form
            covers input-dependent rules — e.g. cashflow with
            `use_latest_asset_allocation=True` reads state that a sibling
            asset allocation call could be writing, so that case must be serial.
        is_read_only: Whether the tool mutates persistent state. Reserved
            for future use (cache invalidation, audit, planner heuristics).
        max_calls_per_iteration: Defensive cap; loops impose stricter
            per-iteration caps via their config today.
        max_result_size_chars: Soft budget for the prompt-ready tool
            result. The loop tracks `prompt_size_chars` returned by the
            compactor against this; today exceeding the budget is logged
            via the compaction metadata (no hard truncation), matching
            Claude Code's `Tool.ts` advisory contract. Bumped to 80K to
            cover the largest cashflow analytical view in practice.
        compactor: Optional callable that turns a raw tool result into a
            prompt-ready replacement plus compaction metadata. Tools
            without a compactor are passed through unchanged with
            `strategy="full"`. See `tools.common.compactors` for the
            protocol and shared helpers.
        manifest_extras: Optional callable that returns extra fields to
            merge into the cold-tier server-side artifact manifest
            (e.g. `available_detail_sections` for cashflow). Reserved
            for tools whose raw payload structure is worth surfacing
            in the manifest.
    """

    name: str
    description: str
    is_concurrent_safe: ConcurrencySafe
    is_read_only: bool
    capability: str = ""
    max_calls_per_iteration: int = 6
    max_result_size_chars: int = 80_000
    compactor: Optional[Compactor] = None
    manifest_extras: Optional[ManifestExtras] = None


def is_concurrent_safe(defn: ToolDefinition, args: Dict[str, Any]) -> bool:
    """Resolve `is_concurrent_safe` against an actual call's args.

    Centralises the bool-or-callable check so the loop never has to inline
    this logic.
    """
    flag = defn.is_concurrent_safe
    if callable(flag):
        try:
            return bool(flag(args or {}))
        except Exception:
            # A predicate that raises is treated as unsafe — failing closed
            # is the conservative choice: we'd rather serialise an extra
            # call than parallelise something that aliases shared state.
            return False
    return bool(flag)


# ---------------------------------------------------------------------------
# Partitioning
# ---------------------------------------------------------------------------

@dataclass
class _Call:
    """Adapter shape used internally by `partition_tool_calls`.

    The function accepts any sequence of objects exposing `name` (str) and
    `args` (dict) — the loop's `pending` rows already match. We don't
    impose a stricter type so callers can keep using their own shapes.
    """

    name: str
    args: Dict[str, Any]


def _resolve(defn_or_none: Union[ToolDefinition, None], args: Dict[str, Any]) -> bool:
    """Default-deny when a tool is unregistered: unknown tools serialise."""
    if defn_or_none is None:
        return False
    return is_concurrent_safe(defn_or_none, args)


def partition_tool_calls(
    calls: List[Any],
    registry: Mapping[str, ToolDefinition],
    *,
    name_attr: str = "name",
    args_attr: str = "args",
) -> List[List[Any]]:
    """Group calls into batches that may run as a single unit.

    Each returned batch is one of:
      - **all-concurrent**: every call's tool is concurrency-safe for its
        args — execute them in parallel.
      - **serial singleton**: a single call whose tool must run alone in
        its own batch — execute synchronously, no neighbours.

    Order is preserved: a serial call splits the surrounding concurrent
    batches, mirroring how the loop already serialises allocation-dependent
    cashflow calls today.

    Args:
        calls: Sequence of objects exposing `.name` (str) and `.args` (dict),
            or whatever attribute names are passed via `name_attr`/`args_attr`.
        registry: Mapping from canonical tool name to `ToolDefinition`.
        name_attr: Attribute on each call element that holds its tool name.
        args_attr: Attribute on each call element that holds its arg dict.

    Returns:
        A list of batches; each batch is a list of the original call
        elements (not copies). Empty input -> empty list.
    """
    batches: List[List[Any]] = []
    current_concurrent: List[Any] = []

    for call in calls:
        name = str(getattr(call, name_attr, "") or "")
        args = getattr(call, args_attr, None) or {}
        if not isinstance(args, dict):
            args = {}

        defn = registry.get(name)
        safe = _resolve(defn, args)

        if safe:
            current_concurrent.append(call)
        else:
            if current_concurrent:
                batches.append(current_concurrent)
                current_concurrent = []
            batches.append([call])  # serial singleton

    if current_concurrent:
        batches.append(current_concurrent)
    return batches
