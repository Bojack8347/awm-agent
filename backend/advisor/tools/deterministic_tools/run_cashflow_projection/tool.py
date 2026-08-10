"""Agent tool declaration for run_cashflow_projection."""

from __future__ import annotations

from advisor.tools.deterministic_tools.run_cashflow_projection.scenarios import (
    CASHFLOW_PUBLIC_CHANGE_KINDS,
)

SCENARIO_CHANGE_KINDS = list(CASHFLOW_PUBLIC_CHANGE_KINDS)
DETAIL_REPORT_GROUPS = [
    "income",
    "spending",
    "taxes",
    "withdrawals",
    "account_balances",
    "mortgage",
]
DETAIL_REPORT_GROUP_COLUMNS = {
    "income": [
        "Income",
        "One-time Income",
        "SS Income",
        "Pension Income",
        "Total Cash Inflows",
    ],
    "spending": [
        "Spending",
        "Base Living Spending",
        "One-time Expenses",
        "Housing",
        "Total Cash Outflows",
    ],
    "taxes": [
        "Taxes",
        "Federal Taxes",
        "State Taxes",
        "SS Taxes",
        "Medicare Taxes",
    ],
    "withdrawals": [
        "401k Withdrawals",
        "RMDs",
        "529 Withdrawals",
        "Early Withdrawal Penalties",
    ],
    "account_balances": [
        "Brokerage Balance",
        "Investment Balance",
        "401k Balance",
        "Traditional IRA Balance",
        "Roth IRA Balance",
        "Total Assets",
        "Total Liabilities",
    ],
    "mortgage": [
        "Mortgage Balance",
        "Mortgage Payments",
        "Mortgage Principal Paid",
        "Mortgage Interest Paid",
        "Housing",
    ],
}
DEFAULT_REPORT_COLUMNS = [
    "Net Worth",
    "Cashflow Shortfall Debt",
    "Bank Balance",
]
DETAIL_REPORT_COLUMNS = list(
    dict.fromkeys(
        [
            *DEFAULT_REPORT_COLUMNS,
            *[
                column
                for group in DETAIL_REPORT_GROUPS
                for column in DETAIL_REPORT_GROUP_COLUMNS[group]
            ],
        ]
    )
)


