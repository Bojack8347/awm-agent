"""Build mobile section 5b data for investment proposal artifacts.

The formulas mirror data-spec's
``saa_mobile_sections_5b_5f_chart_function_reference.md`` but return a
structured payload for the React Native UI instead of chart PNG files.
"""

from __future__ import annotations

import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


FIXED_INCOME_TICKERS = {"GOVT", "LQDA", "BND", "BIL", "SGOV", "EMGA", "EMCR", "USHY"}
EQUITY_HINTS = ("equity", "stock")
FIXED_INCOME_HINTS = ("treasury", "bond", "fixed income", "debt")


def build_mobile_section_5b(policy_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a best-effort 5b proposal payload from real model/fund data.

    This never raises into the agent path. If the optional Excel enrichment
    workbooks are unavailable, the payload still contains model-derived summary
    cards, recommended securities, and allocation tables.
    """

    try:
        import pandas as pd  # type: ignore
    except Exception as exc:  # pragma: no cover - environment dependent
        return _minimal_payload(policy_payload, data_quality_error=f"pandas_unavailable: {exc}")

    fund_book = _load_fund_book(pd)
    saa_clusters = _load_saa_clusters(pd)
    policy = _dict(policy_payload.get("policy"))
    money_pool = _dict(policy_payload.get("money_pool"))
    analytics = _dict(policy_payload.get("portfolio_analytics"))
    securities = _normalise_securities(
        policy.get("recommended_securities") or policy_payload.get("recommended_securities") or [],
        total_amount=_num(money_pool.get("amount") or policy.get("capital_required")),
        fund_book=fund_book,
        saa_clusters=saa_clusters,
    )
    amount = _num(money_pool.get("amount") or policy.get("capital_required")) or _sum(row.get("Amount") for row in securities) or 0.0
    allocation = _asset_allocation(securities)
    active_passive = _active_passive_split(securities)
    equity_sector = _equity_sector_allocation(securities, fund_book)
    fixed_income = _fixed_income_sleeve(securities, fund_book, amount)
    history = _historical_performance(securities, fund_book, amount)
    stress = _stress_tests(history.get("_series"), amount)
    risk_summary = _risk_summary(policy_payload, analytics, history)
    risk_rules = _risk_management_policy(policy_payload)

    return {
        "sectionId": "5b",
        "status": "proposal",
        "title": "Investment Proposal",
        "subtitle": str(policy.get("title") or policy_payload.get("title") or "Investment proposal"),
        "summaryCards": _summary_cards(policy_payload, analytics, money_pool, amount),
        "scopeAndPurpose": str(policy.get("scope_of_purpose") or "").strip(),
        "recommendedSecurities": securities,
        "assetAllocation": allocation,
        "activePassiveSplit": active_passive,
        "equitySectorAllocation": equity_sector,
        "riskSummary": risk_summary,
        "stressTests": stress,
        "fixedIncomeSleeve": fixed_income,
        "historicalPerformance": {k: v for k, v in history.items() if k != "_series"},
        "dataQuality": {
            "source": "asset_allocation_model_plus_fund_data",
            "fundDataLoaded": bool(fund_book),
            "saaClusterMapLoaded": bool(saa_clusters),
            "missingEnrichment": _missing_enrichment(securities, fund_book),
        },
        "riskManagementPolicy": risk_rules,
    }


def _minimal_payload(policy_payload: Dict[str, Any], *, data_quality_error: str) -> Dict[str, Any]:
    policy = _dict(policy_payload.get("policy"))
    analytics = _dict(policy_payload.get("portfolio_analytics"))
    money_pool = _dict(policy_payload.get("money_pool"))
    securities = _normalise_securities(policy.get("recommended_securities") or [], total_amount=_num(money_pool.get("amount")) or 0.0)
    amount = _num(money_pool.get("amount")) or _sum(row.get("Amount") for row in securities) or 0.0
    return {
        "sectionId": "5b",
        "status": "proposal",
        "title": "Investment Proposal",
        "subtitle": str(policy.get("title") or "Investment proposal"),
        "summaryCards": _summary_cards(policy_payload, analytics, money_pool, amount),
        "scopeAndPurpose": str(policy.get("scope_of_purpose") or "").strip(),
        "recommendedSecurities": securities,
        "assetAllocation": _asset_allocation(securities),
        "activePassiveSplit": _active_passive_split(securities),
        "riskSummary": _risk_summary(policy_payload, analytics, {}),
        "stressTests": [],
        "fixedIncomeSleeve": None,
        "historicalPerformance": None,
        "dataQuality": {"source": "asset_allocation_model_only", "error": data_quality_error},
        "riskManagementPolicy": _risk_management_policy(policy_payload),
    }


def _normalise_securities(
    rows: Iterable[Dict[str, Any]],
    *,
    total_amount: float,
    fund_book: Optional[Dict[str, Any]] = None,
    saa_clusters: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    passive_meta = _index_by(fund_book, "Passive_Metadata", "Ticker")
    active_meta = _index_by(fund_book, "Active_Metadata", "ISIN")
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or row.get("ticker") or row.get("recommended_security") or row.get("security") or "").strip()
        if not symbol:
            continue
        isin = str(row.get("isin") or row.get("ISIN") or row.get("ISIN/Ticker") or "").strip()
        lookup = isin or symbol
        vehicle_type = str(
            row.get("vehicle_type")
            or row.get("management_style")
            or row.get("security_type")
            or row.get("Vehicle Type")
            or ""
        ).strip().title()
        meta = passive_meta.get(symbol.upper())
        if meta is not None:
            vehicle_type = vehicle_type or "Passive"
        elif isin and isin in active_meta:
            meta = active_meta[isin]
            vehicle_type = vehicle_type or "Active"
        else:
            meta = None
            vehicle_type = vehicle_type or "Passive"
        weight = _num(row.get("weight"))
        if weight is None:
            weight = _num(row.get("percentage") or row.get("Portfolio Weight (%)"))
            weight = (weight / 100.0) if weight and abs(weight) > 1 else weight
        weight = float(weight or 0.0)
        amount = _num(row.get("amount") or row.get("notional") or row.get("Amount"))
        if amount is None and total_amount:
            amount = total_amount * weight
        asset_class = str(row.get("asset_class") or row.get("Asset Class") or "").strip()
        if meta is not None:
            asset_class = asset_class or str(meta.get("Asset Class III") or "").strip()
        asset_class = asset_class or "Unclassified"
        meta_row = meta if meta is not None else {}
        ticker = str(meta_row.get("Ticker") or row.get("ticker") or symbol).strip()
        name = str(meta_row.get("Name") or row.get("name") or row.get("security_name") or symbol).strip()
        out.append({
            "Asset Class": asset_class,
            "Cluster": (saa_clusters or {}).get(asset_class, _cluster_for(asset_class)),
            "Vehicle Type": vehicle_type,
            "ISIN/Ticker": lookup,
            "Ticker": ticker,
            "Name": name,
            "Benchmark": str(meta_row.get("Benchmark") or "").strip(),
            "Portfolio Weight (%)": round(weight * 100.0, 6),
            "Amount": round(float(amount or 0.0), 2),
        })
    return sorted(out, key=lambda x: float(x.get("Portfolio Weight (%)") or 0.0), reverse=True)


def _summary_cards(policy_payload: Dict[str, Any], analytics: Dict[str, Any], money_pool: Dict[str, Any], amount: float) -> List[Dict[str, Any]]:
    horizon = _num(money_pool.get("horizon_years") or _dict(policy_payload.get("policy")).get("horizon_years"))
    ret = _pct_value(analytics.get("expected_return"))
    vol = _pct_value(analytics.get("expected_volatility"))
    return [
        {"Label": "Investment", "Value": amount, "Display": _currency(amount)},
        {"Label": "Time horizon", "Value": horizon, "Display": f"{horizon:.0f}Y" if horizon else "—"},
        {"Label": "Expected return", "Value": ret, "Display": _percent_display(ret)},
        {"Label": "Expected volatility", "Value": vol, "Display": _percent_display(vol)},
    ]


def _asset_allocation(securities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[tuple[str, str], Dict[str, Any]] = {}
    for row in securities:
        key = (str(row.get("Cluster") or "Unmapped"), str(row.get("Asset Class") or "Unclassified"))
        item = grouped.setdefault(key, {"Cluster": key[0], "Asset Class": key[1], "Portfolio Weight (%)": 0.0, "Amount": 0.0})
        item["Portfolio Weight (%)"] += float(row.get("Portfolio Weight (%)") or 0.0)
        item["Amount"] += float(row.get("Amount") or 0.0)
    return sorted((_round_record(v) for v in grouped.values()), key=lambda x: float(x["Portfolio Weight (%)"]), reverse=True)


def _active_passive_split(securities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in securities:
        asset = str(row.get("Asset Class") or "Unclassified")
        item = grouped.setdefault(asset, {"Asset Class": asset, "Active": 0.0, "Passive": 0.0, "Total": 0.0})
        vehicle = "Active" if str(row.get("Vehicle Type") or "").lower() == "active" else "Passive"
        weight = float(row.get("Portfolio Weight (%)") or 0.0)
        item[vehicle] += weight
        item["Total"] += weight
    return sorted((_round_record(v) for v in grouped.values()), key=lambda x: float(x["Total"]), reverse=True)


def _equity_sector_allocation(securities: List[Dict[str, Any]], fund_book: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not fund_book or "Equity_Sectors" not in fund_book:
        return []
    sector_df = fund_book["Equity_Sectors"]
    equity = [s for s in securities if any(h in str(s.get("Asset Class") or "").lower() for h in EQUITY_HINTS)]
    total_equity = _sum(s.get("Portfolio Weight (%)") for s in equity)
    if not total_equity:
        return []
    grouped: Dict[str, float] = {}
    for sec in equity:
        ticker = str(sec.get("Ticker") or "").upper()
        rows = sector_df[sector_df["Ticker"].astype(str).str.upper() == ticker]
        sleeve_share = float(sec.get("Portfolio Weight (%)") or 0.0) / total_equity
        for _, row in rows.iterrows():
            sector = str(row.get("Sector") or "").strip()
            pct = _num(row.get("Weight %")) or 0.0
            if sector:
                grouped[sector] = grouped.get(sector, 0.0) + sleeve_share * pct
    return [{"Sector": k, "Weight (%)": round(v, 4)} for k, v in sorted(grouped.items(), key=lambda x: x[1], reverse=True)]


def _fixed_income_sleeve(securities: List[Dict[str, Any]], fund_book: Optional[Dict[str, Any]], amount: float) -> Optional[Dict[str, Any]]:
    fixed = [
        s for s in securities
        if str(s.get("Ticker") or "").upper() in FIXED_INCOME_TICKERS
        or any(h in str(s.get("Asset Class") or "").lower() for h in FIXED_INCOME_HINTS)
    ]
    sleeve_weight = _sum(s.get("Portfolio Weight (%)") for s in fixed)
    if not fixed or not sleeve_weight:
        return None
    metrics = _index_by(fund_book, "FI_Metrics", "Ticker")
    holdings = []
    weighted_duration = 0.0
    weighted_ytm = 0.0
    for sec in fixed:
        ticker = str(sec.get("Ticker") or "").upper()
        share = float(sec.get("Portfolio Weight (%)") or 0.0) / sleeve_weight
        metric = metrics.get(ticker, {})
        duration = _num(metric.get("Duration"))
        ytm = _pct_value(metric.get("YTM"))
        if duration is not None:
            weighted_duration += share * duration
        if ytm is not None:
            weighted_ytm += share * ytm
        holdings.append({**sec, "Sleeve Share (%)": round(share * 100.0, 6), "Duration": duration, "YTM (%)": ytm})
    credit = _credit_quality(fixed, fund_book, sleeve_weight)
    credit_score = _weighted_credit_score(credit)
    issuer = _issuer_mix(fixed, fund_book, sleeve_weight)
    return {
        "title": f"{', '.join(str(s.get('Ticker')) for s in fixed[:3])} represent {sleeve_weight:.1f}% of the portfolio.",
        "tickers": [str(s.get("Ticker")) for s in fixed],
        "summaryCards": [
            {"Metric": "Sleeve weight", "Value": sleeve_weight, "Display": _percent_display(sleeve_weight)},
            {"Metric": "Sleeve size", "Value": amount * sleeve_weight / 100.0, "Display": _currency(amount * sleeve_weight / 100.0)},
            {"Metric": "Yield to maturity", "Value": weighted_ytm, "Display": _percent_display(weighted_ytm)},
            {"Metric": "Duration", "Value": weighted_duration, "Display": f"{weighted_duration:.2f}"},
            {"Metric": "Average credit label", "Value": _credit_label(credit_score), "Display": _credit_label(credit_score)},
        ],
        "holdings": holdings,
        "issuerMix": issuer,
        "creditQuality": credit,
    }


def _historical_performance(securities: List[Dict[str, Any]], fund_book: Optional[Dict[str, Any]], amount: float) -> Dict[str, Any]:
    series = _portfolio_history(securities, fund_book)
    if series is None or len(series) < 30:
        return {"title": "Historical Performance", "growthBaseAmount": amount, "periodReturns": [], "metrics": []}
    returns = []
    metrics = []
    for years in (3, 5, 10):
        ret = _annualized_return(series, years)
        returns.append({"Period": f"{years}Y", "Recommended Mix Return (% p.a.)": ret, "Policy Benchmark Return (% p.a.)": None})
        if ret is not None:
            metrics.append({"Metric": f"{years}Y return", "Value": ret, "Unit": "% p.a."})
    daily = series.pct_change().dropna()
    vol = float(daily.std() * math.sqrt(252) * 100.0) if len(daily) else None
    dd = _max_drawdown(series)
    if vol is not None:
        metrics.append({"Metric": "Historical annualized volatility", "Value": vol, "Unit": "%"})
    if dd is not None:
        metrics.append({"Metric": "Max drawdown", "Value": dd, "Unit": "%"})
    sampled = _sample_series(series, amount)
    return {"title": "Historical Performance", "growthBaseAmount": amount, "periodReturns": returns, "metrics": metrics, "growthSeries": sampled, "_series": series}


def _stress_tests(series: Any, amount: float) -> List[Dict[str, Any]]:
    if series is None or len(series) < 30:
        return []
    scenarios = [
        ("COVID shock", "2020-02-19", "2020-03-23"),
        ("2022 rate shock", "2022-01-03", "2022-10-14"),
        ("Recent 12 months", "2025-06-30", "2026-06-30"),
    ]
    out = []
    for name, start, end in scenarios:
        ret = _return_between(series, start, end)
        out.append({"Scenario": name, "Start": start, "End": end, "Return (%)": ret, "Coverage": "available" if ret is not None else "not enough history"})
    rolling = series.pct_change(21).dropna()
    if len(rolling):
        idx = rolling.idxmin()
        out.append({"Scenario": "Worst rolling 1-month", "Start": str(idx.date()), "End": str(idx.date()), "Return (%)": float(rolling.min() * 100.0), "Coverage": "available"})
    return out


def _risk_summary(policy_payload: Dict[str, Any], analytics: Dict[str, Any], history: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = [
        {"Metric": "Portfolio expected return", "Value": _pct_value(analytics.get("expected_return")), "Unit": "%", "Source": "Asset allocation model"},
        {"Metric": "Portfolio expected volatility", "Value": _pct_value(analytics.get("expected_volatility")), "Unit": "%", "Source": "Asset allocation model"},
    ]
    for metric in history.get("metrics") or []:
        rows.append({"Metric": metric["Metric"], "Value": metric["Value"], "Unit": metric.get("Unit", ""), "Source": "Weighted historical prices/NAVs"})
    return [row for row in rows if row.get("Value") is not None]


def _risk_management_policy(policy_payload: Dict[str, Any]) -> List[str]:
    """Read monitoring / risk-management rules produced upstream.

    The mobile UI must not invent portfolio rules. These should come from the
    investment-solution agent/model payload, an approved policy template, or a
    conversation-derived policy object.
    """

    policy = _dict(policy_payload.get("policy"))
    candidates = (
        policy.get("risk_management_policy"),
        policy.get("riskManagementPolicy"),
        policy.get("monitoring_rules"),
        policy.get("monitoringRules"),
        policy_payload.get("risk_management_policy"),
        policy_payload.get("riskManagementPolicy"),
        policy_payload.get("monitoring_rules"),
        policy_payload.get("monitoringRules"),
    )
    for value in candidates:
        rules = _string_list(value)
        if rules:
            return rules
    guardrails = _dict(policy.get("guardrails") or policy_payload.get("guardrails"))
    rules: List[str] = []
    for key in ("rebalance", "liquidity", "tax", "escalation", "review"):
        text = str(guardrails.get(key) or "").strip()
        if text:
            rules.append(text)
    return rules


def _string_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [line.strip(" -•\t") for line in value.splitlines() if line.strip(" -•\t")]
    return []


@lru_cache(maxsize=1)
def _load_fund_book(pd: Any) -> Optional[Dict[str, Any]]:
    path = _find_file("AWM_FUND_DATA_WORKBOOK", "Fund_Data_*.xlsx")
    if not path:
        return None
    sheets = ["Passive_Metadata", "Active_Metadata", "Passive_Prices_IBKR", "Active_Prices_Official", "Equity_Sectors", "FI_Metrics", "FI_Credit_Ratings", "FI_Issuer_Sector"]
    try:
        loaded = pd.read_excel(path, sheet_name=sheets)
        return loaded if isinstance(loaded, dict) else None
    except Exception:
        return None


@lru_cache(maxsize=1)
def _load_saa_clusters(pd: Any) -> Dict[str, str]:
    path = _find_file("AWM_SAA_RESULTS_WORKBOOK", "SAA_Results.xlsx")
    if not path:
        return {}
    try:
        df = pd.read_excel(path, sheet_name="Asset_Allocations")
        return {str(row["Asset"]).strip(): str(row["Cluster"]).strip() for _, row in df.iterrows() if not pd.isna(row.get("Asset"))}
    except Exception:
        return {}


def _find_file(env_name: str, pattern: str) -> Optional[Path]:
    explicit = os.getenv(env_name)
    if explicit and Path(explicit).exists():
        return Path(explicit)
    here = Path(__file__).resolve()
    repo = next((parent for parent in here.parents if (parent / "backend").is_dir()), here.parents[0])
    roots = [
        repo / "backend" / "advisor" / "quant_models" / "asset_allocation_model" / "SAA Model" / "outputs",
        repo.parent / "data-spec" / "data-spec",
        repo.parent / "data-spec" / "data-spec" / "saa_mobile_sections_chart_examples" / "generated_mobile_sections",
        Path.home() / "Downloads",
    ]
    candidates: List[Path] = []
    for root in roots:
        if not root.exists():
            continue
        candidates.extend(root.glob(pattern))
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def _portfolio_history(securities: List[Dict[str, Any]], fund_book: Optional[Dict[str, Any]]) -> Any:
    if not fund_book:
        return None
    try:
        import pandas as pd  # type: ignore
    except Exception:
        return None
    series_parts = []
    weights = []
    passive = fund_book.get("Passive_Prices_IBKR")
    active = fund_book.get("Active_Prices_Official")
    for sec in securities:
        ticker = str(sec.get("Ticker") or "").upper()
        isin = str(sec.get("ISIN/Ticker") or "").strip()
        vehicle = str(sec.get("Vehicle Type") or "").lower()
        df = None
        price_col = None
        if vehicle == "active" and active is not None:
            df = active[(active["ISIN"].astype(str) == isin) | (active["Ticker"].astype(str).str.upper() == ticker)]
            price_col = "NAV Adjusted If Distr" if "NAV Adjusted If Distr" in df.columns else "NAV per Share"
        elif passive is not None:
            df = passive[passive["Ticker"].astype(str).str.upper() == ticker]
            price_col = "Adjusted Price"
        if df is None or df.empty or price_col not in df.columns:
            continue
        s = pd.Series(pd.to_numeric(df[price_col], errors="coerce").values, index=pd.to_datetime(df["Date"], errors="coerce")).dropna()
        s = s[s > 0].sort_index()
        if len(s) < 30:
            continue
        series_parts.append(s / s.iloc[0])
        weights.append(float(sec.get("Portfolio Weight (%)") or 0.0) / 100.0)
    if not series_parts:
        return None
    aligned = pd.concat(series_parts, axis=1).sort_index().ffill()
    returns = aligned.pct_change()
    returns = returns.where(returns.abs() <= 0.50)
    weight_series = pd.Series(weights, index=returns.columns)
    usable_weight = returns.notna().mul(weight_series, axis=1).sum(axis=1)
    portfolio_returns = returns.mul(weight_series, axis=1).sum(axis=1) / usable_weight.replace(0, math.nan)
    portfolio_returns = portfolio_returns[usable_weight >= 0.65].dropna()
    if portfolio_returns.empty:
        return None
    return (1.0 + portfolio_returns).cumprod()


def _index_by(book: Optional[Dict[str, Any]], sheet: str, column: str) -> Dict[str, Any]:
    if not book or sheet not in book:
        return {}
    try:
        return {str(row[column]).strip().upper(): row for _, row in book[sheet].iterrows() if not _is_blank(row.get(column))}
    except Exception:
        return {}


def _credit_quality(fixed: List[Dict[str, Any]], fund_book: Optional[Dict[str, Any]], sleeve_weight: float) -> List[Dict[str, Any]]:
    if not fund_book or "FI_Credit_Ratings" not in fund_book:
        return []
    grouped: Dict[str, Dict[str, Any]] = {}
    df = fund_book["FI_Credit_Ratings"]
    for sec in fixed:
        ticker = str(sec.get("Ticker") or "").upper()
        sec_share = float(sec.get("Portfolio Weight (%)") or 0.0) / sleeve_weight
        for _, row in df[df["Ticker"].astype(str).str.upper() == ticker].iterrows():
            rating = _rating(str(row.get("Credit Rating") or "Not Rated"))
            item = grouped.setdefault(rating, {"Credit Rating": rating, "Score": _credit_score(rating), "Sleeve Weight (%)": 0.0})
            item["Sleeve Weight (%)"] += sec_share * (_num(row.get("Weight %")) or 0.0)
    return sorted((_round_record(v) for v in grouped.values()), key=lambda x: (x.get("Score") is None, x.get("Score") or 99))


def _issuer_mix(fixed: List[Dict[str, Any]], fund_book: Optional[Dict[str, Any]], sleeve_weight: float) -> List[Dict[str, Any]]:
    if not fund_book or "FI_Issuer_Sector" not in fund_book:
        return []
    grouped: Dict[str, float] = {}
    df = fund_book["FI_Issuer_Sector"]
    for sec in fixed:
        ticker = str(sec.get("Ticker") or "").upper()
        sec_share = float(sec.get("Portfolio Weight (%)") or 0.0) / sleeve_weight
        for _, row in df[df["Ticker"].astype(str).str.upper() == ticker].iterrows():
            cat = _issuer_category(str(row.get("Issuer/Sector") or ""))
            grouped[cat] = grouped.get(cat, 0.0) + sec_share * (_num(row.get("Weight %")) or 0.0)
    return [{"Category": k, "Sleeve Weight (%)": round(v, 6)} for k, v in sorted(grouped.items(), key=lambda x: x[1], reverse=True)]


def _missing_enrichment(securities: List[Dict[str, Any]], fund_book: Optional[Dict[str, Any]]) -> List[str]:
    if not fund_book:
        return ["fund_data_workbook"]
    missing = []
    if any(any(h in str(s.get("Asset Class") or "").lower() for h in EQUITY_HINTS) for s in securities) and "Equity_Sectors" not in fund_book:
        missing.append("equity_sectors")
    if any(str(s.get("Ticker") or "").upper() in FIXED_INCOME_TICKERS for s in securities) and "FI_Metrics" not in fund_book:
        missing.append("fixed_income_metrics")
    return missing


def _annualized_return(series: Any, years: int) -> Optional[float]:
    if series is None or len(series) < 2:
        return None
    end = series.index.max()
    start_target = end - type(end)(year=end.year - years, month=end.month, day=end.day).to_pydatetime().utcoffset() if False else None
    try:
        start = series.index[series.index >= (end - __import__("pandas").Timedelta(days=365.25 * years))][0]
    except Exception:
        return None
    days = (end - start).days
    if days <= 0:
        return None
    return float(((series.loc[end] / series.loc[start]) ** (365.25 / days) - 1.0) * 100.0)


def _return_between(series: Any, start: str, end: str) -> Optional[float]:
    try:
        import pandas as pd  # type: ignore
        s_idx = series.index[series.index >= pd.Timestamp(start)][0]
        e_idx = series.index[series.index <= pd.Timestamp(end)][-1]
        return float((series.loc[e_idx] / series.loc[s_idx] - 1.0) * 100.0)
    except Exception:
        return None


def _sample_series(series: Any, amount: float) -> List[Dict[str, Any]]:
    if series is None or len(series) == 0:
        return []
    step = max(1, len(series) // 40)
    sampled = series.iloc[::step]
    return [{"date": str(idx.date()), "value": round(float(value) * amount, 2)} for idx, value in sampled.items()]


def _max_drawdown(series: Any) -> Optional[float]:
    try:
        drawdown = series / series.cummax() - 1.0
        return float(drawdown.min() * 100.0)
    except Exception:
        return None


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).replace(",", "").replace("%", "").strip()
    if not text or text.lower() in {"nan", "none", "n/a"}:
        return None
    try:
        return float(text)
    except Exception:
        return None


def _pct_value(value: Any) -> Optional[float]:
    n = _num(value)
    if n is None:
        return None
    return n * 100.0 if abs(n) <= 1.0 else n


def _sum(values: Iterable[Any]) -> float:
    return sum(float(v) for v in (_num(v) for v in values) if v is not None)


def _currency(value: float) -> str:
    return f"${float(value or 0.0):,.0f}"


def _percent_display(value: Optional[float]) -> str:
    return "—" if value is None else f"{float(value):.2f}%"


def _round_record(record: Dict[str, Any]) -> Dict[str, Any]:
    return {k: (round(v, 6) if isinstance(v, float) and math.isfinite(v) else v) for k, v in record.items()}


def _is_blank(value: Any) -> bool:
    return value is None or str(value).strip() == "" or str(value).lower() == "nan"


def _cluster_for(asset_class: str) -> str:
    label = asset_class.strip()
    low = label.lower()
    if "cash" in low:
        return "Liquidity"
    if any(h in low for h in FIXED_INCOME_HINTS):
        return "Defensive Fixed Income"
    if any(h in low for h in EQUITY_HINTS):
        return "Equity"
    return label or "Unmapped"


def _rating(value: str) -> str:
    raw = value.upper().replace(" RATED", "").strip()
    if "CASH" in raw or "DERIVATIVE" in raw:
        return "Cash and/or derivatives"
    for rating in ("AAA", "AA", "A", "BBB", "BB", "B", "CCC", "CC", "C", "D"):
        if raw == rating or raw.startswith(rating + " "):
            return rating
    return "Not Rated"


def _credit_score(rating: str) -> Optional[float]:
    order = {"AAA": 1.0, "AA": 2.0, "A": 3.0, "BBB": 4.0, "BB": 5.0, "B": 6.0, "CCC": 7.0, "CC": 8.0, "C": 9.0, "D": 10.0}
    return order.get(rating)


def _weighted_credit_score(rows: List[Dict[str, Any]]) -> Optional[float]:
    valid = [r for r in rows if r.get("Score") is not None]
    denom = _sum(r.get("Sleeve Weight (%)") for r in valid)
    if not denom:
        return None
    return sum(float(r["Score"]) * float(r.get("Sleeve Weight (%)") or 0.0) for r in valid) / denom


def _credit_label(score: Optional[float]) -> str:
    if score is None:
        return "N/A"
    labels = [("AAA", 1), ("AA", 2), ("A", 3), ("BBB", 4), ("BB", 5), ("B", 6), ("CCC", 7), ("CC", 8), ("C", 9), ("D", 10)]
    return min(labels, key=lambda x: abs(x[1] - score))[0]


def _issuer_category(value: str) -> str:
    low = value.lower()
    if "treasury" in low or "government" in low or "sovereign" in low:
        return "Government"
    if "cash" in low or "derivative" in low:
        return "Cash and/or derivatives"
    return "Corporate IG"
