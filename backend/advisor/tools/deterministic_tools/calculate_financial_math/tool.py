"""Agent declaration for explicit-input financial arithmetic."""

TOOL_SPEC = {
    "name": "calculate_financial_math",
    "capability": "calculation_toolkit",
    "description": (
        "Evaluate a bounded awm.financial_math.v2 Decimal calculation plan over authenticated "
        "literal, Client File, financial-position, version-matched projection, immutable "
        "cash-flow-analysis, or session-authorized public-fact sources. For cashflow_claim "
        "and cashflow_series_value, submit only the server-issued identifier and declared "
        "selector fields. A session_public_fact source has exactly id, kind, and "
        "session_fact_id; never add selector, unit, or value. The server resolves values and units. "
        "Literal sources require value and unit and are only for numbers typed by the user in the "
        "authenticated current message. Never transcribe a value from Client File context, prior "
        "conversation context, or tool output into a literal; use the corresponding authenticated "
        "source kind and selector, such as client_fact selector annual_income. A formula_constant "
        "may optionally include unit "
        "as an assertion; the server verifies it against the constant's canonical unit and still "
        "owns the resolved value and unit. Omit unit from every other server-resolved source. "
        "Named metric templates own governed formulas; references may use only declared sources "
        "or earlier steps. For household net worth use one financial_position source with selector "
        "net_worth; for employer-equity value use financial_position selector employer_stock_value. "
        "client_fact selectors accept canonical top-level fact names such as annual_income, never "
        "nested account paths. A client_fact source has exactly id, kind, and selector: never add "
        "unit or value. decimal_places is valid only on a separate round step, never directly on "
        "percentage_change or another operation. Agent calls must use this plan contract; legacy one-operation "
        "payloads remain available only to non-agent compatibility callers."
    ),
    "writeback_target": "none",
    "read_only": True,
}


_NULLABLE_NUMBER = {
    "anyOf": [
        {"type": "number"},
        {"type": "null"},
    ]
}


V1_PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": [
                "difference",
                "aggregation",
                "percentage_change",
                "percentage_of_base",
                "annual_to_monthly",
                "monthly_to_annual",
                "future_value_lump_sum",
                "present_value_lump_sum",
                "future_value_recurring_contribution",
                "loan_payment",
                "compound_annual_growth_rate",
            ],
            "description": (
                "Use percentage_change for 'percent higher/lower', percentage_of_base for "
                "either a stated decimal share of a known amount or an amount divided "
                "by its known base (primary_value=amount, secondary_value=base), and difference for "
                "'A minus B'. Use future_value_lump_sum for one starting balance, "
                "future_value_recurring_contribution for repeated deposits, loan_payment "
                "for a level principal-and-interest payment, and "
                "compound_annual_growth_rate for the annual rate connecting two values."
            ),
        },
        "primary_value": {"type": "number"},
        "operands": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": "number"},
                    "direction": {"type": "string", "enum": ["add", "subtract"]},
                    "currency": {"type": "string"},
                    "unit": {"type": "string", "enum": ["money", "percentage", "years", "count"]},
                },
                "required": ["name", "value", "direction", "currency", "unit"],
                "additionalProperties": False,
            },
            "description": "Named, signed inputs for aggregation. All must be money in one currency.",
        },
        "secondary_value": _NULLABLE_NUMBER,
        "annual_rate_decimal": {
            **_NULLABLE_NUMBER,
            "description": (
                "Client-supplied annual rate expressed as a decimal. For a standard "
                "monthly loan-payment question, this is the nominal annual rate divided "
                "by payments_per_year inside the formula."
            ),
        },
        "periods": {
            **_NULLABLE_NUMBER,
            "description": "Number of years for value-growth and loan operations.",
        },
        "payments_per_year": {
            "anyOf": [
                {"type": "integer", "minimum": 1, "maximum": 365},
                {"type": "null"},
            ],
            "description": (
                "Compounding, deposit, or payment frequency. Use 12 when the client "
                "explicitly says monthly."
            ),
        },
        "payment_timing": {
            "anyOf": [
                {"type": "string", "enum": ["end", "begin"]},
                {"type": "null"},
            ]
        },
        "input_unit": {
            "type": "string",
            "enum": [
                "USD",
                "USD_per_year",
                "USD_per_month",
                "decimal",
                "probability_0_to_1",
                "years",
            ],
        },
    },
    "required": [
        "operation",
        "primary_value",
        "secondary_value",
        "annual_rate_decimal",
        "periods",
        "payments_per_year",
        "payment_timing",
        "input_unit",
    ],
    "additionalProperties": False,
}


