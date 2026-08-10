"""Materialize successful cash-flow runs into APP projection rows."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List

from api.services.cashflow_projection_mapper import (
    build_projection_artifact_from_cashflow_result,
)
from api.services.projection_inputs import (
    projection_cashflow_input,
    projection_input_fingerprint,
)


@dataclass(frozen=True)
class ProjectionMaterializationResult:
    records: List[Dict[str, Any]]
    outcomes: List[Dict[str, Any]]

    def __iter__(
        self,
    ) -> Iterator[List[Dict[str, Any]]]:
        yield self.records
        yield self.outcomes


def _projection_id(client_id: str, analysis_id: str) -> str:
    digest = hashlib.sha256(
        f"{client_id}\x1f{analysis_id}".encode("utf-8")
    ).hexdigest()[:24]
    return f"projection_{digest}"


def materialize_projection_artifacts(
    *,
    deps: Any,
    client_id: str,
    session_id: str,
    result: Dict[str, Any],
) -> ProjectionMaterializationResult:
    """Persist APP projection data when the real cash-flow tool succeeds."""

    persisted: List[Dict[str, Any]] = []
    outcomes: List[Dict[str, Any]] = []
    for tool_result in result.get("tool_results") or []:
        if not isinstance(tool_result, dict):
            continue
        if (
            tool_result.get("tool") != "run_cashflow_projection"
            or tool_result.get("ok") is not True
        ):
            continue
        analysis_id = str(tool_result.get("analysis_id") or "").strip()
        if not analysis_id:
            continue
        started = time.perf_counter()
        outcome: Dict[str, Any] = {"analysis_id": analysis_id}
        try:
            snapshot_record = deps.db_get_cashflow_analysis_snapshot(
                client_id=client_id,
                analysis_id=analysis_id,
            )
            snapshot = (
                snapshot_record.get("payload")
                if isinstance(snapshot_record, dict)
                and isinstance(snapshot_record.get("payload"), dict)
                else {}
            )
            source = snapshot.get("projection_source")
            if not isinstance(source, dict):
                raise RuntimeError(
                    "real cashflow projection source was not persisted"
                )

            artifact = build_projection_artifact_from_cashflow_result(
                source,
                client_context=projection_client_context_from_snapshot(snapshot),
            )
            native_ready = any(
                isinstance(section, dict)
                and section.get("section_id") == "native_projection"
                and isinstance(section.get("payload"), dict)
                and section["payload"].get("status") != "not_available"
                for section in artifact.get("sections") or []
            )
            if not native_ready:
                raise RuntimeError(
                    "real cashflow output did not contain chart-ready series"
                )

            _, snapshot_version = projection_cashflow_input(
                deps.db_get_latest_knowledge_snapshot,
                client_id,
            )
            artifact.update(
                {
                    "generation_source": "real_cashflow_engine",
                    "source_cashflow_analysis_id": analysis_id,
                    "knowledge_snapshot_version": snapshot_version,
                    "input_fingerprint": projection_input_fingerprint(
                        snapshot_version
                    ),
                }
            )
            existing = None
            for candidate in deps.db_list_artifacts(
                client_id=client_id,
                artifact_type="projection",
            ) or []:
                payload = (
                    candidate.get("payload")
                    if isinstance(candidate, dict)
                    and isinstance(candidate.get("payload"), dict)
                    else {}
                )
                if payload.get("source_cashflow_analysis_id") == analysis_id:
                    existing = candidate
                    break

            if existing is not None and callable(
                getattr(deps, "db_update_artifact", None)
            ):
                saved = deps.db_update_artifact(
                    artifact_id=existing["id"],
                    client_id=client_id,
                    status="ready",
                    payload_patch=artifact,
                )
                operation = "updated"
            elif existing is not None:
                saved = existing
                operation = "reused"
            else:
                saved = deps.db_save_artifact(
                    artifact_id=_projection_id(client_id, analysis_id),
                    client_id=client_id,
                    artifact_type="projection",
                    title=str(
                        artifact.get("title") or "Projection Analytics"
                    ),
                    payload=artifact,
                    related_type="cashflow_analysis",
                    related_id=analysis_id,
                    status="ready",
                )
                operation = "created"
            if not isinstance(saved, dict):
                raise RuntimeError(
                    "projection artifact persistence returned no record"
                )
            persisted.append(saved)
            outcome.update(
                {
                    "status": "success",
                    "operation": operation,
                    "artifact_id": saved.get("id"),
                }
            )
        except Exception as exc:  # pylint: disable=broad-except
            outcome.update(
                {"status": "error", "operation": "failed", "error": str(exc)}
            )
        outcome["duration_ms"] = round(
            (time.perf_counter() - started) * 1000,
            3,
        )
        outcomes.append(outcome)
    return ProjectionMaterializationResult(persisted, outcomes)


def projection_client_context_from_snapshot(
    snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    """Recover UI labels from the exact input used by the cash-flow engine."""

    analysis = snapshot.get("analysis")
    request_payload = (
        analysis.get("request")
        if isinstance(analysis, dict) and isinstance(analysis.get("request"), dict)
        else {}
    )
    effective_input = request_payload.get("effective_input")
    if not isinstance(effective_input, dict):
        return {}
    profile = (
        effective_input.get("client_profile")
        if isinstance(effective_input.get("client_profile"), dict)
        else {}
    )
    expenses = (
        effective_input.get("expenses")
        if isinstance(effective_input.get("expenses"), dict)
        else {}
    )
    housing = (
        expenses.get("housing")
        if isinstance(expenses.get("housing"), dict)
        else {}
    )
    accounts = (
        effective_input.get("accounts")
        if isinstance(effective_input.get("accounts"), dict)
        else {}
    )
    liabilities = (
        effective_input.get("liabilities")
        if isinstance(effective_input.get("liabilities"), dict)
        else {}
    )
    context: Dict[str, Any] = {}
    for key in ("age", "retirement_age", "life_expectancy"):
        if profile.get(key) is not None:
            context[key] = profile.get(key)
    income = (
        effective_input.get("income")
        if isinstance(effective_input.get("income"), dict)
        else {}
    )
    annual_income = sum(
        _projection_number(income.get(key))
        for key in ("salary", "spouse_income", "bonus")
    )
    annual_spending = _projection_number(expenses.get("base_spending"))
    if annual_income > 0:
        context["annual_income"] = annual_income
    if annual_spending > 0:
        context["annual_spending"] = annual_spending
    for key in (
        "mortgage_interest_rate",
        "mortgage_remaining_term_years",
        "mortgage_type",
    ):
        if housing.get(key) is not None:
            context[key] = housing.get(key)
    liquid = _sum_projection_account_balances(accounts.get("bank"))
    invested = sum(
        _sum_projection_account_balances(accounts.get(category))
        for category in (
            "brokerage",
            "investment",
            "education",
            "retirement",
            "hsa",
            "trust",
            "life_insurance",
            "annuity",
        )
    )
    real_assets = _projection_number(housing.get("home_value"))
    asset_rows = {
        label: value
        for label, value in (
            ("Liquid", liquid),
            ("Invested", invested),
            ("Real", real_assets),
        )
        if value > 0
    }
    mortgage = max(
        _projection_number(liabilities.get("mortgage_balance")),
        _projection_number(housing.get("mortgage_balance")),
    )
    if mortgage > 0:
        context["mortgage_balance"] = mortgage
    monthly_mortgage = _projection_number(
        housing.get("monthly_principal_interest")
    )
    if monthly_mortgage > 0:
        context["monthly_mortgage_payment"] = monthly_mortgage
    other_debt = sum(
        _projection_number(value)
        for key, value in liabilities.items()
        if key != "mortgage_balance"
    )
    liability_rows = {
        label: value
        for label, value in (
            ("Mortgage", mortgage),
            ("Other debt", other_debt),
        )
        if value > 0
    }
    if asset_rows or liability_rows:
        context["balance_sheet"] = {
            "assets": asset_rows,
            "liabilities": liability_rows,
        }
    return context


def _sum_projection_account_balances(value: Any) -> float:
    if not isinstance(value, list):
        return 0.0
    return sum(
        _projection_number(account.get("balance"))
        for account in value
        if isinstance(account, dict)
    )


def _projection_number(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "ProjectionMaterializationResult",
    "materialize_projection_artifacts",
    "projection_client_context_from_snapshot",
]
