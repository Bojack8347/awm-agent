"""JSON schemas for structured LLM output enforcement.

Each schema maps to the expected output shape of a specific agent or stage.
Passed as `response_schema` on LLMGenerateRequest so that providers can
constrain the model to produce valid JSON matching the schema.

Provider behavior
-----------------
These base schemas are the contract AWM validates responses against — see
`advisor.llm.schema_validation`, which enforces them in-process because the
provider cannot. They include
adding `additionalProperties: false` and normalising nullable types. See
"""

from __future__ import annotations

from typing import Any, Dict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Polymorphic value type — for fields like fact.value that can be string,
# number, boolean, or null. Validated in-process by schema_validation
# transformation.
_POLY_VALUE = {"type": ["string", "number", "boolean", "null"]}
_NULLABLE_STRING = {"type": ["string", "null"]}
_NULLABLE_NUMBER = {"type": ["number", "null"]}


def _nullable(type_spec: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap a type so it also accepts null (for optional fields).

    Uses JSON Schema ``type`` array form, validated in-process after
    adapter transformation.
    """
    existing_type = type_spec.get("type")
    if existing_type:
        if isinstance(existing_type, list):
            if "null" not in existing_type:
                return {**type_spec, "type": [*existing_type, "null"]}
            return type_spec
        return {**type_spec, "type": [existing_type, "null"]}
    # For complex specs (e.g. arrays), use anyOf
    return {"anyOf": [type_spec, {"type": "null"}]}


# ---------------------------------------------------------------------------
# ClientProfileExtractor
# ---------------------------------------------------------------------------

CLIENT_PROFILE_SCHEMA: Dict[str, Any] = {
    "title": "client_profile_extraction",
    "type": "object",
    "properties": {
        "candidate_facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string"},
                    "category": {"type": "string"},
                    "label": {"type": "string"},
                    "value": _POLY_VALUE,
                    "confidence": {"type": "number"},
                    "evidence_text": {"type": "string"},
                },
                "required": ["domain", "category", "label", "value", "confidence", "evidence_text"],
            },
        },
    },
    "required": ["candidate_facts"],
}


# ---------------------------------------------------------------------------
# KnowledgeUpdaterAgent
# ---------------------------------------------------------------------------

KNOWLEDGE_UPDATE_SCHEMA: Dict[str, Any] = {
    "title": "knowledge_update",
    "type": "object",
    "properties": {
        "committed_facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": ["string", "null"]},
                    "domain": {"type": "string"},
                    "category": {"type": "string"},
                    "label": {"type": "string"},
                    "value": _POLY_VALUE,
                    "status": {"type": "string"},
                    "confidence": {"type": "number"},
                    "source_event_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["domain", "category", "label", "value", "status", "confidence", "source_event_ids", "id"],
            },
        },
        "pending_confirmations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact_id": {"type": ["string", "null"]},
                    "domain": {"type": "string"},
                    "category": {"type": "string"},
                    "label": {"type": "string"},
                    "current_value": _POLY_VALUE,
                    "proposed_value": _POLY_VALUE,
                    "change_type": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["fact_id", "domain", "category", "label", "current_value", "proposed_value", "change_type", "reason"],
            },
        },
        "section_summaries": {
            "type": "object",
            "description": "Free-form section summaries keyed by section name.",
        },
    },
    "required": ["committed_facts", "pending_confirmations", "section_summaries"],
}


# ---------------------------------------------------------------------------
# Legacy companion response compatibility
# ---------------------------------------------------------------------------

_PROPOSED_FACT_CHANGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "fact_id": _NULLABLE_STRING,
        "domain": _NULLABLE_STRING,
        "category": _NULLABLE_STRING,
        "label": _NULLABLE_STRING,
        "value": _POLY_VALUE,
        "evidence": _NULLABLE_STRING,
        "confidence": _NULLABLE_NUMBER,
    },
}

_RECOMMENDED_JOURNEY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "journey_type": _NULLABLE_STRING,
        "reason": _NULLABLE_STRING,
    },
}

_JOURNEY_CALL_ARGS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "journey_type": _NULLABLE_STRING,
        "consultation_type": _NULLABLE_STRING,
        "objective_id": _NULLABLE_STRING,
        "session_id": _NULLABLE_STRING,
        "skill": _NULLABLE_STRING,
        "phase": _NULLABLE_STRING,
        "reason": _NULLABLE_STRING,
    },
}

_JOURNEY_CALL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool": {
            "type": "string",
            "enum": [
                "journey.start",
                "journey.resume",
                "journey.advance",
                "journey.run_specialist",
                "journey.conclude",
            ],
        },
        "args": _nullable(_JOURNEY_CALL_ARGS_SCHEMA),
    },
}

COMPANION_RESPONSE_SCHEMA: Dict[str, Any] = {
    "title": "companion_response",
    "type": "object",
    "properties": {
        "assistant_message": {"type": "string"},
        "action_type": {
            "type": "string",
            "enum": [
                "chat",
                "confirm_fact",
                "recommend_journey",
                "open_consultation",
                "handoff_to_journey",
                "analyze_financial_question",
            ],
        },
        "ui_directive": {"type": ["string", "null"]},
        "proposed_fact_changes": _nullable({
            "type": "array",
            "items": _PROPOSED_FACT_CHANGE_SCHEMA,
        }),
        "pending_confirmation_ids": _nullable({"type": "array", "items": {"type": "string"}}),
        "recommended_journey": _nullable(_RECOMMENDED_JOURNEY_SCHEMA),
        "next_session_type": {"type": ["string", "null"]},
        "reasoning_question": {"type": ["string", "null"]},
        # Structured journey-call directive. A compatibility mapper may derive
        # this from action_type for old payloads.
        # See plan §9 Phase 6B.
        "journey_call": _nullable({
            **_JOURNEY_CALL_SCHEMA,
            "description": (
                "Structured directive: { tool: 'journey.start' | 'journey.resume' "
                "| 'journey.advance' | 'journey.run_specialist' | "
                "'journey.conclude', args: object }. Server dispatches this "
                "to JourneyRuntime in Phase 6C."
            ),
        }),
    },
    "required": [
        "assistant_message",
        "action_type",
        "ui_directive",
        "proposed_fact_changes",
        "pending_confirmation_ids",
        "recommended_journey",
        "next_session_type",
        "reasoning_question",
        "journey_call",
    ],
}


# ---------------------------------------------------------------------------
# ActivationFactDeriver
# ---------------------------------------------------------------------------

ACTIVATION_MUTATIONS_SCHEMA: Dict[str, Any] = {
    "title": "activation_mutations",
    "type": "object",
    "properties": {
        "mutations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "update", "delete"]},
                    "fact_id": {"type": ["string", "null"]},
                    "domain": {"type": "string"},
                    "category": {"type": "string"},
                    "label": {"type": "string"},
                    "value": _POLY_VALUE,
                    "evidence": {"type": "string"},
                },
                "required": ["action", "fact_id", "domain", "category", "label", "value", "evidence"],
            },
        },
    },
    "required": ["mutations"],
}


# ---------------------------------------------------------------------------
# KnowledgeUpdaterAgent.map_cashflow_state
# ---------------------------------------------------------------------------

CASHFLOW_STATE_SCHEMA: Dict[str, Any] = {
    "title": "cashflow_state",
    "type": "object",
    "properties": {
        "client_profile": {"type": "object", "description": "Client demographic and profile data."},
        "income": {"type": "object", "description": "Income sources and amounts."},
        "expenses": {"type": "object", "description": "Expense categories and amounts."},
        "accounts": {"type": "object", "description": "Financial accounts and balances."},
        "liabilities": {"type": "object", "description": "Debts and liabilities."},
        "preferences": {"type": "object", "description": "Client preferences and risk tolerance."},
        "goals": {"type": "array", "items": {"type": "object", "description": "A financial goal."}, "description": "Financial goals."},
        "one_off_expenses": {"type": "array", "items": {"type": "object", "description": "A one-off expense."}, "description": "One-time expenses."},
    },
    "required": [
        "client_profile",
        "income",
        "expenses",
        "accounts",
        "liabilities",
        "preferences",
        "goals",
        "one_off_expenses",
    ],
}


# ---------------------------------------------------------------------------
# DiagnosisAgent synthesis
# ---------------------------------------------------------------------------

DIAGNOSIS_SCHEMA: Dict[str, Any] = {
    "title": "diagnosis",
    "type": "object",
    "properties": {
        "diagnoses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "category": {"type": "string"},
                    "title": {"type": "string"},
                    "severity": {"type": "string", "enum": ["critical", "high", "moderate", "low", "info"]},
                    "rationale": {"type": "string"},
                    "linked_fact_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "category", "title", "severity", "rationale", "linked_fact_ids"],
            },
        },
    },
    "required": ["diagnoses"],
}


# ---------------------------------------------------------------------------
# FinancialPlanningAgent — synthesize_investment_profile() output
# ---------------------------------------------------------------------------

INVESTMENT_PROFILE_SCHEMA: Dict[str, Any] = {
    "title": "investment_profile",
    "type": "object",
    "properties": {
        "investment_profile": {
            "type": "object",
            "description": "Investment profile with risk capacity, horizon, and allocation recommendations.",
        },
        "knowledge_candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string"},
                    "category": {"type": "string"},
                    "label": {"type": "string"},
                    "value": _POLY_VALUE,
                    "confidence": {"type": "number"},
                    "evidence_text": {"type": "string"},
                },
                "required": ["domain", "category", "label", "value", "confidence", "evidence_text"],
            },
        },
    },
    "required": ["investment_profile", "knowledge_candidates"],
}


# ---------------------------------------------------------------------------
# InvestmentSolutionAgent synthesis
# ---------------------------------------------------------------------------

INVESTMENT_POLICY_SCHEMA: Dict[str, Any] = {
    "title": "investment_policy",
    "type": "object",
    "properties": {
        "menu": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["title", "summary"],
        },
        "detail": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "sections": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "title": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["id", "title", "content"],
                    },
                },
            },
            "required": ["title", "sections"],
        },
        "execution": {
            "type": "object",
            "properties": {
                "remedy_name": {"type": "string"},
                "funding_source": {"type": "string"},
                "total_transfer": {"type": ["number", "null"]},
            },
            "required": ["remedy_name", "funding_source", "total_transfer"],
        },
    },
    "required": ["menu", "detail", "execution"],
}
