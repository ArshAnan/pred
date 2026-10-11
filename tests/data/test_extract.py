"""Hand-checked Delta facts, the five extraction rules, and the override fallback."""

from datetime import date, datetime, timezone

import pytest

from airscen.adapters.edgar import FilingRef
from airscen.extract import extract_facts, load_overrides, validate
from airscen.store import Repository
from airscen.types import DataError, Extraction, LLMError, Metric

_AS_OF = datetime(2026, 10, 11, tzinfo=timezone.utc)
_REF = FilingRef(
    form="8-K",
    filed=date(2026, 10, 9),
    accession="0000027904-26-000035",
    primary_document="deltaairlinesannouncessept.htm",
    url="https://www.sec.gov/Archives/edgar/data/27904/000002790426000035/deltaairlinesannouncessept.htm",
)

# Phrases from the September and June exhibits, plus the two liability sums
# (current + noncurrent) that the releases do not print as one line.
_EVIDENCE = """
Operating Income 1,454
Interest expense, net (146)
Fuel gallons consumed (millions) 1,146
Average price per fuel gallon $ 3.80
Average price per fuel gallon, adjusted $ 3.61
Total debt and finance lease obligations 12,851
Current maturities of debt and finance leases $ 3,324
Total assets $ 86,048
Total current liabilities 33,575
Total noncurrent liabilities 29,960
63,535
Stockholders' Equity 22,513
6,900
Adjusted net debt 13,350
Total operating revenue 20,186
Operating revenue, adjusted $ 17,585
Operating Income 1,864
Interest expense, net (144)
Fuel gallons consumed (millions) 1,122
Average price per fuel gallon $ 3.66
Average price per fuel gallon, adjusted $ 3.93
Total debt and finance lease obligations 13,952
Current maturities of debt and finance leases $ 3,442
Total assets $ 86,321
Total current liabilities 33,604
Total noncurrent liabilities 30,902
64,506
Stockholders' Equity 21,815
7,700
Adjusted net debt $ 13,591
Total operating revenue 19,757
Operating revenue, adjusted $ 17,666
78 percent fixed rate debt and 22 percent variable rate debt
Operating income 1,096
Interest expense, net of capitalized interest and interest income 135
Fuel gallons consumed (millions) 1,219
Average aircraft fuel price per gallon $ 4.19
Debt, finance lease obligations and other financial liabilities $ 26,464
Current maturities of long-term debt, finance leases, and other financial liabilities 2,170
Long-term debt, finance leases, and other financial liabilities 24,294
Ending available liquidity of $19.6 billion
19,600
Adjusted net debt (Non-GAAP) $ 18,105
Total operating revenue 17,672
Total assets $ 84,569
Total current liabilities 27,764
Total noncurrent liabilities 40,108
67,872
Total stockholders' equity 16,697
Operating income 446
Interest expense, net (409)
Fuel consumption (gallons in millions) 1,204
Average aircraft fuel price including related taxes 4.05
Current maturities of long-term debt and finance leases $ 3,094
Long-term debt and finance leases, net of current maturities 25,833
28,927
total available liquidity $11.3 billion
11,300
Total operating revenues 16,735
Total assets $ 64,233
Total current liabilities 26,756
Total noncurrent liabilities 41,449
68,205
Total stockholders' deficit (3,972)
"""


class _LLM:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def complete_json(self, task, system, user, schema, cache_key):
        self.calls.append({"task": task, "schema": schema, "cache_key": cache_key})
        if self.error is not None:
            raise self.error
        return self.payload


def _by_metric(facts, metric, basis="GAAP"):
    found = [
        fact for fact in facts
        if fact.metric == metric and fact.basis.value == basis
    ]
    assert found
    return max(found, key=lambda fact: fact.period_end)


def test_latest_facts_include_the_required_delta_metrics():
    repo = Repository(":memory:")
    repo.migrate()
    repo.upsert_facts(load_overrides())
    facts, flags = repo.latest_facts("DAL", _AS_OF)
    assert flags == []
    assert _by_metric(facts, Metric.OPERATING_INCOME).value == 1454
    assert _by_metric(facts, Metric.NET_INTEREST_EXPENSE).value == 146
    assert _by_metric(facts, Metric.FUEL_GALLONS_M).value == 1146
    assert _by_metric(facts, Metric.DEBT_TOTAL).value == 12851
    assert _by_metric(facts, Metric.VARIABLE_DEBT_SHARE).value == 0.22
    assert _by_metric(facts, Metric.OPERATING_INCOME).extraction == Extraction.OVERRIDE
    assert _by_metric(facts, Metric.OPERATING_INCOME).validated is True


def test_delta_figures_pass_rules_1_2_4_and_5():
    validate(load_overrides(), evidence=_EVIDENCE)


