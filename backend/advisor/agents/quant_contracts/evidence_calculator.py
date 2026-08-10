from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any, List, Mapping

from advisor.agents.quant_contracts._shared import _finite_numeric, _string_list
from advisor.agents.quant_contracts.models import QuantEvidenceClaim, QuantEvidenceEnvelope


def _quant_comparison_evidence(
    result: Mapping[str, Any],
) -> QuantEvidenceEnvelope:
    """Validate exact comparison rows without exploding each row into many claims."""

    full_result = (
        result.get("full_result")
        if isinstance(result.get("full_result"), dict)
        else {}
    )
    execution_ok = result.get("ok") is True
    errors: List[str] = []
    if full_result.get("schema_version") != "awm.quant_analysis_comparison.v1":
        errors.append("quant_comparison_schema_invalid")
    deltas = full_result.get("deltas")
    if not isinstance(deltas, list) or not deltas:
        errors.append("quant_comparison_deltas_missing")
        deltas = []
    claims: List[QuantEvidenceClaim] = []
    for index, row in enumerate(deltas):
        if not isinstance(row, dict):
            errors.append(f"quant_comparison_delta_{index}_invalid")
            continue
        metric_key = str(row.get("delta_metric_key") or "").strip()
        unit = str(row.get("unit") or "").strip()
        base_value = _finite_numeric(row.get("base_value"))
        comparison_value = _finite_numeric(row.get("comparison_value"))
        delta = _finite_numeric(row.get("delta"))
        if (
            not metric_key
            or not unit
            or base_value is None
            or comparison_value is None
            or delta is None
        ):
            errors.append(f"quant_comparison_delta_{index}_incomplete")
            continue
        tolerance = max(1e-9, abs(comparison_value - base_value) * 1e-9)
        if abs((comparison_value - base_value) - delta) > tolerance:
            errors.append(f"quant_comparison_delta_{index}_not_reconciled")
            continue
        claims.append(
            QuantEvidenceClaim(
                metric_key=metric_key,
                value={
                    "base_value": base_value,
                    "comparison_value": comparison_value,
                    "delta": delta,
                },
                unit=unit,
                source_path=f"$.full_result.deltas[{index}]",
                claim_id=f"delta_{index}",
                evidence_ref=f"compare_quant_analyses/delta_{index}",
                semantic_metric_keys=[
                    str(row.get("metric_key") or "").strip(),
                    str(row.get("value_path") or "").strip(),
                ],
            )
        )
    valid = bool(execution_ok and claims and not errors)
    interpretation_policy = (
        full_result.get("interpretation_policy")
        if isinstance(full_result.get("interpretation_policy"), dict)
        else {}
    )
    warnings = _string_list(
        [interpretation_policy.get("not_allowed")]
        if interpretation_policy.get("not_allowed")
        else []
    )
    return QuantEvidenceEnvelope(
        tool="compare_quant_analyses",
        status="complete" if valid else "invalid",
        execution_ok=execution_ok,
        valid_for_reporting=valid,
        valid_for_conclusion=False,
        valid_for_recommendation=False,
        claims=claims,
        warnings=warnings,
        assumptions=[
            "Arithmetic direction is comparison minus base.",
            "The comparison does not establish causation.",
        ],
        errors=errors,
    )


