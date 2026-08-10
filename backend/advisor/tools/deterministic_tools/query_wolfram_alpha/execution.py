"""Bounded Wolfram|Alpha adapter for de-identified pure mathematics."""

from __future__ import annotations

import hashlib
import json
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Mapping, Optional

import requests


WOLFRAM_ALPHA_URL = "https://api.wolframalpha.com/v2/query"
DEFAULT_TIMEOUT_SECONDS = 8.0
MIN_TIMEOUT_SECONDS = 1.0
MAX_TIMEOUT_SECONDS = 15.0
MAX_QUERY_CHARS = 300
MAX_RESPONSE_BYTES = 64 * 1024
MAX_RESULT_SIGNIFICANT_DIGITS = 100
MAX_RESULT_DECIMAL_EXPONENT = 1000

_SUPPORTED_UNITS = {
    "unitless",
    "decimal",
    "percentage",
    "count",
    "years",
    "months",
}
_ALLOWED_WORDS = {
    "abs",
    "absolute",
    "acos",
    "approximately",
    "asin",
    "assuming",
    "atan",
    "calculate",
    "complex",
    "compute",
    "convert",
    "cos",
    "cosh",
    "decimal",
    "derivative",
    "differentiate",
    "evaluate",
    "exp",
    "expand",
    "factor",
    "find",
    "for",
    "from",
    "gcd",
    "inf",
    "infinity",
    "integer",
    "integers",
    "integral",
    "integrate",
    "into",
    "lcm",
    "limit",
    "ln",
    "log",
    "max",
    "mean",
    "median",
    "min",
    "mod",
    "modulo",
    "month",
    "months",
    "negative",
    "numeric",
    "numerically",
    "of",
    "percent",
    "percentage",
    "pi",
    "positive",
    "product",
    "real",
    "respect",
    "root",
    "roots",
    "simplify",
    "sin",
    "sinh",
    "solve",
    "sqrt",
    "standard",
    "sum",
    "tan",
    "tanh",
    "to",
    "value",
    "variance",
    "where",
    "with",
    "year",
    "years",
}
_PRIVACY_MARKERS = re.compile(
    r"\b(?:account|address|advisor|age|allocation|analysis|beneficiary|benefit|"
    r"birth|brokerage|cash[ -]?flow|client|customer|date of birth|debt|dob|email|"
    r"household|income|insurance|investment|ira|loan|mortgage|my|name|our|phone|"
    r"portfolio|projection|retirement|roth|salary|social security|spending|ssn|"
    r"tax|taxes|user|wealth|withdrawal|401k)\b",
    re.IGNORECASE,
)
_CURRENCY_MARKERS = re.compile(
    r"(?:[$€£¥]|\b(?:aud|cad|chf|cny|dollar|dollars|eur|euro|euros|gbp|jpy|"
    r"pound|pounds|usd|yen|yuan)\b)",
    re.IGNORECASE,
)
_IDENTIFIER_MARKERS = re.compile(
    r"(?:https?://|www\.|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|"
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-"
    r"[0-9a-f]{12}\b|\b(?:analysis|cashflow|allocation)[_:-][A-Za-z0-9_-]+\b|"
    r"\b\d{3}[- .]\d{2}[- .]\d{4}\b|\b\d{3}[- .]\d{3}[- .]\d{4}\b|"
    r"\b\d{1,3}(?:,\d{3})+\b|\b\d+(?:\.\d+)?[eE][+-]?\d+\b|\b\d{4,}\b)",
    re.IGNORECASE,
)
_DATE_MARKERS = re.compile(
    r"\b(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}\b|"
    r"\b\d{1,2}[-/]\d{1,2}[-/](?:19|20)?\d{2}\b"
)
_ALLOWED_QUERY_CHARS = re.compile(r"^[A-Za-z0-9_+\-*/^().,=<>!% \[\]{}|]+$")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER = (
    r"[+-]?(?:(?:\d{1,3}(?:,\d{3})+)|\d+)(?:\.\d+)?"
    r"(?:[eE][+-]?\d+)?"
)


