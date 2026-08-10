"""Map cashflow-model projection bands into AWM Projection artifacts.

The standalone cashflow model and data-spec produce chart-ready model data as:

    {"years": [...], "percentile_bands": {"Net Worth": {"Median": [...]}}}

The mobile app should not consume data-spec PNGs. This mapper keeps the same
underlying deterministic model data and shapes it for React Native native
charts and AWM artifact storage.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

from client_file.fact_vocabulary import (
    fact_aliases_for_engine_field,
    fact_value_for_engine_field,
)


P10 = "Bottom 10%"
P50 = "Median"
P90 = "Top 10%"

ASSET_COLUMNS = (
    "Bank Balance",
    "Brokerage Balance",
    "Investment Balance",
    "401k Balance",
    "Traditional IRA Balance",
    "Roth IRA Balance",
    "HSA Balance",
    "529 Balance",
    "Trust Balance",
    "Life Ins Cash Value",
    "Annuity Balance",
    "Home Value",
    "Real Asset Value",
)

FINANCIAL_ASSET_COLUMNS = (
    "Bank Balance",
    "Brokerage Balance",
    "Investment Balance",
    "401k Balance",
    "Traditional IRA Balance",
    "Roth IRA Balance",
    "HSA Balance",
    "529 Balance",
    "Trust Balance",
    "Life Ins Cash Value",
    "Annuity Balance",
)

OUTFLOW_COLUMNS = (
    "Base Living Spending",
    "Housing",
    "Loan Payments",
    "401k Contrib",
    "529 Contributions",
    "Healthcare Costs",
    "Real Asset Costs",
    "Charity",
    "Life Ins Premiums",
    "Insurance Premiums",
    "Taxes",
)


def build_projection_artifact_from_cashflow_result(
    cashflow_result: Mapping[str, Any],
    *,
    title: str = "Projection Analytics",
    client_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build an AWM projection artifact from cashflow-model output."""
    result = _unwrap_result(cashflow_result)
    bands = result.get("percentile_bands") if isinstance(result.get("percentile_bands"), dict) else {}
    series_length = _bands_series_length(bands)
    years = _normalize_years(result.get("years") or [], series_length=series_length)
    if not years or not bands:
        return _artifact(
            title,
            [
                _section("hero", title, {"headline": title, "status": "not_available"}),
                _section("native_projection", "Native Projection Payload", {"status": "not_available"}),
            ],
            raw_source=result,
        )

    context = dict(client_context or {})
    meta = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    if context.get("age") is None:
        metadata_sources = [meta]
        for nested_key in ("projection_parameters", "parameters", "client_profile"):
            nested = meta.get(nested_key)
            if isinstance(nested, dict):
                metadata_sources.append(nested)
        for metadata_source in metadata_sources:
            for key in ("age", "current_age", "client_age", "primary_current_age"):
                if metadata_source.get(key) is not None:
                    context["age"] = metadata_source.get(key)
                    break
            if context.get("age") is not None:
                break
    native = build_native_projection_payload(years, bands, client_context=context)
    sections = [
        _section(
            "hero",
            title,
            {
                "headline": title,
                "net_worth_today": native["netWorth"]["todayValue"],
                "base_case_10y": native["netWorth"].get("tenYearMedianValue"),
                "success_rate": result.get("success_rate"),
                "source": "cashflow_model",
            },
        ),
        _section("native_projection", "Native Projection Payload", native),
        _section("cash_flow", "Cash Flow", native["cashFlow"]),
        _section("balance_sheet", "Balance Sheet", native["balance"]),
        _section("asset_mix", "Financial Asset Mix", native["assetMix"]),
        _section("debt", "Debt Burden", native["debt"]),
        _section("stress", "Stress Test", {"scenarios": native["stress"]}),
        _section(
            "cashflow_model_output",
            "Cashflow Model Output",
            {
                "years": years,
                "percentile_bands": bands,
                "success_rate": result.get("success_rate"),
                "metadata": result.get("metadata"),
                "warnings": result.get("warnings"),
            },
        ),
    ]
    return _artifact(title, sections, raw_source=result)


