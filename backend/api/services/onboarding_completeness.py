"""Shared advisor-onboarding completeness over canonical typed Client File facts."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


REQUIRED_AREAS: Dict[str, frozenset[str]] = {
    "income": frozenset({"annual_income", "monthly_income", "weekly_income", "hourly_income"}),
    "expenses": frozenset({"annual_spending", "monthly_spending", "annual_expenses", "monthly_expenses"}),
    "accounts": frozenset({"cash", "taxable_brokerage", "retirement_accounts", "accounts", "holdings"}),
    "employment": frozenset({"employment_status", "employer", "occupation"}),
    "household": frozenset({"marital_status", "household", "spouse", "partner"}),
    "dependents": frozenset({"dependents", "dependent_count", "children"}),
}

# Four areas is the existing product threshold. The named result is shared by
# app-entry and proactive delivery so those paths cannot silently diverge.
COMPLETENESS_THRESHOLD = 4


def advisor_onboarding_completeness(
    canonical_facts: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return populated and missing discovery areas from typed fact rows only."""

    populated: set[str] = set()
    for row in canonical_facts:
        if not isinstance(row, dict):
            continue
        entity_id = str(row.get("entity_id") or "").strip().lower()
        value = row.get("value")
        if isinstance(value, dict):
            field = str(value.get("field") or "").strip().lower()
            actual = value.get("value", value)
        else:
            field = ""
            actual = value
        if actual is None or actual == "" or actual == [] or actual == {}:
            continue
        names = {entity_id, field} - {""}
        for area, accepted_names in REQUIRED_AREAS.items():
            if names & accepted_names:
                populated.add(area)

    missing: List[str] = [area for area in REQUIRED_AREAS if area not in populated]
    return {
        "complete": len(populated) >= COMPLETENESS_THRESHOLD,
        "populated_count": len(populated),
        "required_count": len(REQUIRED_AREAS),
        "threshold": COMPLETENESS_THRESHOLD,
        "populated_areas": [area for area in REQUIRED_AREAS if area in populated],
        "missing_areas": missing,
    }
