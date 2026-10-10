import unittest
from datetime import date, datetime, timedelta, timezone

from pydantic import ValidationError

from airscen.types import (
    Fact,
    MarketLabel,
    MarketMeta,
    Metric,
    RunRequest,
    SourceStatus,
)


class TypesContractTests(unittest.TestCase):
    def fact(self, **changes):
        values = dict(
            company="DAL",
            metric="debt_total",
            value=12_851,
            unit="usd_m",
            basis="GAAP",
            period_end=date(2026, 9, 30),
            filed_at=datetime(2026, 10, 9, tzinfo=timezone.utc),
            source_url="https://example.test/filing",
            extraction="override",
        )
        values.update(changes)
        return Fact(**values)

    def test_all_models_share_frozen_config(self):
        for model in (Fact, MarketMeta, MarketLabel, RunRequest, SourceStatus):
            self.assertTrue(model.model_config["frozen"], model.__name__)
        with self.assertRaises(ValidationError):
            self.fact().value = 1

    def test_fact_contract(self):
        self.assertEqual(self.fact().metric, Metric.DEBT_TOTAL)
        invalid = (
            {"filed_at": datetime(2026, 9, 29, tzinfo=timezone.utc)},
            {"metric": "variable_debt_share", "value": 1.1, "unit": "fraction"},
            {"unit": "usd"},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.fact(**changes)

    def test_relevant_market_label_requires_factor_and_direction(self):
        with self.assertRaises(ValidationError):
            MarketLabel(relevant=True, label_source="hand")
        label = MarketLabel(
            relevant=True, factor="fuel", yes_moves_factor="up", label_source="hand"
        )
        self.assertEqual(label.factor, "fuel")

    def test_run_request_bounds_and_time(self):
        base = dict(ticker="DAL", note_size_usd_m=1000, tenor_years=7, coupon_spread_bp=150)
        self.assertEqual(RunRequest(**base).coverage_threshold, 3.0)
        invalid = (
            {"note_size_usd_m": 0},
            {"note_size_usd_m": 50_001},
            {"tenor_years": 41},
            {"coupon_spread_bp": -1},
            {"coverage_threshold": 0},
            {"fuel_passthrough": 1.1},
            {"as_of": datetime.now(timezone.utc) + timedelta(minutes=2)},
            {"as_of": datetime(2026, 7, 10)},
            {"unexpected": True},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                RunRequest(**(base | changes))


if __name__ == "__main__":
    unittest.main()