def _calculator_evidence(
    tool_name: str,
    result: Mapping[str, Any],
) -> QuantEvidenceEnvelope:
    """Validate one deterministic calculator result as reporting-only evidence."""

    full_result = (
        result.get("full_result")
        if isinstance(result.get("full_result"), dict)
        else {}
    )
    if tool_name == "calculate_financial_math" and full_result.get("schema_version") == "awm.financial_math.v2":
        return _financial_math_v2_evidence(result, full_result)
    expected_schema = (
        "awm.cashflow_metric_calculation.v1"
        if tool_name == "calculate_cashflow_metrics"
        else "awm.financial_math.v1"
    )
    errors: List[str] = []
    if full_result.get("schema_version") != expected_schema:
        errors.append("calculator_schema_invalid")
    operation = str(full_result.get("operation") or "").strip()
    if not operation:
        errors.append("calculator_operation_missing")
    result_payload = (
        full_result.get("result")
        if isinstance(full_result.get("result"), dict)
        else {}
    )
    result_value = _finite_numeric(result_payload.get("value"))
    result_unit = str(result_payload.get("unit") or "").strip()
    if result_value is None:
        errors.append("calculator_result_invalid")
    if not result_unit:
        errors.append("calculator_result_unit_missing")
    reconciliation = (
        full_result.get("reconciliation")
        if isinstance(full_result.get("reconciliation"), dict)
        else {}
    )
    if reconciliation:
        recomputed = _finite_numeric(reconciliation.get("recomputed_value"))
        difference = _finite_numeric(reconciliation.get("difference"))
        if (
            recomputed is None
            or difference is None
            or result_value is None
            or not math.isclose(recomputed, result_value, rel_tol=0.0, abs_tol=1e-9)
            or not math.isclose(difference, 0.0, rel_tol=0.0, abs_tol=1e-9)
        ):
            errors.append("calculator_reconciliation_failed")

    claims: List[QuantEvidenceClaim] = []
    operand_metric_keys = [
        str(full_result.get(role, {}).get("metric_key") or "").strip()
        for role in ("primary", "secondary")
        if isinstance(full_result.get(role), dict)
    ]
    if result_value is not None and result_unit:
        claims.append(
            QuantEvidenceClaim(
                metric_key="calculation_result",
                value=result_value,
                unit=result_unit,
                source_path="$.full_result.result.value",
                claim_id="calculation_result",
                evidence_ref=f"{tool_name}/calculation_result",
                semantic_metric_keys=operand_metric_keys,
            )
        )
    if tool_name == "calculate_cashflow_metrics":
        for role in ("primary", "secondary"):
            operand = (
                full_result.get(role)
                if isinstance(full_result.get(role), dict)
                else {}
            )
            operand_value = _finite_numeric(operand.get("value"))
            operand_unit = str(operand.get("unit") or "").strip()
            if not operand:
                continue
            if operand_value is None or not operand_unit:
                errors.append(f"calculator_{role}_operand_invalid")
                continue
            claims.append(
                QuantEvidenceClaim(
                    metric_key=f"{role}_operand",
                    value=operand_value,
                    unit=operand_unit,
                    source_path=f"$.full_result.{role}.value",
                    claim_id=f"{role}_operand",
                    evidence_ref=str(operand.get("evidence_ref") or ""),
                    semantic_metric_keys=[
                        str(operand.get("metric_key") or "").strip(),
                        str(operand.get("value_path") or "").strip(),
                    ],
                )
            )
    else:
        inputs = (
            full_result.get("inputs")
            if isinstance(full_result.get("inputs"), dict)
            else {}
        )
        for key in (
            "primary_value",
            "secondary_value",
            "annual_rate_decimal",
            "periods",
            "payments_per_year",
        ):
            value = _finite_numeric(inputs.get(key))
            if value is None:
                continue
            unit = (
                str(inputs.get("input_unit") or "unspecified")
                if key in {"primary_value", "secondary_value"}
                else "annual_decimal"
                if key == "annual_rate_decimal"
                else "count"
            )
            claims.append(
                QuantEvidenceClaim(
                    metric_key=f"input.{key}",
                    value=value,
                    unit=unit,
                    source_path=f"$.full_result.inputs.{key}",
                    claim_id=f"input_{key}",
                    evidence_ref=f"{tool_name}/input_{key}",
                )
            )

    comparison_context = (
        full_result.get("comparison_context")
        if isinstance(full_result.get("comparison_context"), dict)
        else None
    )
    comparison_operation = (
        operation in {"ratio", "percentage_change"}
        if tool_name == "calculate_cashflow_metrics"
        else operation == "percentage_change"
    )
    context_warning = ""
    if comparison_operation:
        if comparison_context is None:
            errors.append("calculator_comparison_context_missing")
        else:
            if tool_name == "calculate_cashflow_metrics":
                primary_payload = (
                    full_result.get("primary")
                    if isinstance(full_result.get("primary"), dict)
                    else {}
                )
                secondary_payload = (
                    full_result.get("secondary")
                    if isinstance(full_result.get("secondary"), dict)
                    else {}
                )
                primary_value = _finite_numeric(primary_payload.get("value"))
                secondary_value = _finite_numeric(secondary_payload.get("value"))
                difference_unit = str(primary_payload.get("unit") or "").strip()
            else:
                inputs = (
                    full_result.get("inputs")
                    if isinstance(full_result.get("inputs"), dict)
                    else {}
                )
                primary_value = _finite_numeric(inputs.get("primary_value"))
                secondary_value = _finite_numeric(inputs.get("secondary_value"))
                difference_unit = str(inputs.get("input_unit") or "").strip()
            signed_difference = _finite_numeric(
                comparison_context.get("signed_difference")
            )
            absolute_difference = _finite_numeric(
                comparison_context.get("absolute_difference")
            )
            context_unit = str(
                comparison_context.get("difference_unit") or ""
            ).strip()
            crosses_zero = comparison_context.get("crosses_zero")
            expected_crosses_zero = bool(
                primary_value is not None
                and secondary_value is not None
                and primary_value * secondary_value < 0.0
            )
            expected_difference = (
                secondary_value - primary_value
                if primary_value is not None and secondary_value is not None
                else None
            )
            if not isinstance(crosses_zero, bool):
                errors.append("calculator_crosses_zero_invalid")
            elif crosses_zero != expected_crosses_zero:
                errors.append("calculator_crosses_zero_mismatch")
            if (
                expected_difference is None
                or signed_difference is None
                or absolute_difference is None
                or not math.isclose(
                    signed_difference,
                    expected_difference,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                or not math.isclose(
                    absolute_difference,
                    abs(expected_difference),
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
            ):
                errors.append("calculator_comparison_difference_invalid")
            if not context_unit or context_unit != difference_unit:
                errors.append("calculator_comparison_unit_invalid")
            context_warning = str(
                comparison_context.get("warning") or ""
            ).strip()
            if expected_crosses_zero and not context_warning:
                errors.append("calculator_cross_zero_warning_missing")
            if (
                signed_difference is not None
                and absolute_difference is not None
                and context_unit
            ):
                claims.extend(
                    [
                        QuantEvidenceClaim(
                            metric_key="comparison.signed_difference",
                            value=signed_difference,
                            unit=context_unit,
                            source_path=(
                                "$.full_result.comparison_context.signed_difference"
                            ),
                            claim_id="comparison_signed_difference",
                            evidence_ref=(
                                f"{tool_name}/comparison_signed_difference"
                            ),
                            semantic_metric_keys=operand_metric_keys,
                        ),
                        QuantEvidenceClaim(
                            metric_key="comparison.absolute_difference",
                            value=absolute_difference,
                            unit=context_unit,
                            source_path=(
                                "$.full_result.comparison_context.absolute_difference"
                            ),
                            claim_id="comparison_absolute_difference",
                            evidence_ref=(
                                f"{tool_name}/comparison_absolute_difference"
                            ),
                            semantic_metric_keys=operand_metric_keys,
                        ),
                    ]
                )

    execution_ok = result.get("ok") is True
    valid = bool(execution_ok and claims and result_value is not None and not errors)
    calculation_policy = str(full_result.get("calculation_policy") or "").strip()
    warnings = [
        "This calculation reports arithmetic only and does not establish "
        "feasibility, sustainability, suitability, or a recommendation."
    ]
    if context_warning:
        warnings.insert(0, context_warning)
    return QuantEvidenceEnvelope(
        tool=tool_name,
        status="complete" if valid else "invalid",
        execution_ok=execution_ok,
        valid_for_reporting=valid,
        valid_for_conclusion=False,
        valid_for_recommendation=False,
        claims=claims,
        warnings=warnings if valid else [],
        assumptions=[calculation_policy] if calculation_policy else [],
        errors=errors,
    )


def _financial_math_v2_evidence(
    result: Mapping[str, Any], full_result: Mapping[str, Any],
) -> QuantEvidenceEnvelope:
    outputs = full_result.get("outputs") if isinstance(full_result.get("outputs"), list) else []
    errors: List[str] = []
    claims: List[QuantEvidenceClaim] = []
    for index, output in enumerate(outputs):
        if not isinstance(output, dict):
            errors.append(f"calculator_output_{index}_invalid")
            continue
        decimal_text = str(output.get("value_decimal") or "")
        try:
            numeric = Decimal(decimal_text)
        except InvalidOperation:
            errors.append(f"calculator_output_{index}_invalid")
            continue
        unit = str(output.get("unit") or "")
        if not numeric.is_finite() or not unit:
            errors.append(f"calculator_output_{index}_invalid")
            continue
        reference = str(output.get("reference") or f"output_{index}").lstrip("$")
        claims.append(QuantEvidenceClaim(
            metric_key="calculation_result" if index == 0 else f"calculation_result.{reference}",
            value=decimal_text,
            value_decimal=decimal_text,
            display_value=str(output.get("display_value") or decimal_text),
            unit=unit,
            source_path=f"$.full_result.outputs[{index}].value_decimal",
            claim_id=reference,
            evidence_ref=f"calculate_financial_math/{reference}",
            semantic_metric_keys=[reference],
        ))
    resolved_sources = (
        full_result.get("resolved_sources")
        if isinstance(full_result.get("resolved_sources"), list)
        else []
    )
    for index, source in enumerate(resolved_sources):
        if not isinstance(source, dict):
            errors.append(f"calculator_source_{index}_invalid")
            continue
        decimal_text = str(source.get("value_decimal") or "")
        try:
            numeric = Decimal(decimal_text)
        except InvalidOperation:
            errors.append(f"calculator_source_{index}_invalid")
            continue
        unit = str(source.get("unit") or "")
        source_id = str(source.get("id") or f"source_{index}").lstrip("$")
        if not numeric.is_finite() or not unit or not source_id:
            errors.append(f"calculator_source_{index}_invalid")
            continue
        semantic_keys = [
            source_id,
            str(source.get("kind") or ""),
            str(source.get("metric_key") or ""),
            str(source.get("selector") or ""),
            str(source.get("column") or ""),
        ]
        metric_key = str(source.get("metric_key") or "").strip()
        value_path = str(source.get("value_path") or "").strip()
        if metric_key and value_path:
            semantic_keys.append(f"{metric_key}.{value_path}")
        claims.append(QuantEvidenceClaim(
            metric_key=f"calculation_source.{source_id}",
            value=decimal_text,
            value_decimal=decimal_text,
            unit=unit,
            source_path=f"$.full_result.resolved_sources[{index}].value_decimal",
            claim_id=f"source.{source_id}",
            evidence_ref=f"calculate_financial_math/source.{source_id}",
            semantic_metric_keys=semantic_keys,
        ))
    execution_ok = result.get("ok") is True
    valid = bool(execution_ok and claims and not errors)
    return QuantEvidenceEnvelope(
        tool="calculate_financial_math",
        status="complete" if valid else "invalid",
        execution_ok=execution_ok,
        valid_for_reporting=valid,
        valid_for_conclusion=False,
        valid_for_recommendation=False,
        claims=claims,
        warnings=_string_list(full_result.get("warnings")) if valid else [],
        assumptions=[
            str(full_result.get("precision_policy") or ""),
            *[str(item) for item in full_result.get("metric_template_versions") or []],
        ],
        errors=errors,
    )


def _wolfram_alpha_evidence(
    result: Mapping[str, Any],
) -> QuantEvidenceEnvelope:
    """Validate one externally computed scalar as reporting-only evidence."""

    full_result = (
        result.get("full_result")
        if isinstance(result.get("full_result"), dict)
        else {}
    )
    errors: List[str] = []
    if full_result.get("schema_version") != "awm.wolfram_alpha_calculation.v1":
        errors.append("wolfram_alpha_schema_invalid")
    payload = (
        full_result.get("result")
        if isinstance(full_result.get("result"), dict)
        else {}
    )
    decimal_text = str(payload.get("value_decimal") or "")
    try:
        numeric = Decimal(decimal_text)
    except InvalidOperation:
        numeric = Decimal("NaN")
    unit = str(payload.get("unit") or "").strip()
    if not numeric.is_finite():
        errors.append("wolfram_alpha_result_invalid")
    if unit not in {
        "unitless",
        "decimal",
        "percentage",
        "count",
        "years",
        "months",
    }:
        errors.append("wolfram_alpha_unit_invalid")
    validation = (
        full_result.get("validation")
        if isinstance(full_result.get("validation"), dict)
        else {}
    )
    if not all(
        validation.get(key) is True
        for key in (
            "input_unambiguous",
            "finite_scalar",
            "declared_unit_matched",
        )
    ):
        errors.append("wolfram_alpha_validation_incomplete")
    attribution = (
        full_result.get("attribution")
        if isinstance(full_result.get("attribution"), dict)
        else {}
    )
    if attribution.get("provider") != "Wolfram|Alpha":
        errors.append("wolfram_alpha_attribution_missing")

    claims: List[QuantEvidenceClaim] = []
    if numeric.is_finite() and unit:
        claims.append(
            QuantEvidenceClaim(
                metric_key="calculation_result",
                value=decimal_text,
                value_decimal=decimal_text,
                display_value=str(payload.get("display_value") or decimal_text),
                unit=unit,
                source_path="$.full_result.result.value_decimal",
                claim_id="wolfram_result",
                evidence_ref="query_wolfram_alpha/wolfram_result",
                semantic_metric_keys=["external_pure_math_scalar"],
            )
        )
    execution_ok = result.get("ok") is True
    valid = bool(execution_ok and claims and not errors)
    return QuantEvidenceEnvelope(
        tool="query_wolfram_alpha",
        status="complete" if valid else "invalid",
        execution_ok=execution_ok,
        valid_for_reporting=valid,
        valid_for_conclusion=False,
        valid_for_recommendation=False,
        claims=claims,
        warnings=_string_list(full_result.get("warnings")) if valid else [],
        assumptions=(
            [str(full_result.get("calculation_policy"))]
            if valid and full_result.get("calculation_policy")
            else []
        ),
        errors=errors,
    )
