"""Filing text to facts. Numbers used later come from overrides when a batch fails.

`extract_facts(company, text, ref)` still works with three arguments. Pass an
object with `complete_json` to try an extraction; any schema or validation
failure returns that company's hand-checked overrides.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from airscen.types import Basis, DataError, Extraction, Fact, LLMError, Metric

log = logging.getLogger("airscen.extract")

PROMPT_VERSION = "extract_metrics_v1"
_TOLERANCE = 0.005
_REQUIRED = ("metric", "value", "unit", "basis", "period_end", "evidence")

_SCHEMA = {
    "type": "object",
    "required": ["facts"],
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": list(_REQUIRED),
                "properties": {
                    "metric": {"type": "string"},
                    "value": {"type": "number"},
                    "unit": {"type": "string"},
                    "basis": {"type": "string"},
                    "period_end": {"type": "string"},
                    "evidence": {"type": "string"},
                },
            },
        }
    },
}


def load_overrides(path=None) -> list[Fact]:
    """Hand-checked facts. `debt_noncurrent` lives under checks and is not loaded."""
    payload = _read_yaml(path)
    facts = []
    for row in payload.get("facts") or []:
        facts.append(_fact_from_override(row))
    return facts


def load_checks(path=None) -> list[dict]:
    """Identity inputs that are not metrics: noncurrent debt and the liability split."""
    payload = _read_yaml(path)
    checks = []
    for row in payload.get("checks") or []:
        checks.append({
            "company": row["company"],
            "period_end": _as_date(row["period_end"]),
            "basis": Basis(row.get("basis") or "GAAP"),
            "debt_noncurrent": _optional_float(row.get("debt_noncurrent")),
            "current_liabilities": _optional_float(row.get("current_liabilities")),
            "noncurrent_liabilities": _optional_float(row.get("noncurrent_liabilities")),
        })
    return checks


def validate(facts, evidence=None, checks=None, overrides=None) -> None:
    """Raise DataError E_VALIDATION_FAILED when any of the five rules fails.

    Rule 3 is checked before rule 5 so a number that is not in its evidence
    is reported as rule 3 even when it also disagrees with the override.
    `evidence` is one string for every fact, a list aligned with `facts`, or
    omitted when each item is a `(Fact, evidence)` pair.
    """
    rows, texts = _unpack(facts, evidence)
    if checks is None:
        checks = load_checks()
    if overrides is None:
        overrides = load_overrides()
    _rule_evidence(rows, texts)
    _rule_debt(rows, checks)
    _rule_balance_sheet(rows, checks)
    _rule_ranges(rows)
    _rule_matches_overrides(rows, overrides)


def extract_facts(company, text, ref, llm=None) -> list[Fact]:
    """Return validated LLM facts, or the company's overrides when that fails."""
    ticker = company.ticker if hasattr(company, "ticker") else str(company)
    fallback = [fact for fact in load_overrides() if fact.company == ticker]
    if llm is None:
        return fallback
    try:
        payload = llm.complete_json(
            "extract_metrics",
            _SYSTEM,
            _user_prompt(ticker, text),
            _SCHEMA,
            _cache_key(ticker, text),
        )
        drafted = _draft_facts(ticker, ref, payload)
        validate(drafted)
    except (DataError, LLMError, TypeError, ValueError, KeyError) as exc:
        log.info("extract fell back to overrides for %s: %s", ticker, exc)
        return fallback
    return [fact.model_copy(update={"validated": True}) for fact, _evidence in drafted]


_SYSTEM = (
    "Extract quarterly lender metrics from the filing excerpt. "
    "Reply with a JSON object {\"facts\": [...]} . "
    "Each fact has metric, value, unit, basis, period_end, and evidence. "
    "evidence is a short quote from the filing that contains the value. "
    "Money is USD millions (unit usd_m). Gallons are millions of gallons. "
    "variable_debt_share is a fraction from 0 to 1, not a percent."
)


def _user_prompt(ticker: str, text: str) -> str:
    names = ", ".join(metric.value for metric in Metric)
    return f"Company: {ticker}\nMetrics: {names}\n\n{text}"


def _cache_key(ticker: str, text: str) -> str:
    blob = f"extract_metrics\n{PROMPT_VERSION}\n{ticker}\n{text}".encode()
    return hashlib.sha256(blob).hexdigest()


