"""Knowledge, diagnosis, and narrative-edit HTTP routes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request

from client_file.fact_vocabulary import fact_value_for_engine_field


class KnowledgeDeps:
    """Attribute container protocol by convention.

    ``api.server`` supplies a SimpleNamespace of callables. Keeping this
    dynamic preserves existing tests that monkeypatch server symbols.
    """


def _norm_key(domain: str, category: str, label: str) -> str:
    return f"{domain.strip().lower()}:{category.strip().lower()}:{label.strip().lower()}"


_META_FACT_KEYS = {
    "diagnosis_snapshot_version",
    "diagnosis_summary",
    "knowledge_snapshot_version",
    "knowledge_summary",
    "pending_confirmation_count",
    "planning_facts",
}

# Map Client File fact keys onto the APP Knowledge domains (people / wealth / health).
_CLIENT_FILE_FACT_MAP: Dict[str, Tuple[str, str, str]] = {
    "age": ("people", "household", "Age"),
    "current_age": ("people", "household", "Age"),
    "client_age": ("people", "household", "Age"),
    "spouse_age": ("people", "household", "Spouse age"),
    "marital_status": ("people", "household", "Marital status"),
    "child_count": ("people", "dependents", "Child count"),
    "children": ("people", "dependents", "Children"),
    "child_age": ("people", "dependents", "Child age"),
    "child_current_age": ("people", "dependents", "Child age"),
    "dependents": ("people", "dependents", "Dependents"),
    "retirement_age": ("people", "household", "Retirement age"),
    "name": ("people", "personal_information", "Name"),
    "annual_income": ("wealth", "income", "Annual income"),
    "annual_household_income_before_tax": ("wealth", "income", "Annual income"),
    "household_income": ("wealth", "income", "Annual income"),
    "annual_spending": ("wealth", "income", "Annual spending"),
    "annual_household_spending": ("wealth", "income", "Annual spending"),
    "annual_spending_includes_mortgage": ("wealth", "income", "Spending includes mortgage"),
    "cash": ("wealth", "assets_and_debts", "Cash"),
    "taxable_brokerage": ("wealth", "assets_and_debts", "Taxable brokerage"),
    "brokerage_accounts": ("wealth", "assets_and_debts", "Brokerage accounts"),
    "brokerage": ("wealth", "assets_and_debts", "Brokerage"),
    "retirement_accounts": ("wealth", "assets_and_debts", "Retirement accounts"),
    "education_529": ("wealth", "assets_and_debts", "529 education savings"),
    "college_529": ("wealth", "assets_and_debts", "529 education savings"),
    "college_529_balance": ("wealth", "assets_and_debts", "529 education savings"),
    "college_projected_cost": ("wealth", "goals_and_constraints", "College target"),
    "college_target_amount": ("wealth", "goals_and_constraints", "College target"),
    "college_goal_amount_today_dollars": ("wealth", "goals_and_constraints", "College target"),
    "college_years_until": ("wealth", "goals_and_constraints", "Years until college"),
    "college_target_horizon_years": ("wealth", "goals_and_constraints", "Years until college"),
    "college_goal_time_horizon_years": ("wealth", "goals_and_constraints", "Years until college"),
    "education_goal_amount": ("wealth", "goals_and_constraints", "College target"),
    "education_horizon_years": ("wealth", "goals_and_constraints", "Years until college"),
    "home_value": ("wealth", "assets_and_debts", "Home value"),
    "mortgage_balance": ("wealth", "assets_and_debts", "Mortgage balance"),
    "mortgage_interest_rate": ("wealth", "assets_and_debts", "Mortgage interest rate"),
    "mortgage_remaining_term_years": ("wealth", "assets_and_debts", "Mortgage term remaining"),
    "mortgage_type": ("wealth", "assets_and_debts", "Mortgage type"),
    "home_appreciation_rate": ("wealth", "assets_and_debts", "Home appreciation rate"),
}


def _map_client_file_fact_key(key: str) -> Optional[Tuple[str, str, str]]:
    return _CLIENT_FILE_FACT_MAP.get(str(key or "").strip().lower())


def _format_client_file_value(value: Any) -> Any:
    """Render Client File values as short human text for Knowledge UI."""
    if value is None:
        return value
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            if isinstance(item, dict):
                name = item.get("name") or item.get("full_name")
                age = fact_value_for_engine_field(item, "current_age")
                if name and age is not None:
                    parts.append(f"{name} (age {age})")
                elif age is not None:
                    parts.append(f"age {age}")
                elif name:
                    parts.append(str(name))
                else:
                    compact = ", ".join(
                        f"{str(k).replace('_', ' ')} {v}"
                        for k, v in item.items()
                        if v is not None and not str(k).startswith("_")
                    )
                    if compact:
                        parts.append(compact)
            elif item is not None:
                parts.append(str(item))
        return "; ".join(parts) if parts else ""
    if isinstance(value, dict):
        compact = ", ".join(
            f"{str(k).replace('_', ' ')}: {_format_client_file_value(v)}"
            for k, v in value.items()
            if v is not None and not str(k).startswith("_")
        )
        return compact
    return value


def _knowledge_from_client_file(client_id: str, *, domain_filter: Any = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    try:
        from api.services.client_state_view import build_client_state_view

        state = build_client_state_view(client_id)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[knowledge] client-file fallback failed: {exc}", flush=True)
        return {}, {}
    facts = state.get("facts") if isinstance(state, dict) else {}
    if not isinstance(facts, dict):
        return {}, {}
    grouped: Dict[str, Any] = {}
    # Top-level CF keys and household_profile.* often carry the same labels.
    seen_labels: Dict[str, str] = {}

    def _append(domain: str, category: str, label: str, value: Any, *, source_key: str) -> None:
        if domain_filter and str(domain_filter) != domain:
            return
        dedupe_key = _norm_key(domain, category, label)
        prior_source = seen_labels.get(dedupe_key)
        if prior_source is not None:
            # Prefer top-level keys over nested household_profile.* duplicates.
            if prior_source.startswith("household_profile.") and not source_key.startswith("household_profile."):
                rows = grouped[domain]["categories"][category]
                for idx, row in enumerate(rows):
                    if str(row.get("label") or "").strip().lower() == label.strip().lower():
                        rows[idx] = _client_file_fact(domain, category, label, value, source_key=source_key)
                        seen_labels[dedupe_key] = source_key
                        break
            return
        grouped.setdefault(domain, {"domain": domain, "categories": {}})
        rows = grouped[domain]["categories"].setdefault(category, [])
        rows.append(_client_file_fact(domain, category, label, value, source_key=source_key))
        seen_labels[dedupe_key] = source_key

    # Map nested household_profile first, then top-level keys overwrite duplicates.
    household = facts.get("household_profile")
    if isinstance(household, dict):
        for nested_key, nested_value in household.items():
            mapped = _map_client_file_fact_key(str(nested_key))
            if mapped:
                domain, category, label = mapped
                _append(domain, category, label, nested_value, source_key=f"household_profile.{nested_key}")

    for key, value in facts.items():
        key_s = str(key or "").strip()
        if not key_s or key_s in _META_FACT_KEYS or key_s == "household_profile":
            continue
        mapped = _map_client_file_fact_key(key_s)
        if not mapped:
            continue
        domain, category, label = mapped
        _append(domain, category, label, value, source_key=key_s)

    summaries = {
        domain: {
            "title": {"people": "People", "wealth": "Wealth", "health": "Health"}.get(domain, domain.title()),
            "fact_count": sum(len(rows) for rows in payload.get("categories", {}).values()),
            "source": "client_state_view.facts_mapped",
        }
        for domain, payload in grouped.items()
    }
    return grouped, summaries


def _client_file_fact(
    domain: str,
    category: str,
    label: str,
    value: Any,
    *,
    source_key: str = "",
) -> Dict[str, Any]:
    return {
        "id": f"client-file:{domain}:{category}:{source_key or label}",
        "domain": domain,
        "category": category,
        "label": label,
        "value": _format_client_file_value(value),
        "status": "confirmed",
        "confidence": 1.0,
        "source": "client_file_writeback",
    }


def _diagnosis_from_client_file(client_id: str) -> Dict[str, Any]:
    try:
        from api.services.client_state_view import build_client_state_view

        state = build_client_state_view(client_id)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[diagnoses] client-file fallback failed: {exc}", flush=True)
        state = {}
    facts = state.get("facts") if isinstance(state, dict) else {}
    if not isinstance(facts, dict):
        facts = {}
    summary = state.get("summary") if isinstance(state, dict) else {}
    if not isinstance(summary, dict):
        summary = {}
    diagnoses = _diagnoses_list_from_client_file(facts=facts, summary=summary)
    return {
        "version": 0,
        "client_id": client_id,
        "snapshot_data": {
            "source": "client_state_view.fallback",
            "status": "available_from_client_file",
            "summary": summary,
            "fact_categories": sorted(facts.keys()),
            "facts": facts,
        },
        "diagnosis_data": {
            "diagnoses": diagnoses,
            "source": "client_state_view.fallback",
        },
    }


def _first_number(facts: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[float]:
    for key in keys:
        value = facts.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.replace(",", "").replace("$", "").strip())
            except ValueError:
                continue
    return None


def _money_label(amount: float) -> str:
    return f"${amount:,.0f}"


def _diagnoses_list_from_client_file(
    *,
    facts: Dict[str, Any],
    summary: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Build client-facing financial-risk diagnosis cards from Client File facts.

    System/workflow state (onboarding status, open loops, model-run TODOs) must not
    appear here — Diagnoses is a household risk surface, not an ops checklist.
    """

    del summary  # reserved for future client-facing summary signals only
    items: List[Dict[str, Any]] = []

    college_target = _first_number(
        facts,
        (
            "college_goal_amount_today_dollars",
            "college_target_amount",
            "college_projected_cost",
        ),
    )
    college_savings = _first_number(
        facts,
        ("education_529", "college_529", "college_529_balance"),
    )
    if college_target is not None and college_savings is not None:
        gap = college_target - college_savings
        if gap > 0:
            items.append(
                {
                    "id": "cf:college_funding_gap",
                    "category": "education",
                    "severity": "medium",
                    "title": "College savings trail the stated target",
                    "rationale": (
                        f"Current education savings are {_money_label(college_savings)} against a "
                        f"{_money_label(college_target)} college target "
                        f"({_money_label(gap)} short today)."
                    ),
                    "evidence_fact_ids": [],
                }
            )

    income = _first_number(
        facts,
        ("annual_income", "annual_household_income_before_tax", "household_income"),
    )
    retirement_age = _first_number(facts, ("retirement_age",))
    retirement_balance = _first_number(
        facts,
        ("retirement_accounts", "taxable_brokerage", "cash"),
    )
    if income is not None and retirement_age is not None and retirement_balance is not None:
        items.append(
            {
                "id": "cf:retirement_funding_pressure",
                "category": "retirement",
                "severity": "medium",
                "title": "Retirement funding path still looks thin",
                "rationale": (
                    f"With {_money_label(income)} household income, a target retirement age of "
                    f"{int(retirement_age)}, and about {_money_label(retirement_balance)} in "
                    "investable balances, the retirement path still carries funding pressure "
                    "until a projection confirms the gap."
                ),
                "evidence_fact_ids": [],
            }
        )

    return items
