"""ProactivePlanner — deterministic trigger evaluator for proactive companion engagement.

Evaluates a client's current state (knowledge, diagnoses, pending confirmations,
active journeys, last interaction) against trigger rules and decides:
  - Whether to fire a proactive outreach
  - Which trigger class and type
  - Which guidance mode to use
  - What the objective and grounding facts are
  - The escalation level

Purely deterministic — no LLM calls. The Message Composer agent turns the
planner's output into natural language.

Extracted as a standalone service (no Flask dependencies) following the same
pattern as DiagnosisService and KnowledgeService.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger("awm.services.proactive_planner")

try:
    from api.persistence import (
        get_pending_confirmations as db_get_pending_confirmations,
        get_active_journey_runs as db_get_active_journey_runs,
        get_latest_knowledge_snapshot as db_get_latest_knowledge_snapshot,
        get_latest_diagnosis_snapshot as db_get_latest_diagnosis_snapshot,
        get_knowledge_facts as db_get_knowledge_facts,
        get_client_escalation_trackings as db_get_client_escalation_trackings,
        get_escalation_tracking as db_get_escalation_tracking,
        get_last_companion_interaction_at as db_get_last_companion_interaction_at,
        get_recent_outreach as db_get_recent_outreach,
        list_canonical_client_facts as db_list_canonical_client_facts,
    )
except ImportError:
    db_get_pending_confirmations = lambda *a, **kw: []  # noqa: E731
    db_get_active_journey_runs = lambda *a, **kw: []  # noqa: E731
    db_get_latest_knowledge_snapshot = lambda *a, **kw: None  # noqa: E731
    db_get_latest_diagnosis_snapshot = lambda *a, **kw: None  # noqa: E731
    db_get_knowledge_facts = lambda *a, **kw: []  # noqa: E731
    db_get_client_escalation_trackings = lambda *a, **kw: []  # noqa: E731
    db_get_escalation_tracking = lambda *a, **kw: None  # noqa: E731
    db_get_last_companion_interaction_at = lambda *a, **kw: None  # noqa: E731
    db_get_recent_outreach = lambda *a, **kw: []  # noqa: E731
    db_list_canonical_client_facts = lambda *a, **kw: []  # noqa: E731


# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

MAX_ESCALATION_MENTIONS = 3
ESCALATION_COOLDOWN_HOURS = 72        # minimum hours between mentions of same topic
STALE_CONFIRMATION_DAYS = 3           # days before a pending confirmation is "stale"
JOURNEY_ABANDONMENT_HOURS = 48        # hours of inactivity before journey is "abandoned"
REENGAGEMENT_DAYS = 5                 # days of silence before relational re-engagement
ADVISORY_COOLDOWN_HOURS = 96          # hours between advisory trigger mentions
GLOBAL_OUTREACH_COOLDOWN_HOURS = 20   # minimum hours between any two outreach messages per client

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TriggerResult:
    """Output of the planner: what to do and why."""
    should_fire: bool
    trigger_class: str = ""           # 'operational', 'advisory', 'relational'
    trigger_type: str = ""            # e.g. 'incomplete_onboarding'
    trigger_reason: str = ""          # human-readable explanation
    guidance_mode: str = ""           # 'open_listening', 'gentle_steering', 'direct_guidance'
    objective: str = ""               # e.g. 'complete_onboarding', 'confirm_pending:abc'
    grounding_facts: List[Dict[str, Any]] = field(default_factory=list)
    grounding_fact_ids: List[str] = field(default_factory=list)
    escalation_level: int = 0
    allowed_cta: str = ""             # what the message is allowed to suggest
    diagnosis_snapshot_version: Optional[int] = None
    knowledge_snapshot_version: Optional[int] = None


class TriggerResults(list):
    """List of trigger results with legacy single-result attribute access.

    The proactive service consumes all eligible triggers as a list. Older tests
    and call sites treated ``evaluate_from_state`` as returning only the
    highest-priority result. This wrapper supports both contracts: list
    behavior for the digest pipeline and attribute access for the primary
    trigger.
    """

    def _primary(self) -> TriggerResult:
        if self:
            return self[0]
        return TriggerResult(should_fire=False)

    @property
    def should_fire(self) -> bool:
        return bool(self)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._primary(), name)


@dataclass
class ClientState:
    """Snapshot of a client's current state for trigger evaluation."""
    client_id: str
    knowledge_snapshot: Optional[Dict[str, Any]] = None
    knowledge_snapshot_version: Optional[int] = None
    diagnosis_snapshot: Optional[Dict[str, Any]] = None
    diagnosis_snapshot_version: Optional[int] = None
    diagnosis_knowledge_snapshot_version: Optional[int] = None
    pending_confirmations: List[Dict[str, Any]] = field(default_factory=list)
    active_journeys: List[Dict[str, Any]] = field(default_factory=list)
    knowledge_facts: List[Dict[str, Any]] = field(default_factory=list)
    canonical_facts: List[Dict[str, Any]] = field(default_factory=list)
    escalation_trackings: List[Dict[str, Any]] = field(default_factory=list)
    last_interaction_at: Optional[datetime] = None
    recent_outreach: List[Dict[str, Any]] = field(default_factory=list)
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# ProactivePlanner
# ---------------------------------------------------------------------------

