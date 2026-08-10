"""Deterministic validation rules for activation mutations.

Each rule is a plain function. Adding a new rule = writing a function and
appending it to ALL_RULES.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class ValidationError(Exception):
    """Raised when mutations fail validation."""

    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__(f"Activation mutation validation failed: {errors}")


# ---------------------------------------------------------------------------
# Valid taxonomy
# ---------------------------------------------------------------------------

VALID_DOMAINS = {"wealth", "people", "healthcare"}

VALID_CATEGORIES: Dict[str, set] = {
    "wealth": {
        "client_profile", "accounts", "asset_allocation", "liabilities",
        "income", "recurring_expenses", "non_recurring_expenses",
        "insurance", "preferences", "tax",
    },
    "people": {
        "personal_information", "dependents", "employment", "household",
    },
    "healthcare": {
        "coverage", "insurance", "wellness",
    },
}


# ---------------------------------------------------------------------------
# Individual validation rules
# ---------------------------------------------------------------------------

def validate_required_fields(
    mutations: List[Dict[str, Any]],
    current_facts: List[Dict[str, Any]],
) -> List[str]:
    """Every mutation must have domain, category, label, and value."""
    errors = []
    for i, m in enumerate(mutations):
        for field in ("domain", "category", "label"):
            if not m.get(field):
                errors.append(f"Mutation {i}: missing required field '{field}'")
        if "value" not in m:
            errors.append(f"Mutation {i}: missing required field 'value'")
    return errors


def validate_domain_category(
    mutations: List[Dict[str, Any]],
    current_facts: List[Dict[str, Any]],
) -> List[str]:
    """Domain and category must be from the known taxonomy."""
    errors = []
    for i, m in enumerate(mutations):
        domain = m.get("domain", "")
        category = m.get("category", "")
        if domain and domain not in VALID_DOMAINS:
            errors.append(
                f"Mutation {i}: unknown domain '{domain}' "
                f"(valid: {sorted(VALID_DOMAINS)})"
            )
        elif domain and category:
            valid_cats = VALID_CATEGORIES.get(domain, set())
            if category not in valid_cats:
                errors.append(
                    f"Mutation {i}: unknown category '{category}' "
                    f"for domain '{domain}' (valid: {sorted(valid_cats)})"
                )
    return errors


def validate_referential_integrity(
    mutations: List[Dict[str, Any]],
    current_facts: List[Dict[str, Any]],
) -> List[str]:
    """If a mutation has an id, it must reference an existing fact."""
    existing_ids = {f["id"] for f in current_facts if f.get("id")}
    errors = []
    for i, m in enumerate(mutations):
        mid = m.get("id")
        if mid is not None and mid not in existing_ids:
            errors.append(
                f"Mutation {i}: references non-existent fact id '{mid}'"
            )
    return errors


def validate_no_negative_balances(
    mutations: List[Dict[str, Any]],
    current_facts: List[Dict[str, Any]],
) -> List[str]:
    """No account balance should go below zero."""
    errors = []
    for i, m in enumerate(mutations):
        if m.get("category") != "accounts":
            continue
        balance = _extract_balance(m.get("value"))
        if balance is not None and balance < 0:
            errors.append(
                f"Mutation {i} ({m.get('label', '?')}): "
                f"negative balance {balance}"
            )
    return errors


def validate_no_duplicate_fact_ids(
    mutations: List[Dict[str, Any]],
    current_facts: List[Dict[str, Any]],
) -> List[str]:
    """Same fact ID should not be mutated more than once."""
    seen: Dict[str, int] = {}
    errors = []
    for i, m in enumerate(mutations):
        mid = m.get("id")
        if mid is None:
            continue
        if mid in seen:
            errors.append(
                f"Mutation {i}: duplicate update to fact id '{mid}' "
                f"(first seen at mutation {seen[mid]})"
            )
        else:
            seen[mid] = i
    return errors


def validate_confidence_range(
    mutations: List[Dict[str, Any]],
    current_facts: List[Dict[str, Any]],
) -> List[str]:
    """Confidence must be between 0 and 1."""
    errors = []
    for i, m in enumerate(mutations):
        conf = m.get("confidence")
        if conf is not None and not (0 <= conf <= 1):
            errors.append(
                f"Mutation {i}: confidence {conf} outside [0, 1]"
            )
    return errors


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_balance(value: Any) -> Optional[float]:
    """Extract a numeric balance from a fact value."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("balance", "amount", "value"):
            v = value.get(key)
            if isinstance(v, (int, float)):
                return float(v)
    return None


# ---------------------------------------------------------------------------
# Rule registry — append new rules here
# ---------------------------------------------------------------------------

ALL_RULES = [
    validate_required_fields,
    validate_domain_category,
    validate_referential_integrity,
    validate_no_negative_balances,
    validate_no_duplicate_fact_ids,
    validate_confidence_range,
]


def validate_mutations(
    mutations: List[Dict[str, Any]],
    current_facts: List[Dict[str, Any]],
) -> None:
    """Run all validation rules. Raises ValidationError if any fail."""
    all_errors: List[str] = []
    for rule in ALL_RULES:
        errors = rule(mutations, current_facts)
        all_errors.extend(errors)
    if all_errors:
        raise ValidationError(all_errors)
