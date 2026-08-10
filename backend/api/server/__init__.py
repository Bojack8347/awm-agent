"""AWM Flask API server composition package."""

from __future__ import annotations

import sys


def _require_supported_python(version_info) -> None:
    major, minor = version_info[:2]
    if (major, minor) < (3, 11):
        raise RuntimeError(
            "AWM backend requires Python 3.11 or newer; "
            f"found {major}.{minor}."
        )


_require_supported_python(sys.version_info)

# Load project .env before persistence and other modules read process env.
from . import bootstrap as _bootstrap  # noqa: F401

from domain.knowledge.truth import commit_truth_update
from advisor.pipelines.activation import ACTIVATION_PIPELINE
from advisor.runtime.task_definition import list_task_profiles

from .auth import (
    expected_companion_session_id as _expected_companion_session_id,
    normalize_email as _normalize_email,
    serialize_auth_payload as _serialize_auth_payload,
    validate_credentials as _validate_credentials,
    validate_login_credentials as _validate_login_credentials,
    validate_registration_invite as _validate_registration_invite,
)
from .deps import (
    _CATEGORY_TO_SECTION,
    _SECTION_TITLES,
    get_activation_mutator,
    get_business_event_worker,
    get_client_profile_extractor,
    get_advisor_runtime,
    get_companion_service,
    get_conversation_compactor,
    get_conversation_memory_service,
    get_diagnosis_service,
    get_knowledge_service,
    get_knowledge_updater,
    get_message_composer,
    get_planning_refresh_coordinator,
    get_proactive_service,
    get_session_runtime,
    schedule_conversation_compaction,
)
from .persistence import *
from .state import (
    _BACKGROUND_EXECUTOR,
    _CONSULTATION_INGESTS,
    _CONSULTATION_TASKS,
    _DIAGNOSIS_REFRESH_LOCK,
    _DIAGNOSIS_REFRESH_RUNNING,
    _INGEST_LOCK,
    _INGEST_STORE_PATH,
    _TASK_LOCK,
)
from .ingest_store import (
    _append_ingest_to_disk,
    _get_ingested_consultation,
    _load_ingests_from_disk,
)
from .middleware import (
    _extract_bearer_token,
    _get_authenticated_account,
    _parse_nonempty_json_body,
    _require_authenticated_account,
    require_api_key,
    require_api_key_auth,
    require_user_auth,
)
from .context import (
    _build_companion_context,
)
from .operations import (
    _apply_same_turn_companion_confirmations,
    _carry_forward_section_summaries,
    _commit_confirmed_pending,
    _get_diagnosis_refresh_payload,
    _ground_companion_result_to_current_turn,
    _is_diagnosis_material,
    _queue_confirmation_diagnosis_refresh,
    _queue_consultation_diagnosis_refresh,
    _queue_diagnosis_refresh_if_material,
    _rebuild_snapshot_targeted,
    _refresh_diagnosis_if_material,
    _sanitize_companion_result,
)
from .journey_runtime import (
    _journey_runtime_v2_enabled,
    _v2_disabled_response,
)
from .background import (
    _app_deps,
    _pipeline_checkpoint,
)
from .timing import collect_server_timing, timed
from .factory import app, create_app, logger
from .startup import _eager_init_agents, _reconcile_orphaned_journeys, run_startup_hooks

run_startup_hooks()
