"""Peer margins. Delta's refinery sales are the September-quarter 8-K figures."""

from airscen.scenario import compare_margins
from airscen.types import Basis, Metric

from tests.model.conftest import make_fact

# GAAP revenue 20,186 includes 2,601 of third-party refinery sales.
_GAAP = 20_186.0
_ADJUSTED = 20_186.0 - 2_601.0
_OI = 1_454.0


def _book(adjusted: bool):
    book = {
        Metric.OPERATING_INCOME: make_fact(Metric.OPERATING_INCOME, _OI),
        Metric.REVENUE_GAAP: make_fact(Metric.REVENUE_GAAP, _GAAP),
    }
    if adjusted:
        book[Metric.REVENUE_ADJUSTED] = make_fact(Metric.REVENUE_ADJUSTED, _ADJUSTED)
    return book


def test_adjusted_revenue_is_used_when_every_company_has_it():
    compared = compare_margins([
        ("DAL", _book(True)),
        ("UAL", _book(True)),
        ("AAL", _book(True)),
    ])
    assert compared.basis == Basis.ADJUSTED
    assert compared.flags == []
    assert compared.margins["DAL"] == _OI / _ADJUSTED


def test_gaap_is_used_with_a_mismatch_flag_when_a_peer_lacks_adjusted_revenue():
    compared = compare_margins([
        ("DAL", _book(True)),
        ("UAL", _book(False)),
    ])
    assert compared.basis == Basis.GAAP
    assert compared.flags == ["MARGIN_BASIS_MISMATCH"]
    assert compared.margins["DAL"] == _OI / _GAAP