def create_knowledge_blueprint(
    *,
    user_auth_decorator: Callable[[Any], Any],
    deps: Any,
) -> Blueprint:
    """Create knowledge routes with app-level dependency hooks."""
    bp = Blueprint("knowledge", __name__)

    @bp.route("/api/v1/knowledge", methods=["GET"])
    @user_auth_decorator
    def get_knowledge(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        domain_filter = request.args.get("domain")

        facts = deps.db_get_knowledge_facts(client_id, domain=domain_filter)
        grouped: Dict[str, Any] = {}
        for fact in facts:
            d = fact["domain"]
            if d not in grouped:
                grouped[d] = {"domain": d, "categories": {}}
            cat = fact["category"]
            if cat not in grouped[d]["categories"]:
                grouped[d]["categories"][cat] = []
            grouped[d]["categories"][cat].append(fact)

        snapshot = deps.db_get_latest_knowledge_snapshot(client_id)
        section_summaries: Dict[str, Any] = {}
        if snapshot and snapshot.get("snapshot_data"):
            snap_data = snapshot["snapshot_data"]
            for _domain_key, domain_data in snap_data.items():
                if isinstance(domain_data, dict) and "_section_summaries" in domain_data:
                    section_summaries.update(domain_data["_section_summaries"])

        if not grouped:
            grouped, section_summaries = _knowledge_from_client_file(client_id, domain_filter=domain_filter)

        return jsonify({
            "success": True,
            "knowledge": grouped,
            "snapshot_version": snapshot["version"] if snapshot else 0,
            "section_summaries": section_summaries,
        }), 200

    @bp.route("/api/v1/knowledge/facts/<fact_id>/confirm", methods=["POST"])
    @user_auth_decorator
    def confirm_fact(fact_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        fact = deps.db_get_knowledge_fact(fact_id)
        if not fact:
            return jsonify({"success": False, "error": "Fact not found"}), 404
        if fact["client_id"] != auth_session["client_id"]:
            return jsonify({"success": False, "error": "Forbidden"}), 403

        now_iso = datetime.now(timezone.utc).isoformat()
        deps.db_update_knowledge_fact(
            fact_id,
            status="confirmed",
            last_confirmed_at=now_iso,
            last_updated_at=now_iso,
        )

        new_version = deps.rebuild_snapshot_targeted(
            client_id=fact["client_id"],
            changed_fact=fact,
            trigger_event="fact_confirm",
            trigger_event_id=fact_id,
        )

        return jsonify({
            "success": True,
            "fact_id": fact_id,
            "status": "confirmed",
            "snapshot_version": new_version,
        }), 200

    @bp.route("/api/v1/knowledge/facts/<fact_id>/correct", methods=["POST"])
    @user_auth_decorator
    def correct_fact(fact_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        fact = deps.db_get_knowledge_fact(fact_id)
        if not fact:
            return jsonify({"success": False, "error": "Fact not found"}), 404
        if fact["client_id"] != auth_session["client_id"]:
            return jsonify({"success": False, "error": "Forbidden"}), 403

        body = request.get_json(silent=True) or {}
        corrected_value = body.get("corrected_value")
        if corrected_value is None:
            return jsonify({"success": False, "error": "corrected_value is required"}), 400

        now_iso = datetime.now(timezone.utc).isoformat()
        deps.db_update_knowledge_fact(
            fact_id,
            value=corrected_value,
            status="confirmed",
            confidence=1.0,
            last_confirmed_at=now_iso,
            last_updated_at=now_iso,
        )

        new_version = deps.rebuild_snapshot_targeted(
            client_id=fact["client_id"],
            changed_fact=fact,
            trigger_event="fact_correct",
            trigger_event_id=fact_id,
        )

        return jsonify({
            "success": True,
            "fact_id": fact_id,
            "status": "confirmed",
            "snapshot_version": new_version,
        }), 200

    @bp.route("/api/v1/knowledge/facts/<fact_id>/dismiss", methods=["POST"])
    @user_auth_decorator
    def dismiss_fact(fact_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        fact = deps.db_get_knowledge_fact(fact_id)
        if not fact:
            return jsonify({"success": False, "error": "Fact not found"}), 404
        if fact["client_id"] != auth_session["client_id"]:
            return jsonify({"success": False, "error": "Forbidden"}), 403

        deps.db_update_knowledge_fact(fact_id, status="dismissed")
        new_version = deps.rebuild_snapshot_targeted(
            client_id=fact["client_id"],
            changed_fact=fact,
            trigger_event="fact_dismiss",
            trigger_event_id=fact_id,
        )

        return jsonify({
            "success": True,
            "fact_id": fact_id,
            "status": "dismissed",
            "snapshot_version": new_version,
        }), 200

    @bp.route("/api/v1/diagnoses", methods=["GET"])
    @user_auth_decorator
    def get_diagnoses(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        snapshot = deps.db_get_latest_diagnosis_snapshot(client_id)
        refresh_state = deps.get_diagnosis_refresh_payload(client_id)
        if not snapshot:
            snapshot = _diagnosis_from_client_file(client_id)
        else:
            diagnosis_data = snapshot.get("diagnosis_data") if isinstance(snapshot, dict) else None
            diagnoses = (
                diagnosis_data.get("diagnoses")
                if isinstance(diagnosis_data, dict)
                else None
            )
            source = str((diagnosis_data or {}).get("source") or "").lower() if isinstance(diagnosis_data, dict) else ""
            snap_source = ""
            snap_data = snapshot.get("snapshot_data") if isinstance(snapshot, dict) else None
            if isinstance(snap_data, dict):
                snap_source = str(snap_data.get("source") or "").lower()
            needs_client_file_cards = (
                not isinstance(diagnoses, list)
                or not diagnoses
                or "client_state_view" in source
                or "client_state_view" in snap_source
                or any(
                    str(item.get("category") or "").lower() in {"onboarding", "planning"}
                    or "onboarding" in str(item.get("title") or "").lower()
                    for item in diagnoses
                    if isinstance(item, dict)
                )
            )
            if needs_client_file_cards:
                fallback = _diagnosis_from_client_file(client_id)
                if isinstance(snapshot, dict):
                    snapshot = dict(snapshot)
                    snapshot["diagnosis_data"] = fallback.get("diagnosis_data") or {
                        "diagnoses": [],
                        "source": "client_state_view.fallback",
                    }
        return jsonify({
            "success": True,
            "diagnoses": snapshot,
            "diagnosis_refresh": refresh_state,
        }), 200

    @bp.route("/api/v1/knowledge/pending-confirmations", methods=["GET"])
    @user_auth_decorator
    def get_pending_confirmations_endpoint(auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        confirmations = deps.db_get_pending_confirmations(auth_session["client_id"])
        return jsonify({
            "success": True,
            "pending_confirmations": confirmations,
        }), 200

    @bp.route("/api/v1/knowledge/pending-confirmations/<confirmation_id>/resolve", methods=["POST"])
    @user_auth_decorator
    def resolve_confirmation(confirmation_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        resolution = body.get("resolution")
        if resolution not in ("confirmed", "dismissed"):
            return jsonify({"success": False, "error": "resolution must be 'confirmed' or 'dismissed'"}), 400

        confirmation = deps.db_get_pending_confirmation(confirmation_id)
        if not confirmation:
            return jsonify({"success": False, "error": "Confirmation not found"}), 404
        if confirmation["client_id"] != auth_session["client_id"]:
            return jsonify({"success": False, "error": "Forbidden"}), 403

        client_id = confirmation["client_id"]
        deps.db_resolve_pending_confirmation(confirmation_id, resolution)

        diagnosis_version = None
        diagnosis_refresh_queued = False
        if resolution == "confirmed":
            confirm_ctx = deps.commit_confirmed_pending(
                client_id=client_id,
                confirmation_id=confirmation_id,
                confirmation=confirmation,
                caller="resolve_confirmation",
                refresh_diagnosis=False,
                return_context=True,
            )
            committed_fact = confirm_ctx.get("committed_fact")
            if committed_fact:
                diagnosis_refresh_queued = deps.queue_confirmation_diagnosis_refresh(
                    client_id=client_id,
                    committed_fact=committed_fact,
                )
            diagnosis_version = confirm_ctx.get("diagnosis_version")

            return jsonify({
                "success": True,
                "confirmation_id": confirmation_id,
                "resolution": resolution,
                "diagnosis_refreshed": diagnosis_version is not None,
                "diagnosis_refresh_queued": diagnosis_refresh_queued,
                "diagnosis_status": (
                    deps.get_diagnosis_refresh_payload(client_id).get("status")
                    if diagnosis_refresh_queued or diagnosis_version is not None
                    else "not_required"
                ),
            }), 200

        return jsonify({
            "success": True,
            "confirmation_id": confirmation_id,
            "resolution": resolution,
            "diagnosis_refreshed": False,
            "diagnosis_refresh_queued": False,
            "diagnosis_status": "not_required",
        }), 200

    @bp.route("/api/v1/knowledge/sections/<section_id>/narrative-parse", methods=["POST"])
    @user_auth_decorator
    def parse_section_narrative(section_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        body = request.get_json(silent=True) or {}
        narrative = (body.get("narrative") or "").strip()
        if not narrative:
            return jsonify({"success": False, "error": "narrative is required"}), 400

        section_titles = deps.section_titles()
        if section_id not in section_titles:
            return jsonify({"success": False, "error": "Unknown section"}), 404

        section_title = section_titles[section_id]
        if section_id in ("people", "health"):
            section_facts_raw = deps.db_get_knowledge_facts(client_id, domain=section_id)
        else:
            category_to_section = deps.category_to_section()
            cats = {cat for cat, sec in category_to_section.items() if sec == section_id}
            section_facts_raw = deps.db_get_knowledge_facts(client_id, domain="wealth")
            section_facts_raw = [f for f in section_facts_raw if f.get("category") in cats]
        section_facts = [f for f in section_facts_raw if f.get("status") != "dismissed"]

        try:
            extractor = deps.get_client_profile_extractor()
            extraction = extractor.extract_facts(
                transcript={"turns": [{"role": "client", "text": narrative}]},
                session_metadata={
                    "session_type": "narrative_edit",
                    "section": section_title,
                    "context": (
                        f"The client is editing their '{section_title}' section directly. "
                        "Extract every financial fact stated in the text."
                    ),
                },
            )
            candidates = extraction.get("candidate_facts", [])
        except Exception as exc:
            print(f"[narrative_parse] Extraction failed: {exc}", flush=True)
            return jsonify({"success": False, "error": "Failed to analyse narrative"}), 500

        existing_by_key: Dict[str, Any] = {
            _norm_key(f.get("domain", ""), f.get("category", ""), f.get("label", "")): f
            for f in section_facts
        }
        updated: List[Dict[str, Any]] = []
        new_facts: List[Dict[str, Any]] = []
        matched_existing_ids: set = set()

        for candidate in candidates:
            key = _norm_key(
                candidate.get("domain", ""),
                candidate.get("category", ""),
                candidate.get("label", ""),
            )
            existing = existing_by_key.get(key)
            if existing:
                matched_existing_ids.add(existing["id"])
                if existing.get("value") != candidate.get("value"):
                    updated.append({
                        **candidate,
                        "fact_id": existing["id"],
                        "old_value": existing.get("value"),
                    })
            else:
                new_facts.append(candidate)

        not_mentioned = [
            f for f in section_facts
            if f.get("id") and f["id"] not in matched_existing_ids
        ]

        return jsonify({
            "success": True,
            "section_id": section_id,
            "updated": updated,
            "new": new_facts,
            "not_mentioned": not_mentioned,
        }), 200

    @bp.route("/api/v1/knowledge/sections/<section_id>/narrative-commit", methods=["POST"])
    @user_auth_decorator
    def commit_section_narrative(section_id: str, auth_session: Dict[str, Any]) -> Tuple[Any, int]:
        client_id = auth_session["client_id"]
        body = request.get_json(silent=True) or {}
        narrative = (body.get("narrative") or "").strip()
        confirmed_candidates = body.get("confirmed_candidates", [])
        dismissed_fact_ids = body.get("dismissed_fact_ids", [])

        if not narrative:
            return jsonify({"success": False, "error": "narrative is required"}), 400
        section_titles = deps.section_titles()
        if section_id not in section_titles:
            return jsonify({"success": False, "error": "Unknown section"}), 404

        source_event_id = f"narrative_edit:{section_id}:{datetime.now(timezone.utc).isoformat()}"
        current_facts = deps.db_get_knowledge_facts(client_id)
        updater = deps.get_knowledge_updater()
        update_result = updater.update_knowledge(
            client_id=client_id,
            current_facts=current_facts,
            candidate_updates=confirmed_candidates,
            evidence_refs=[{"type": "narrative_edit", "section": section_id}],
            source_event_id=source_event_id,
            trigger_event="narrative_edit",
        )

        from domain.knowledge.truth import commit_truth_update  # noqa: PLC0415

        truth_result = commit_truth_update(
            client_id=client_id,
            update_result=update_result,
            trigger_event="narrative_edit",
            trigger_event_id=source_event_id,
        )

        if truth_result.pending_confirmations:
            forced_facts = []
            for pc in truth_result.pending_confirmations:
                if pc.get("id"):
                    deps.db_resolve_pending_confirmation(pc["id"], "confirmed")
                forced_facts.append({
                    "id": pc.get("fact_id") or None,
                    "domain": pc["domain"],
                    "category": pc["category"],
                    "label": pc["label"],
                    "value": pc.get("proposed_value"),
                    "status": "confirmed",
                    "confidence": 0.95,
                    "source_event_ids": [source_event_id],
                })
            if forced_facts:
                deps.db_bulk_upsert_knowledge_facts(client_id, forced_facts)

        now_iso = datetime.now(timezone.utc).isoformat()
        for fact_id in dismissed_fact_ids:
            fact = deps.db_get_knowledge_fact(fact_id)
            if fact and fact.get("client_id") == client_id:
                deps.db_update_knowledge_fact(fact_id, status="dismissed", last_updated_at=now_iso)

        latest_snapshot = deps.db_get_latest_knowledge_snapshot(client_id)
        if latest_snapshot and latest_snapshot.get("snapshot_data"):
            existing_summaries = deps.carry_forward_section_summaries(client_id) or {}
            existing_summaries[section_id] = narrative

            all_facts_now = deps.db_get_knowledge_facts(client_id)
            patched_snapshot = updater.build_compact_snapshot(
                all_facts_now,
                section_summaries=existing_summaries,
            )
            patch_version = deps.db_get_current_snapshot_version(client_id) + 1
            deps.db_store_knowledge_snapshot(
                client_id=client_id,
                version=patch_version,
                snapshot_data=patched_snapshot,
                fact_ids=[f["id"] for f in all_facts_now if f.get("id")],
                trigger_event="narrative_summary_patch",
                trigger_event_id=source_event_id,
            )
            snapshot_version = patch_version
        else:
            snapshot_version = truth_result.snapshot_version

        return jsonify({
            "success": True,
            "section_id": section_id,
            "snapshot_version": snapshot_version,
        }), 200

    return bp


__all__ = ["create_knowledge_blueprint"]