def query_wolfram_alpha(
    arguments: Mapping[str, Any],
    *,
    http_session: Optional[Any] = None,
    env_getter: Callable[[str, str], Any] = os.getenv,
) -> Dict[str, Any]:
    """Execute one validated Full Results query and return a scalar result.

    ``http_session`` and ``env_getter`` are injected seams for unit tests and
    production composition. The function never returns the AppID or provider
    exception text.
    """

    if not isinstance(arguments, Mapping) or set(arguments) - {
        "query",
        "expected_unit",
    }:
        return _failure("wolfram_alpha_arguments_invalid")

    expected_unit = str(arguments.get("expected_unit") or "").strip()
    if expected_unit not in _SUPPORTED_UNITS:
        return _failure("wolfram_alpha_expected_unit_invalid")

    normalized_query, query_error = _validated_query(arguments.get("query"))
    if query_error:
        return _failure(query_error)
    query_hash = _query_hash(normalized_query)

    mode = str(env_getter("AWM_WOLFRAM_ALPHA_MODE", "off") or "").strip().lower()
    if mode != "live":
        return _failure("wolfram_alpha_disabled", query_hash=query_hash)
    app_id = str(env_getter("WOLFRAM_ALPHA_APP_ID", "") or "").strip()
    if not app_id:
        return _failure("wolfram_alpha_not_configured", query_hash=query_hash)

    timeout_seconds = _timeout_seconds(env_getter)
    session = http_session or requests.Session()
    try:
        session.trust_env = False
        response = session.get(
            WOLFRAM_ALPHA_URL,
            params={
                "appid": app_id,
                "input": normalized_query,
                "output": "json",
                "format": "plaintext",
                "reinterpret": "false",
                "scantimeout": str(min(timeout_seconds, 5.0)),
                "podtimeout": str(min(timeout_seconds, 4.0)),
                "formattimeout": str(min(timeout_seconds, 5.0)),
            },
            headers={"Accept": "application/json"},
            timeout=timeout_seconds,
            allow_redirects=False,
        )
    except requests.Timeout:
        return _failure("wolfram_alpha_timeout", query_hash=query_hash)
    except requests.RequestException:
        return _failure("wolfram_alpha_provider_unavailable", query_hash=query_hash)
    except Exception:
        return _failure("wolfram_alpha_provider_unavailable", query_hash=query_hash)

    status_code = _status_code(response)
    if status_code != 200:
        return _failure(_provider_error(status_code), query_hash=query_hash)
    if _declared_response_too_large(response):
        return _failure("wolfram_alpha_response_invalid", query_hash=query_hash)
    try:
        payload = response.json()
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    except Exception:
        return _failure("wolfram_alpha_response_invalid", query_hash=query_hash)
    if len(encoded) > MAX_RESPONSE_BYTES:
        return _failure("wolfram_alpha_response_invalid", query_hash=query_hash)

    parsed, parse_error = _parse_full_results(payload, expected_unit=expected_unit)
    if parse_error:
        return _failure(parse_error, query_hash=query_hash)

    warnings = [
        (
            "This result came from an external Wolfram|Alpha computation. AWM "
            "validated only that it is one finite scalar with the declared unit."
        ),
        (
            "Reporting only: this result does not establish feasibility, suitability, "
            "tax treatment, model validity, or a recommendation."
        ),
    ]
    full_result = {
        "schema_version": "awm.wolfram_alpha_calculation.v1",
        "status": "complete",
        "operation": "external_pure_math_scalar",
        "query": {
            "sha256": query_hash,
            "input_interpretation_sha256": parsed[
                "input_interpretation_sha256"
            ],
        },
        "result": {
            "value_decimal": parsed["value_decimal"],
            "value": parsed["value_decimal"],
            "display_value": parsed["display_value"],
            "unit": expected_unit,
        },
        "validation": {
            "input_unambiguous": True,
            "finite_scalar": True,
            "declared_unit_matched": True,
            "scope": "finite_scalar_and_declared_unit_only",
        },
        "attribution": {
            "provider": "Wolfram|Alpha",
            "product": "Wolfram|Alpha Full Results API",
            "url": "https://www.wolframalpha.com/",
        },
        "provider_metadata": {
            "query_id": parsed["query_id"],
            "api_version": parsed["api_version"],
        },
        "warnings": warnings,
        "calculation_policy": (
            "External, de-identified pure-math fallback. Provider interpretation and "
            "mathematical correctness were not independently reproduced locally."
        ),
    }
    return {"success": True, "full_result": full_result}