class ProactivePlanner:
    """Deterministic trigger evaluator for proactive companion engagement.

    Instantiate once and share across the process (lazy singleton pattern).

    Usage in app.py::

        _PROACTIVE_PLANNER: Optional[ProactivePlanner] = None

        def get_proactive_planner() -> ProactivePlanner:
            global _PROACTIVE_PLANNER
            if _PROACTIVE_PLANNER is None:
                _PROACTIVE_PLANNER = ProactivePlanner()
            return _PROACTIVE_PLANNER
    """

    def evaluate(self, client_id: str) -> TriggerResults:
        """Evaluate all triggers for a client and return every eligible one.

        Fetches client state from DB, then runs all trigger checks in priority
        order, collecting every trigger that warrants outreach (subject to
        per-topic cooldowns and the global daily cooldown).

        Returns a list of TriggerResults with should_fire=True, ordered by
        priority (operational → advisory → relational). Returns an empty list
        if nothing should fire.
        """
        state = self._load_client_state(client_id)
        return self.evaluate_from_state(state)

    def evaluate_from_state(self, state: ClientState) -> TriggerResults:
        """Evaluate triggers from a pre-loaded client state.

        Useful for testing — avoids DB calls by accepting a ClientState directly.
        Returns a list of all eligible TriggerResults (may be empty).
        """
        # Global cooldown: never send more than one daily digest within
        # GLOBAL_OUTREACH_COOLDOWN_HOURS, regardless of how many triggers fire.
        if not self._is_globally_cooled_down(state):
            logger.info(
                "[proactive_planner] Global outreach cooldown active for client=%s — skipping",
                state.client_id,
            )
            return TriggerResults()

        triggers = TriggerResults()

        # Priority 1: Operational triggers (collect all that are eligible)
        result = self._check_incomplete_onboarding(state)
        if result.should_fire:
            triggers.append(result)

        result = self._check_stale_confirmations(state)
        if result.should_fire:
            triggers.append(result)

        result = self._check_journey_abandonment(state)
        if result.should_fire:
            triggers.append(result)

        # Priority 2: Advisory triggers
        result = self._check_diagnosis_gaps(state)
        if result.should_fire:
            triggers.append(result)

        # Priority 3: Relational — only include if nothing operational/advisory fired,
        # so V never mixes "thinking about you" warmth with a task list in the same message.
        if not triggers:
            result = self._check_reengagement(state)
            if result.should_fire:
                triggers.append(result)

        return triggers

    # ------------------------------------------------------------------
    # State loading
    # ------------------------------------------------------------------

    def _load_client_state(self, client_id: str) -> ClientState:
        """Fetch all state needed for trigger evaluation from DB."""
        knowledge_snapshot_raw = db_get_latest_knowledge_snapshot(client_id)
        diagnosis_snapshot_raw = db_get_latest_diagnosis_snapshot(client_id)

        knowledge_snapshot = None
        knowledge_snapshot_version = None
        if knowledge_snapshot_raw:
            knowledge_snapshot = knowledge_snapshot_raw.get("snapshot_data", {})
            knowledge_snapshot_version = knowledge_snapshot_raw.get("version")

        diagnosis_snapshot = None
        diagnosis_snapshot_version = None
        diagnosis_knowledge_snapshot_version = None
        if diagnosis_snapshot_raw:
            diagnosis_snapshot = diagnosis_snapshot_raw.get("diagnosis_data", {})
            diagnosis_snapshot_version = diagnosis_snapshot_raw.get("version")
            diagnosis_knowledge_snapshot_version = diagnosis_snapshot_raw.get(
                "knowledge_snapshot_version"
            )

        last_interaction_str = db_get_last_companion_interaction_at(client_id)
        last_interaction_at = None
        if last_interaction_str:
            try:
                last_interaction_at = datetime.fromisoformat(last_interaction_str)
                if last_interaction_at.tzinfo is None:
                    last_interaction_at = last_interaction_at.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass

        return ClientState(
            client_id=client_id,
            knowledge_snapshot=knowledge_snapshot,
            knowledge_snapshot_version=knowledge_snapshot_version,
            diagnosis_snapshot=diagnosis_snapshot,
            diagnosis_snapshot_version=diagnosis_snapshot_version,
            diagnosis_knowledge_snapshot_version=diagnosis_knowledge_snapshot_version,
            pending_confirmations=db_get_pending_confirmations(client_id, status="pending"),
            active_journeys=db_get_active_journey_runs(client_id),
            knowledge_facts=db_get_knowledge_facts(client_id),
            canonical_facts=db_list_canonical_client_facts(client_id=client_id),
            escalation_trackings=db_get_client_escalation_trackings(client_id),
            last_interaction_at=last_interaction_at,
            recent_outreach=db_get_recent_outreach(client_id, limit=5),
        )

    # ------------------------------------------------------------------
    # Global cooldown helpers
    # ------------------------------------------------------------------

    def _is_globally_cooled_down(self, state: ClientState) -> bool:
        """Return True if enough time has passed since the last outreach of any kind.

        Prevents the client from receiving multiple proactive messages in rapid
        succession when the scheduler is called more than once or when several
        triggers happen to be eligible at the same time.
        """
        if not state.recent_outreach:
            return True
        latest = state.recent_outreach[0]
        created_at_str = latest.get("created_at")
        if not created_at_str:
            return True
        try:
            last_dt = datetime.fromisoformat(created_at_str)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            return (state.now - last_dt) >= timedelta(hours=GLOBAL_OUTREACH_COOLDOWN_HOURS)
        except (ValueError, TypeError):
            return True

    # ------------------------------------------------------------------
    # Escalation helpers
    # ------------------------------------------------------------------

    def _get_escalation_for_topic(
        self, state: ClientState, topic_key: str,
    ) -> Optional[Dict[str, Any]]:
        """Find escalation tracking for a specific topic from pre-loaded state."""
        for t in state.escalation_trackings:
            if t.get("topic_key") == topic_key:
                return t
        return None

    def _is_topic_cooled_down(
        self,
        state: ClientState,
        topic_key: str,
        cooldown_hours: int = ESCALATION_COOLDOWN_HOURS,
    ) -> bool:
        """Check if enough time has passed since the last mention of this topic."""
        tracking = self._get_escalation_for_topic(state, topic_key)
        if not tracking:
            return True  # never mentioned before — ok to fire
        last_surfaced = tracking.get("last_surfaced_at")
        if not last_surfaced:
            return True
        try:
            last_dt = datetime.fromisoformat(last_surfaced)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            return (state.now - last_dt) >= timedelta(hours=cooldown_hours)
        except (ValueError, TypeError):
            return True

    def _is_topic_backed_off(
        self, state: ClientState, topic_key: str,
    ) -> bool:
        """Check if this topic has hit the max mention limit."""
        tracking = self._get_escalation_for_topic(state, topic_key)
        if not tracking:
            return False
        return bool(tracking.get("backed_off"))

    def _get_escalation_level(
        self, state: ClientState, topic_key: str,
    ) -> int:
        """Get the current escalation level (times_surfaced) for a topic."""
        tracking = self._get_escalation_for_topic(state, topic_key)
        if not tracking:
            return 0
        return tracking.get("times_surfaced", 0)

    def _select_guidance_mode(
        self, trigger_class: str, escalation_level: int,
    ) -> str:
        """Select guidance mode based on trigger class and escalation level."""
        if trigger_class == "relational":
            return "open_listening"

        if trigger_class == "operational":
            if escalation_level < 2:
                return "gentle_steering"
            return "direct_guidance"

        if trigger_class == "advisory":
            if escalation_level < 1:
                return "gentle_steering"
            return "direct_guidance"

        return "open_listening"

    # ------------------------------------------------------------------
    # Trigger checks — each returns TriggerResult
    # ------------------------------------------------------------------

    def _check_incomplete_onboarding(self, state: ClientState) -> TriggerResult:
        """Check if the client has incomplete onboarding (critical fields missing)."""
        topic_key = "incomplete_onboarding"
        if self._is_topic_backed_off(state, topic_key):
            return TriggerResult(should_fire=False)
        if not self._is_topic_cooled_down(state, topic_key):
            return TriggerResult(should_fire=False)

        from api.services.onboarding_completeness import advisor_onboarding_completeness

        completeness = advisor_onboarding_completeness(state.canonical_facts)
        if completeness["complete"]:
            return TriggerResult(should_fire=False)
        populated_areas = int(completeness["populated_count"])
        total_critical = int(completeness["required_count"])
        missing_areas = list(completeness["missing_areas"])
        escalation_level = self._get_escalation_level(state, topic_key)

        return TriggerResult(
            should_fire=True,
            trigger_class="operational",
            trigger_type="incomplete_onboarding",
            trigger_reason=(
                f"Client has {populated_areas}/{total_critical} critical profile areas "
                f"populated. Missing: {', '.join(missing_areas[:3])}"
            ),
            guidance_mode=self._select_guidance_mode("operational", escalation_level),
            objective="complete_onboarding",
            grounding_facts=[
                {"area": area, "status": "missing"} for area in missing_areas[:3]
            ],
            escalation_level=escalation_level,
            allowed_cta="open_consultation",
            knowledge_snapshot_version=state.knowledge_snapshot_version,
        )

    def _check_stale_confirmations(self, state: ClientState) -> TriggerResult:
        """Check for pending confirmations older than STALE_CONFIRMATION_DAYS."""
        stale_threshold = state.now - timedelta(days=STALE_CONFIRMATION_DAYS)

        for conf in state.pending_confirmations:
            created_at_str = conf.get("created_at")
            if not created_at_str:
                continue
            try:
                created_at = datetime.fromisoformat(created_at_str)
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

            if created_at > stale_threshold:
                continue

            conf_id = conf.get("id", "unknown")
            topic_key = f"stale_confirmation:{conf_id}"

            if self._is_topic_backed_off(state, topic_key):
                continue
            if not self._is_topic_cooled_down(state, topic_key):
                continue

            escalation_level = self._get_escalation_level(state, topic_key)
            change_type = conf.get("change_type", "update")
            evidence = conf.get("evidence", "")

            return TriggerResult(
                should_fire=True,
                trigger_class="operational",
                trigger_type="stale_pending_confirmation",
                trigger_reason=(
                    f"Pending confirmation ({change_type}) created "
                    f"{(state.now - created_at).days} days ago: {evidence}"
                ),
                guidance_mode=self._select_guidance_mode("operational", escalation_level),
                objective=f"confirm_pending:{conf_id}",
                grounding_facts=[conf],
                grounding_fact_ids=[conf.get("fact_id", "")] if conf.get("fact_id") else [],
                escalation_level=escalation_level,
                allowed_cta="confirm_fact",
                knowledge_snapshot_version=state.knowledge_snapshot_version,
            )

        return TriggerResult(should_fire=False)

    def _check_journey_abandonment(self, state: ClientState) -> TriggerResult:
        """Check for active journeys with no progress in JOURNEY_ABANDONMENT_HOURS."""
        abandonment_threshold = state.now - timedelta(hours=JOURNEY_ABANDONMENT_HOURS)

        for journey in state.active_journeys:
            status = journey.get("status", "")
            if status not in ("pending", "consulting", "running"):
                continue

            # Use created_at as proxy for last activity
            created_at_str = journey.get("created_at")
            if not created_at_str:
                continue
            try:
                created_at = datetime.fromisoformat(created_at_str)
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

            if created_at > abandonment_threshold:
                continue

            journey_id = journey.get("id", "unknown")
            topic_key = f"journey_abandonment:{journey_id}"

            if self._is_topic_backed_off(state, topic_key):
                continue
            if not self._is_topic_cooled_down(state, topic_key):
                continue

            escalation_level = self._get_escalation_level(state, topic_key)
            journey_type = journey.get("journey_type", "investment")

            return TriggerResult(
                should_fire=True,
                trigger_class="operational",
                trigger_type="journey_abandonment",
                trigger_reason=(
                    f"{journey_type.title()} journey ({status}) started "
                    f"{(state.now - created_at).days} days ago with no completion"
                ),
                guidance_mode=self._select_guidance_mode("operational", escalation_level),
                objective=f"resume_journey:{journey_id}",
                grounding_facts=[{
                    "journey_type": journey_type,
                    "status": status,
                    "started": created_at_str,
                }],
                escalation_level=escalation_level,
                allowed_cta="handoff_to_journey",
                knowledge_snapshot_version=state.knowledge_snapshot_version,
            )

        return TriggerResult(should_fire=False)

    def _check_diagnosis_gaps(self, state: ClientState) -> TriggerResult:
        """Check for high-severity diagnosis gaps.

        Only fires if diagnosis is fresh relative to the latest knowledge snapshot.
        """
        if not state.diagnosis_snapshot:
            return TriggerResult(should_fire=False)

        # Freshness guard: diagnosis must be computed from the current knowledge version
        if (
            state.knowledge_snapshot_version is not None
            and state.diagnosis_knowledge_snapshot_version is not None
            and state.diagnosis_knowledge_snapshot_version < state.knowledge_snapshot_version
        ):
            logger.info(
                "[proactive_planner] Skipping diagnosis triggers: diagnosis is stale "
                "(diagnosis_ksv=%s, current_ksv=%s)",
                state.diagnosis_knowledge_snapshot_version,
                state.knowledge_snapshot_version,
            )
            return TriggerResult(should_fire=False)

        diagnoses = state.diagnosis_snapshot.get("diagnoses", [])
        for diagnosis in diagnoses:
            severity = str(diagnosis.get("severity", "")).lower()
            if severity not in ("high", "critical"):
                continue

            title = diagnosis.get("title", "unknown gap")
            topic_key = f"diagnosis_gap:{title.lower().replace(' ', '_')}"

            if self._is_topic_backed_off(state, topic_key):
                continue
            if not self._is_topic_cooled_down(state, topic_key, ADVISORY_COOLDOWN_HOURS):
                continue

            escalation_level = self._get_escalation_level(state, topic_key)
            rationale = diagnosis.get("rationale", "")

            return TriggerResult(
                should_fire=True,
                trigger_class="advisory",
                trigger_type="diagnosis_gap",
                trigger_reason=(
                    f"High-severity diagnosis gap: {title}. {rationale[:100]}"
                ),
                guidance_mode=self._select_guidance_mode("advisory", escalation_level),
                objective=f"surface_diagnosis:{title.lower().replace(' ', '_')}",
                grounding_facts=[{
                    "title": title,
                    "severity": severity,
                    "rationale": rationale[:200],
                }],
                escalation_level=escalation_level,
                allowed_cta="open_consultation",
                diagnosis_snapshot_version=state.diagnosis_snapshot_version,
                knowledge_snapshot_version=state.knowledge_snapshot_version,
            )

        return TriggerResult(should_fire=False)

    def _check_reengagement(self, state: ClientState) -> TriggerResult:
        """Check if the client hasn't interacted in REENGAGEMENT_DAYS.

        Relational triggers only fire when no operational or advisory triggers are active.
        Must anchor to a specific fact — never generic "how are you?"
        """
        if not state.last_interaction_at:
            return TriggerResult(should_fire=False)

        days_silent = (state.now - state.last_interaction_at).total_seconds() / 86400
        if days_silent < REENGAGEMENT_DAYS:
            return TriggerResult(should_fire=False)

        topic_key = "reengagement"
        if self._is_topic_backed_off(state, topic_key):
            return TriggerResult(should_fire=False)
        if not self._is_topic_cooled_down(state, topic_key):
            return TriggerResult(should_fire=False)

        # Find a grounding fact to anchor the message
        grounding_fact = self._find_reengagement_anchor(state)
        if not grounding_fact:
            # No specific fact to anchor to — stay silent rather than be generic
            return TriggerResult(should_fire=False)

        escalation_level = self._get_escalation_level(state, topic_key)

        return TriggerResult(
            should_fire=True,
            trigger_class="relational",
            trigger_type="reengagement",
            trigger_reason=(
                f"No interaction in {int(days_silent)} days. "
                f"Anchoring to: {grounding_fact.get('label', 'known fact')}"
            ),
            guidance_mode="open_listening",
            objective="maintain_relationship",
            grounding_facts=[grounding_fact],
            grounding_fact_ids=[grounding_fact["id"]] if grounding_fact.get("id") else [],
            escalation_level=escalation_level,
            allowed_cta="",  # no CTA for relational triggers
            knowledge_snapshot_version=state.knowledge_snapshot_version,
        )

    @staticmethod
    def _find_reengagement_anchor(state: ClientState) -> Optional[Dict[str, Any]]:
        """Find a meaningful fact to anchor a re-engagement message.

        Prefers goals, then accounts, then income. Never returns generic metadata.
        """
        priority_categories = ["goals", "accounts", "income", "dependents"]
        for category in priority_categories:
            for fact in state.knowledge_facts:
                if fact.get("status") == "dismissed":
                    continue
                if fact.get("category") == category:
                    return {
                        "id": fact.get("id"),
                        "domain": fact.get("domain"),
                        "category": fact.get("category"),
                        "label": fact.get("label"),
                        "value": fact.get("value"),
                    }
        # Fallback: any non-dismissed fact
        for fact in state.knowledge_facts:
            if fact.get("status") != "dismissed":
                return {
                    "id": fact.get("id"),
                    "domain": fact.get("domain"),
                    "category": fact.get("category"),
                    "label": fact.get("label"),
                    "value": fact.get("value"),
                }
        return None
