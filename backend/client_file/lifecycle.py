"""Client File lifecycle helpers for advisory facts.

This is the small architecture layer between conversation extraction and
long-term Client File truth. It follows the business journey model:
consultations collect draft facts first, then an extraction/confirmation step
commits structured facts at a lifecycle boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from client_file.fact_vocabulary import (
    CANONICAL_FACT_FIELDS,
    CONFIDENCE_LEVELS,
    FACT_TYPES,
    FactField,
    ValidatedFacts,
    canonical_fact_name,
    normalize_fact_keys,
    validate_facts,
    validate_entities,
)

ASSESSMENT_SCHEMA_VERSION = "investment_assessment.v1"

_PUBLIC_METADATA_KEYS = {
    "source",
    "source_message_id",
    "observed_at",
    "note",
}


class FactWriteValidationError(ValueError):
    """Raised when a fact write cannot safely produce trusted facts."""

    def __init__(
        self,
        code: str,
        *,
        details: Optional[List[Dict[str, Any]]] = None,
        unrecognized: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.details = details or []
        self.unrecognized = unrecognized or {}


ONBOARDING_SLOTS = (
    "confirmed_linked_balances",
    "household_context",
    "income_context",
    "health_context",
    "future_goals",
)


def planning_is_fresh(client_file: Dict[str, Any]) -> bool:
    """Return whether consequential planning artifacts match Client File truth."""

    version = client_file.get("client_file_version")
    if not isinstance(version, int) or version <= 0:
        return True
    current = client_file.get("current_planning_set")
    refresh = client_file.get("planning_refresh")
    expected_fingerprint = (
        refresh.get("latest_requested_input_fingerprint")
        if isinstance(refresh, dict)
        else None
    )
    if not isinstance(current, dict) or current.get("status") != "ready":
        return False
    if expected_fingerprint:
        return current.get("source_input_fingerprint") == expected_fingerprint
    # Legacy artifact sets predate financial-input fingerprints.
    return current.get("source_client_version") == version

INVESTMENT_SLOTS = (
    "purpose",
    "amount",
    "horizon",
    "risk_tolerance",
    "source_of_funds",
)

def build_draft_fact_payload(
    *,
    fact_type: str,
    facts: Dict[str, Any],
    confidence: str,
    metadata: Dict[str, Any],
    entities: Optional[List[Dict[str, Any]]] = None,
    client_id: str = "",
) -> Dict[str, Any]:
    """Return a draft fact payload that is not yet long-term truth."""

    validated = (
        _validated_fact_write(facts, empty_error="no_structured_facts_to_draft")
        if facts else ValidatedFacts()
    )
    prepared_entities = _prepare_draft_entities(
        entities or [], client_id=client_id,
        source_message_id=str(metadata.get("source_message_id") or ""),
    )
    if not validated.canonical and not prepared_entities:
        raise FactWriteValidationError("no_structured_facts_to_draft")
    normalized_fact_type, normalized_confidence, coercions = _normalize_fact_headers(
        fact_type,
        confidence,
    )
    lifecycle_metadata = _fact_write_metadata(
        metadata,
        validated=validated,
        coercions=coercions,
        write_model="draft_then_commit",
    )
    draft_fingerprint = json.dumps(
        {"facts": validated.canonical, "entities": prepared_entities},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    draft_id = (
        f"draft:{normalized_fact_type}:"
        f"{hashlib.sha1(draft_fingerprint.encode('utf-8')).hexdigest()[:8]}"
    )
    group_seed = f"{client_id}:{metadata.get('source_message_id') or draft_id}:entity-group"
    draft_group_id = (
        f"draft-group:{uuid.uuid5(uuid.NAMESPACE_URL, group_seed)}"
        if prepared_entities else None
    )
    return {
        "draft_id": draft_id,
        "fact_type": normalized_fact_type,
        "facts": validated.canonical,
        "entities": prepared_entities,
        **({"draft_group_id": draft_group_id} if draft_group_id else {}),
        "confidence": normalized_confidence,
        "lifecycle_stage": "draft",
        "status": "draft",
        "metadata": lifecycle_metadata,
    }


def build_save_fact_payload(
    *,
    fact_type: str,
    facts: Dict[str, Any],
    confidence: str,
    metadata: Dict[str, Any],
    entities: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Return a directly confirmed fact payload using the shared fact contract."""

    validated = _validated_fact_write(facts, empty_error="no_structured_facts_to_save") if facts else ValidatedFacts()
    try:
        committed_entities = validate_entities(entities or [], committed=True)
    except ValueError as exc:
        raise FactWriteValidationError(str(exc)) from exc
    if not validated.canonical and not committed_entities:
        raise FactWriteValidationError("no_structured_facts_to_save")
    normalized_fact_type, normalized_confidence, coercions = _normalize_fact_headers(
        fact_type,
        confidence,
    )
    structured = structure_onboarding_facts(validated.canonical)
    return {
        "fact_type": normalized_fact_type,
        "facts": validated.canonical,
        "entities": committed_entities,
        "structured_facts": structured,
        "confidence": normalized_confidence,
        "lifecycle_stage": "committed",
        "status": "confirmed",
        "metadata": {
            **_fact_write_metadata(
                metadata,
                validated=validated,
                coercions=coercions,
                write_model="direct_save",
            ),
            "committed_slots": sorted(validated.canonical),
            "structured_slots": sorted(structured),
        },
    }


