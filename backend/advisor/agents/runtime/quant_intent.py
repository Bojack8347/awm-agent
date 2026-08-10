from __future__ import annotations


def _is_explicit_read_only_quant_request(user_message: str) -> bool:
    text = " ".join(str(user_message or "").lower().split())
    return any(
        marker in text
        for marker in (
            "read-only",
            "read only",
            "do not save",
            "don't save",
            "without saving",
            "no proposal",
            "do not create a proposal",
            "don't create a proposal",
            "do not execute",
            "don't execute",
        )
    )


def _stored_followup_requests_assumptions(user_message: str) -> bool:
    text = " ".join(str(user_message or "").lower().split())
    return any(
        marker in text
        for marker in (
            "assumption",
            "default",
            "configured parameter",
            "effective parameter",
        )
    )


def _remove_applied_assumptions_appendix(response_text: str) -> str:
    """Remove an unrequested model-authored assumption appendix from a narrow follow-up."""

    marker = "\n\nApplied model assumptions:"
    start = str(response_text or "").find(marker)
    if start < 0:
        return response_text
    tail = response_text[start + len(marker) :]
    next_section_candidates = [
        index
        for section in ("\n\nModel limitations:", "\n\nLimitations:", "\n\nWarnings:")
        if (index := tail.find(section)) >= 0
    ]
    if not next_section_candidates:
        return response_text[:start].rstrip()
    next_section = min(next_section_candidates)
    return (
        response_text[:start].rstrip()
        + tail[next_section:]
    ).strip()