def build_native_projection_payload(
    years: List[int],
    bands: Mapping[str, Any],
    *,
    client_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Map cashflow percentile bands to the current Projection page shape."""
    net_worth = _net_worth_payload(years, bands, client_context=client_context)
    balance = _balance_payload(bands, client_context=client_context)
    cash_flow = _cash_flow_payload(bands, client_context=client_context)
    asset_mix = _asset_mix_payload(bands, client_context=client_context)
    debt = _debt_payload(years, bands, client_context=client_context)
    return {
        "source": "cashflow_model",
        "years": years,
        "netWorth": net_worth,
        "balance": balance,
        "cashFlow": cash_flow,
        "runway": _runway_payload(bands, client_context=client_context),
        "debt": debt,
        "assetMix": asset_mix,
        "stress": _stress_payload(bands),
        "simulatedProjection": _simulated_projection_payload(years, bands),
        "ages": net_worth.get("ages"),
        "currentAge": net_worth.get("currentAge"),
    }


def required_projection_columns() -> List[str]:
    """Minimum model columns needed by the current AWM Projection page."""
    columns = {
        "Year",
        "Net Worth",
        "Total Cash Inflows",
        "Total Cash Outflows",
        "Net Cashflow",
        "Income",
        "Spending",
        "Taxes",
        "Total Assets",
        "Total Liabilities",
        "Mortgage Balance",
        "Loan Balance",
        "Life Ins Loan Balance",
        "Cashflow Shortfall Debt",
        "Mortgage Payments",
        "Loan Payments",
        "Interest Paid",
        "Mortgage Interest Paid",
        "Loan Interest Paid",
        "Life Ins Loan Interest Paid",
        "Cashflow Shortfall Interest Paid",
    }
    columns.update(ASSET_COLUMNS)
    columns.update(OUTFLOW_COLUMNS)
    return sorted(columns)


def _net_worth_payload(
    years: List[int],
    bands: Mapping[str, Any],
    *,
    client_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    median_values = _series(bands, "Net Worth", P50)
    lower_values = _series(bands, "Net Worth", P10)
    upper_values = _series(bands, "Net Worth", P90)
    today = _at(median_values, 0)
    ten_year_index = _index_for_horizon(years, 10)
    peak = max(median_values) if median_values else 0.0
    payload: Dict[str, Any] = {
        "history": [_k(today)],
        "median": [_k(value) for value in median_values],
        "lower": [_k(value) for value in lower_values],
        "upper": [_k(value) for value in upper_values],
        "today": _money_short(today),
        "todayValue": round(today, 2),
        "peak": _money_short(peak),
        "peakValue": round(peak, 2),
        "late": _money_short(_at(median_values, -1)),
        "lateValue": round(_at(median_values, -1), 2),
        "tenYearMedianValue": round(_at(median_values, ten_year_index), 2),
        "years": years,
    }
    age = None
    if isinstance(client_context, Mapping):
        for key in fact_aliases_for_engine_field("current_age"):
            try:
                age = float(client_context.get(key))  # type: ignore[arg-type]
                break
            except (TypeError, ValueError):
                continue
    if age is not None and (years or median_values):
        axis_len = max(len(years), len(median_values))
        # Prefer index offsets when years are missing/collapsed (common when the
        # engine returns years=[None,...]); otherwise use calendar deltas.
        use_index = (
            not years
            or len(set(years)) <= 1
            or (years[0] <= 5 and years[-1] < 120 and years == list(range(years[0], years[0] + len(years))))
        )
        if use_index:
            ages = [int(round(age + index)) for index in range(axis_len)]
        else:
            base = years[0]
            ages = [int(round(age + (year - base))) for year in years]
            if len(ages) < axis_len:
                ages.extend(int(round(age + index)) for index in range(len(ages), axis_len))
        payload["ages"] = ages
        payload["years"] = list(range(axis_len)) if use_index else years
        payload["currentAge"] = int(round(age))
        payload["terminalAge"] = ages[-1]
        payload["endAge"] = ages[-1]
    return payload


def _cash_flow_payload(
    bands: Mapping[str, Any],
    *,
    client_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    inflow = _preferred_or_sum(bands, "Total Cash Inflows", ("Income",), P50)
    outflow = _preferred_or_sum(bands, "Total Cash Outflows", OUTFLOW_COLUMNS, P50)
    net = _preferred_or_sum(bands, "Net Cashflow", ("Net Cashflow",), P50)
    # Cashflow-model output includes an opening baseline row. Balance-sheet
    # charts use row 0, but flow metrics should use the first activity year.
    flow_idx = 1 if len(inflow) > 1 or len(outflow) > 1 or len(net) > 1 else 0
    current_inflow = _at(inflow, flow_idx)
    current_outflow = _at(outflow, flow_idx)
    current_net = _at(net, flow_idx) if net else current_inflow - current_outflow
    # Some real engine result shapes contain only balance-sheet percentile
    # bands. Use the exact effective input from that same engine run for current
    # flow metrics; do not invent projected future points.
    if isinstance(client_context, Mapping):
        if current_inflow <= 0:
            current_inflow = _as_float(client_context.get("annual_income"))
        if current_outflow <= 0:
            current_outflow = _as_float(client_context.get("annual_spending"))
        if not net:
            current_net = current_inflow - current_outflow
    savings_rate = current_net / current_inflow if current_inflow else 0.0
    event: Dict[str, Any] = {}
    if isinstance(client_context, Mapping):
        try:
            current_age = float(
                fact_value_for_engine_field(client_context, "current_age")
            )
            retirement_age = float(
                fact_value_for_engine_field(client_context, "retirement_age")
            )
            if retirement_age >= current_age:
                event = {"at": int(round(retirement_age - current_age)), "label": "Retirement"}
        except (TypeError, ValueError):
            pass
    return {
        "inflow": _money_short(current_inflow / 12.0),
        "outflow": _money_short(current_outflow / 12.0),
        "surplus": _signed_money_short(current_net / 12.0),
        "savingsRate": _percent(savings_rate),
        "savings_rate": round(savings_rate, 4),
        "inHist": (
            [_k_month(value) for value in inflow[flow_idx:flow_idx + 4]]
            if inflow
            else ([_k_month(current_inflow)] if current_inflow > 0 else [])
        ),
        "inProj": [_k_month(value) for value in inflow[flow_idx + 3:flow_idx + 11] or inflow[flow_idx:flow_idx + 8]],
        "outHist": (
            [_k_month(value) for value in outflow[flow_idx:flow_idx + 4]]
            if outflow
            else ([_k_month(current_outflow)] if current_outflow > 0 else [])
        ),
        "outProj": [_k_month(value) for value in outflow[flow_idx + 3:flow_idx + 11] or outflow[flow_idx:flow_idx + 8]],
        "event": event,
        "outflows": _where_money_goes_payload(bands, index=flow_idx),
    }


def _balance_payload(
    bands: Mapping[str, Any],
    *,
    client_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    assets = _first_available(bands, "Total Assets", sum_columns=ASSET_COLUMNS)
    liabilities = _first_available(
        bands,
        "Total Liabilities",
        sum_columns=("Mortgage Balance", "Loan Balance", "Life Ins Loan Balance", "Cashflow Shortfall Debt"),
    )
    net = _first_available(bands, "Net Worth")
    input_assets, input_liabilities = _context_balance_sheet(client_context)
    if input_assets:
        assets = sum(input_assets.values())
        liabilities = sum(input_liabilities.values())
        composition = _rows_from_named_values(input_assets)
        if net <= 0:
            net = assets - liabilities
    else:
        composition = _composition_rows(bands, ASSET_COLUMNS)
    # When the engine only returns Bank Balance + Net Worth, assets look like
    # cash-only while NW is much larger. Rebuild assets from NW + liabilities and
    # put the residual into an investments bucket so the UI is not misleading.
    composed_total = sum(row["value"] for row in composition)
    if not input_assets and net > 0 and (
        assets <= 0 or assets + 1 < net or composed_total + 1 < net
    ):
        assets = max(assets, net + liabilities)
        residual = max(0.0, assets - composed_total)
        if residual > 1:
            composition = list(composition)
            composition.append(
                {
                    "label": "Investments & other",
                    "pct": 0,
                    "value": round(residual, 2),
                    "c": "#14121A",
                }
            )
            total = sum(row["value"] for row in composition) or 1.0
            for row in composition:
                row["pct"] = int(round(100.0 * row["value"] / total))
    return {
        "assets": _money_short(assets),
        "assetsValue": round(assets, 2),
        "liabilities": _money_short(liabilities),
        "liabilitiesValue": round(liabilities, 2),
        "net": _money_short(net),
        "netValue": round(net, 2),
        "capital": [
            {"label": "Net worth", "v": round(_k(net), 2), "c": "#14121A"},
            {"label": "Debt", "v": round(_k(liabilities), 2), "c": "#C8C4D2"},
        ],
        "composition": composition,
    }


def _asset_mix_payload(
    bands: Mapping[str, Any],
    *,
    client_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    input_assets, _ = _context_balance_sheet(client_context)
    financial_assets = {
        label: value
        for label, value in input_assets.items()
        if label.lower() not in {"real", "real assets", "real estate", "home"}
    }
    rows = (
        _rows_from_named_values(financial_assets)
        if financial_assets
        else _composition_rows(bands, FINANCIAL_ASSET_COLUMNS)
    )
    total = sum(row["value"] for row in rows)
    net = _first_available(bands, "Net Worth")
    if not financial_assets and net > 0 and total + 1 < net:
        residual = net - total
        rows = list(rows)
        rows.append(
            {
                "label": "Investments & other",
                "pct": 0,
                "value": round(residual, 2),
                "c": "#14121A",
            }
        )
        total = sum(row["value"] for row in rows) or 1.0
        for row in rows:
            row["pct"] = int(round(100.0 * row["value"] / total))
    return {
        "total": _money_short(total),
        "totalValue": round(total, 2),
        "rows": [
            {
                "label": row["label"],
                "v": round(_k(row["value"]), 2),
                "value": round(row["value"], 2),
                "pct": row["pct"],
            }
            for row in rows
        ],
    }


def _debt_payload(
    years: List[int],
    bands: Mapping[str, Any],
    *,
    client_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    # Debt presentation semantics follow the cashflow domain expert mapping:
    # Loan (not Auto loan), Interest Paid preferred for aggregate rate, per-loan payoffYear.
    loans = []
    for label, column in (
        ("Mortgage", "Mortgage Balance"),
        ("Loan", "Loan Balance"),
        ("Life insurance loan", "Life Ins Loan Balance"),
        ("Cashflow shortfall", "Cashflow Shortfall Debt"),
    ):
        values = _series(bands, column, P50)
        value = _first_non_zero_value(values)
        if value > 0:
            loans.append({
                "label": label,
                "v": round(_k(value), 2),
                "value": round(value, 2),
                "rate": None,
                "payoffYear": _payoff_year(years, values),
            })
    balance = sum(item["value"] for item in loans)
    # The engine's first row is an opening balance sheet, so payment series
    # commonly begin with zero. Use the first activity value for UI metrics.
    monthly = (
        _first_non_zero(bands, "Mortgage Payments")
        + _first_non_zero(bands, "Loan Payments")
    ) / 12.0
    interest_values = _series(bands, "Interest Paid", P50)
    mortgage_interest_values = _series(bands, "Mortgage Interest Paid", P50)
    interest = _at(interest_values, 0) if interest_values else _at(mortgage_interest_values, 0)
    rate = interest / balance if balance and interest > 0 else None
    input_assets, input_liabilities = _context_balance_sheet(client_context)
    del input_assets
    if balance <= 0 and input_liabilities:
        mortgage = _as_float(input_liabilities.get("Mortgage"))
        if mortgage > 0:
            loans = [{
                "label": "Mortgage",
                "v": round(_k(mortgage), 2),
                "value": round(mortgage, 2),
                "rate": None,
                "payoffYear": None,
            }]
            balance = mortgage
    if rate is None and isinstance(client_context, Mapping):
        try:
            rate = float(client_context.get("mortgage_interest_rate"))  # type: ignore[arg-type]
            if rate > 1:
                rate /= 100.0
        except (TypeError, ValueError):
            rate = None
    if monthly <= 0 and isinstance(client_context, Mapping):
        monthly = _as_float(client_context.get("monthly_mortgage_payment"))
        if monthly <= 0 and balance > 0 and rate is not None:
            term_years = _as_float(client_context.get("mortgage_remaining_term_years"))
            monthly = _amortized_monthly_payment(balance, rate, term_years)
    if loans and rate is not None:
        loans[0]["rate"] = _rate_percent(rate)
    return {
        "balance": _money_short(balance),
        "balanceValue": round(balance, 2),
        "rate": _rate_percent(rate) if rate is not None else "--",
        "rateValue": round(rate, 6) if rate is not None else None,
        "monthly": _money_short(monthly),
        "monthlyValue": round(monthly, 2),
        "loans": loans,
    }


def _runway_payload(
    bands: Mapping[str, Any],
    *,
    client_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    bank = _first_non_zero(bands, "Bank Balance")
    outflows = _preferred_or_sum(bands, "Total Cash Outflows", OUTFLOW_COLUMNS, P50)
    flow_idx = 1 if len(outflows) > 1 else 0
    monthly_spending = _at(outflows, flow_idx) / 12.0 if outflows else 0.0
    if monthly_spending <= 0:
        spending = _series(bands, "Spending", P50)
        monthly_spending = _at(spending, flow_idx) / 12.0 if spending else 0.0
    if isinstance(client_context, Mapping):
        input_assets, _ = _context_balance_sheet(client_context)
        if bank <= 0:
            bank = _as_float(input_assets.get("Liquid"))
        if monthly_spending <= 0:
            monthly_spending = _as_float(client_context.get("annual_spending")) / 12.0
    months = bank / monthly_spending if monthly_spending else 0.0
    return {
        "months": round(months, 1),
        "years": f"{months / 12.0:.1f} yrs",
        "targetMonths": 12,
        "source": "bank_balance_and_activity_year_outflows",
    }


def _stress_payload(bands: Mapping[str, Any]) -> List[Dict[str, Any]]:
    net = _first_available(bands, "Net Worth")
    income = _first_available(bands, "Income")
    mortgage = _first_available(bands, "Mortgage Balance")
    investable = sum(_first_available(bands, column) for column in FINANCIAL_ASSET_COLUMNS if column != "Bank Balance")
    return [
        {"name": "Market -30%", "nw": _signed_money_short(-0.30 * investable), "cf": "--", "rw": "-1.1 yr", "sev": True},
        {"name": "Income stops", "nw": _signed_money_short(-0.25 * income), "cf": _signed_money_short(-income / 12.0), "rw": "model", "sev": True},
        {"name": "Rates +2%", "nw": _signed_money_short(-0.02 * mortgage), "cf": _signed_money_short(-0.02 * mortgage / 12.0), "rw": "-0.3 yr", "sev": False},
        {"name": "Net worth -10%", "nw": _signed_money_short(-0.10 * net), "cf": "--", "rw": "--", "sev": False},
    ]


def _simulated_projection_payload(years: List[int], bands: Mapping[str, Any]) -> Dict[str, Any]:
    """Chart-ready 08 section, derived from saved cashflow net-worth bands."""
    median_values = _series(bands, "Net Worth", P50)
    lower_values = _series(bands, "Net Worth", P10)
    upper_values = _series(bands, "Net Worth", P90)
    return {
        "source": "cashflow_model_net_worth_bands",
        "years": years,
        "median": [_k(value) for value in median_values],
        "lower": [_k(value) for value in lower_values],
        "upper": [_k(value) for value in upper_values],
        "base": _money_short(_at(median_values, -1)),
        "lowerCase": _money_short(_at(lower_values, -1)),
        "higherCase": _money_short(_at(upper_values, -1)),
    }


def _where_money_goes_payload(bands: Mapping[str, Any], *, index: int = 0) -> List[Dict[str, Any]]:
    rows = []
    total = 0.0
    for column in OUTFLOW_COLUMNS:
        values = _series(bands, column, P50)
        value = _at(values, index)
        if value > 0:
            rows.append({"label": column, "value": round(value, 2)})
            total += value
    for row in rows:
        row["pct"] = round(row["value"] / total * 100.0, 1) if total else 0
    return sorted(rows, key=lambda row: row["value"], reverse=True)


def _composition_rows(bands: Mapping[str, Any], columns: Iterable[str]) -> List[Dict[str, Any]]:
    rows = []
    total = 0.0
    for column in columns:
        value = _first_available(bands, column)
        if value > 0:
            rows.append({"label": _clean_label(column), "value": value})
            total += value
    for row in rows:
        row["pct"] = round(row["value"] / total * 100.0) if total else 0
        row["c"] = _color_for_label(row["label"])
    return sorted(rows, key=lambda row: row["value"], reverse=True)


def _context_balance_sheet(
    client_context: Optional[Mapping[str, Any]],
) -> tuple[Dict[str, float], Dict[str, float]]:
    if not isinstance(client_context, Mapping):
        return {}, {}
    balance_sheet = client_context.get("balance_sheet")
    if not isinstance(balance_sheet, Mapping):
        return {}, {}
    return (
        _named_positive_values(balance_sheet.get("assets")),
        _named_positive_values(balance_sheet.get("liabilities")),
    )


def _named_positive_values(value: Any) -> Dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(label): amount
        for label, raw_amount in value.items()
        if (amount := _as_float(raw_amount)) > 0
    }


def _amortized_monthly_payment(balance: float, annual_rate: float, term_years: float) -> float:
    months = int(round(term_years * 12))
    if balance <= 0 or months <= 0:
        return 0.0
    monthly_rate = annual_rate / 12.0
    if monthly_rate <= 0:
        return balance / months
    factor = (1.0 + monthly_rate) ** months
    return balance * monthly_rate * factor / (factor - 1.0)


def _rows_from_named_values(values: Mapping[str, float]) -> List[Dict[str, Any]]:
    total = sum(values.values())
    rows = [
        {
            "label": str(label),
            "value": round(value, 2),
            "pct": int(round(value / total * 100.0)) if total else 0,
            "c": _color_for_label(str(label)),
        }
        for label, value in values.items()
        if value > 0
    ]
    return sorted(rows, key=lambda row: row["value"], reverse=True)


def _preferred_or_sum(
    bands: Mapping[str, Any],
    preferred: str,
    fallback_columns: Iterable[str],
    percentile: str,
) -> List[float]:
    values = _series(bands, preferred, percentile)
    if values:
        return values
    total: List[float] = []
    for column in fallback_columns:
        values = _series(bands, column, percentile)
        if not values:
            continue
        if not total:
            total = [0.0 for _ in values]
        for idx, value in enumerate(values):
            if idx < len(total):
                total[idx] += value
    return total


def _first_available(
    bands: Mapping[str, Any],
    column: str,
    *,
    percentile: str = P50,
    sum_columns: Iterable[str] = (),
) -> float:
    values = _series(bands, column, percentile)
    if values:
        return _at(values, 0)
    if sum_columns:
        return sum(_first_available(bands, item, percentile=percentile) for item in sum_columns)
    return 0.0


def _first_non_zero(
    bands: Mapping[str, Any],
    column: str,
    *,
    percentile: str = P50,
) -> float:
    return _first_non_zero_value(_series(bands, column, percentile))


def _first_non_zero_value(values: List[float]) -> float:
    for value in values:
        if value > 0:
            return value
    return _at(values, 0)


def _payoff_year(years: List[int], values: List[float]) -> Optional[int]:
    if not values or _first_non_zero_value(values) <= 0:
        return None
    for index, value in enumerate(values):
        if index == 0:
            continue
        if value <= 0 and index < len(years):
            return years[index]
    return None


def _series(bands: Mapping[str, Any], metric: str, percentile: str) -> List[float]:
    metric_bands = bands.get(metric)
    if not isinstance(metric_bands, Mapping):
        return []
    values = metric_bands.get(percentile)
    if not isinstance(values, list):
        return []
    return [_as_float(value) for value in values]


def _unwrap_result(cashflow_result: Mapping[str, Any]) -> Mapping[str, Any]:
    result = cashflow_result.get("result")
    if isinstance(result, Mapping):
        return result
    return cashflow_result


def _artifact(title: str, sections: List[Dict[str, Any]], *, raw_source: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "artifact_type": "projection",
        "title": title,
        "schema_version": "cashflow_projection.v3",
        "generation_source": "real_cashflow_engine",
        "sections": sections,
        "section_ids": [section["section_id"] for section in sections],
        "engine_run": {
            "domain": "projection",
            "engine_name": "cashflow-model",
            "engine_version": str(((raw_source.get("metadata") or {}) if isinstance(raw_source.get("metadata"), Mapping) else {}).get("version") or "unknown"),
            "status": "succeeded" if raw_source.get("percentile_bands") else "not_available",
        },
    }


def _section(section_id: str, title: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"section_id": section_id, "title": title, "payload": payload}


def _index_for_horizon(years: List[int], horizon_years: int) -> int:
    if not years:
        return 0
    if len(set(years)) <= 1:
        return min(horizon_years, max(len(years) - 1, 0))
    target = years[0] + horizon_years
    distances = [abs(year - target) for year in years]
    return distances.index(min(distances))


def _bands_series_length(bands: Mapping[str, Any]) -> int:
    for column in ("Net Worth", "Bank Balance", "Total Assets"):
        values = _series(bands, column, P50)
        if values:
            return len(values)
    for column_bands in bands.values():
        if not isinstance(column_bands, Mapping):
            continue
        for series in column_bands.values():
            if isinstance(series, list) and series:
                return len(series)
    return 0


def _normalize_years(raw_years: Iterable[Any], *, series_length: int) -> List[int]:
    parsed: List[Optional[int]] = []
    for value in raw_years:
        if value is None or value == "":
            parsed.append(None)
            continue
        try:
            parsed.append(int(value))
        except (TypeError, ValueError):
            parsed.append(None)
    length = series_length or len(parsed)
    if length <= 0:
        return []
    if not parsed or all(value is None for value in parsed) or len(set(v for v in parsed if v is not None)) <= 1:
        return list(range(length))
    years = [(0 if value is None else value) for value in parsed]
    if len(years) < length:
        start = years[-1] + 1 if years else 0
        years.extend(range(start, start + (length - len(years))))
    return years[:length]


def _at(values: List[float], index: int) -> float:
    if not values:
        return 0.0
    try:
        return float(values[index])
    except IndexError:
        return float(values[-1])


def _k(value: float) -> float:
    return round(value / 1000.0, 4)


def _k_month(value: float) -> float:
    return round(value / 12.0 / 1000.0, 4)


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _money_short(value: float) -> str:
    sign = "-" if value < 0 else ""
    abs_value = abs(float(value))
    if abs_value >= 1_000_000:
        return f"{sign}${abs_value / 1_000_000:.2f}M"
    if abs_value >= 1_000:
        return f"{sign}${abs_value / 1_000:.0f}K"
    return f"{sign}${abs_value:.0f}"


def _signed_money_short(value: float) -> str:
    return _money_short(value) if value < 0 else f"+{_money_short(value)}"


def _percent(value: float) -> str:
    return f"{value * 100.0:.0f}%"


def _rate_percent(value: float) -> str:
    return f"{value * 100.0:.2f}".rstrip("0").rstrip(".") + "%"


def _clean_label(column: str) -> str:
    return (
        column.replace(" Balance", "")
        .replace("401k", "401k")
        .replace("529", "529")
    )


def _color_for_label(label: str) -> str:
    text = label.lower()
    if "liquid" in text:
        return "#78909C"
    if "invested" in text or "investment" in text:
        return "#43A4F4"
    if text == "real" or "real asset" in text:
        return "#38AEBF"
    if "bank" in text or "cash" in text:
        return "#C8C4D2"
    if "bond" in text or "debt" in text:
        return "#8E8B98"
    if "529" in text:
        return "#C9F227"
    if "retirement" in text or "401k" in text or "ira" in text:
        return "#6E5BD0"
    if "home" in text or "real asset" in text:
        return "#2F7D92"
    return "#14121A"