def _draft_facts(ticker: str, ref, payload) -> list[tuple[Fact, str]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("facts"), list):
        raise DataError(
            "extract schema failure: expected an object with facts",
            code="E_VALIDATION_FAILED",
            details={"rule": "schema"},
        )
    if not payload["facts"]:
        raise DataError(
            "extract schema failure: facts is empty",
            code="E_VALIDATION_FAILED",
            details={"rule": "schema"},
        )
    filed_at = _ref_filed(ref)
    source_url = _ref_url(ref)
    drafted = []
    for item in payload["facts"]:
        if not isinstance(item, dict) or any(key not in item for key in _REQUIRED):
            raise DataError(
                "extract schema failure: a fact is missing a required field",
                code="E_VALIDATION_FAILED",
                details={"rule": "schema"},
            )
        metric_name = item["metric"]
        if metric_name not in Metric._value2member_map_:
            raise DataError(
                f"extract schema failure: unknown metric {metric_name}",
                code="E_VALIDATION_FAILED",
                details={"rule": "schema"},
            )
        fact = Fact(
            company=ticker,
            metric=Metric(metric_name),
            value=float(item["value"]),
            unit=str(item["unit"]),
            basis=Basis(item["basis"]),
            period_end=_as_date(item["period_end"]),
            filed_at=filed_at,
            source_url=source_url,
            extraction=Extraction.LLM,
            validated=False,
        )
        drafted.append((fact, str(item["evidence"])))
    return drafted


def _unpack(facts, evidence) -> tuple[list[Fact], list[str]]:
    rows: list[Fact] = []
    texts: list[str] = []
    for index, item in enumerate(facts):
        if isinstance(item, tuple):
            rows.append(item[0])
            texts.append(str(item[1]))
            continue
        rows.append(item)
        if isinstance(evidence, list):
            texts.append(str(evidence[index]))
        elif isinstance(evidence, dict):
            key = (item.metric, item.basis, item.period_end)
            texts.append(str(evidence.get(key, "")))
        else:
            texts.append("" if evidence is None else str(evidence))
    return rows, texts


def _rule_evidence(facts: list[Fact], texts: list[str]) -> None:
    for fact, text in zip(facts, texts):
        if _appears(fact.value, text):
            continue
        raise _failed(
            3,
            f"{fact.metric.value} {fact.value} does not appear in its evidence",
            metric=fact.metric.value,
            value=fact.value,
        )


def _rule_debt(facts: list[Fact], checks: list[dict]) -> None:
    for key, group in _groups(facts).items():
        company, period_end, basis = key
        total = _value(group, Metric.DEBT_TOTAL)
        if total is None:
            continue
        current = _value(group, Metric.DEBT_CURRENT)
        noncurrent = _check_value(checks, company, period_end, basis, "debt_noncurrent")
        if current is None or noncurrent is None:
            raise _failed(1, f"{company} {period_end} is missing current or noncurrent debt")
        if not _close(current + noncurrent, total):
            raise _failed(
                1,
                f"{company} {period_end} debt {current} + {noncurrent} != {total}",
            )


def _rule_balance_sheet(facts: list[Fact], checks: list[dict]) -> None:
    for key, group in _groups(facts).items():
        company, period_end, basis = key
        assets = _value(group, Metric.TOTAL_ASSETS)
        if assets is None:
            continue
        equity = _value(group, Metric.EQUITY)
        current = _check_value(checks, company, period_end, basis, "current_liabilities")
        noncurrent = _check_value(checks, company, period_end, basis, "noncurrent_liabilities")
        liabilities = _value(group, Metric.TOTAL_LIABILITIES)
        if equity is None:
            raise _failed(2, f"{company} {period_end} is missing equity")
        if current is not None and noncurrent is not None:
            expected = current + noncurrent + equity
        elif liabilities is not None:
            expected = liabilities + equity
        else:
            raise _failed(2, f"{company} {period_end} is missing liabilities")
        if not _close(assets, expected):
            raise _failed(
                2,
                f"{company} {period_end} assets {assets} != liabilities + equity {expected}",
            )


def _rule_ranges(facts: list[Fact]) -> None:
    for fact in facts:
        if fact.metric == Metric.FUEL_GALLONS_M and fact.value <= 0:
            raise _failed(4, "fuel gallons must be above 0", metric=fact.metric.value)
        if fact.metric == Metric.NET_INTEREST_EXPENSE and fact.value <= 0:
            raise _failed(4, "net interest must be above 0", metric=fact.metric.value)
        if fact.metric == Metric.VARIABLE_DEBT_SHARE and not 0 <= fact.value <= 1:
            raise _failed(4, "variable share must be between 0 and 1", metric=fact.metric.value)


