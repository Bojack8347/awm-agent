"""KnowledgeService — snapshot rebuild helpers.

Extracted from app.py. Contains no Flask dependencies.

Handles:
- Targeted knowledge snapshot rebuild after individual fact changes

Injected callables (to avoid import cycles with app.py):
  get_knowledge_updater_fn  — returns KnowledgeUpdaterAgent instance
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

try:
    from api.persistence import (
        get_knowledge_facts as db_get_knowledge_facts,
        get_latest_knowledge_snapshot as db_get_latest_knowledge_snapshot,
        get_current_snapshot_version as db_get_current_snapshot_version,
        store_knowledge_snapshot as db_store_knowledge_snapshot,
    )
except ImportError:
    db_get_knowledge_facts = lambda *a, **kw: []  # noqa: E731
    db_get_latest_knowledge_snapshot = lambda *a, **kw: None  # noqa: E731
    db_get_current_snapshot_version = lambda *a, **kw: 0  # noqa: E731
    db_store_knowledge_snapshot = lambda *a, **kw: None  # noqa: E731

# ---------------------------------------------------------------------------
# Domain → section mapping (used for targeted snapshot rebuild)
# ---------------------------------------------------------------------------

CATEGORY_TO_SECTION: Dict[str, str] = {
    "client_profile": "financial_position",
    "accounts": "financial_position",
    "asset_allocation": "financial_position",
    "liabilities": "financial_position",
    "income": "income",
    "non_recurring_expenses": "expected_future_expenses",
    "recurring_expenses": "expected_future_expenses",
    "tax": "tax",
    "preferences": "preferences_and_constraints",
    "insurance": "protection_and_resilience",
}

SECTION_TITLES: Dict[str, str] = {
    "financial_position": "Financial Position",
    "income": "Income",
    "expected_future_expenses": "Expected Future Expenses",
    "tax": "Tax",
    "preferences_and_constraints": "Preferences & Constraints",
    "protection_and_resilience": "Protection & Resilience",
    "people": "People",
    "health": "Health",
}


class KnowledgeService:
    """Handles knowledge snapshot rebuilds.

    Usage in app.py::

        _KNOWLEDGE_SERVICE: Optional[KnowledgeService] = None

        def get_knowledge_service() -> KnowledgeService:
            global _KNOWLEDGE_SERVICE
            if _KNOWLEDGE_SERVICE is None:
                _KNOWLEDGE_SERVICE = KnowledgeService(
                    get_knowledge_updater,
                )
            return _KNOWLEDGE_SERVICE
    """

    def __init__(
        self,
        get_knowledge_updater_fn: Callable,
    ) -> None:
        self._get_updater = get_knowledge_updater_fn

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------

    def sections_for_fact(self, fact: Dict[str, Any]) -> set:
        """Return the set of section keys affected by a single fact change."""
        domain = fact.get("domain", "")
        category = fact.get("category", "")
        if domain == "wealth" and category in CATEGORY_TO_SECTION:
            return {CATEGORY_TO_SECTION[category]}
        if domain in ("people", "health"):
            return {domain}
        return set()

    def carry_forward_section_summaries(
        self,
        client_id: str,
        invalidate_sections: Optional[set] = None,
    ) -> Optional[Dict[str, str]]:
        """Extract section summaries from the latest snapshot to carry forward.

        Used by non-LLM truth-change paths (confirm, correct, dismiss) so that
        unaffected section summaries are preserved when rebuilding the snapshot.
        Sections in invalidate_sections are dropped so stale content doesn't persist.
        """
        snapshot = db_get_latest_knowledge_snapshot(client_id)
        if not snapshot or not snapshot.get("snapshot_data"):
            return None
        summaries: Dict[str, str] = {}
        for domain_data in snapshot["snapshot_data"].values():
            if isinstance(domain_data, dict) and "_section_summaries" in domain_data:
                summaries.update(domain_data["_section_summaries"])
        if invalidate_sections:
            for key in invalidate_sections:
                summaries.pop(key, None)
        return summaries or None

    def rebuild_snapshot_targeted(
        self,
        client_id: str,
        changed_fact: Dict[str, Any],
        trigger_event: str,
        trigger_event_id: str,
    ) -> int:
        """Rebuild the knowledge snapshot, regenerating only the affected section.

        1. Carry forward all existing summaries
        2. Identify which section the changed fact belongs to
        3. Regenerate that section's LLM summary (skipped if no active facts remain)
        4. Build and store a new snapshot
        Returns the new snapshot version.
        """
        affected_sections = self.sections_for_fact(changed_fact)
        existing_summaries = self.carry_forward_section_summaries(client_id) or {}

        all_facts = db_get_knowledge_facts(client_id)
        active_facts = [
            f for f in all_facts
            if f.get("status") != "dismissed" and not f.get("is_deleted")
        ]

        if affected_sections:
            updater = self._get_updater()
            for section_key in affected_sections:
                title = SECTION_TITLES.get(section_key, section_key)

                if section_key in ("people", "health"):
                    section_facts = [f for f in active_facts if f.get("domain") == section_key]
                else:
                    cats = {cat for cat, sec in CATEGORY_TO_SECTION.items() if sec == section_key}
                    section_facts = [
                        f for f in active_facts
                        if f.get("domain") == "wealth" and f.get("category") in cats
                    ]

                if section_facts:
                    new_summary = updater.regenerate_section_summary(section_key, title, section_facts)
                    if new_summary:
                        existing_summaries[section_key] = new_summary
                else:
                    existing_summaries.pop(section_key, None)

        updater = self._get_updater()
        snapshot_data = updater.build_compact_snapshot(
            all_facts, section_summaries=existing_summaries or None,
        )
        new_version = db_get_current_snapshot_version(client_id) + 1
        db_store_knowledge_snapshot(
            client_id=client_id,
            version=new_version,
            snapshot_data=snapshot_data,
            fact_ids=[f["id"] for f in all_facts if f.get("id")],
            trigger_event=trigger_event,
            trigger_event_id=trigger_event_id,
        )

        return new_version