def build_assessment_signoff_payload(
    arguments: Dict[str, Any],
    *,
    client_file: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Normalize assessment sign-off into a durable investment_assessment artifact.

    Agents often pass only pool_label + signed_off. Investment Solution requires a
    versioned assessment identity and signed content (amount/horizon/risk) that can
    be found later on the Client File read model.
    """

    args = arguments if isinstance(arguments, dict) else {}
    client_file = client_file if isinstance(client_file, dict) else {}
    pool, pool_resolution = _resolve_money_pool_for_signoff(args, client_file)
    assessment_content = args.get("assessment") if isinstance(args.get("assessment"), dict) else {}
    pool_label = str(args.get("pool_label") or pool.get("label") or "").strip()
    money_pool_id = str(args.get("money_pool_id") or pool.get("id") or "").strip() or None
    assessment_id = str(args.get("assessment_id") or "").strip() or None
    if not assessment_id:
        stem = money_pool_id or re.sub(r"[^a-z0-9]+", "-", pool_label.lower()).strip("-") or "pool"
        assessment_id = f"assess-{stem}-signed"

    content: Dict[str, Any] = {}
    for key, candidates in (
        ("amount", ("amount", "investment_amount", "capital_required")),
        ("purpose", ("purpose", "purpose_type", "goal")),
        ("horizon_years", ("horizon_years", "horizon")),
        ("target_risk", ("target_risk", "recommended_risk", "risk", "risk_tolerance")),
        ("target_volatility_pct", ("target_volatility_pct", "target_volatility")),
        ("funding_source", ("funding_source", "source_of_funds")),
        ("complexity_preference", ("complexity_preference",)),
    ):
        value = _first_present(assessment_content, candidates)
        if value in (None, "", []):
            value = _first_present(pool, candidates if key != "purpose" else ("purpose_type", "purpose", "label", "goal"))
        if key == "purpose" and value in (None, "", []):
            value = pool_label or None
        if value not in (None, "", []):
            content[key] = value
    exclusions = assessment_content.get("exclusions") or assessment_content.get("excluded_asset_classes")
    if exclusions in (None, [], ""):
        exclusions = pool.get("excluded_asset_classes") or pool.get("exclusions")
    if exclusions not in (None, [], ""):
        content["exclusions"] = exclusions
    for optional_key in ("suitability_signals", "concerns", "evidence"):
        if assessment_content.get(optional_key) not in (None, "", []):
            content[optional_key] = assessment_content.get(optional_key)

    signed_off = args.get("signed_off") is True
    signed_off_at = str(args.get("signed_off_at") or "").strip() or (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        if signed_off
        else None
    )
    try:
        assessment_version = int(args.get("assessment_version") or 1)
    except (TypeError, ValueError):
        assessment_version = 1

    payload: Dict[str, Any] = {
        **args,
        "schema_version": ASSESSMENT_SCHEMA_VERSION,
        "artifact_type": "investment_assessment",
        "assessment_id": assessment_id,
        "assessment_version": max(assessment_version, 1),
        "money_pool_id": money_pool_id,
        "pool_label": pool_label or None,
        "status": "signed_off" if signed_off else str(args.get("status") or "unsigned"),
        "signed_off": signed_off,
        "signed_off_at": signed_off_at,
        "signoff": {"signed_off": signed_off, "signed_off_at": signed_off_at},
        "assessment": content,
    }
    if pool_resolution.get("warning"):
        metadata = dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {}
        metadata["pool_resolution_warning"] = pool_resolution["warning"]
        payload["metadata"] = metadata
    return payload


def _resolve_money_pool_for_signoff(
    args: Dict[str, Any],
    client_file: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, str]]:
    """Resolve the money pool for sign-off without guessing across multiple pools.

    Returns (pool, resolution_meta). Fallback to the only pool is allowed when exactly
    one pool exists; with multiple unmatched pools we return {} and a warning.
    """

    pools = client_file.get("money_pools")
    pools = [item for item in pools if isinstance(item, dict)] if isinstance(pools, list) else []
    money_pool_id = str(args.get("money_pool_id") or "").strip()
    pool_label = str(args.get("pool_label") or "").strip().lower()
    for pool in pools:
        if money_pool_id and str(pool.get("id") or "") == money_pool_id:
            return pool, {"matched_by": "money_pool_id"}
    for pool in pools:
        if pool_label and str(pool.get("label") or "").strip().lower() == pool_label:
            return pool, {"matched_by": "pool_label"}
    if len(pools) == 1:
        return pools[0], {"matched_by": "single_pool_fallback"}
    if len(pools) > 1:
        return {}, {
            "warning": (
                "multiple_money_pools_unmatched: refuse ambiguous pools[-1] fallback; "
                "provide money_pool_id or an exact pool_label"
            )
        }
    return {}, {"matched_by": "none"}


def _first_present(source: Dict[str, Any], keys: tuple[str, ...]) -> Any:
    if not isinstance(source, dict):
        return None
    for key in keys:
        value = source.get(key)
        if value not in (None, "", []):
            return value
    return None


def build_commit_facts_payload(
    *,
    client_file: Dict[str, Any],
    confirmation_text: str,
    facts: Dict[str, Any] | None = None,
    source: str = "client_confirmation",
    fact_type: str = "captured_fact",
    confidence: str = "medium",
    metadata: Optional[Dict[str, Any]] = None,
    fact_ids: Optional[List[str]] = None,
    draft_items: Optional[List[Dict[str, str]]] = None,
    entities: Optional[List[Dict[str, Any]]] = None,
    confirmation_action_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a commit payload from draft facts and any facts confirmed this turn."""

    draft_facts = client_file.get("draft_facts")
    if not isinstance(draft_facts, list):
        draft_facts = []
    draft_facts = [item for item in draft_facts if isinstance(item, dict)]
    indexed_drafts = [
        (draft_identity(item, index=index), item)
        for index, item in enumerate(draft_facts)
    ]
    selectors = {
        str(item).strip()
        for item in (fact_ids or [])
        if str(item).strip()
    }
    exact_items = {
        (str(item.get("draft_id") or "").strip(), str(item.get("field") or "").strip())
        for item in (draft_items or [])
        if isinstance(item, dict)
    }
    available_ids = {draft_id for draft_id, _item in indexed_drafts}
    available_fields = {
        canonical
        for _draft_id, item in indexed_drafts
        for raw_field in (
            item.get("facts", {}).keys()
            if isinstance(item.get("facts"), dict)
            else ()
        )
        if (canonical := canonical_fact_name(str(raw_field))) is not None
    }
    available_entity_ids = {
        str(entity.get("entity_id") or "")
        for _draft_id, item in indexed_drafts
        for entity in (item.get("entities") or [])
        if isinstance(entity, dict) and entity.get("entity_id")
    }
    unknown_selectors = sorted(
        selector
        for selector in selectors
        if selector not in available_ids and selector not in available_fields and selector not in available_entity_ids
    )
    if unknown_selectors:
        raise FactWriteValidationError(
            "unknown_fact_ids",
            details=[
                {
                    "reason": "unknown_fact_ids",
                    "unknown_fact_ids": unknown_selectors,
                    "available_draft_ids": sorted(available_ids),
                    "available_fields": sorted(available_fields),
                }
            ],
        )
    selected_drafts: list[tuple[str, Dict[str, Any], set[str], set[str]]] = []
    for draft_id, item in indexed_drafts:
        fields = {
            canonical
            for raw_field in (
                item.get("facts", {}).keys()
                if isinstance(item.get("facts"), dict)
                else ()
            )
            if (canonical := canonical_fact_name(str(raw_field))) is not None
        }
        selected_fields = fields if draft_id in selectors else fields.intersection(selectors)
        selected_fields.update(
            field for item_draft_id, field in exact_items if item_draft_id == draft_id and field in fields
        )
        entity_ids = {
            str(entity.get("entity_id") or "")
            for entity in (item.get("entities") or [])
            if isinstance(entity, dict) and entity.get("entity_id")
        }
        selected_entity_ids = entity_ids if draft_id in selectors else entity_ids.intersection(selectors)
        selected_entity_ids.update(
            field
            for item_draft_id, field in exact_items
            if item_draft_id == draft_id and field in entity_ids
        )
        if selected_fields or selected_entity_ids:
            selected_drafts.append((draft_id, item, selected_fields, selected_entity_ids))
    selected_ids = [
        draft_id
        for draft_id, _item, selected_fields, selected_entity_ids in selected_drafts
        if selected_fields == {
            canonical
            for raw_field in (_item.get("facts") or {})
            if (canonical := canonical_fact_name(str(raw_field))) is not None
        } and selected_entity_ids == {
            str(entity.get("entity_id") or "") for entity in (_item.get("entities") or []) if isinstance(entity, dict) and entity.get("entity_id")
        }
    ]
    selected_items = [
        {"draft_id": draft_id, "field": field}
        for draft_id, _item, selected_fields, _selected_entity_ids in selected_drafts
        for field in sorted(selected_fields)
    ]
    remaining_ids = [
        draft_id
        for draft_id, _item in indexed_drafts
        if draft_id not in set(selected_ids)
    ]
    merged: Dict[str, Any] = {}
    merged_provenance: Dict[str, Dict[str, Any]] = {}
    aliases_applied: list[tuple[str, str]] = []
    unrecognized: Dict[str, Any] = {}
    merged_entities: Dict[str, Dict[str, Any]] = {}
    # Client State returns the newest draft first. Apply older drafts before
    # newer corrections so an explicit correction remains the active value.
    for _draft_id, item, selected_fields, selected_entity_ids in reversed(selected_drafts):
        if isinstance(item, dict) and isinstance(item.get("facts"), dict):
            draft_validated = _validate_stored_draft(item)
            merged.update({key: value for key, value in draft_validated.canonical.items() if key in selected_fields})
            merged_provenance.update({key: value for key, value in draft_validated.provenance.items() if key in selected_fields})
            aliases_applied.extend(draft_validated.aliases_applied)
            unrecognized.update(draft_validated.unrecognized)
        for entity in item.get("entities") or []:
            if isinstance(entity, dict) and str(entity.get("entity_id") or "") in selected_entity_ids:
                merged_entities[str(entity["entity_id"])] = dict(entity)
    if isinstance(facts, dict):
        # Explicit values are the latest client-confirmed correction and must
        # win over an older draft of the same field.
        explicit_validated = _validated_fact_write(
            facts,
            empty_error="no_structured_facts_to_commit",
        )
        merged.update(explicit_validated.canonical)
        merged_provenance.update(explicit_validated.provenance)
        aliases_applied.extend(explicit_validated.aliases_applied)
        unrecognized.update(explicit_validated.unrecognized)
    try:
        for entity in validate_entities(entities or [], committed=True):
            merged_entities[str(entity["entity_id"])] = entity
        committed_entities = validate_entities(list(merged_entities.values()), committed=True)
    except ValueError as exc:
        raise FactWriteValidationError(str(exc)) from exc
    account_ids = {entity["entity_id"] for entity in committed_entities if entity["entity_type"] == "account"}
    for entity in committed_entities:
        if entity["entity_type"] == "holding" and entity.get("account_id") not in account_ids:
            # Existing-account references are verified again by canonical persistence.
            entity["requires_existing_account_validation"] = True
    if not merged and not committed_entities:
        raise FactWriteValidationError(
            "no_structured_facts_to_commit",
            unrecognized=unrecognized,
        )
    normalized_fact_type, normalized_confidence, coercions = _normalize_fact_headers(
        fact_type,
        confidence,
    )
    structured = structure_onboarding_facts(merged)
    validated_metadata = ValidatedFacts(
        provenance=merged_provenance,
        aliases_applied=tuple(aliases_applied),
        unrecognized=unrecognized,
    )

    return {
        "fact_type": normalized_fact_type,
        "facts": merged,
        "entities": committed_entities,
        "draft_group_ids": sorted({str(item.get("draft_group_id")) for _draft_id, item, _fields, entity_ids in selected_drafts if entity_ids and item.get("draft_group_id")}),
        "confirmation_action_id": confirmation_action_id,
        "structured_facts": structured,
        "confidence": normalized_confidence,
        "lifecycle_stage": "committed",
        "status": "committed",
        "confirmation_text": confirmation_text,
        "source": source,
        "resolved_draft_ids": selected_ids,
        "resolved_draft_items": selected_items,
        "metadata": {
            **_fact_write_metadata(
                metadata or {},
                validated=validated_metadata,
                coercions=coercions,
                write_model="draft_then_commit",
            ),
            "draft_count": len(selected_drafts),
            "total_draft_count": len(draft_facts),
            "explicit_fact_count": len(facts) if isinstance(facts, dict) else 0,
            "committed_slots": sorted(merged.keys()),
            "committed_entity_ids": sorted(merged_entities),
            "committed_draft_ids": selected_ids,
            "committed_draft_items": selected_items,
            "remaining_draft_ids": remaining_ids,
            "structured_slots": sorted(structured.keys()),
        },
    }


def draft_identity(item: Dict[str, Any], *, index: int = 0) -> str:
    draft_id = str(item.get("draft_id") or item.get("source_event_id") or "").strip()
    if draft_id:
        return draft_id
    facts = item.get("facts") if isinstance(item.get("facts"), dict) else {}
    if facts:
        fingerprint = json.dumps(
            normalize_fact_keys(facts),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        fact_type = str(item.get("fact_type") or "captured_fact")
        return (
            f"draft:{fact_type}:"
            f"{hashlib.sha1(fingerprint.encode('utf-8')).hexdigest()[:8]}"
        )
    return f"legacy-draft:{index}"


def _prepare_draft_entities(
    entities: List[Dict[str, Any]], *, client_id: str, source_message_id: str,
) -> List[Dict[str, Any]]:
    if not entities:
        return []
    seed = source_message_id or hashlib.sha1(
        json.dumps(entities, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    prepared = []
    assigned: Dict[str, str] = {}
    for index, raw in enumerate(entities):
        if not isinstance(raw, dict):
            raise FactWriteValidationError("entity_must_be_an_object")
        entity = dict(raw)
        entity_type = str(entity.get("entity_type") or "")
        original_id = str(entity.get("entity_id") or "")
        if not original_id:
            entity["entity_id"] = f"{entity_type}:{uuid.uuid5(uuid.NAMESPACE_URL, f'{client_id}:{seed}:{entity_type}:{index}')}"
        if original_id:
            assigned[original_id] = str(entity["entity_id"])
        prepared.append(entity)
    for entity in prepared:
        account_id = str(entity.get("account_id") or "")
        if account_id and account_id in assigned:
            entity["account_id"] = assigned[account_id]
    try:
        return validate_entities(prepared, committed=False)
    except ValueError as exc:
        raise FactWriteValidationError(str(exc)) from exc


def _validated_fact_write(facts: Any, *, empty_error: str):
    validated = validate_facts(facts)
    if validated.rejected:
        primary = str(validated.rejected[0].get("reason") or "fact_shape_invalid")
        raise FactWriteValidationError(
            primary,
            details=list(validated.rejected),
            unrecognized=validated.unrecognized,
        )
    if not validated.canonical:
        raise FactWriteValidationError(
            empty_error,
            unrecognized=validated.unrecognized,
        )
    return validated


def _validate_stored_draft(item: Dict[str, Any]):
    """Validate a projected draft, tolerating pre-contract period scalars."""

    raw_facts = item.get("facts") if isinstance(item.get("facts"), dict) else {}
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    stored_provenance = (
        metadata.get("fact_provenance")
        if isinstance(metadata.get("fact_provenance"), dict)
        else {}
    )
    reconstructed: Dict[str, Any] = {}
    legacy_period_values: Dict[str, Any] = {}
    legacy_aliases: list[tuple[str, str]] = []
    for raw_key, value in raw_facts.items():
        canonical = next(
            (
                key
                for key in normalize_fact_keys({str(raw_key): value})
                if key in CANONICAL_FACT_FIELDS
            ),
            str(raw_key),
        )
        definition = CANONICAL_FACT_FIELDS.get(canonical)
        provenance = stored_provenance.get(canonical)
        if isinstance(definition, FactField) and definition.period:
            if isinstance(provenance, dict) and provenance.get("basis"):
                reconstructed[raw_key] = {"value": value, **provenance}
            else:
                # Historical drafts predate mandatory basis. They remain
                # committable; all new draft writes are strict.
                legacy_period_values[canonical] = value
                if str(raw_key) != canonical:
                    legacy_aliases.append((str(raw_key), canonical))
        else:
            reconstructed[raw_key] = value
    validated = validate_facts(reconstructed)
    if validated.rejected:
        raise FactWriteValidationError(
            str(validated.rejected[0].get("reason") or "fact_shape_invalid"),
            details=list(validated.rejected),
            unrecognized=validated.unrecognized,
        )
    canonical_values = {**validated.canonical, **legacy_period_values}
    return ValidatedFacts(
        canonical=canonical_values,
        provenance=validated.provenance,
        aliases_applied=validated.aliases_applied + tuple(legacy_aliases),
        unrecognized=validated.unrecognized,
    )


def _normalize_fact_headers(
    fact_type: Any,
    confidence: Any,
) -> tuple[str, str, List[Dict[str, str]]]:
    coercions: List[Dict[str, str]] = []
    normalized_fact_type = str(fact_type or "")
    if normalized_fact_type not in FACT_TYPES:
        coercions.append(
            {
                "field": "fact_type",
                "given": normalized_fact_type,
                "used": "captured_fact",
            }
        )
        normalized_fact_type = "captured_fact"
    normalized_confidence = str(confidence or "")
    if normalized_confidence not in CONFIDENCE_LEVELS:
        coercions.append(
            {
                "field": "confidence",
                "given": normalized_confidence,
                "used": "medium",
            }
        )
        normalized_confidence = "medium"
    return normalized_fact_type, normalized_confidence, coercions


def _fact_write_metadata(
    metadata: Any,
    *,
    validated: Any,
    coercions: List[Dict[str, str]],
    write_model: str,
) -> Dict[str, Any]:
    public_metadata = (
        {
            key: value
            for key, value in metadata.items()
            if key in _PUBLIC_METADATA_KEYS
        }
        if isinstance(metadata, dict)
        else {}
    )
    result: Dict[str, Any] = {
        **public_metadata,
        "write_model": write_model,
    }
    if validated.provenance:
        result["fact_provenance"] = validated.provenance
    if validated.aliases_applied:
        result["normalization_applied"] = [
            {"given": given, "canonical": canonical}
            for given, canonical in validated.aliases_applied
        ]
    if validated.unrecognized:
        result["facts_unrecognized"] = validated.unrecognized
    if coercions:
        result["coercions"] = coercions
    return result


def build_consultation_checkpoint_payload(
    *,
    client_file: Dict[str, Any],
    selected_skill: str | None,
    session_id: str,
    user_message: str,
) -> Dict[str, Any]:
    """Build a resumable consultation checkpoint for the current turn.

    The checkpoint records where the consultation is, not long-term financial
    truth. It gives the next app entry enough state to resume from the next
    unanswered slot instead of restarting discovery.
    """

    skill = selected_skill or "base"
    progress = consultation_progress_from_client_file(client_file, skill=skill)
    missing_slots = progress.get("missing_slots") or []
    next_slot = missing_slots[0] if missing_slots else None
    status = "complete" if progress.get("status") == "complete" else "in_progress"
    return {
        "id": f"checkpoint:{session_id}:{skill}",
        "session_id": session_id,
        "consultation_type": skill,
        "status": status,
        "lifecycle_stage": "active" if status != "complete" else "complete",
        "completed_slots": progress.get("completed_slots") or [],
        "missing_slots": missing_slots,
        "next_slot": next_slot,
        "next_action": _next_checkpoint_action(skill=skill, next_slot=next_slot, status=status),
        "last_user_message": str(user_message or "")[:500],
        "source": "advisor_turn_checkpoint",
    }


def consultation_progress_from_client_file(client_file: Dict[str, Any], *, skill: str) -> Dict[str, Any]:
    """Return resumable progress in the vocabulary of the active consultation."""
    if skill == "onboarding-consult":
        return onboarding_progress_from_client_file(client_file)
    if skill == "investment-consult":
        return _investment_progress_from_client_file(client_file)
    if skill == "policy-review":
        return _policy_review_progress_from_client_file(client_file)
    if skill == "confirm-facts":
        draft_facts = client_file.get("draft_facts") if isinstance(client_file, dict) else []
        has_drafts = bool(draft_facts) if isinstance(draft_facts, list) else False
        return {
            "status": "in_progress" if has_drafts else "complete",
            "completed_slots": [] if has_drafts else ["fact_confirmation"],
            "missing_slots": ["fact_confirmation"] if has_drafts else [],
        }
    # Regular consultation resumes from its objective/checkpoint text instead
    # of borrowing onboarding fields that may no longer be relevant.
    return {"status": "in_progress", "completed_slots": [], "missing_slots": []}


def _investment_progress_from_client_file(client_file: Dict[str, Any]) -> Dict[str, Any]:
    pools = client_file.get("money_pools") if isinstance(client_file, dict) else []
    pools = [item for item in pools if isinstance(item, dict)] if isinstance(pools, list) else []
    pool = pools[-1] if pools else {}
    aliases = {
        "purpose": ("purpose_type", "purpose", "goal", "label"),
        "amount": ("amount",),
        "horizon": ("horizon_years", "horizon_text", "horizon"),
        "risk_tolerance": ("risk_tolerance", "risk_profile"),
        "source_of_funds": ("source_of_funds", "funding_source"),
    }
    completed = [
        slot
        for slot, keys in aliases.items()
        if any(pool.get(key) not in (None, "", []) for key in keys)
    ]
    missing = [slot for slot in INVESTMENT_SLOTS if slot not in completed]
    return {
        "status": "complete" if not missing else "in_progress",
        "completed_slots": completed,
        "missing_slots": missing,
    }


def _policy_review_progress_from_client_file(client_file: Dict[str, Any]) -> Dict[str, Any]:
    proposals = client_file.get("proposals") if isinstance(client_file, dict) else []
    proposals = [item for item in proposals if isinstance(item, dict)] if isinstance(proposals, list) else []
    policies = client_file.get("policies") if isinstance(client_file, dict) else {}
    if isinstance(policies, dict):
        proposed = policies.get("proposed")
        if isinstance(proposed, list):
            proposals.extend(item for item in proposed if isinstance(item, dict))
    proposal = proposals[-1] if proposals else {}
    status = str(proposal.get("status") or "").lower()
    terminal = status in {"approved", "deferred", "declined", "closed", "needs_revision"}
    complete = bool(proposal.get("review_outcome")) or terminal
    return {
        "status": "complete" if complete else "in_progress",
        "completed_slots": ["review_decision"] if complete else [],
        "missing_slots": [] if complete else ["review_decision"],
    }


def build_journey_status(client_file: Dict[str, Any]) -> Dict[str, Any]:
    """Project journey phase status from Client File read model."""
    onboarding = client_file.get("onboarding") if isinstance(client_file.get("onboarding"), dict) else {}
    pools = client_file.get("money_pools") if isinstance(client_file.get("money_pools"), list) else []
    policies = client_file.get("policies") if isinstance(client_file.get("policies"), dict) else {}
    proposed = policies.get("proposed") if isinstance(policies.get("proposed"), list) else []
    active = policies.get("active") if isinstance(policies.get("active"), list) else []
    return {
        "onboarding": str(onboarding.get("status") or "unknown"),
        "investment_consult": "complete" if pools else "pending",
        "regular_consult": "due" if active else "not_started",
        "policy_review": "pending" if proposed else ("active" if active else "none"),
    }


def structure_onboarding_facts(facts: Dict[str, Any]) -> Dict[str, Any]:
    """Create a stable structured onboarding view from draft fact text.

    This intentionally stays conservative. It is the replaceable phase-end
    extraction boundary; a richer business/LLM extractor can later implement
    the same output shape.
    """

    structured: Dict[str, Any] = {}

    if facts.get("confirmed_linked_balances") is True:
        structured["linked_balances"] = {"confirmed": True}

    household_text = str(facts.get("household_context") or "").strip()
    household = _structure_household(household_text)
    if household:
        structured["household"] = household

    goals_text = str(facts.get("future_goals") or "").strip()
    goals = _structure_goals(goals_text)
    if goals:
        structured["goals"] = goals

    income_text = str(facts.get("income_context") or "").strip()
    if income_text:
        structured["income"] = {
            "description": income_text,
            "stability": "stable" if "stable" in income_text.lower() or "long term" in income_text.lower() else "unknown",
        }

    health_text = str(facts.get("health_context") or "").strip()
    if health_text:
        structured["health"] = {"description": health_text}

    return structured


def onboarding_progress_from_client_file(client_file: Dict[str, Any]) -> Dict[str, Any]:
    """Return completed/missing onboarding slots from committed and draft facts."""

    committed = client_file.get("facts") if isinstance(client_file, dict) else {}
    committed = committed if isinstance(committed, dict) else {}
    draft_facts = client_file.get("draft_facts") if isinstance(client_file, dict) else []
    draft_facts = draft_facts if isinstance(draft_facts, list) else []

    completed = set()
    for slot in ONBOARDING_SLOTS:
        if slot in committed:
            completed.add(slot)
    if "household" in committed:
        completed.add("household_context")
    if "income" in committed:
        completed.add("income_context")

    for item in draft_facts:
        if isinstance(item, dict) and isinstance(item.get("facts"), dict):
            completed.update(key for key in item["facts"].keys() if key in ONBOARDING_SLOTS)

    missing = [slot for slot in ONBOARDING_SLOTS if slot not in completed]
    return {
        "status": "complete" if not missing else "in_progress",
        "completed_slots": sorted(completed),
        "missing_slots": missing,
        "draft_count": len(draft_facts),
    }


def has_pending_draft_facts(client_file: Dict[str, Any]) -> bool:
    drafts = client_file.get("draft_facts") if isinstance(client_file, dict) else None
    return isinstance(drafts, list) and any(isinstance(item, dict) and item.get("facts") for item in drafts)
def extract_explicit_planning_facts(text: str) -> Dict[str, Any]:
    """Extract only plainly stated planning facts from a confirmed chat message."""

    source = " ".join(str(text or "").strip().split())
    facts: Dict[str, Any] = {}

    same_age_match = re.search(
        r"\b(?:we(?:'re|\s+are)|(?:my\s+)?(?:spouse|partner)\s+and\s+i\s+are)\s+"
        r"both\s+(\d{1,3})(?:\s+years?\s+old)?\b",
        source,
        re.IGNORECASE,
    )
    if same_age_match:
        shared_age = int(same_age_match.group(1))
        if 18 <= shared_age <= 120:
            facts["current_age"] = shared_age
            facts["spouse_age"] = shared_age
    else:
        age_patterns = (
            r"\b(?:my\s+)?(?:spouse|partner)\s+and\s+i\s+are\s+(\d{1,3})\s+(?:and|,)\s+(\d{1,3})\b",
            r"\bi\s+am\s+(\d{1,3})\b.{0,40}\b(?:spouse|partner)\s+(?:is|is\s+age)\s+(\d{1,3})\b",
            r"\b(?:we\s+are|ages?\s+are|ages?)\s+(\d{1,3})\s+(?:and|,)\s+(\d{1,3})\b",
        )
        for pattern in age_patterns:
            match = re.search(pattern, source, re.IGNORECASE)
            if not match:
                continue
            primary_age, partner_age = int(match.group(1)), int(match.group(2))
            if 18 <= primary_age <= 120 and 18 <= partner_age <= 120:
                facts["current_age"] = primary_age
                facts["spouse_age"] = partner_age
            break

    if "current_age" not in facts:
        current_age_match = re.search(
            r"\b(?:i\s+am|i['’]m)\s+(\d{1,3})(?:\s+years?\s+old)?\b",
            source,
            re.IGNORECASE,
        )
        if current_age_match:
            current_age = int(current_age_match.group(1))
            if 18 <= current_age <= 120:
                facts["current_age"] = current_age

    if "spouse_age" in facts:
        if re.search(r"\b(?:spouse|wife|husband|married)\b", source, re.IGNORECASE):
            facts["marital_status"] = "married"
        elif re.search(r"\bpartner\b", source, re.IGNORECASE):
            # A partner is not necessarily a joint tax-filing spouse. Preserve
            # that distinction so the cash-flow bridge does not assume MFJ.
            facts["marital_status"] = "partnered"

    retirement_match = re.search(
        r"\b(?:want|plan|hope|expect)?\s*(?:to\s+)?"
        r"(?:retire|stop\s+working)\s+"
        r"(?:(?:at|by|around|about|near)\s+)?(?:age\s+)?(\d{1,3})\b",
        source,
        re.IGNORECASE,
    )
    if retirement_match:
        retirement_age = int(retirement_match.group(1))
        if 18 <= retirement_age <= 120:
            facts["retirement_age"] = retirement_age

    money_fields = {
        "annual_income": (
            r"annual\s+(?:pre[- ]tax\s+)?(?:household\s+)?income",
            r"(?:pre[- ]tax\s+)?household\s+income",
        ),
        "annual_spending": (
            r"annual\s+(?:household\s+)?spending",
            r"annual\s+(?:household\s+)?expenses?",
        ),
        "cash": (r"cash(?:\s+balance)?",),
        "retirement_accounts": (
            r"retirement\s+accounts?",
            r"retirement\s+savings",
        ),
        "brokerage_accounts": (r"brokerage(?:\s+accounts?)?",),
        "mortgage_balance": (r"mortgage(?:\s+balance)?",),
        "home_value": (
            r"(?:current\s+)?home\s+(?:market\s+)?value",
            r"home\s+(?:is\s+)?worth",
            r"primary\s+residence\s+value",
        ),
        "mortgage_monthly_payment": (
            r"monthly\s+(?:mortgage\s+)?principal\s+and\s+interest",
            r"monthly\s+mortgage\s+payment",
        ),
    }
    for key, labels in money_fields.items():
        value = _extract_money_for_labels(source, labels)
        if value is not None and value >= 0:
            facts[key] = value
    amount_pattern = (
        r"\$?\s*(?P<amount>\d[\d,]*(?:\.\d+)?)\s*"
        r"(?P<scale>k|m|thousand|million)?"
    )
    if "annual_income" not in facts:
        income_match = re.search(
            rf"\b(?:our\s+)?household\s+(?:earns?|makes?)\s+"
            rf"(?:about|roughly|approximately)?\s*{amount_pattern}"
            r"(?:\s+(?:annually|per\s+year|a\s+year))?",
            source,
            re.IGNORECASE,
        )
        if income_match:
            facts["annual_income"] = _money_value_from_match(income_match)
    if "annual_spending" not in facts:
        spending_match = re.search(
            rf"\b(?:our\s+household\s+|we\s+)?spends?\s+"
            rf"(?:about|roughly|approximately)?\s*{amount_pattern}"
            r"(?:\s+(?:annually|per\s+year|a\s+year))?",
            source,
            re.IGNORECASE,
        )
        if spending_match:
            facts["annual_spending"] = _money_value_from_match(spending_match)

    monthly_contribution_match = re.search(
        rf"\b(?:we\s+)?(?:contribute(?:s)?|add(?:s)?|save(?:s)?|"
        rf"put\s+away|set\s+aside)\s+"
        rf"(?:about|roughly|approximately)?\s*{amount_pattern}\s+"
        r"(?:per\s+month|a\s+month|each\s+month|monthly)\b",
        source,
        re.IGNORECASE,
    )
    if monthly_contribution_match:
        facts["monthly_retirement_contribution"] = _money_value_from_match(
            monthly_contribution_match
        )
    if "annual_spending" not in facts and re.search(
        r"\bannual\s+(?:(?:pre[- ]tax|household)\s+)*income\b[^.!?]{0,100}"
        r"\band\s+(?:household\s+)?spending\b",
        source,
        re.IGNORECASE,
    ):
        # "Annual" commonly scopes both sides of "income ... and spending ...".
        # Only use the shorter label inside that bounded annual-income clause.
        shared_annual_spending = _extract_money_for_labels(source, (r"spending",))
        if shared_annual_spending is not None and shared_annual_spending >= 0:
            facts["annual_spending"] = shared_annual_spending

    percent_patterns = {
        "mortgage_interest_rate": (
            r"\bmortgage(?:\s+interest)?\s+rate\s*(?:is|of|:|at)?\s*(\d+(?:\.\d+)?)\s*%",
            r"\bfixed(?:[- ]rate)?\s+mortgage\s+(?:is\s+)?at\s+(\d+(?:\.\d+)?)\s*%",
            r"\bmortgage\s+(?:is\s+)?(?:fixed\s+)?at\s+(\d+(?:\.\d+)?)\s*%",
            r"\bmortgage\b[^.!?]{0,100}\bat\s+(\d+(?:\.\d+)?)\s*%",
        ),
        "home_appreciation_rate": (
            r"\bhome(?:[- ]value)?\s+(?:growth|appreciation)(?:\s+rate)?\s*(?:is|of|:|at)?\s*(\d+(?:\.\d+)?)\s*%",
            r"\buse\s+(\d+(?:\.\d+)?)\s*%\s+(?:annual\s+)?home(?:[- ]value)?\s+(?:growth|appreciation)",
        ),
    }
    for key, patterns in percent_patterns.items():
        for pattern in patterns:
            match = re.search(pattern, source, re.IGNORECASE)
            if match:
                facts[key] = float(match.group(1)) / 100.0
                break

    term_patterns = (
        r"\b(?:mortgage\s+)?remaining\s+term\s*(?:is|of|:)?\s*(\d{1,3})\s+years?\b",
        r"\b(\d{1,3})\s+years?\s+(?:remain|remaining|left)\s+(?:on\s+)?(?:the\s+)?mortgage\b",
        r"\bmortgage\s+has\s+(\d{1,3})\s+years?\s+(?:remaining|left)\b",
        r"\bmortgage\b.{0,140}?\b(\d{1,3})\s+years?\s+(?:remaining|left)\b",
    )
    for pattern in term_patterns:
        match = re.search(pattern, source, re.IGNORECASE)
        if match:
            years = int(match.group(1))
            if 1 <= years <= 100:
                facts["mortgage_remaining_term_years"] = years
            break

    if re.search(
        r"\b(?:"
        r"fixed(?:[- ]rate)?\s+(?:mortgage|loan)|"
        r"mortgage\s+is\s+(?:a\s+)?fixed(?:[- ]rate)?(?:\s+loan)?"
        r")\b",
        source,
        re.IGNORECASE,
    ):
        facts["mortgage_type"] = "fixed_rate"

    excludes_mortgage = re.search(
        r"\b(?:annual\s+)?spending\b[^.!?]{0,80}\b(?:does\s+not\s+include|excludes?)\b"
        r"[^.!?]{0,40}\bmortgage\b",
        source,
        re.IGNORECASE,
    )
    includes_mortgage = re.search(
        r"\b(?:annual\s+)?spending\b[^.!?]{0,80}\b(?:includes?|including)\b"
        r"[^.!?]{0,40}\bmortgage\b",
        source,
        re.IGNORECASE,
    )
    if excludes_mortgage:
        facts["annual_spending_includes_mortgage"] = False
    elif includes_mortgage:
        facts["annual_spending_includes_mortgage"] = True
    return facts


def _extract_money_for_labels(source: str, labels: tuple[str, ...]) -> int | float | None:
    amount = r"\$?\s*(?P<amount>\d[\d,]*(?:\.\d+)?)\s*(?P<scale>k|m|thousand|million)?"
    for label in labels:
        patterns = (
            rf"\b{label}\b\s*(?:is|of|:)?\s*{amount}",
            rf"{amount}\s*(?:(?:in|of)\s+(?:an?\s+)?)?\b{label}\b",
        )
        for pattern in patterns:
            match = re.search(pattern, source, re.IGNORECASE)
            if not match:
                continue
            # "Brokerage: 60% US Equity" is an allocation weight, not a $60 balance.
            trailing = source[match.end() : match.end() + 8]
            if re.match(r"\s*%", trailing):
                continue
            return _money_value_from_match(match)
    return None


def _money_value_from_match(match: re.Match[str]) -> int | float:
    value = float(match.group("amount").replace(",", ""))
    scale = str(match.group("scale") or "").lower()
    if scale in {"k", "thousand"}:
        value *= 1_000
    elif scale in {"m", "million"}:
        value *= 1_000_000
    return int(value) if value.is_integer() else value

def _next_checkpoint_action(*, skill: str, next_slot: Any, status: str) -> str:
    if status == "complete":
        return "Move to the next best engagement."
    if skill == "onboarding-consult" and next_slot:
        return f"Resume onboarding by asking for {next_slot}."
    if skill == "investment-consult":
        return "Resume investment consultation from the current money goal."
    if skill == "regular-consult":
        return "Resume the regular check-up from the last unresolved update."
    if skill == "confirm-facts":
        return "Resume fact confirmation."
    if skill == "policy-review":
        return "Resume proposal review from the pending client decision."
    return "Resume the current consultation."


def _structure_household(text: str) -> Dict[str, Any]:
    """Store household text only — typed fields come from LLM structured facts, not regex."""

    if not text:
        return {}
    return {"description": text}


def _structure_goals(text: str) -> List[Dict[str, Any]]:
    """Store goal text only — goal typing is LLM structured extract, not keyword/regex NLU."""

    if not text:
        return []
    return [{"description": text}]