def test_invented_number_fails_rule_3():
    facts = []
    for fact in load_overrides():
        if fact.metric == Metric.OPERATING_INCOME and fact.period_end == date(2026, 9, 30):
            facts.append(fact.model_copy(update={"value": 99999, "extraction": Extraction.LLM}))
        else:
            facts.append(fact)
    with pytest.raises(DataError) as caught:
        validate(facts, evidence=_EVIDENCE)
    assert caught.value.code == "E_VALIDATION_FAILED"
    assert caught.value.details["rule"] == 3


def _september_payload():
    rows = []
    for fact in load_overrides():
        if fact.company != "DAL" or fact.period_end != date(2026, 9, 30):
            continue
        rows.append({
            "metric": fact.metric.value,
            "value": fact.value,
            "unit": fact.unit,
            "basis": fact.basis.value,
            "period_end": fact.period_end.isoformat(),
            "evidence": _EVIDENCE,
        })
    return {"facts": rows}


def test_good_llm_output_is_validated():
    llm = _LLM(_september_payload())
    facts = extract_facts("DAL", _EVIDENCE, _REF, llm=llm)
    assert llm.calls[0]["task"] == "extract_metrics"
    income = _by_metric(facts, Metric.OPERATING_INCOME)
    assert income.value == 1454
    assert income.extraction == Extraction.LLM
    assert income.validated is True
    assert income.source_url == _REF.url


def test_bad_llm_output_falls_back_to_overrides():
    payload = _september_payload()
    payload["facts"][0]["value"] = 99999
    llm = _LLM(payload)
    facts = extract_facts("DAL", _EVIDENCE, _REF, llm=llm)
    income = _by_metric(facts, Metric.OPERATING_INCOME)
    assert income.value == 1454
    assert income.extraction == Extraction.OVERRIDE


def test_llm_error_falls_back_to_overrides():
    llm = _LLM(error=LLMError("providers failed"))
    facts = extract_facts("DAL", "no filing", _REF, llm=llm)
    assert _by_metric(facts, Metric.DEBT_TOTAL).extraction == Extraction.OVERRIDE


def test_schema_failure_falls_back_to_overrides():
    llm = _LLM({"facts": []})
    facts = extract_facts("DAL", "no filing", _REF, llm=llm)
    assert facts
    assert all(fact.extraction == Extraction.OVERRIDE for fact in facts)


def test_july_10_returns_the_june_quarter():
    repo = Repository(":memory:")
    repo.migrate()
    repo.upsert_facts(load_overrides())
    facts, flags = repo.latest_facts("DAL", datetime(2026, 7, 10, tzinfo=timezone.utc))
    assert flags == []
    income = _by_metric(facts, Metric.OPERATING_INCOME)
    assert income.value == 1864
    assert income.period_end == date(2026, 6, 30)
    assert _by_metric(facts, Metric.NET_INTEREST_EXPENSE).value == 144
    assert _by_metric(facts, Metric.FUEL_GALLONS_M).value == 1122
    assert _by_metric(facts, Metric.DEBT_TOTAL).value == 13952
    assert _by_metric(facts, Metric.VARIABLE_DEBT_SHARE).value == 0.22
    assert all(fact.period_end < date(2026, 9, 30) for fact in facts)


def test_every_override_value_cites_a_filing():
    import yaml
    from pathlib import Path

    raw = yaml.safe_load(Path("overrides.yaml").read_text())
    for row in raw["facts"]:
        assert row["source_url"].startswith("https://www.sec.gov/Archives/"), row["metric"]
    for row in raw["missing"]:
        assert row["source_url"].startswith("https://www.sec.gov/Archives/")
        assert row["reason"]


def test_peer_metrics_are_present_or_explained():
    import yaml
    from pathlib import Path

    raw = yaml.safe_load(Path("overrides.yaml").read_text())
    required = {
        "operating_income", "net_interest_expense", "fuel_gallons_m", "fuel_price_per_gal",
        "debt_total", "debt_current", "liquidity", "adjusted_net_debt",
        "revenue_gaap", "revenue_adjusted", "total_assets", "total_liabilities",
        "equity", "variable_debt_share",
    }
    for company in ("UAL", "AAL"):
        present = {row["metric"] for row in raw["facts"] if row["company"] == company}
        missing = [row for row in raw["missing"] if row["company"] == company]
        unexplained = required - present - {row["metric"] for row in missing}
        assert not unexplained
        for row in missing:
            assert row["reason"]
            assert row["source_url"].startswith("https://www.sec.gov/")
    facts = [fact for fact in load_overrides() if fact.company in {"UAL", "AAL"}]
    by_company = {}
    for fact in facts:
        if fact.metric == Metric.OPERATING_INCOME:
            by_company[fact.company] = fact.value
    assert by_company == {"UAL": 1096, "AAL": 446}


def test_three_argument_call_returns_overrides():
    facts = extract_facts("DAL", "filing text", _REF)
    assert _by_metric(facts, Metric.FUEL_GALLONS_M).value == 1146
    assert _by_metric(facts, Metric.VARIABLE_DEBT_SHARE).value == 0.22