def _validated_query(value: Any) -> tuple[str, Optional[str]]:
    if not isinstance(value, str):
        return "", "wolfram_alpha_query_invalid"
    if any(char in value for char in "\r\n\t"):
        return "", "wolfram_alpha_query_invalid"
    query = " ".join(value.split())
    if not query or len(query) > MAX_QUERY_CHARS or not query.isascii():
        return "", "wolfram_alpha_query_invalid"
    if (
        _PRIVACY_MARKERS.search(query)
        or _CURRENCY_MARKERS.search(query)
        or _IDENTIFIER_MARKERS.search(query)
        or _DATE_MARKERS.search(query)
    ):
        return "", "wolfram_alpha_query_sensitive"
    if not _ALLOWED_QUERY_CHARS.fullmatch(query):
        return "", "wolfram_alpha_query_sensitive"
    for token in _WORD.findall(query):
        lowered = token.lower()
        if lowered in _ALLOWED_WORDS:
            continue
        if re.fullmatch(r"[a-z](?:\d{1,2})?", lowered):
            continue
        return "", "wolfram_alpha_query_not_pure_math"
    return query, None


def _parse_full_results(
    payload: Any,
    *,
    expected_unit: str,
) -> tuple[Optional[Dict[str, str]], Optional[str]]:
    if not isinstance(payload, dict):
        return None, "wolfram_alpha_response_invalid"
    query_result = payload.get("queryresult")
    if not isinstance(query_result, dict):
        return None, "wolfram_alpha_response_invalid"
    if query_result.get("success") is not True or query_result.get("error") is True:
        return None, "wolfram_alpha_query_unavailable"
    if _provider_result_is_ambiguous(query_result):
        return None, "wolfram_alpha_result_ambiguous"

    pods = _object_list(query_result.get("pods"))
    if not pods:
        return None, "wolfram_alpha_result_missing"
    input_pod = _single_input_pod(pods)
    if input_pod is None:
        return None, "wolfram_alpha_input_interpretation_missing"
    input_texts = _pod_plaintexts(input_pod)
    if len(input_texts) != 1:
        return None, "wolfram_alpha_input_interpretation_missing"

    primary = [pod for pod in pods if pod.get("primary") is True]
    if len(primary) > 1:
        return None, "wolfram_alpha_result_ambiguous"

    # Full Results commonly returns an exact symbolic primary pod (for
    # example, ``sqrt(2)``) and a separate decimal-approximation pod. Accept
    # that shape only when all parseable result pods resolve to one scalar.
    # Conflicting numeric pods fail closed instead of selecting one by rank.
    candidate_pods: list[Dict[str, Any]] = list(primary)
    for pod in pods:
        if pod in candidate_pods:
            continue
        if str(pod.get("id") or "").lower() in {
            "result",
            "decimalapproximation",
            "decimalform",
        }:
            candidate_pods.append(pod)
    if not candidate_pods:
        return None, "wolfram_alpha_result_missing"

    parsed_results: list[tuple[Decimal, str]] = []
    for pod in candidate_pods:
        result_texts = _pod_plaintexts(pod)
        if len(result_texts) != 1:
            return None, "wolfram_alpha_result_ambiguous"
        decimal_value = _parse_scalar(
            result_texts[0],
            expected_unit=expected_unit,
        )
        if decimal_value is not None:
            parsed_results.append((decimal_value, result_texts[0]))
    if not parsed_results:
        return None, "wolfram_alpha_scalar_or_unit_invalid"
    distinct_values = {
        _canonical_decimal(decimal_value)
        for decimal_value, _result_text in parsed_results
    }
    if len(distinct_values) != 1:
        return None, "wolfram_alpha_result_ambiguous"
    decimal_value, result_text = parsed_results[0]
    return {
        "value_decimal": _canonical_decimal(decimal_value),
        "display_value": result_text,
        "input_interpretation_sha256": _query_hash(input_texts[0]),
        "query_id": _bounded_provider_value(query_result.get("id")),
        "api_version": _bounded_provider_value(query_result.get("version")),
    }, None


