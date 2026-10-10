from __future__ import annotations


class Repository:
    def __init__(self, path):
        raise NotImplementedError

    def migrate(self):
        raise NotImplementedError

    def upsert_facts(self, rows):
        raise NotImplementedError

    def latest_facts(self, company, as_of):
        raise NotImplementedError

    def put_snapshots(self, rows):
        raise NotImplementedError

    def latest_snapshot(self, venue, market_id, as_of):
        raise NotImplementedError

    def snapshot_series(self, venue, market_id, start, end, as_of):
        raise NotImplementedError

    def upsert_market(self, m):
        raise NotImplementedError

    def markets(self, factor=None):
        raise NotImplementedError

    def put_macro(self, rows):
        raise NotImplementedError

    def macro_series(self, series_id, start, end, as_of):
        raise NotImplementedError

    def save_run(self, run):
        raise NotImplementedError

    def get_run(self, run_id):
        raise NotImplementedError

    def update_run_brief(self, run_id, md, status):
        raise NotImplementedError

    def save_alert(self, a):
        raise NotImplementedError

    def alerts(self, company, since):
        raise NotImplementedError

    def cache_get(self, key):
        raise NotImplementedError

    def cache_put(self, entry):
        raise NotImplementedError

    def data_version(self, as_of):
        raise NotImplementedError
