"""Scripted source adapters. Attributes match the LLD adapter functions."""

from airscen.types import SourceError


class _Scripted:
    def __init__(self, **returns):
        self.returns = returns
        self.calls = []
        self.fail = False

    def _call(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self.fail:
            raise SourceError(f"{name} failed", code="E_SOURCE_UNAVAILABLE")
        value = self.returns.get(name, [])
        if isinstance(value, dict):
            key = args[0] if args else None
            if key in value:
                return value[key]
        return value


class FakeKalshi(_Scripted):
    def list_markets(self, series):
        return self._call("list_markets", series)

    def quote(self, ticker):
        return self._call("quote", ticker)

    def candles(self, ticker, start, end, period_min):
        return self._call("candles", ticker, start, end, period_min)

    def historical_cutoff(self):
        return self._call("historical_cutoff")


class FakePolymarket(_Scripted):
    def search(self, query):
        return self._call("search", query)

    def quotes(self, token_ids):
        return self._call("quotes", token_ids)

    def history(self, token_id, start, end, bucket_s):
        return self._call("history", token_id, start, end, bucket_s)


class FakeEdgar(_Scripted):
    def resolve_cik(self, ticker):
        return self._call("resolve_cik", ticker)

    def recent_filings(self, cik, since):
        return self._call("recent_filings", cik, since)

    def fetch_document(self, ref):
        return self._call("fetch_document", ref)

    def xbrl_facts(self, cik, tags):
        return self._call("xbrl_facts", cik, tags)


class FakeFred(_Scripted):
    def observations(self, series_id, start, end, as_of):
        return self._call("observations", series_id, start, end, as_of)


class FakeAlpaca(_Scripted):
    def daily_bars(self, symbols, start, end):
        return self._call("daily_bars", symbols, start, end)


class FakeGdelt(_Scripted):
    def headlines(self, query, before, hours, n):
        return self._call("headlines", query, before, hours, n)