def _provider_result_is_ambiguous(query_result: Mapping[str, Any]) -> bool:
    if str(query_result.get("timedout") or "").strip():
        return True
    if str(query_result.get("timedoutpods") or "").strip():
        return True
    for key in (
        "assumptions",
        "didyoumeans",
        "futuretopic",
        "languagemsg",
        "tips",
        "warnings",
    ):
        if _nonempty_provider_value(query_result.get(key)):
            return True
    return False


def _nonempty_provider_value(value: Any) -> bool:
    if value in (None, "", False):
        return False
    if isinstance(value, (list, tuple)):
        return bool(value)
    if isinstance(value, dict):
        count = value.get("count", value.get("@count"))
        if count in (0, "0") and len(value) == 1:
            return False
        return bool(value)
    return True


def _single_input_pod(pods: list[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    exact = [pod for pod in pods if str(pod.get("id") or "").lower() == "input"]
    if len(exact) == 1:
        return exact[0]
    candidates = [
        pod
        for pod in pods
        if str(pod.get("id") or "").lower() == "inputinterpretation"
        or str(pod.get("title") or "").lower().startswith("input")
    ]
    return candidates[0] if len(candidates) == 1 else None


def _pod_plaintexts(pod: Mapping[str, Any]) -> list[str]:
    texts = []
    for subpod in _object_list(pod.get("subpods")):
        text = " ".join(str(subpod.get("plaintext") or "").split())
        if text:
            texts.append(text)
    return texts


def _object_list(value: Any) -> list[Dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _parse_scalar(text: str, *, expected_unit: str) -> Optional[Decimal]:
    normalized = " ".join(str(text or "").split())
    suffix = ""
    if expected_unit == "percentage":
        suffix = r"\s*(?:%|percent|percentage)"
    elif expected_unit == "years":
        suffix = r"\s*(?:year|years)"
    elif expected_unit == "months":
        suffix = r"\s*(?:month|months)"
    assignment = r"(?:[A-Za-z](?:\d{1,2})?\s*(?:=|\u2248)\s*)?"
    match = re.fullmatch(
        rf"{assignment}({_NUMBER}){suffix}",
        normalized,
        re.IGNORECASE,
    )
    if match is None:
        return None
    try:
        value = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    if (
        len(value.as_tuple().digits) > MAX_RESULT_SIGNIFICANT_DIGITS
        or abs(value.adjusted()) > MAX_RESULT_DECIMAL_EXPONENT
    ):
        return None
    if expected_unit == "count" and value != value.to_integral_value():
        return None
    return value


def _canonical_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _timeout_seconds(env_getter: Callable[[str, str], Any]) -> float:
    raw = env_getter(
        "AWM_WOLFRAM_ALPHA_TIMEOUT_SECONDS",
        str(DEFAULT_TIMEOUT_SECONDS),
    )
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT_SECONDS
    if value != value:
        value = DEFAULT_TIMEOUT_SECONDS
    return min(MAX_TIMEOUT_SECONDS, max(MIN_TIMEOUT_SECONDS, value))


def _query_hash(query: str) -> str:
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _status_code(response: Any) -> int:
    value = getattr(response, "status_code", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _declared_response_too_large(response: Any) -> bool:
    headers = getattr(response, "headers", {})
    if not isinstance(headers, Mapping):
        return False
    try:
        return int(headers.get("Content-Length", 0) or 0) > MAX_RESPONSE_BYTES
    except (TypeError, ValueError):
        return False


def _provider_error(status_code: int) -> str:
    if status_code in {400, 501}:
        return "wolfram_alpha_query_unavailable"
    if status_code in {401, 403}:
        return "wolfram_alpha_authentication_failed"
    if status_code == 429:
        return "wolfram_alpha_rate_limited"
    return "wolfram_alpha_provider_unavailable"


def _bounded_provider_value(value: Any) -> str:
    return " ".join(str(value or "").split())[:160]


def _failure(error: str, *, query_hash: str = "") -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "success": False,
        "error": error,
        "retry_allowed": False,
    }
    if query_hash:
        result["query_hash"] = query_hash
    return result


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "WOLFRAM_ALPHA_URL",
    "query_wolfram_alpha",
]
