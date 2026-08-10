"""Canonical Client File fact vocabulary and shape validation.

This module is the contract between conversational fact capture, Client File
storage, and deterministic planning readers.  It deliberately does not convert
or judge numeric values: the advisor supplies values in the canonical field's
declared period and this layer validates only names and shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Tuple


FACT_TYPES: Tuple[str, ...] = (
    "captured_fact",
    "onboarding_context",
    "demographics",
    "household_context",
    "income_context",
    "spending_context",
    "assets_context",
    "retirement_context",
    "housing_context",
    "education_context",
    "employment_context",
    "health_context",
    "insurance_context",
    "liability_context",
    "future_goals",
    "goal_context",
    "preference_context",
    "investment_context",
    "investment_consultation_assessment",
)

CONFIDENCE_LEVELS: Tuple[str, ...] = (
    "explicit_user_statement",
    "approximate",
    "inferred",
    "high",
    "medium",
    "low",
)

MORTGAGE_TYPE_VALUES: Tuple[str, ...] = (
    "fixed_rate",
    "adjustable_rate",
    "interest_only",
    "balloon",
)

MARITAL_STATUS_VALUES: Tuple[str, ...] = (
    "single",
    "married",
    "partnered",
)

CASHFLOW_ASSET_CLASSES: Tuple[str, ...] = (
    "Cash",
    "US Treasury",
    "Global Investment Grade Corporate Bond",
    "Global High Yield Bond BB-B",
    "Emerging Market Local Currency Government Bonds",
    "Emerging Market Hard Currency Debt",
    "US Equity",
    "Dev. Europe ex UK Equity",
    "Japan Equity",
    "China Equity",
    "India Equity",
    "Commodities",
    "Gold",
    "Hedge Funds",
    "Bitcoin",
)

BASIS_FACTORS: Mapping[str, int] = {
    "annual": 1,
    "monthly": 12,
    "weekly": 52,
    "quarterly": 4,
}

PROVENANCE_KEYS = frozenset(
    {
        "basis",
        "as_stated",
        "scope",
        "includes_mortgage",
    }
)


@dataclass(frozen=True)
class FactField:
    kind: str
    period: str | None = None
    accepted_basis: Tuple[str, ...] = ()
    impact: str = "medium"
    planning_input: bool = False
    dependent_records: Tuple[str, ...] = ()
    engine_field: str | None = None
    legacy_aliases: Tuple[str, ...] = ()
    qualifiers: Tuple[str, ...] = ()
    unit: str | None = None
    minimum: int | float | None = None
    maximum: int | float | None = None
    exclusive_minimum: bool = False
    allowed_values: Tuple[str, ...] = ()


@dataclass(frozen=True)
class DerivedField:
    derived_from: Tuple[str, ...]
    engine_field: str
    planning_input: bool = False
    legacy_aliases: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidatedFacts:
    canonical: Dict[str, Any] = field(default_factory=dict)
    provenance: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    aliases_applied: Tuple[Tuple[str, str], ...] = ()
    unrecognized: Dict[str, Any] = field(default_factory=dict)
    rejected: Tuple[Dict[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.rejected


ACCOUNT_TYPES = (
    "taxable_brokerage", "retirement", "cash", "education", "real_estate", "other",
)
HOLDING_OWNERSHIP_STATUSES = (
    "beneficially_owned", "settled", "unvested", "unsettled", "unexercised",
)


def entity_collection_schema() -> Dict[str, Any]:
    """Strict shared tool schema for account/holding entity envelopes."""

    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "entity_type": {"type": "string", "enum": ["account", "holding"]},
                "account_id": {"type": ["string", "null"]},
                "account_type": {"type": "string", "enum": list(ACCOUNT_TYPES)},
                "account_subtype": {"type": ["string", "null"]},
                "total_balance": {"type": ["number", "null"]},
                "holdings_coverage": {"type": "string", "enum": ["complete", "partial", "unknown"]},
                "label": {"type": ["string", "null"]},
                "symbol": {"type": ["string", "null"]},
                "issuer": {"type": ["string", "null"]},
                "market_value": {"type": ["number", "null"]},
                "currency": {"type": "string"},
                "security_type": {"type": ["string", "null"]},
                "asset_class": {"type": ["string", "null"]},
                "concentration_class": {"type": ["string", "null"]},
                "ownership_status": {"type": "string", "enum": list(HOLDING_OWNERSHIP_STATUSES)},
                "account_relationship_status": {"type": ["string", "null"], "enum": ["resolved", "unresolved", None]},
                "reported_at": {"type": ["string", "null"]},
                "valuation_as_of": {"type": ["string", "null"]},
                "valuation_basis": {"type": ["string", "null"]},
                "valuation_freshness": {"type": ["string", "null"]},
                "lifecycle_status": {"type": "string", "enum": ["active", "inactive", "superseded"]},
                "legacy_scalar_ref": {"type": ["string", "null"]},
                "provider_identity": {"type": ["string", "null"]},
            },
            "required": ["entity_type", "currency", "lifecycle_status"],
            "additionalProperties": False,
        },
    }


def validate_entities(entities: Any, *, committed: bool) -> List[Dict[str, Any]]:
    """Validate entity semantics; JSON schema alone cannot check references."""

    if entities in (None, []):
        return []
    if not isinstance(entities, list):
        raise ValueError("entities_must_be_an_array")
    normalized: List[Dict[str, Any]] = []
    seen = set()
    for raw in entities:
        if not isinstance(raw, dict):
            raise ValueError("entity_must_be_an_object")
        entity = dict(raw)
        entity_type = str(entity.get("entity_type") or "")
        entity_id = str(entity.get("entity_id") or "")
        if entity_type not in {"account", "holding"}:
            raise ValueError("entity_type_invalid")
        if not entity_id or entity_id in seen:
            raise ValueError("entity_id_invalid")
        seen.add(entity_id)
        currency = str(entity.get("currency") or "").upper()
        if len(currency) != 3:
            raise ValueError("entity_currency_invalid")
        entity["currency"] = currency
        if entity.get("lifecycle_status") not in {"active", "inactive", "superseded"}:
            raise ValueError("entity_lifecycle_status_invalid")
        if entity_type == "account":
            if entity.get("account_type") not in ACCOUNT_TYPES:
                raise ValueError("account_type_invalid")
            if entity.get("holdings_coverage") not in {"complete", "partial", "unknown"}:
                raise ValueError("account_holdings_coverage_invalid")
            if entity.get("total_balance") is not None and (isinstance(entity["total_balance"], bool) or not isinstance(entity["total_balance"], (int, float)) or entity["total_balance"] < 0):
                raise ValueError("account_total_balance_invalid")
        else:
            value = entity.get("market_value")
            if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError("holding_market_value_invalid")
            if entity.get("ownership_status") not in HOLDING_OWNERSHIP_STATUSES:
                raise ValueError("holding_ownership_status_invalid")
            if entity.get("lifecycle_status") == "active" and committed:
                if not entity.get("account_id") or entity.get("account_relationship_status") == "unresolved":
                    raise ValueError("active_holding_account_required")
                if entity.get("ownership_status") not in {"beneficially_owned", "settled"}:
                    raise ValueError("contingent_holding_not_calculation_eligible")
            if not entity.get("reported_at"):
                raise ValueError("holding_reported_at_required")
        normalized.append(entity)
    return normalized


_PLAN_DEPENDENCIES = ("financial_plan", "investment_assessment")

CANONICAL_FACT_FIELDS: Mapping[str, FactField | DerivedField] = {
    "age": FactField(
        kind="integer",
        planning_input=True,
        engine_field="current_age",
        unit="years",
        minimum=0,
        maximum=120,
        legacy_aliases=(
            "client_age",
            "current_age",
            "person_1_age",
            "primary_age",
            "primary_client_age",
            "spouse_1_age",
        ),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "spouse_age": FactField(
        kind="integer",
        planning_input=True,
        engine_field="spouse_age",
        unit="years",
        minimum=0,
        maximum=120,
        legacy_aliases=("partner_age", "spouse_2_age", "spouse_partner_age"),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "marital_status": FactField(
        kind="text",
        planning_input=True,
        engine_field="marital_status",
        unit="household_status",
        allowed_values=MARITAL_STATUS_VALUES,
        legacy_aliases=("marriage_status", "filing_status"),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "retirement_age": FactField(
        kind="integer",
        planning_input=True,
        engine_field="retirement_age",
        unit="years",
        minimum=0,
        maximum=120,
        legacy_aliases=(
            "client_retirement_age",
            "target_retirement_age",
            "retirement_age_goal",
            "planned_retirement_age",
        ),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "annual_income": FactField(
        kind="money",
        period="annual",
        accepted_basis=("annual", "monthly", "weekly", "quarterly"),
        impact="high",
        planning_input=True,
        engine_field="annual_income",
        unit="USD_per_year",
        minimum=0,
        legacy_aliases=(
            "household_income",
            "household_income_annual",
            "annual_household_income",
            "annual_household_income_usd",
            "income",
            "salary",
        ),
        qualifiers=("scope",),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "annual_spending": FactField(
        kind="money",
        period="annual",
        accepted_basis=("annual", "monthly", "weekly", "quarterly"),
        impact="high",
        planning_input=True,
        engine_field="annual_spending",
        unit="USD_per_year",
        minimum=0,
        legacy_aliases=("annual_household_spending_usd", "spending", "base_spending"),
        qualifiers=("scope", "includes_mortgage"),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "retirement_contribution_pct": FactField(
        kind="number",
        planning_input=True,
        engine_field="retirement_contribution_pct",
        unit="annual_decimal_rate",
        minimum=0,
        maximum=1,
        legacy_aliases=("retirement_contribution_percent",),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "monthly_retirement_contribution": FactField(
        kind="money",
        period="monthly",
        accepted_basis=("monthly", "annual"),
        impact="high",
        planning_input=True,
        engine_field="monthly_retirement_contribution",
        unit="USD_per_month",
        minimum=0,
        legacy_aliases=(
            "monthly_savings_contribution_usd",
            "monthly_retirement_savings",
        ),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "life_expectancy": FactField(
        kind="integer",
        planning_input=True,
        engine_field="life_expectancy",
        unit="years",
        minimum=0,
        maximum=130,
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "cash": FactField(
        kind="money",
        impact="high",
        planning_input=True,
        engine_field="cash_balance",
        unit="USD",
        minimum=0,
        legacy_aliases=("cash_balance", "bank_balance"),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "taxable_brokerage": FactField(
        kind="money",
        impact="high",
        planning_input=True,
        engine_field="brokerage_balance",
        unit="USD",
        minimum=0,
        legacy_aliases=(
            "brokerage",
            "brokerage_accounts",
            "taxable_brokerage_accounts",
            "brokerage_balance",
            "taxable_brokerage_balance",
        ),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "retirement_accounts": FactField(
        kind="money",
        impact="high",
        planning_input=True,
        engine_field="retirement_balance",
        unit="USD",
        minimum=0,
        legacy_aliases=(
            "retirement",
            "retirement_balance",
            "retirement_account_balance",
            "retirement_savings_total_usd",
        ),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "brokerage_asset_allocation": FactField(
        kind="asset_allocation",
        planning_input=True,
        engine_field="brokerage_asset_allocation",
        unit="decimal_weights_sum_to_1",
        legacy_aliases=(
            "brokerage_allocation",
            "taxable_brokerage_asset_allocation",
            "taxable_brokerage_allocation",
        ),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "retirement_asset_allocation": FactField(
        kind="asset_allocation",
        planning_input=True,
        engine_field="retirement_asset_allocation",
        unit="decimal_weights_sum_to_1",
        legacy_aliases=(
            "retirement_allocation",
            "retirement_accounts_asset_allocation",
        ),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "college_529": FactField(
        kind="money",
        planning_input=True,
        engine_field="education_balance",
        unit="USD",
        minimum=0,
        legacy_aliases=(
            "education_accounts",
            "education_balance",
            "education_account_balance",
        ),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "mortgage_balance": FactField(
        kind="money",
        planning_input=True,
        engine_field="mortgage_balance",
        unit="USD",
        minimum=0,
        legacy_aliases=("mortgage",),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "home_value": FactField(
        kind="money",
        planning_input=True,
        engine_field="home_value",
        unit="USD",
        minimum=0,
        legacy_aliases=("current_home_value", "home_worth", "primary_residence_value"),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "home_tax_basis": FactField(
        kind="money",
        planning_input=True,
        engine_field="home_tax_basis",
        unit="USD",
        minimum=0,
        legacy_aliases=("home_cost_basis", "property_tax_basis"),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "home_appreciation_rate": FactField(
        kind="number",
        planning_input=True,
        engine_field="home_appreciation_rate",
        unit="annual_decimal_rate",
        minimum=-1,
        maximum=1,
        exclusive_minimum=True,
        legacy_aliases=("home_value_growth_rate", "home_appreciation_rate_percent"),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "mortgage_interest_rate": FactField(
        kind="number",
        planning_input=True,
        engine_field="mortgage_interest_rate",
        unit="annual_decimal_rate",
        minimum=0,
        maximum=1,
        legacy_aliases=("mortgage_rate", "mortgage_interest_rate_percent"),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "mortgage_remaining_term_years": FactField(
        kind="integer",
        planning_input=True,
        engine_field="mortgage_remaining_term_years",
        unit="years",
        minimum=0,
        maximum=100,
        legacy_aliases=("remaining_mortgage_term_years", "remaining_term_years"),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "mortgage_monthly_payment": FactField(
        kind="money",
        period="monthly",
        accepted_basis=("monthly", "annual"),
        impact="high",
        planning_input=True,
        engine_field="mortgage_monthly_payment",
        unit="USD_per_month",
        minimum=0,
        legacy_aliases=("monthly_mortgage_payment", "monthly_principal_interest"),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "mortgage_type": FactField(
        kind="text",
        planning_input=True,
        engine_field="mortgage_type",
        unit="mortgage_category",
        allowed_values=MORTGAGE_TYPE_VALUES,
        legacy_aliases=("loan_type",),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "annual_spending_includes_mortgage": FactField(
        kind="boolean",
        planning_input=True,
        engine_field="annual_spending_includes_mortgage",
        legacy_aliases=(
            "spending_includes_mortgage",
            "annual_spending_includes_housing",
        ),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "education_goal_amount": FactField(
        kind="money",
        planning_input=True,
        engine_field="education_goal_amount",
        unit="USD",
        minimum=0,
        legacy_aliases=("college_projected_cost",),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "education_horizon_years": FactField(
        kind="integer",
        planning_input=True,
        engine_field="education_horizon_years",
        unit="years_from_now",
        minimum=0,
        maximum=100,
        legacy_aliases=("college_years_until",),
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "emergency_reserve_months": FactField(
        kind="number",
        planning_input=True,
        engine_field="emergency_reserve_months",
        unit="months",
        minimum=0,
        dependent_records=_PLAN_DEPENDENCIES,
    ),
    "starting_assets": DerivedField(
        derived_from=("cash", "taxable_brokerage", "retirement_accounts"),
        engine_field="starting_assets",
    ),
    # Non-engine Client File facts used by consultation workflows.
    "name": FactField(kind="text", impact="low"),
    "confirmed_linked_balances": FactField(kind="any", impact="high"),
    "household_context": FactField(kind="any", impact="high"),
    "income_context": FactField(kind="any", impact="high"),
    "health_context": FactField(kind="any", impact="high"),
    "future_goals": FactField(kind="any", impact="high"),
    "employment_status": FactField(kind="text", impact="high"),
    "dependents": FactField(kind="any", impact="high"),
    "insurance_context": FactField(kind="any", impact="high"),
    "liabilities": FactField(kind="any", impact="high"),
    "purpose": FactField(kind="any", impact="high"),
    "amount": FactField(kind="money", impact="high"),
    "horizon": FactField(kind="any", impact="high"),
    "horizon_years": FactField(kind="number", impact="high"),
    "risk_tolerance": FactField(kind="any", impact="high"),
    "source_of_funds": FactField(kind="any", impact="high"),
    "liquidity_needs": FactField(kind="any", impact="high"),
    "investment_preferences": FactField(kind="any", impact="medium"),
    "constraints": FactField(kind="any", impact="high"),
    "income_change_material": FactField(kind="boolean", impact="high"),
}


def _alias_map() -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for canonical, definition in CANONICAL_FACT_FIELDS.items():
        for alias in definition.legacy_aliases:
            previous = aliases.get(alias)
            if previous is not None and previous != canonical:
                raise ValueError(
                    f"duplicate fact alias {alias!r}: {previous!r} and {canonical!r}"
                )
            aliases[alias] = canonical
    return aliases


FACT_KEY_ALIASES: Mapping[str, str] = _alias_map()


def canonical_fact_name(name: str) -> str | None:
    candidate = str(name)
    if candidate in CANONICAL_FACT_FIELDS:
        return candidate
    return FACT_KEY_ALIASES.get(candidate)


def fact_aliases_for_engine_field(engine_field: str) -> Tuple[str, ...]:
    """Return canonical-first Client File names for one engine field."""

    matches = [
        (canonical, definition)
        for canonical, definition in CANONICAL_FACT_FIELDS.items()
        if definition.engine_field == engine_field
    ]
    if len(matches) != 1:
        raise KeyError(
            f"engine field {engine_field!r} must map from exactly one Client File fact"
        )
    canonical, definition = matches[0]
    return tuple(dict.fromkeys((canonical, engine_field, *definition.legacy_aliases)))


def fact_value_for_engine_field(facts: Mapping[str, Any], engine_field: str) -> Any:
    """Return the first present value for an engine field's declared aliases."""

    if not isinstance(facts, Mapping):
        return None
    for alias in fact_aliases_for_engine_field(engine_field):
        if alias in facts and facts[alias] is not None:
            return facts[alias]
    return None