SOURCE_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "pattern": "^[A-Za-z0-9_-]+$",
        },
        "kind": {
            "type": "string",
            "enum": [
                "literal",
                "client_fact",
                "financial_position",
                "projection_metric",
                "formula_constant",
                "cashflow_claim",
                "cashflow_series_value",
                "session_public_fact",
            ],
            "description": (
                "Use financial_position for resolved balance-sheet totals such as net worth or "
                "employer equity; use client_fact only for canonical top-level fact fields. "
                "Use literal only for a number typed by the user in the authenticated current "
                "message, never for a value shown in Client File or conversation context. "
                "Cash-flow and session public-fact values and units are always resolved "
                "server-side."
            ),
        },
        "value": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": (
                "Decimal string; permitted only for a literal explicitly typed by the user in "
                "the authenticated current message, never for a copied Client File value."
            ),
        },
        "unit": {
            "type": "string",
            "maxLength": 64,
            "pattern": (
                "^(?:decimal|percentage|probability_0_to_1|years|months|count|unitless|"
                "money(?:_per_year|_per_month)?:[A-Z]{3})$"
            ),
            "description": (
                "Required for literal sources. Optional for formula_constant only as an "
                "assertion that must match the server-owned canonical unit; omit otherwise."
            ),
        },
        "source_message_id": {
            "type": "string",
            "maxLength": 160,
            "description": (
                "For literal sources this is server-owned and replaced with the "
                "authenticated current turn id. Omit it from an agent-authored plan."
            ),
        },
        "selector": {
            "type": "string",
            "maxLength": 160,
            "description": (
                "For financial_position use net_worth or employer_stock_value. For client_fact use "
                "a canonical top-level field such as annual_income; nested paths are invalid. "
                "For formula_constant use zero, one, twelve, "
                "annual_frequency, monthly_frequency, payment_timing_end, or "
                "payment_timing_begin. Frequency and timing constants are server-typed counts. "
                "Never use selector for session_public_fact."
            ),
        },
        "metric": {"type": "string", "maxLength": 160},
        "analysis_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160,
            "description": "Exact immutable cash-flow analysis identifier.",
        },
        "session_fact_id": {
            "type": "string",
            "pattern": "^session-public-fact:[a-f0-9]{32}$",
            "description": (
                "Server-issued identifier from research_public_financial_fact. Never copy "
                "the researched value into a literal source."
            ),
        },
        "metric_key": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160,
            "description": "Typed cash-flow evidence metric key for cashflow_claim.",
        },
        "value_path": {
            "anyOf": [
                {"type": "string", "minLength": 1, "maxLength": 240},
                {"type": "null"},
            ],
            "description": "Optional nested scalar path inside a typed cash-flow claim.",
        },
        "column": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160,
            "description": "Stored LifeModel detail-series column for cashflow_series_value.",
        },
        "calendar_year": {
            "type": "integer",
            "minimum": 1900,
            "maximum": 2200,
        },
        "percentile": {
            "type": "string",
            "enum": ["p10", "p50", "p90"],
        },
    },
    "required": ["id", "kind"],
    "additionalProperties": False,
}

STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "pattern": "^[A-Za-z0-9_-]+$",
        },
        "operation": {
            "type": "string",
            "enum": ["metric", "add", "subtract", "multiply", "divide", "sum", "average", "aggregation", "power", "root", "absolute", "minimum", "maximum", "ratio", "apply_rate", "percentage_change", "as_percentage", "probability_complement", "round", "annual_to_monthly", "monthly_to_annual", "future_value_lump_sum", "present_value_lump_sum", "future_value_recurring_contribution", "loan_payment", "compound_annual_growth_rate"],
            "description": (
                "Argument order is significant: subtract [a,b] computes a-b; divide or "
                "ratio [a,b] computes a/b; percentage_change [base,comparison] computes "
                "(comparison-base)/abs(base). Use percentage_change directly for percent "
                "higher/lower; its typed decimal-change result is rendered as a percentage, "
                "so do not reproduce it as divide followed by multiply-by-100. When a "
                "separately computed decimal ratio must be displayed as a percentage, use "
                "as_percentage [ratio]; never multiply by one_hundred for unit conversion."
            ),
        },
        "template": {"type": "string", "enum": ["net_worth", "annual_surplus", "monthly_surplus", "holding_concentration", "loan_payment"]},
        "arguments": {
            "type": "array",
            "minItems": 1,
            "maxItems": 64,
            "items": {"type": "string", "pattern": "^\\$[A-Za-z0-9_-]+$"},
            "description": (
                "References to declared sources or earlier steps only. Steps execute in "
                "array order; never reference a later step."
            ),
        },
        "directions": {"type": "array", "maxItems": 64, "items": {"type": "string", "enum": ["add", "subtract"]}},
        "decimal_places": {
            "type": "integer",
            "minimum": 0,
            "maximum": 12,
            "description": (
                "Valid only when operation is round. For percentage_change or every other "
                "operation, omit this field; add a later round step if explicit rounding is needed."
            ),
        },
    },
    "required": ["id", "operation", "arguments"],
    "additionalProperties": False,
}

V2_PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "string", "const": "awm.financial_math.v2"},
        "client_file_version": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Server-owned current Client File version. The execution boundary "
                "replaces any agent-supplied value before evaluation."
            ),
        },
        "sources": {"type": "array", "maxItems": 32, "items": SOURCE_SCHEMA},
        "steps": {"type": "array", "minItems": 1, "maxItems": 24, "items": STEP_SCHEMA},
        "outputs": {"type": "array", "minItems": 1, "maxItems": 24, "items": {"type": "string", "pattern": "^\\$[A-Za-z0-9_-]+$"}},
    },
    "required": ["schema_version", "client_file_version", "sources", "steps", "outputs"],
    "additionalProperties": False,
}

# OpenAI function tools require a plain object at the schema root and reject a
# root-level union. Keep the transitional V1/V2 fields in one envelope; the
# deterministic execution boundary validates the selected contract.
PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        **V2_PARAMS_SCHEMA["properties"],
        **V1_PARAMS_SCHEMA["properties"],
    },
    "additionalProperties": False,
}
