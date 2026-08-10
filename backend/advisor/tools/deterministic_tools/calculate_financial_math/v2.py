"""Bounded Decimal plan compiler/evaluator for awm.financial_math.v2."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from decimal import (
    Context, Decimal, DivisionByZero, FloatOperation, Inexact, InvalidOperation,
    Overflow, ROUND_HALF_EVEN, Rounded, Subnormal, Underflow, localcontext,
)
from typing import Any, Callable, Dict, List, Mapping, Optional

from client_file.fact_vocabulary import CANONICAL_FACT_FIELDS, FactField, canonical_fact_name
from client_file.financial_position import resolve_financial_position


PRECISION_POLICY = "financial_decimal_34_half_even_v1"
MAX_SOURCES = 32
MAX_STEPS = 24
MAX_ARGUMENTS = 64
MAX_OUTPUTS = 24
MAX_LITERAL_DIGITS = 80
MAX_DECIMAL_ADJUSTED_EXPONENT = 100
MAX_FORMULA_CHARS = 8192
OPERATIONS = {
    "metric",
    "add",
    "subtract",
    "multiply",
    "divide",
    "sum",
    "average",
    "aggregation",
    "power",
    "root",
    "absolute",
    "minimum",
    "maximum",
    "ratio",
    "apply_rate",
    "percentage_change",
    "as_percentage",
    "probability_complement",
    "round",
    "annual_to_monthly",
    "monthly_to_annual",
    "future_value_lump_sum",
    "present_value_lump_sum",
    "future_value_recurring_contribution",
    "loan_payment",
    "compound_annual_growth_rate",
}
TEMPLATE_VERSIONS = {
    "net_worth": "net_worth.v1",
    "annual_surplus": "annual_surplus.v1",
    "monthly_surplus": "monthly_surplus.v1",
    "holding_concentration": "holding_concentration.v1",
    "loan_payment": "loan_payment.v1",
}
FORMULA_CONSTANT_VERSION = "formula_constants.v1"
FORMULA_CONSTANTS = {
    "zero": Decimal("0"),
    "one": Decimal("1"),
    "twelve": Decimal("12"),
    "one_hundred": Decimal("100"),
    "annual_frequency": Decimal("1"),
    "monthly_frequency": Decimal("12"),
    "payment_timing_end": Decimal("0"),
    "payment_timing_begin": Decimal("1"),
}
FORMULA_CONSTANT_UNITS = {
    "zero": "unitless",
    "one": "unitless",
    "twelve": "unitless",
    "one_hundred": "unitless",
    "annual_frequency": "count",
    "monthly_frequency": "count",
    "payment_timing_end": "count",
    "payment_timing_begin": "count",
}


def evaluate_plan(
    arguments: Dict[str, Any], *, client_id: str, companion_turn_id: str,
    client_file: Dict[str, Any], calculation_result_reader: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    try:
        _validate_plan_shape(arguments)
    except PlanError as exc:
        return {"success": False, "error": str(exc)}
    sources = arguments.get("sources")
    steps = arguments.get("steps")
    outputs = arguments.get("outputs")
    requested_version = arguments["client_file_version"]
    raw_actual_version = client_file.get("client_file_version", 0)
    if isinstance(raw_actual_version, bool):
        return {"success": False, "error": "client_file_version_invalid"}
    try:
        actual_version = int(raw_actual_version)
    except (TypeError, ValueError):
        return {"success": False, "error": "client_file_version_invalid"}
    if requested_version != actual_version:
        return {"success": False, "error": "client_file_version_mismatch", "expected": actual_version}

    context = Context(
        prec=34,
        rounding=ROUND_HALF_EVEN,
        Emin=-MAX_DECIMAL_ADJUSTED_EXPONENT,
        Emax=MAX_DECIMAL_ADJUSTED_EXPONENT,
    )
    context.traps[DivisionByZero] = True
    context.traps[InvalidOperation] = True
    context.traps[Overflow] = True
    context.traps[Underflow] = True
    context.traps[Subnormal] = True
    context.traps[FloatOperation] = True
    try:
        with localcontext(context) as decimal_context:
            resolved, position = _resolve_sources(
                sources,
                client_id=client_id,
                client_file=client_file,
                calculation_result_reader=calculation_result_reader,
            )
            evaluated: Dict[str, Dict[str, Any]] = dict(resolved)
            evaluated_steps = []
            template_versions: List[str] = []
            for index, step in enumerate(steps):
                if not isinstance(step, dict):
                    raise PlanError("financial_math_step_invalid")
                step_id = _identifier(step.get("id"), "step")
                if step_id in evaluated:
                    raise PlanError("financial_math_identifier_duplicate")
                references = step.get("arguments")
                if not isinstance(references, list) or not references or len(references) > MAX_ARGUMENTS:
                    raise PlanError("financial_math_arguments_invalid")
                values = [_reference(ref, evaluated) for ref in references]
                operation = str(step.get("operation") or "")
                template = str(step.get("template") or "")
                calculated = _execute(operation, values, step)
                if operation == "metric":
                    if template not in TEMPLATE_VERSIONS:
                        raise PlanError("financial_math_metric_template_invalid")
                    calculated["metric_template_id"] = TEMPLATE_VERSIONS[template]
                    template_versions.append(TEMPLATE_VERSIONS[template])
                calculated.update(
                    {
                        "id": step_id,
                        "index": index + 1,
                        "operation": operation,
                        "argument_refs": list(references),
                    }
                )
                if template:
                    calculated["template"] = template
                evaluated[step_id] = calculated
                evaluated_steps.append(_serialize_value(calculated))
            output_rows = []
            for output_ref in outputs:
                value = _reference(output_ref, evaluated)
                output_rows.append({
                    "reference": output_ref,
                    **_serialize_value(value),
                    "display_value": _display(value["value"], value["unit"]),
                })
            signals = []
            if decimal_context.flags[Inexact]:
                signals.append("Inexact")
            if decimal_context.flags[Rounded]:
                signals.append("Rounded")
    except (Underflow, Subnormal, Overflow):
        return {"success": False, "error": "financial_math_decimal_out_of_range"}
    except (PlanError, DivisionByZero, FloatOperation, InvalidOperation) as exc:
        return {"success": False, "error": str(exc) or "financial_math_decimal_error"}

    source_ledger = [_serialize_source(item) for item in resolved.values()]
    input_fingerprint = _fingerprint(source_ledger)
    normalized_plan_hash = _fingerprint({"sources": sources, "steps": steps, "outputs": outputs})
    calculation_id = f"calculation:{uuid.uuid5(uuid.NAMESPACE_URL, f'{client_id}:{companion_turn_id}:{normalized_plan_hash}:{input_fingerprint}')}"
    uses_cashflow_sources = any(
        item.get("kind") in {"cashflow_claim", "cashflow_series_value"}
        for item in source_ledger
    )
    uses_session_public_facts = any(
        item.get("kind") == "session_public_fact" for item in source_ledger
    )
    uses_live_public_research = any(
        item.get("kind") == "session_public_fact"
        and item.get("origin") == "live_research"
        for item in source_ledger
    )
    full_result = {
        "schema_version": "awm.financial_math.v2",
        "calculation_id": calculation_id,
        "financial_input_snapshot_id": position.get("snapshot_id") if position else None,
        "source_client_file_version": actual_version,
        "source_provider_revisions": position.get("source_provider_revisions") if position else [],
        "source_input_fingerprint": input_fingerprint,
        "normalized_plan_hash": normalized_plan_hash,
        "metric_template_versions": sorted(set(template_versions)),
        "precision_policy": PRECISION_POLICY,
        "resolved_sources": source_ledger,
        "steps": evaluated_steps,
        "outputs": output_rows,
        "audit_trace": {
            "schema_version": "awm.financial_math.audit.v1",
            "execution": {
                "engine": "local_python_decimal",
                "external_services_used": (
                    ["openai_web_search"] if uses_live_public_research else []
                ),
            },
            "checks": {
                "bounded_plan_schema": {"status": "passed"},
                "operation_allowlist": {"status": "passed"},
                "source_resolution": {"status": "passed"},
                "client_file_freshness": {"status": "passed"},
                "analysis_ownership": {
                    "status": "passed" if uses_cashflow_sources else "not_applicable"
                },
                "analysis_freshness": {
                    "status": "passed" if uses_cashflow_sources else "not_applicable"
                },
                "reporting_permission": {
                    "status": (
                        "passed"
                        if uses_cashflow_sources or uses_session_public_facts
                        else "not_applicable"
                    )
                },
                "session_public_fact_authorization": {
                    "status": (
                        "passed" if uses_session_public_facts else "not_applicable"
                    )
                },
                "dimensional_units": {"status": "passed"},
                "finite_decimal_result": {"status": "passed"},
            },
            "sources": source_ledger,
            "steps": evaluated_steps,
            "outputs": output_rows,
        },
        "evidence_refs": sorted({ref for item in source_ledger for ref in item.get("evidence_refs", [])}),
        "warnings": (["Based on confirmed disclosed inputs; account coverage is partial."] if position and position.get("completeness") == "confirmed_disclosed_partial" else []),
        "arithmetic_signals": signals,
        "result": {
            "value_decimal": output_rows[0]["value_decimal"],
            "value": output_rows[0]["value_decimal"],
            "display_value": output_rows[0]["display_value"],
            "unit": output_rows[0]["unit"],
        },
        "operation": str(steps[-1].get("operation") or "plan"),
    }
    return {"success": True, "full_result": full_result}


class PlanError(ValueError):
    pass


def _validate_plan_shape(arguments: Any) -> None:
    if not isinstance(arguments, dict):
        raise PlanError("financial_math_schema_invalid")
    root_fields = {
        "schema_version",
        "client_file_version",
        "sources",
        "steps",
        "outputs",
    }
    if set(arguments) != root_fields:
        raise PlanError("financial_math_plan_fields_invalid")
    if arguments.get("schema_version") != "awm.financial_math.v2":
        raise PlanError("financial_math_schema_invalid")
    version = arguments.get("client_file_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise PlanError("client_file_version_invalid")

    sources = arguments.get("sources")
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
        raise PlanError("financial_math_source_limit")
    source_fields = {
        "literal": ({"id", "kind", "value", "unit", "source_message_id"}, set()),
        "client_fact": ({"id", "kind", "selector"}, set()),
        "formula_constant": (
            {"id", "kind", "selector", "unit"},
            {"unit"},
        ),
        "financial_position": (
            {"id", "kind", "selector", "metric"},
            {"selector", "metric"},
        ),
        "projection_metric": ({"id", "kind", "selector"}, set()),
        "cashflow_claim": (
            {"id", "kind", "analysis_id", "metric_key", "value_path"},
            {"value_path"},
        ),
        "cashflow_series_value": (
            {
                "id",
                "kind",
                "analysis_id",
                "column",
                "calendar_year",
                "percentile",
            },
            set(),
        ),
        "session_public_fact": (
            {"id", "kind", "session_fact_id"},
            set(),
        ),
    }
    for source in sources:
        if not isinstance(source, dict):
            raise PlanError("financial_math_source_invalid")
        kind = source.get("kind")
        if not isinstance(kind, str) or kind not in source_fields:
            raise PlanError("financial_math_source_kind_invalid")
        allowed, optional = source_fields[kind]
        required = allowed - optional
        if set(source) - allowed or not required.issubset(source):
            if "unit" in set(source) - allowed:
                raise PlanError("financial_math_server_resolved_source_unit_forbidden")
            if kind in {"cashflow_claim", "cashflow_series_value"}:
                raise PlanError("financial_math_cashflow_source_fields_invalid")
            raise PlanError("financial_math_source_fields_invalid")
        _identifier(source.get("id"), "source")
        if kind == "literal":
            if not isinstance(source.get("value"), str):
                raise PlanError("literal_must_be_decimal_string")
            _bounded_text(source.get("value"), "literal_value", 128)
            _bounded_text(source.get("unit"), "literal_unit", 64)
            _bounded_text(
                source.get("source_message_id"),
                "literal_source_message_id",
                160,
            )
        elif kind == "financial_position":
            selector_count = sum(
                bool(str(source.get(key) or "").strip())
                for key in ("selector", "metric")
            )
            if selector_count != 1:
                raise PlanError("financial_position_selector_invalid")
        elif kind == "cashflow_claim":
            _required_source_text(source.get("analysis_id"), "analysis_id", 160)
            _required_source_text(source.get("metric_key"), "metric_key", 160)
            if source.get("value_path") is not None:
                _required_source_text(source.get("value_path"), "value_path", 240)
        elif kind == "cashflow_series_value":
            _required_source_text(source.get("analysis_id"), "analysis_id", 160)
            _required_source_text(source.get("column"), "column", 160)
            year = source.get("calendar_year")
            if isinstance(year, bool) or not isinstance(year, int) or not 1900 <= year <= 2200:
                raise PlanError("cashflow_series_calendar_year_invalid")
            percentile = source.get("percentile")
            if not isinstance(percentile, str) or percentile not in {"p10", "p50", "p90"}:
                raise PlanError("cashflow_series_percentile_invalid")
        elif kind == "session_public_fact":
            session_fact_id = source.get("session_fact_id")
            if not isinstance(session_fact_id, str) or not re.fullmatch(
                r"session-public-fact:[a-f0-9]{32}", session_fact_id
            ):
                raise PlanError("session_public_fact_id_invalid")
        else:
            selector = source.get("selector")
            if kind != "financial_position":
                _bounded_text(selector, f"{kind}_selector", 160)

    steps = arguments.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > MAX_STEPS:
        raise PlanError("financial_math_step_limit")
    allowed_step_fields = {
        "id",
        "operation",
        "template",
        "arguments",
        "directions",
        "decimal_places",
    }
    for step in steps:
        if not isinstance(step, dict) or set(step) - allowed_step_fields:
            raise PlanError("financial_math_step_fields_invalid")
        if not {"id", "operation", "arguments"}.issubset(step):
            raise PlanError("financial_math_step_fields_invalid")
        _identifier(step.get("id"), "step")
        operation = step.get("operation")
        if not isinstance(operation, str) or operation not in OPERATIONS:
            raise PlanError("financial_math_operation_invalid")
        references = step.get("arguments")
        if (
            not isinstance(references, list)
            or not references
            or len(references) > MAX_ARGUMENTS
        ):
            raise PlanError("financial_math_arguments_invalid")
        if not all(
            isinstance(reference, str)
            and reference.startswith("$")
            and len(reference) <= 65
            for reference in references
        ):
            raise PlanError("financial_math_reference_invalid")
        if operation == "metric":
            template = step.get("template")
            if not isinstance(template, str) or template not in TEMPLATE_VERSIONS:
                raise PlanError("financial_math_metric_template_invalid")
        elif "template" in step:
            raise PlanError("financial_math_step_fields_invalid")
        if "directions" in step:
            directions = step.get("directions")
            if (
                operation not in {"add", "sum", "aggregation"}
                or not isinstance(directions, list)
                or len(directions) != len(references)
                or any(
                    not isinstance(direction, str)
                    or direction not in {"add", "subtract"}
                    for direction in directions
                )
            ):
                raise PlanError("financial_math_directions_invalid")
        if operation == "round":
            places = step.get("decimal_places", 2)
            if (
                isinstance(places, bool)
                or not isinstance(places, int)
                or not 0 <= places <= 12
            ):
                raise PlanError("round_places_invalid")
        elif "decimal_places" in step:
            raise PlanError("financial_math_decimal_places_requires_round_step")

    outputs = arguments.get("outputs")
    if (
        not isinstance(outputs, list)
        or not outputs
        or len(outputs) > MAX_OUTPUTS
    ):
        raise PlanError("financial_math_outputs_invalid")
    if not all(
        isinstance(reference, str)
        and reference.startswith("$")
        and len(reference) <= 65
        for reference in outputs
    ):
        raise PlanError("financial_math_outputs_invalid")


def _bounded_text(value: Any, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise PlanError(f"financial_math_{field}_invalid")
    text = value.strip()
    if not text or len(text) > max_length:
        raise PlanError(f"financial_math_{field}_invalid")
    return text


def _resolve_sources(
    sources: List[Any], *, client_id: str, client_file: Dict[str, Any],
    calculation_result_reader: Optional[Callable[..., Any]],
):
    resolved: Dict[str, Dict[str, Any]] = {}
    position = None
    for raw in sources:
        if not isinstance(raw, dict):
            raise PlanError("financial_math_source_invalid")
        source_id = _identifier(raw.get("id"), "source")
        if source_id in resolved:
            raise PlanError("financial_math_identifier_duplicate")
        kind = str(raw.get("kind") or "")
        if kind == "literal":
            if not raw.get("source_message_id"):
                raise PlanError("literal_source_message_id_required")
            value = _decimal(raw.get("value"), "literal")
            unit = _unit(raw.get("unit"))
            evidence = [str(raw["source_message_id"])]
            formula = source_id
            lineage = {"source_message_id": str(raw["source_message_id"])}
        elif kind == "client_fact":
            selector = canonical_fact_name(str(raw.get("selector") or ""))
            if not selector:
                raise PlanError("client_fact_selector_invalid")
            value = _client_fact_value(client_file, selector)
            definition = CANONICAL_FACT_FIELDS.get(selector)
            unit = _fact_unit(definition)
            evidence = [selector]
            formula = selector
            lineage = {
                "selector": selector,
                "source_client_file_version": int(
                    client_file.get("client_file_version") or 0
                ),
            }
        elif kind == "formula_constant":
            if set(raw) - {"id", "kind", "selector", "unit"}:
                raise PlanError("financial_math_formula_constant_fields_invalid")
            selector = str(raw.get("selector") or "")
            if selector not in FORMULA_CONSTANTS:
                raise PlanError("financial_math_formula_constant_invalid")
            value = FORMULA_CONSTANTS[selector]
            unit = FORMULA_CONSTANT_UNITS[selector]
            if "unit" in raw and raw.get("unit") != unit:
                raise PlanError("financial_math_formula_constant_unit_mismatch")
            evidence = []
            formula = selector
            lineage = {
                "selector": selector,
                "constant_version": FORMULA_CONSTANT_VERSION,
            }
        elif kind == "financial_position":
            position = position or resolve_financial_position(client_id=client_id, client_file=client_file)
            if position.get("conflicts"):
                raise PlanError("financial_position_conflicted")
            selector = str(raw.get("selector") or raw.get("metric") or "")
            if selector in {"net_worth", "net_worth_operands"}:
                value = sum(
                    (
                        _trusted_decimal(item["value"], "financial_position")
                        * (-1 if item["direction"] == "subtract" else 1)
                        for item in position["net_worth_operands"]
                    ),
                    Decimal(0),
                )
                unit = "money:USD"
                evidence = [str(item["id"]) for item in position["net_worth_operands"]]
                formula = " + ".join(("-" if item["direction"] == "subtract" else "") + str(item["id"]) for item in position["net_worth_operands"])
            elif selector in {"employer_stock", "employer_stock_value"}:
                value = sum(
                    (
                        _trusted_decimal(item["value"], "financial_position")
                        for item in position["employer_stock_operands"]
                    ),
                    Decimal(0),
                )
                unit = "money:USD"
                evidence = [str(item["id"]) for item in position["employer_stock_operands"]]
                formula = "sum(employer_stock_operands)"
            else:
                raise PlanError("financial_position_selector_invalid")
            lineage = {
                "selector": selector,
                "financial_input_snapshot_id": position.get("snapshot_id"),
                "source_input_fingerprint": position.get("source_input_fingerprint"),
                "source_client_file_version": int(
                    client_file.get("client_file_version") or 0
                ),
            }
        elif kind == "projection_metric":
            projection = client_file.get("projection_metrics") if isinstance(client_file.get("projection_metrics"), dict) else {}
            selector = str(raw.get("selector") or "")
            record = projection.get(selector)
            if not isinstance(record, dict) or int(record.get("source_client_version") or 0) != int(client_file.get("client_file_version") or 0):
                raise PlanError("projection_metric_source_unavailable")
            value = _trusted_decimal(
                record.get("value_decimal", record.get("value")),
                "projection_metric",
            )
            unit = _unit(record.get("unit"))
            evidence = [str(record.get("artifact_id") or selector)]
            formula = selector
            lineage = {
                "selector": selector,
                "artifact_id": record.get("artifact_id"),
                "source_client_file_version": int(
                    record.get("source_client_version") or 0
                ),
            }
        elif kind in {"cashflow_claim", "cashflow_series_value"}:
            trusted = _resolve_calculation_result_source(
                raw,
                calculation_result_reader=calculation_result_reader,
            )
            value = trusted.pop("value")
            unit = trusted.pop("unit")
            formula = str(trusted.pop("formula"))
            evidence = list(trusted.pop("evidence_refs"))
            lineage = trusted
        elif kind == "session_public_fact":
            trusted = _resolve_session_public_fact_source(
                raw,
                calculation_result_reader=calculation_result_reader,
            )
            value = trusted.pop("value")
            unit = trusted.pop("unit")
            formula = str(trusted.pop("formula"))
            evidence = list(trusted.pop("evidence_refs"))
            lineage = trusted
        else:
            raise PlanError("financial_math_source_kind_invalid")
        value = _bounded_decimal(value, "financial_math_source_out_of_range")
        formula = _bounded_formula(formula)
        if unit == "probability_0_to_1" and (value < 0 or value > 1):
            raise PlanError("financial_math_probability_out_of_range")
        resolved[source_id] = {
            "id": source_id,
            "kind": kind,
            "value": value,
            "unit": unit,
            "formula": formula,
            "evidence_refs": evidence,
            **lineage,
        }
    return resolved, position


def _resolve_calculation_result_source(
    raw: Dict[str, Any], *,
    calculation_result_reader: Optional[Callable[..., Any]],
) -> Dict[str, Any]:
    kind = str(raw.get("kind") or "")
    allowed_fields = (
        {"id", "kind", "analysis_id", "metric_key", "value_path"}
        if kind == "cashflow_claim"
        else {"id", "kind", "analysis_id", "column", "calendar_year", "percentile"}
    )
    if set(raw) - allowed_fields:
        raise PlanError("financial_math_cashflow_source_fields_invalid")
    if calculation_result_reader is None:
        raise PlanError("financial_math_calculation_source_reader_missing")

    analysis_id = _required_source_text(raw.get("analysis_id"), "analysis_id", 160)
    descriptor: Dict[str, Any] = {
        "id": str(raw["id"]),
        "kind": kind,
        "analysis_id": analysis_id,
    }
    if kind == "cashflow_claim":
        descriptor["metric_key"] = _required_source_text(
            raw.get("metric_key"), "metric_key", 160
        )
        value_path = raw.get("value_path")
        if value_path is not None:
            descriptor["value_path"] = _required_source_text(
                value_path, "value_path", 240
            )
    else:
        descriptor["column"] = _required_source_text(
            raw.get("column"), "column", 160
        )
        year = raw.get("calendar_year")
        if isinstance(year, bool) or not isinstance(year, int) or not 1900 <= year <= 2200:
            raise PlanError("cashflow_series_calendar_year_invalid")
        percentile = str(raw.get("percentile") or "")
        if percentile not in {"p10", "p50", "p90"}:
            raise PlanError("cashflow_series_percentile_invalid")
        descriptor.update({"calendar_year": year, "percentile": percentile})

    try:
        trusted = calculation_result_reader(source=dict(descriptor))
    except Exception as exc:
        raise PlanError("financial_math_calculation_source_unavailable") from exc
    if not isinstance(trusted, Mapping):
        raise PlanError("financial_math_calculation_source_invalid")
    if trusted.get("success") is False or trusted.get("ok") is False:
        error = str(trusted.get("error") or "").strip()
        raise PlanError(error or "financial_math_calculation_source_unavailable")
    if "value" not in trusted or not str(trusted.get("unit") or "").strip():
        raise PlanError("financial_math_calculation_source_invalid")

    for field in ("analysis_id", "metric_key", "value_path", "column", "percentile"):
        if field not in descriptor or trusted.get(field) is None:
            continue
        if str(trusted.get(field)) != str(descriptor[field]):
            raise PlanError("financial_math_calculation_source_lineage_mismatch")
    if (
        "calendar_year" in descriptor
        and trusted.get("calendar_year") is not None
        and str(trusted.get("calendar_year")) != str(descriptor["calendar_year"])
    ):
        raise PlanError("financial_math_calculation_source_lineage_mismatch")

    evidence_refs: List[str] = []
    raw_evidence_refs = trusted.get("evidence_refs")
    if isinstance(raw_evidence_refs, list):
        evidence_refs.extend(
            str(item).strip() for item in raw_evidence_refs if str(item).strip()
        )
    evidence_ref = str(trusted.get("evidence_ref") or "").strip()
    if evidence_ref:
        evidence_refs.append(evidence_ref)
    claim_id = str(trusted.get("claim_id") or "").strip()
    if claim_id:
        evidence_refs.append(claim_id)
    if not evidence_refs:
        evidence_refs.append(analysis_id)

    selector = (
        descriptor["metric_key"]
        + (f".{descriptor['value_path']}" if descriptor.get("value_path") else "")
        if kind == "cashflow_claim"
        else (
            f"{descriptor['column']}[{descriptor['calendar_year']}]"
            f".{descriptor['percentile']}"
        )
    )
    lineage = {
        key: descriptor[key]
        for key in (
            "analysis_id", "metric_key", "value_path", "column",
            "calendar_year", "percentile",
        )
        if key in descriptor
    }
    for key in ("input_fingerprint", "claim_id", "evidence_ref", "source_path"):
        value = trusted.get(key)
        if value is not None and str(value).strip():
            lineage[key] = str(value)
    validation = _bounded_validation_lineage(trusted.get("validation"))
    if validation:
        lineage["validation"] = validation
    return {
        "value": _trusted_decimal(trusted["value"], "cashflow_source"),
        "unit": _unit(trusted["unit"]),
        "formula": f"{analysis_id}:{selector}",
        "evidence_refs": sorted(set(evidence_refs)),
        **lineage,
    }


def _resolve_session_public_fact_source(
    raw: Dict[str, Any], *,
    calculation_result_reader: Optional[Callable[..., Any]],
) -> Dict[str, Any]:
    if set(raw) != {"id", "kind", "session_fact_id"}:
        raise PlanError("session_public_fact_source_fields_invalid")
    if calculation_result_reader is None:
        raise PlanError("financial_math_calculation_source_reader_missing")
    session_fact_id = str(raw.get("session_fact_id") or "").strip()
    if not re.fullmatch(r"session-public-fact:[a-f0-9]{32}", session_fact_id):
        raise PlanError("session_public_fact_id_invalid")
    descriptor = {
        "id": str(raw["id"]),
        "kind": "session_public_fact",
        "session_fact_id": session_fact_id,
    }
    try:
        trusted = calculation_result_reader(source=dict(descriptor))
    except Exception as exc:
        raise PlanError("session_public_fact_unavailable") from exc
    if not isinstance(trusted, Mapping):
        raise PlanError("session_public_fact_invalid")
    if trusted.get("success") is False or trusted.get("ok") is False:
        error = str(trusted.get("error") or "").strip()
        raise PlanError(error or "session_public_fact_unavailable")
    if str(trusted.get("session_fact_id") or "") != session_fact_id:
        raise PlanError("session_public_fact_lineage_mismatch")
    if "value" not in trusted or not str(trusted.get("unit") or "").strip():
        raise PlanError("session_public_fact_invalid")
    raw_evidence_refs = trusted.get("evidence_refs")
    if not isinstance(raw_evidence_refs, list):
        raise PlanError("session_public_fact_sources_missing")
    evidence_refs = sorted(
        {
            str(item).strip()
            for item in raw_evidence_refs
            if str(item).strip()
        }
    )
    if not evidence_refs:
        raise PlanError("session_public_fact_sources_missing")
    lineage: Dict[str, Any] = {"session_fact_id": session_fact_id}
    for key in (
        "variable_key",
        "effective_year",
        "content_sha256",
        "source_path",
        "origin",
    ):
        value = trusted.get(key)
        if value is not None and str(value).strip():
            lineage[key] = value
    validation = _bounded_validation_lineage(trusted.get("validation"))
    if validation:
        lineage["validation"] = validation
    return {
        "value": _trusted_decimal(trusted["value"], "session_public_fact"),
        "unit": _unit(trusted["unit"]),
        "formula": f"{session_fact_id}:validated_public_fact",
        "evidence_refs": evidence_refs,
        **lineage,
    }


def _required_source_text(value: Any, field: str, max_length: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > max_length:
        raise PlanError(f"cashflow_source_{field}_invalid")
    return text


def _bounded_validation_lineage(value: Any) -> Dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    output: Dict[str, str] = {}
    for raw_key in (
        "ownership",
        "freshness",
        "reporting_permission",
        "detail_series",
        "session_authorization",
    ):
        if raw_key not in value:
            continue
        raw_value = value[raw_key]
        if not isinstance(raw_value, str):
            continue
        text = raw_value.strip()
        if text and len(text) <= 240:
            output[raw_key] = text
    return output


def _execute(operation: str, values: List[Dict[str, Any]], step: Dict[str, Any]) -> Dict[str, Any]:
    nums = [item["value"] for item in values]
    units = [item["unit"] for item in values]
    # Formulas reference bounded source/step identifiers instead of recursively
    # embedding prior formulas. ``argument_refs`` carries the full DAG lineage.
    formulas = [f"${item['id']}" for item in values]
    template = str(step.get("template") or "")
    if operation == "metric":
        if template == "net_worth":
            _same_money_units(units, cadence="")
            return _value(nums[0] if len(nums) == 1 else sum(nums, Decimal(0)), units[0], f"net_worth({', '.join(formulas)})", values)
        if template == "annual_surplus":
            _same_money_units(units, cadence="_per_year")
            operation = "subtract"
        elif template == "monthly_surplus":
            if len(nums) != 1:
                raise PlanError("unit_mismatch")
            _same_money_units(units, cadence="_per_year")
            return _value(nums[0] / Decimal(12), units[0].replace("money_per_year:", "money_per_month:"), f"{formulas[0]} / 12", values)
        elif template == "holding_concentration":
            _same_money_units(units, cadence="")
            operation = "ratio"
        elif template == "loan_payment":
            operation = "loan_payment"
    if operation in {"add", "sum", "aggregation"}:
        _same_units(units)
        directions = step.get("directions") if isinstance(step.get("directions"), list) else ["add"] * len(nums)
        if len(directions) != len(nums):
            raise PlanError("financial_math_directions_invalid")
        result = sum((number if direction == "add" else -number for number, direction in zip(nums, directions)), Decimal(0))
        return _value(result, units[0], " + ".join(("-" if direction == "subtract" else "") + formula for formula, direction in zip(formulas, directions)), values)
    if operation == "average":
        _same_units(units)
        return _value(
            sum(nums, Decimal(0)) / Decimal(len(nums)),
            units[0],
            f"average({', '.join(formulas)})",
            values,
        )
    if operation == "subtract":
        _arity(nums, 2); _same_units(units)
        return _value(nums[0] - nums[1], units[0], f"{formulas[0]} - {formulas[1]}", values)
    if operation in {"divide", "ratio"}:
        _arity(nums, 2)
        if nums[1] == 0:
            raise PlanError("division_by_zero")
        unit = "decimal" if units[0] == units[1] else _divide_unit(units[0], units[1])
        return _value(nums[0] / nums[1], unit, f"{formulas[0]} / {formulas[1]}", values)
    if operation in {"multiply", "apply_rate"}:
        _arity(nums, 2)
        unit = _multiply_unit(units[0], units[1])
        normalized_nums = [
            number / Decimal(100) if source_unit == "percentage" else number
            for number, source_unit in zip(nums, units)
        ]
        normalized_formulas = [
            f"({formula} / 100)" if source_unit == "percentage" else formula
            for formula, source_unit in zip(formulas, units)
        ]
        result = normalized_nums[0] * normalized_nums[1]
        formula = f"{normalized_formulas[0]} * {normalized_formulas[1]}"
        if unit == "percentage":
            result *= Decimal(100)
            formula = f"({formula}) * 100"
        return _value(result, unit, formula, values)
    if operation == "percentage_change":
        _arity(nums, 2); _same_units(units)
        if nums[0] == 0:
            raise PlanError("percentage_change_base_must_be_nonzero")
        return _value((nums[1] - nums[0]) / abs(nums[0]), "decimal_change", f"({formulas[1]} - {formulas[0]}) / abs({formulas[0]})", values)
    if operation == "as_percentage":
        _arity(nums, 1)
        if units[0] not in {"decimal", "decimal_change"}:
            raise PlanError("as_percentage_requires_decimal")
        return _value(
            nums[0] * Decimal(100),
            "percentage",
            f"({formulas[0]}) * 100",
            values,
        )
    if operation == "probability_complement":
        _arity(nums, 1)
        if units[0] != "probability_0_to_1" or nums[0] < 0 or nums[0] > 1:
            raise PlanError("probability_complement_requires_probability")
        return _value(
            Decimal(1) - nums[0],
            "probability_0_to_1",
            f"1 - {formulas[0]}",
            values,
        )
    if operation == "absolute":
        _arity(nums, 1); return _value(abs(nums[0]), units[0], f"abs({formulas[0]})", values)
    if operation in {"minimum", "maximum"}:
        _same_units(units); result = min(nums) if operation == "minimum" else max(nums)
        return _value(result, units[0], f"{operation}({', '.join(formulas)})", values)
    if operation == "power":
        _arity(nums, 2)
        if (
            units[0] not in {"decimal", "unitless"}
            or units[1] not in {"count", "unitless"}
            or abs(nums[1]) > 100
        ):
            raise PlanError("power_exponent_invalid")
        return _value(nums[0] ** nums[1], units[0], f"{formulas[0]} ** {formulas[1]}", values)
    if operation == "root":
        _arity(nums, 2)
        if (
            units[0] not in {"decimal", "unitless"}
            or units[1] not in {"count", "unitless"}
            or nums[1] <= 0
            or nums[1] > 12
            or nums[1] != nums[1].to_integral_value()
            or nums[0] < 0
        ):
            raise PlanError("root_degree_invalid")
        return _value(nums[0] ** (Decimal(1) / nums[1]), units[0], f"root({formulas[0]}, {formulas[1]})", values)
    if operation == "annual_to_monthly":
        _arity(nums, 1)
        if not units[0].startswith("money_per_year:"):
            raise PlanError("unit_mismatch")
        return _value(nums[0] / Decimal(12), units[0].replace("money_per_year:", "money_per_month:"), f"{formulas[0]} / 12", values)
    if operation == "monthly_to_annual":
        _arity(nums, 1)
        if not units[0].startswith("money_per_month:"):
            raise PlanError("unit_mismatch")
        return _value(nums[0] * Decimal(12), units[0].replace("money_per_month:", "money_per_year:"), f"{formulas[0]} * 12", values)
    if operation == "loan_payment":
        _arity(nums, 4)
        principal, annual_rate, years, payments = nums
        if not units[0].startswith("money:") or units[1] != "decimal" or units[2] != "years" or units[3] != "count" or principal < 0 or annual_rate <= -1 or years <= 0 or payments <= 0 or payments > 365 or payments != payments.to_integral_value():
            raise PlanError("loan_payment_inputs_invalid")
        rate = annual_rate / payments
        count = years * payments
        payment = principal / count if rate == 0 else principal * rate / (Decimal(1) - (Decimal(1) + rate) ** (-count))
        return _value(payment, units[0].replace("money:", "money_per_payment:"), "principal * periodic_rate / (1 - (1 + periodic_rate) ** -period_count)", values)
    if operation in {"future_value_lump_sum", "present_value_lump_sum"}:
        _arity(nums, 4)
        amount, annual_rate, years, periods_per_year = nums
        if not units[0].startswith("money:") or units[1] != "decimal" or units[2] != "years" or units[3] != "count" or annual_rate <= -1 or years < 0 or periods_per_year <= 0 or periods_per_year > 365 or periods_per_year != periods_per_year.to_integral_value():
            raise PlanError("value_macro_inputs_invalid")
        factor = (Decimal(1) + annual_rate / periods_per_year) ** (years * periods_per_year)
        result = amount * factor if operation == "future_value_lump_sum" else amount / factor
        formula = "amount * (1 + periodic_rate) ** period_count" if operation == "future_value_lump_sum" else "amount / (1 + periodic_rate) ** period_count"
        return _value(result, units[0], formula, values)
    if operation == "future_value_recurring_contribution":
        _arity(nums, 5)
        contribution, annual_rate, years, periods_per_year, timing = nums
        if not units[0].startswith("money:") or units[1] != "decimal" or units[2] != "years" or units[3] != "count" or units[4] != "count" or timing not in {0, 1} or annual_rate <= -1 or years < 0 or periods_per_year <= 0 or periods_per_year > 365 or periods_per_year != periods_per_year.to_integral_value():
            raise PlanError("recurring_value_inputs_invalid")
        rate, count = annual_rate / periods_per_year, years * periods_per_year
        result = contribution * count if rate == 0 else contribution * (((Decimal(1) + rate) ** count - Decimal(1)) / rate) * ((Decimal(1) + rate) if timing == 1 else Decimal(1))
        return _value(result, units[0], "contribution * annuity_factor * timing_factor", values)
    if operation == "compound_annual_growth_rate":
        _arity(nums, 3); _same_units(units[:2])
        if units[2] != "years" or nums[0] <= 0 or nums[1] < 0 or nums[2] <= 0:
            raise PlanError("cagr_inputs_invalid")
        return _value((nums[1] / nums[0]) ** (Decimal(1) / nums[2]) - Decimal(1), "decimal", "(ending / beginning) ** (1 / years) - 1", values)
    if operation == "round":
        _arity(nums, 1)
        places = step.get("decimal_places", 2)
        return _value(nums[0].quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN), units[0], f"round({formulas[0]}, {places})", values)
    raise PlanError("financial_math_operation_invalid")


def _client_fact_value(client_file: Dict[str, Any], selector: str) -> Decimal:
    facts = client_file.get("facts") if isinstance(client_file.get("facts"), dict) else {}
    if selector in facts:
        return _trusted_decimal(facts[selector], selector)
    for row in client_file.get("typed_facts") or []:
        if not isinstance(row, dict) or str(row.get("entity_id") or "") != selector:
            continue
        envelope = row.get("value") if isinstance(row.get("value"), dict) else {}
        return _trusted_decimal(envelope.get("value"), selector)
    raise PlanError("client_fact_source_unavailable")


def _fact_unit(definition: Any) -> str:
    if isinstance(definition, FactField):
        if definition.kind == "money":
            period = "year" if definition.period == "annual" else definition.period
            return f"money_per_{period}:USD" if period else "money:USD"
        if definition.kind == "number":
            return "decimal"
    return "unitless"


def _value(value: Decimal, unit: str, formula: str, inputs: List[Dict[str, Any]]) -> Dict[str, Any]:
    value = _bounded_decimal(value, "financial_math_result_out_of_range")
    formula = _bounded_formula(formula)
    if unit == "probability_0_to_1" and (value < 0 or value > 1):
        raise PlanError("financial_math_probability_out_of_range")
    return {"value": value, "unit": unit, "formula": formula, "evidence_refs": sorted({ref for item in inputs for ref in item.get("evidence_refs", [])})}


def _reference(reference: Any, values: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(reference, str) or not reference.startswith("$") or reference[1:] not in values:
        raise PlanError("financial_math_reference_invalid")
    return values[reference[1:]]


def _identifier(value: Any, kind: str) -> str:
    text = str(value or "")
    if not text or len(text) > 64 or not text.replace("_", "").replace("-", "").isalnum():
        raise PlanError(f"financial_math_{kind}_id_invalid")
    return text


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise PlanError(f"{field}_must_be_decimal_string")
    text = str(value)
    if len([char for char in text if char.isdigit()]) > MAX_LITERAL_DIGITS:
        raise PlanError("financial_math_literal_digit_limit")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise PlanError(f"{field}_must_be_decimal") from exc
    if not number.is_finite():
        raise PlanError(f"{field}_must_be_finite")
    return _bounded_decimal(number, "financial_math_decimal_out_of_range")


def _trusted_decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PlanError(f"{field}_must_be_decimal")
    text = str(value)
    if len([char for char in text if char.isdigit()]) > MAX_LITERAL_DIGITS:
        raise PlanError("financial_math_literal_digit_limit")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise PlanError(f"{field}_must_be_decimal") from exc
    if not number.is_finite():
        raise PlanError(f"{field}_must_be_finite")
    return _bounded_decimal(number, "financial_math_decimal_out_of_range")


def _bounded_decimal(value: Decimal, error: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise PlanError(error)
    if value.is_zero():
        return Decimal(0)
    if (
        len(value.as_tuple().digits) > MAX_LITERAL_DIGITS
        or abs(value.adjusted()) > MAX_DECIMAL_ADJUSTED_EXPONENT
    ):
        raise PlanError(error)
    return value


def _bounded_formula(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_FORMULA_CHARS:
        raise PlanError("financial_math_formula_limit")
    return value


def _unit(value: Any) -> str:
    unit = str(value or "")
    allowed = {
        "decimal",
        "percentage",
        "probability_0_to_1",
        "years",
        "months",
        "count",
        "unitless",
    }
    if unit in allowed or re.fullmatch(
        r"money(?:_per_year|_per_month)?:[A-Z]{3}",
        unit,
    ):
        return unit
    raise PlanError("financial_math_unit_invalid")


def _same_units(units: List[str]) -> None:
    if not units or len(set(units)) != 1:
        if len({item.split(":")[-1] for item in units if item.startswith("money")}) > 1:
            raise PlanError("currency_mismatch")
        raise PlanError("unit_mismatch")


def _same_money_units(units: List[str], *, cadence: str) -> None:
    _same_units(units)
    if not re.fullmatch(rf"money{re.escape(cadence)}:[A-Z]{{3}}", units[0]):
        raise PlanError("unit_mismatch")


def _multiply_unit(left: str, right: str) -> str:
    dimensionless = {
        "decimal",
        "decimal_change",
        "percentage",
        "probability_0_to_1",
        "unitless",
    }
    if left in dimensionless and right in dimensionless:
        for preferred in (
            "probability_0_to_1",
            "percentage",
            "decimal_change",
            "decimal",
            "unitless",
        ):
            if preferred in {left, right}:
                return preferred
    if right in dimensionless:
        return left
    if left in dimensionless:
        return right
    raise PlanError("unit_mismatch")


def _divide_unit(left: str, right: str) -> str:
    if right in {"decimal", "unitless"}:
        return left
    if left.startswith("money:"):
        currency = left.split(":", 1)[1]
        if right == f"money_per_month:{currency}":
            return "months"
        if right == f"money_per_year:{currency}":
            return "years"
        if right.startswith(("money_per_month:", "money_per_year:")):
            raise PlanError("currency_mismatch")
    if left.startswith("money") and right.startswith("money"):
        left_currency = left.split(":", 1)[-1]
        right_currency = right.split(":", 1)[-1]
        if left_currency != right_currency:
            raise PlanError("currency_mismatch")
    raise PlanError("unit_mismatch")


def _arity(values: List[Decimal], expected: int) -> None:
    if len(values) != expected:
        raise PlanError("financial_math_arity_invalid")


def _serialize_value(item: Dict[str, Any]) -> Dict[str, Any]:
    return {key: (str(value) if isinstance(value, Decimal) else value) for key, value in item.items() if key != "value"} | {"value_decimal": _canonical_decimal(item["value"])}


def _serialize_source(item: Dict[str, Any]) -> Dict[str, Any]:
    return {**_serialize_value(item), "id": item["id"], "kind": item["kind"]}


def _canonical_decimal(value: Decimal) -> str:
    value = _bounded_decimal(value, "financial_math_result_out_of_range")
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _display(value: Decimal, unit: str) -> str:
    value = _bounded_decimal(value, "financial_math_result_out_of_range")
    if unit in {"probability_0_to_1", "decimal_change"}:
        return f"{format(value * Decimal(100), '.2f')}%"
    if unit == "percentage":
        return f"{format(value, '.2f')}%"
    if unit == "decimal":
        return format(value, ".4f")
    if unit.startswith("money"):
        return format(value, ".2f")
    return format(value, ".6f")


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"