def normalize_fact_keys(facts: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize known aliases while retaining unknown keys for legacy readers."""

    if not isinstance(facts, dict):
        return {}
    normalized: Dict[str, Any] = {}
    # Canonical keys win even when an alias appeared earlier in the payload.
    for key, value in facts.items():
        string_key = str(key)
        canonical = canonical_fact_name(string_key) or string_key
        if canonical in normalized and string_key != canonical:
            continue
        normalized[canonical] = value
    for key, value in facts.items():
        string_key = str(key)
        if string_key in CANONICAL_FACT_FIELDS:
            normalized[string_key] = value
    return normalized


def normalize_asset_allocation(value: Any) -> Dict[str, float]:
    """Validate canonical decimal asset weights without interpreting labels."""

    if not isinstance(value, dict) or not value:
        raise ValueError("asset_allocation_must_be_a_nonempty_object")
    unknown = sorted(str(name) for name in value if name not in CASHFLOW_ASSET_CLASSES)
    if unknown:
        raise ValueError(
            "unsupported_asset_allocation_classes:" + ",".join(unknown)
        )
    normalized: Dict[str, float] = {}
    for name, raw_weight in value.items():
        if (
            isinstance(raw_weight, bool)
            or not isinstance(raw_weight, (int, float))
            or raw_weight < 0
            or raw_weight > 1
        ):
            raise ValueError(f"invalid_decimal_asset_weight:{name}")
        normalized[str(name)] = float(raw_weight)
    if abs(sum(normalized.values()) - 1.0) > 0.001:
        raise ValueError("asset_allocation_weights_must_sum_to_1")
    return normalized


def validate_facts(facts: Any) -> ValidatedFacts:
    """Validate canonical fact names and envelopes without converting values."""

    if not isinstance(facts, dict):
        return ValidatedFacts(
            rejected=({"field": "facts", "reason": "facts_must_be_an_object"},)
        )

    canonical: Dict[str, Any] = {}
    provenance: Dict[str, Dict[str, Any]] = {}
    aliases_applied: list[tuple[str, str]] = []
    unrecognized: Dict[str, Any] = {}
    rejected: list[Dict[str, Any]] = []

    # Canonical values take precedence when both forms are present.
    ordered = sorted(
        facts.items(),
        key=lambda item: 0 if str(item[0]) not in CANONICAL_FACT_FIELDS else 1,
    )
    for raw_key, raw_value in ordered:
        given_key = str(raw_key)
        canonical_key = canonical_fact_name(given_key)
        if canonical_key is None:
            unrecognized[given_key] = raw_value
            continue
        definition = CANONICAL_FACT_FIELDS[canonical_key]
        if given_key != canonical_key:
            aliases_applied.append((given_key, canonical_key))
        if isinstance(definition, DerivedField):
            rejected.append(
                {
                    "field": canonical_key,
                    "reason": "derived_field_not_accepted",
                    "derived_from": list(definition.derived_from),
                }
            )
            continue
        if definition.period:
            if not isinstance(raw_value, dict) or "value" not in raw_value:
                rejected.append(
                    {
                        "field": canonical_key,
                        "reason": "basis_required",
                        "accepted_basis": list(definition.accepted_basis),
                    }
                )
                continue
            basis = raw_value.get("basis")
            if basis not in definition.accepted_basis:
                rejected.append(
                    {
                        "field": canonical_key,
                        "reason": "basis_required",
                        "accepted_basis": list(definition.accepted_basis),
                    }
                )
                continue
            value = raw_value.get("value")
            value_rejection = _fact_value_rejection(
                canonical_key,
                value,
                definition,
            )
            if value_rejection:
                rejected.append(value_rejection)
                continue
            allowed_provenance = {"basis", "as_stated", *definition.qualifiers}
            unexpected = sorted(set(raw_value) - {"value"} - allowed_provenance)
            if unexpected:
                rejected.append(
                    {
                        "field": canonical_key,
                        "reason": "unrecognized_envelope_fields",
                        "fields": unexpected,
                    }
                )
                continue
            invalid_provenance = []
            if "as_stated" in raw_value and not isinstance(
                raw_value.get("as_stated"), str
            ):
                invalid_provenance.append("as_stated")
            if "scope" in raw_value and raw_value.get("scope") not in {
                "household",
                "individual",
            }:
                invalid_provenance.append("scope")
            if "includes_mortgage" in raw_value and not isinstance(
                raw_value.get("includes_mortgage"), bool
            ):
                invalid_provenance.append("includes_mortgage")
            if invalid_provenance:
                rejected.append(
                    {
                        "field": canonical_key,
                        "reason": "invalid_provenance_shape",
                        "fields": invalid_provenance,
                    }
                )
                continue
            canonical[canonical_key] = value
            provenance[canonical_key] = {
                key: raw_value[key]
                for key in allowed_provenance
                if key in raw_value
            }
            continue
        if definition.kind == "asset_allocation":
            try:
                canonical[canonical_key] = normalize_asset_allocation(raw_value)
            except ValueError as exc:
                rejected.append(
                    {
                        "field": canonical_key,
                        "reason": str(exc),
                        "expected_unit": definition.unit,
                        "allowed_values": list(CASHFLOW_ASSET_CLASSES),
                    }
                )
            continue
        value_rejection = _fact_value_rejection(
            canonical_key,
            raw_value,
            definition,
        )
        if value_rejection:
            rejected.append(value_rejection)
            continue
        canonical[canonical_key] = raw_value

    return ValidatedFacts(
        canonical=canonical,
        provenance=provenance,
        aliases_applied=tuple(aliases_applied),
        unrecognized=unrecognized,
        rejected=tuple(rejected),
    )


def fact_properties_schema() -> Dict[str, Any]:
    """Build the non-strict JSON-schema guidance for a ``facts`` object."""

    properties: Dict[str, Any] = {}
    for name, definition in CANONICAL_FACT_FIELDS.items():
        if isinstance(definition, DerivedField):
            continue
        value_schema = _fact_value_schema(definition)
        if definition.period:
            envelope_properties: Dict[str, Any] = {
                "value": value_schema,
                "basis": {
                    "type": "string",
                    "enum": list(definition.accepted_basis),
                },
                "as_stated": {"type": "string"},
            }
            if "scope" in definition.qualifiers:
                envelope_properties["scope"] = {
                    "type": "string",
                    "enum": ["household", "individual"],
                }
            if "includes_mortgage" in definition.qualifiers:
                envelope_properties["includes_mortgage"] = {"type": "boolean"}
            properties[name] = {
                "type": "object",
                "description": _fact_description(name, definition),
                "properties": envelope_properties,
                "required": ["value", "basis"],
                "additionalProperties": False,
            }
        else:
            properties[name] = {
                **value_schema,
                "description": _fact_description(name, definition),
            }
    return {
        "type": "object",
        "properties": properties,
        # Kept open for model/tool compatibility; the server quarantines unknowns.
        "additionalProperties": True,
    }


def _value_matches_kind(value: Any, kind: str) -> bool:
    if kind in {"number", "money"}:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "text":
        return isinstance(value, str)
    if kind in {"mapping", "asset_allocation"}:
        return isinstance(value, dict)
    return value is not None


def _fact_value_rejection(
    field_name: str,
    value: Any,
    definition: FactField,
) -> Dict[str, Any] | None:
    detail: Dict[str, Any] = {
        "field": field_name,
        "expected_kind": definition.kind,
    }
    if definition.unit:
        detail["expected_unit"] = definition.unit
    if not _value_matches_kind(value, definition.kind):
        return {**detail, "reason": "invalid_value_shape"}
    if definition.allowed_values and value not in definition.allowed_values:
        return {
            **detail,
            "reason": "value_out_of_contract",
            "allowed_values": list(definition.allowed_values),
        }
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        below_minimum = (
            definition.minimum is not None
            and (
                value <= definition.minimum
                if definition.exclusive_minimum
                else value < definition.minimum
            )
        )
        above_maximum = (
            definition.maximum is not None and value > definition.maximum
        )
        if below_minimum or above_maximum:
            bounded = {**detail, "reason": "value_out_of_contract"}
            if definition.minimum is not None:
                bounded[
                    "exclusive_minimum" if definition.exclusive_minimum else "minimum"
                ] = definition.minimum
            if definition.maximum is not None:
                bounded["maximum"] = definition.maximum
            return bounded
    return None


def _fact_value_schema(definition: FactField) -> Dict[str, Any]:
    schema = _kind_schema(definition.kind)
    if definition.allowed_values:
        schema["enum"] = list(definition.allowed_values)
    if definition.minimum is not None:
        schema[
            "exclusiveMinimum" if definition.exclusive_minimum else "minimum"
        ] = definition.minimum
    if definition.maximum is not None:
        schema["maximum"] = definition.maximum
    return schema


def _fact_description(name: str, definition: FactField) -> str:
    if definition.period:
        description = (
            f"Canonical {definition.period} value; impact={definition.impact}. "
            "Convert the client-stated figure to the canonical period; basis records "
            "the period originally stated."
        )
    elif definition.kind == "asset_allocation":
        description = (
            "Map the client's holdings to the allowed canonical asset-class keys and "
            "submit decimal weights that sum to 1."
        )
    elif definition.allowed_values:
        description = (
            "Interpret the client's wording and choose one allowed canonical value."
        )
    else:
        description = (
            f"Canonical non-period fact; impact={definition.impact}. Send as a plain scalar."
        )
    if definition.unit:
        description += f" Required representation: {definition.unit}."
    if name == "mortgage_type":
        description += " Keep mortgage duration in mortgage_remaining_term_years."
    return description


def _kind_schema(kind: str) -> Dict[str, Any]:
    if kind in {"number", "money"}:
        return {"type": "number"}
    if kind == "integer":
        return {"type": "integer"}
    if kind == "boolean":
        return {"type": "boolean"}
    if kind == "text":
        return {"type": "string"}
    if kind == "asset_allocation":
        weight_schema = {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        }
        return {
            "type": "object",
            "properties": {
                name: dict(weight_schema) for name in CASHFLOW_ASSET_CLASSES
            },
            "additionalProperties": False,
        }
    if kind == "mapping":
        return {"type": "object"}
    return {
        "anyOf": [
            {"type": "string"},
            {"type": "number"},
            {"type": "boolean"},
            {"type": "array"},
            {"type": "object"},
        ]
    }
