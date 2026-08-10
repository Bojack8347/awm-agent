"""Server-authored terminal result for unsupported calculation follow-ups."""

from __future__ import annotations

from typing import Any, Dict


_REASON_DETAILS = {
    "unsupported_operation": (
        "The requested operation is outside the current calculator catalog."
    ),
    "cross_domain_calculation": (
        "The requested calculation combines evidence domains that the current tools "
        "cannot validate together."
    ),
    "unsupported_source": (
        "One or more requested operands do not have a supported, typed evidence source."
    ),
    "external_solver_unavailable": (
        "The optional external pure-math solver is unavailable or cannot receive the "
        "request under the current privacy boundary."
    ),
    "external_solver_unvalidated": (
        "The external solver did not return one unambiguous finite scalar result that "
        "the server could validate."
    ),
}


def build_calculation_capability_gap(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Return a bounded result whose client message is authored by the server."""

    reason_code = str(arguments.get("reason_code") or "").strip()
    if reason_code not in _REASON_DETAILS:
        reason_code = "unsupported_operation"
    request_summary = " ".join(
        str(arguments.get("request_summary") or "").split()
    )[:240]
    source_refs = list(
        dict.fromkeys(
            str(item).strip()
            for item in (arguments.get("source_refs") or [])
            if str(item).strip()
        )
    )[:8]
    client_message = (
        "I can’t complete that follow-up calculation with the currently available "
        f"validated tools. {_REASON_DETAILS[reason_code]} I won’t estimate it in "
        "prose or keep retrying tools."
    )
    return {
        "schema_version": "awm.calculation_capability_gap.v1",
        "status": "unsupported",
        "terminal": True,
        "retry_allowed": False,
        "reason_code": reason_code,
        "request_summary": request_summary,
        "source_refs": source_refs,
        "client_message": client_message,
    }
