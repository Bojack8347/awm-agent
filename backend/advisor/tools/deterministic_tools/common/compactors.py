"""Per-tool prompt-result compactors.

safety declaration"), with the Claude Code `Tool.ts` interface that pairs
`isConcurrencySafe(input)` with `maxResultSizeChars` and a
tool-specific compaction strategy.

The loop should not hardcode tool-name branches to decide whether to
compact a result. That mixes tool semantics into the loop runtime — a
brittle pattern that won't scale to additional tools.

This module declares the compaction *on each tool*. `tool_definition.py`
exposes a `compactor` slot on `ToolDefinition`; the loop's
`_build_prompt_ready_tool_result()` looks up the compactor by tool name
and calls it. New tools register their compactor next to their
ToolDefinition in `agent_tool_catalog.py` — no loop changes required.

Two callable shapes are supported:

  - **Compactor**: `(raw_result) -> (compacted, metadata)`. Returns
    a prompt-ready replacement for `raw_result` plus a metadata dict
    that the loop merges into the audit/decision_relevance trail.
    Tools without a compactor are passed through unchanged with
    `strategy="full"`.

  - **ManifestExtras**: `(raw_result) -> dict`. Returns extra fields
    to merge into the cold-tier server-side artifact manifest
    (e.g. `available_detail_sections` for cashflow). Optional.

This module has zero side effects on import.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Public type aliases
# ---------------------------------------------------------------------------

CompactorResult = Tuple[Dict[str, Any], Dict[str, Any]]
"""(compacted_result, metadata). Metadata MUST carry the keys
`strategy`, `raw_size_chars`, `prompt_size_chars`,
`raw_result_retained_server_side`. Other keys (saved_chars,
omitted_detail_keys, etc.) are tool-specific."""

Compactor = Callable[[Dict[str, Any]], CompactorResult]
ManifestExtras = Callable[[Dict[str, Any]], Dict[str, Any]]


# ---------------------------------------------------------------------------
# Shared helpers (pure functions; previously methods on ToolLoopRunner)
# ---------------------------------------------------------------------------


def json_char_length(value: Any) -> int:
    """Approximate serialized JSON size for prompt budgeting."""
    try:
        return len(json.dumps(value, ensure_ascii=True, default=str))
    except Exception:  # pylint: disable=broad-except
        return len(str(value))


def first_matching_numeric(
    payload: Dict[str, Any], paths: List[List[str]]
) -> Optional[float]:
    """Read first numeric value from nested dict paths."""
    for path in paths:
        current: Any = payload
        for key in path:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        if isinstance(current, (int, float)):
            return float(current)
    return None


def passthrough_compactor(raw_result: Dict[str, Any]) -> CompactorResult:
    """No-op compactor: emit `raw_result` unchanged with strategy='full'.

    Useful as an explicit declaration on tools that intentionally do not
    benefit from compaction (small bounded payloads, etc.). Equivalent
    behaviorally to `compactor=None` but signals the choice was deliberate.
    """
    raw_size = json_char_length(raw_result)
    return raw_result, {
        "strategy": "full",
        "raw_size_chars": raw_size,
        "prompt_size_chars": raw_size,
        "raw_result_retained_server_side": True,
    }


# ---------------------------------------------------------------------------
# Cashflow compactor
# ---------------------------------------------------------------------------


def _cashflow_yearly_snapshot_sample(
    full_result: Dict[str, Any],
    max_snapshots: int = 8,
) -> Dict[str, Any]:
    """Compact deterministic yearly snapshots into a milestone sample."""
    details = full_result.get("details")
    if not isinstance(details, dict):
        return {}

    snapshots = details.get("yearly_snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        return {}

    inputs = details.get("inputs")
    if not isinstance(inputs, dict):
        inputs = {}

    available_years: List[int] = []
    snapshots_by_year: Dict[int, Dict[str, Any]] = {}
    for item in snapshots:
        if not isinstance(item, dict):
            continue
        year_value = item.get("year")
        if not isinstance(year_value, (int, float)):
            continue
        year = int(year_value)
        snapshots_by_year[year] = item
        available_years.append(year)

    if not available_years:
        return {}

    first_year = min(available_years)
    last_year = max(available_years)
    milestone_years = {
        first_year,
        last_year,
        first_year + 1 - 1,
        first_year + 3 - 1,
        first_year + 5 - 1,
        first_year + 10 - 1,
    }

    age = first_matching_numeric(inputs, [["age"]])
    retirement_age = first_matching_numeric(inputs, [["retirement_age"]])
    if age is not None and retirement_age is not None:
        retirement_year = first_year + max(0, int(retirement_age - age))
        milestone_years.add(retirement_year)

    education_year = first_matching_numeric(inputs, [["education_goal_year"]])
    if education_year is not None:
        milestone_years.add(int(education_year))

    one_off_expenses = inputs.get("one_off_expenses")
    if isinstance(one_off_expenses, list):
        for item in one_off_expenses:
            if isinstance(item, dict) and isinstance(item.get("year"), (int, float)):
                milestone_years.add(int(item["year"]))

    depletion_year = None
    for item in snapshots:
        if not isinstance(item, dict):
            continue
        total_assets = item.get("total_assets")
        year_value = item.get("year")
        if isinstance(total_assets, (int, float)) and isinstance(year_value, (int, float)):
            if float(total_assets) <= 0:
                depletion_year = int(year_value)
                break
    if depletion_year is not None:
        milestone_years.add(depletion_year)

    sampled_years = [
        year for year in sorted(milestone_years) if year in snapshots_by_year
    ]
    if len(sampled_years) > max_snapshots:
        # Preserve first/last and evenly sample interior years.
        interior = sampled_years[1:-1]
        keep_count = max(0, max_snapshots - 2)
        if keep_count and interior:
            step = max(1, len(interior) // keep_count)
            sampled_years = (
                [sampled_years[0]] + interior[::step][:keep_count] + [sampled_years[-1]]
            )
        else:
            sampled_years = sampled_years[:max_snapshots]

    sampled_snapshots = [snapshots_by_year[year] for year in sampled_years]
    return {
        "yearly_snapshot_count": len(snapshots),
        "sampled_yearly_snapshots": sampled_snapshots,
        "sampling_strategy": (
            "milestone-based sample preserving start, 1/3/5/10-year horizons, "
            "retirement, expected future expense years, depletion, and final year"
        ),
    }


def _compact_cashflow_full_result(full_result: Dict[str, Any]) -> Dict[str, Any]:
    """Build an analysis-friendly cashflow view for prompt context."""
    details = full_result.get("details")
    if not isinstance(details, dict):
        return full_result

    simulation_mode = str(full_result.get("simulation_mode", "") or "")
    compact_details: Dict[str, Any] = {}
    omitted_detail_keys: List[str] = []

    if isinstance(details.get("inputs"), dict):
        compact_details["inputs"] = details["inputs"]

    if simulation_mode == "deterministic":
        if "yearly_snapshots" in details:
            compact_details.update(_cashflow_yearly_snapshot_sample(full_result))
            omitted_detail_keys.append("yearly_snapshots")
    else:
        for key in (
            "num_simulations",
            "terminal_value_percentiles",
            "shortfall_percentiles",
            "milestone_percentile_trajectory",
            "failure_diagnostics",
            "non_recurring_expected_future_expense_diagnostics",
            "representative_path_summaries",
            "risk_features",
        ):
            if key in details:
                compact_details[key] = details[key]
        omitted_detail_keys.extend(
            key for key in details.keys() if key not in compact_details and key != "inputs"
        )

    return {
        "simulation_mode": full_result.get("simulation_mode"),
        "success": full_result.get("success"),
        "summary": full_result.get("summary"),
        "details": compact_details,
        "prompt_compaction": {
            "strategy": "analysis_focused",
            "raw_result_retained_server_side": True,
            "omitted_detail_keys": omitted_detail_keys,
        },
    }


def cashflow_compactor(raw_result: Dict[str, Any]) -> CompactorResult:
    """Cashflow compactor: replace large time-series with milestone samples.

    The loop's old code path returned the raw result unchanged when
    `full_result` was missing or non-dict. Preserved verbatim.
    """
    raw_size = json_char_length(raw_result)
    base_metadata: Dict[str, Any] = {
        "strategy": "full",
        "raw_size_chars": raw_size,
        "prompt_size_chars": raw_size,
        "raw_result_retained_server_side": True,
    }

    full_result = raw_result.get("full_result")
    if not isinstance(full_result, dict):
        return raw_result, base_metadata

    compacted = {
        "success": raw_result.get("success"),
        "full_result": _compact_cashflow_full_result(full_result),
    }
    compact_size = json_char_length(compacted)

    return compacted, {
        "strategy": "analysis_focused",
        "raw_size_chars": raw_size,
        "prompt_size_chars": compact_size,
        "saved_chars": max(0, raw_size - compact_size),
        "raw_result_retained_server_side": True,
    }


def cashflow_manifest_extras(raw_result: Dict[str, Any]) -> Dict[str, Any]:
    """Cashflow-specific cold-tier manifest fields.

    Adds `available_detail_sections` listing the keys retained in
    server-side raw artifacts. Preserves the prior in-loop behavior.
    """
    full_result = raw_result.get("full_result") if isinstance(raw_result, dict) else None
    details = full_result.get("details") if isinstance(full_result, dict) else None
    if isinstance(details, dict):
        return {"available_detail_sections": sorted(details.keys())}
    return {}