TOOL_SPEC = {
    "name": "run_cashflow_projection",
    "capability": "cashflow_projection",
    "description": (
        "Validate and execute a future-looking cash-flow scenario through "
        "the silent Financial Planning boundary. Use for affordability, stress, "
        "probability, depletion, sustainability, liquidity, investment capacity, "
        "retirement readiness, or policy revalidation. When the client explicitly "
        "asks to use one completed allocation, pass its immutable allocation_analysis_id. "
        "For multiple separately funded account pools, pass allocation_analysis_ids; "
        "the server validates every signed money-pool mapping and account capacity before "
        "LifeModel runs. The default fast path reports Net Worth, Cashflow Shortfall Debt, "
        "and Bank Balance. Request explicit detail_report_groups only when the question "
        "requires annual income, spending, tax, withdrawal, account, or mortgage detail. "
        "When the same request names exact calendar years, pass calendar_years and the "
        "canonical detail_columns so the new run returns those stored annual percentiles "
        "without relying on a second tool call."
    ),
    "writeback_target": "none",
    "read_only": True,
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "minLength": 1, "maxLength": 1000},
        "allocation_analysis_id": {
            "anyOf": [
                {"type": "string", "minLength": 1, "maxLength": 160},
                {"type": "null"},
            ],
            "description": (
                "Immutable analysis_id returned by run_asset_allocation or "
                "get_asset_allocation_analysis when this projection must use that "
                "exact allocation. Pass null for the Client File's current "
                "persisted allocation policies."
            ),
        },
        "allocation_analysis_ids": {
            "anyOf": [
                {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 10,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 160,
                    },
                },
                {"type": "null"},
            ],
            "description": (
                "Immutable analysis ids for distinct confirmed money pools that "
                "must be applied in one projection. Pass null when using zero or "
                "one linked allocation."
            ),
        },
        "monte_carlo_paths": {
            "anyOf": [
                {"type": "integer", "minimum": 10, "maximum": 1000},
                {"type": "null"},
            ],
            "description": (
                "Execution choice: null runs one deterministic path; an integer runs "
                "Monte Carlo with exactly that many paths. Use the client's count when "
                "specified. Path count changes sampling precision, not economic assumptions."
            ),
        },
        "detail_report_groups": {
            "anyOf": [
                {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 6,
                    "items": {
                        "type": "string",
                        "enum": DETAIL_REPORT_GROUPS,
                    },
                },
                {"type": "null"},
            ],
            "description": (
                "Optional LifeModel report groups required for this question. "
                "Null preserves the optimized three-report path."
            ),
        },
        "calendar_years": {
            "anyOf": [
                {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 12,
                    "items": {
                        "type": "integer",
                        "minimum": 1900,
                        "maximum": 2200,
                    },
                },
                {"type": "null"},
            ],
            "description": (
                "Optional exact calendar years to return from the annual series "
                "generated by this run."
            ),
        },
        "detail_columns": {
            "anyOf": [
                {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": {
                        "type": "string",
                        "enum": DETAIL_REPORT_COLUMNS,
                    },
                },
                {"type": "null"},
            ],
            "description": (
                "Canonical LifeModel columns for calendar_years. Every non-default "
                "column must belong to an explicitly requested detail_report_group."
            ),
        },
        "public_fact_refs": {
            "anyOf": [
                {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "variable_key": {
                                "type": "string",
                                "enum": ["social_security_taxable_maximum"],
                            },
                            "session_fact_id": {
                                "type": "string",
                                "pattern": "^session-public-fact:[a-f0-9]{32}$",
                            },
                        },
                        "required": ["variable_key", "session_fact_id"],
                        "additionalProperties": False,
                    },
                },
                {"type": "null"},
            ],
            "description": (
                "Optional opaque reference returned by public-fact research. Selecting it "
                "authorizes immediate use in this session only; the server resolves its "
                "value. Never supply a value here."
            ),
        },
        "scenario": {
            "type": "object",
            "description": (
                "Structured changes to the server-owned Client File baseline, selected "
                "by the Financial Planning Agent from the user's intent. "
                "The server does not classify the request from keywords or parse the "
                "user utterance for financial values."
            ),
            "properties": {
                "scenario_summary": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 160,
                    "description": (
                        "Concise agent-authored name for the baseline or what-if being tested. "
                        "This is a declarative audit record, not hidden chain-of-thought."
                    ),
                },
                "scenario_rationale": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 600,
                    "description": (
                        "Concise agent-authored explanation of how the selected changes match "
                        "the user's intent and what financial question the run tests. Do not "
                        "include private chain-of-thought, hand calculations, invented values, "
                        "or advice."
                    ),
                },
                "scenario_changes": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": SCENARIO_CHANGE_KINDS,
                                "description": "Exact deterministic compiler change kind.",
                            },
                            "amount": {
                                "anyOf": [
                                    {"type": "number", "exclusiveMinimum": 0},
                                    {"type": "null"},
                                ],
                                "description": "Positive USD amount, otherwise null.",
                            },
                            "value": {
                                "anyOf": [
                                    {
                                        "type": "integer",
                                        "minimum": -120,
                                        "maximum": 130,
                                    },
                                    {"type": "null"},
                                ],
                                "description": (
                                    "Whole-year age or age delta for the selected kind, "
                                    "otherwise null."
                                ),
                            },
                            "percentage": {
                                "anyOf": [
                                    {"type": "number", "minimum": 0, "maximum": 1},
                                    {"type": "null"},
                                ],
                                "description": "Decimal percentage from 0 to 1, otherwise null.",
                            },
                            "account_type": {
                                "anyOf": [
                                    {
                                        "type": "string",
                                        "enum": ["bank", "brokerage", "retirement", "education"],
                                    },
                                    {"type": "null"},
                                ],
                                "description": "Account affected by a balance haircut, otherwise null.",
                            },
                            "target_year": {
                                "anyOf": [
                                    {"type": "integer", "minimum": 1900, "maximum": 2200},
                                    {"type": "null"},
                                ],
                                "description": "Calendar year for a one-off event, otherwise null.",
                            },
                            "horizon_years": {
                                "anyOf": [
                                    {"type": "integer", "minimum": 0, "maximum": 100},
                                    {"type": "null"},
                                ],
                                "description": "Years from now for a one-off event, otherwise null.",
                            },
                            "duration_years": {
                                "anyOf": [
                                    {"type": "integer", "minimum": 1, "maximum": 100},
                                    {"type": "null"},
                                ],
                                "description": (
                                    "Whole-year duration for a bounded income, spending-growth, "
                                    "or contribution event, otherwise null."
                                ),
                            },
                            "duration_months": {
                                "anyOf": [
                                    {"type": "integer", "minimum": 1, "maximum": 1200},
                                    {"type": "null"},
                                ],
                                "description": (
                                    "Month duration for a bounded income event beginning "
                                    "at the start of target_year. A partial final year is "
                                    "modeled as a disclosed prorated annual income multiplier."
                                ),
                            },
                            "person": {
                                "anyOf": [
                                    {
                                        "type": "string",
                                        "enum": ["primary", "spouse"],
                                    },
                                    {"type": "null"},
                                ],
                                "description": (
                                    "Household person affected by a person-specific event, "
                                    "otherwise null. Null resolves to primary."
                                ),
                            },
                            "label": {
                                "anyOf": [
                                    {"type": "string", "minLength": 1, "maxLength": 160},
                                    {"type": "null"},
                                ],
                                "description": "Short event label, otherwise null.",
                            },
                            "unit": {
                                "anyOf": [
                                    {"type": "string", "enum": ["USD"]},
                                    {"type": "null"},
                                ],
                                "description": (
                                    "Required as USD for every amount-bearing change; otherwise null."
                                ),
                            },
                        },
                        "required": [
                            "kind",
                            "amount",
                            "value",
                            "percentage",
                            "account_type",
                            "target_year",
                            "horizon_years",
                            "duration_years",
                            "duration_months",
                            "person",
                            "label",
                            "unit",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": [
                "scenario_summary",
                "scenario_rationale",
                "scenario_changes",
            ],
            "additionalProperties": False,
        },
    },
    "required": [
        "question",
        "allocation_analysis_id",
        "allocation_analysis_ids",
        "monte_carlo_paths",
        "detail_report_groups",
        "calendar_years",
        "detail_columns",
        "public_fact_refs",
        "scenario",
    ],
    "additionalProperties": False,
}