def _rule_matches_overrides(facts: list[Fact], overrides: list[Fact]) -> None:
    indexed = {
        (fact.company, fact.metric, fact.basis, fact.period_end): fact.value
        for fact in overrides
    }
    for fact in facts:
        key = (fact.company, fact.metric, fact.basis, fact.period_end)
        if key not in indexed:
            continue
        if not _close(fact.value, indexed[key]):
            raise _failed(
                5,
                f"{fact.metric.value} {fact.value} disagrees with override {indexed[key]}",
                metric=fact.metric.value,
            )


def _groups(facts: list[Fact]) -> dict[tuple, list[Fact]]:
    grouped: dict[tuple, list[Fact]] = {}
    for fact in facts:
        grouped.setdefault((fact.company, fact.period_end, fact.basis), []).append(fact)
    return grouped


def _value(facts: list[Fact], metric: Metric) -> float | None:
    for fact in facts:
        if fact.metric == metric:
            return fact.value
    return None


def _check_value(checks, company, period_end, basis, name) -> float | None:
    for row in checks:
        if row["company"] == company and row["period_end"] == period_end and row["basis"] == basis:
            return row.get(name)
    return None


def _appears(value: float, evidence: str) -> bool:
    if not evidence:
        return False
    normalized = re.sub(r"(?<=\d),(?=\d)", "", evidence)
    for form in _forms(value):
        if re.search(rf"(?<!\d){re.escape(form)}(?!\d)", normalized):
            return True
    return False


def _forms(value: float) -> list[str]:
    forms: list[str] = []
    if abs(value) >= 1 and abs(value - round(value)) < 1e-6:
        whole = int(round(value))
        forms.append(str(whole))
        if whole < 0:
            # Parentheses in a filing, "(3,972)", are a negative amount.
            forms.append(str(abs(whole)))
    else:
        forms.append(f"{value:.2f}")
        trimmed = f"{value:.10f}".rstrip("0").rstrip(".")
        if trimmed not in forms:
            forms.append(trimmed)
    if 0 < value <= 1:
        percent = value * 100
        if abs(percent - round(percent)) < 1e-6:
            forms.append(str(int(round(percent))))
    return forms


def _close(left: float, right: float) -> bool:
    scale = max(abs(left), abs(right))
    if scale == 0:
        return True
    return abs(left - right) / scale <= _TOLERANCE


def _failed(rule: int, message: str, **details) -> DataError:
    return DataError(
        f"extraction failed rule {rule}: {message}",
        code="E_VALIDATION_FAILED",
        details={"rule": rule, **details},
    )


def _fact_from_override(row: dict) -> Fact:
    return Fact(
        company=row["company"],
        metric=Metric(row["metric"]),
        value=float(row["value"]),
        unit=str(row["unit"]),
        basis=Basis(row["basis"]),
        period_end=_as_date(row["period_end"]),
        filed_at=_as_datetime(row["filed_at"]),
        source_url=row["source_url"],
        extraction=Extraction.OVERRIDE,
        validated=True,
    )


def _read_yaml(path) -> dict:
    target = _find_overrides(path)
    loaded = yaml.safe_load(target.read_text())
    if not isinstance(loaded, dict):
        raise DataError("overrides.yaml must be a mapping", code="E_MISSING_FACT")
    return loaded


def _find_overrides(path) -> Path:
    if path is not None:
        return Path(path)
    starts = [Path.cwd(), Path(__file__).resolve().parent]
    seen: set[Path] = set()
    for start in starts:
        for directory in [start, *start.parents]:
            if directory in seen:
                continue
            seen.add(directory)
            candidate = directory / "overrides.yaml"
            if candidate.is_file():
                return candidate
    raise DataError("overrides.yaml was not found", code="E_MISSING_FACT")


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _optional_float(value) -> float | None:
    if value is None:
        return None
    return float(value)


def _ref_url(ref) -> str:
    if hasattr(ref, "url"):
        return str(ref.url)
    return str(ref)


def _ref_filed(ref) -> datetime:
    filed = getattr(ref, "filed", None)
    if isinstance(filed, datetime):
        return _as_datetime(filed)
    if isinstance(filed, date):
        return datetime(filed.year, filed.month, filed.day, 12, tzinfo=timezone.utc)
    return datetime.now(timezone.utc)
