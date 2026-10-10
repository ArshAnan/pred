"""SQLite persistence. This is the only module that opens the database."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from airscen.types import MarketMeta, MarketView, RunResult, StoreError


# LLD §4 schema. Keep column names, constraints, and indexes aligned with it.
SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY,
  company TEXT NOT NULL, metric TEXT NOT NULL,
  basis TEXT NOT NULL CHECK (basis IN ('GAAP','ADJUSTED')),
  value REAL NOT NULL, unit TEXT NOT NULL,
  period_end TEXT NOT NULL, filed_at TEXT NOT NULL, source_url TEXT NOT NULL,
  extraction TEXT NOT NULL CHECK (extraction IN ('xbrl','parser','llm','override')),
  validated INTEGER NOT NULL DEFAULT 0, loaded_at TEXT NOT NULL,
  UNIQUE (company, metric, basis, period_end, extraction)
);
CREATE INDEX IF NOT EXISTS ix_facts ON facts (company, metric, filed_at);

CREATE TABLE IF NOT EXISTS markets (
  venue TEXT NOT NULL, market_id TEXT NOT NULL, token_id TEXT,
  title TEXT NOT NULL, rules TEXT NOT NULL DEFAULT '', close_time TEXT,
  kind TEXT NOT NULL, outcome TEXT, threshold REAL, threshold_unit TEXT, group_key TEXT,
  relevant INTEGER, factor TEXT, yes_moves_factor TEXT, mechanism TEXT, citation TEXT,
  label_source TEXT, labeled_at TEXT, content_hash TEXT NOT NULL,
  PRIMARY KEY (venue, market_id)
);

CREATE TABLE IF NOT EXISTS snapshots (
  venue TEXT NOT NULL, market_id TEXT NOT NULL, ts TEXT NOT NULL,
  bid REAL, ask REAL, bid_size REAL, ask_size REAL, last REAL,
  volume_24h REAL, open_interest REAL, liquidity REAL,
  src TEXT NOT NULL DEFAULT 'live',
  PRIMARY KEY (venue, market_id, ts),
  FOREIGN KEY (venue, market_id) REFERENCES markets (venue, market_id)
);

CREATE TABLE IF NOT EXISTS macro (
  series_id TEXT NOT NULL, obs_date TEXT NOT NULL, value REAL NOT NULL, realtime_start TEXT NOT NULL,
  PRIMARY KEY (series_id, obs_date, realtime_start)
);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, company TEXT NOT NULL, as_of TEXT NOT NULL, created_at TEXT NOT NULL,
  inputs_json TEXT NOT NULL, result_json TEXT NOT NULL, data_version TEXT NOT NULL,
  brief_template_md TEXT NOT NULL, brief_llm_md TEXT,
  brief_llm_status TEXT NOT NULL CHECK (brief_llm_status IN ('pending','ready','failed','rejected','skipped'))
);

CREATE TABLE IF NOT EXISTS alerts (
  alert_id TEXT PRIMARY KEY, run_id TEXT REFERENCES runs (run_id),
  ts TEXT NOT NULL, company TEXT NOT NULL, rule TEXT NOT NULL, dedup_key TEXT NOT NULL,
  severity TEXT NOT NULL, payload_json TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, sent_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('pending','sent','failed','suppressed'))
);
CREATE INDEX IF NOT EXISTS ix_alerts ON alerts (company, ts);

CREATE TABLE IF NOT EXISTS llm_cache (
  cache_key TEXT PRIMARY KEY, task TEXT NOT NULL, provider TEXT, model TEXT,
  response_json TEXT NOT NULL, tokens_in INTEGER, tokens_out INTEGER, created_at TEXT NOT NULL
);
"""


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _json(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


class Repository:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def _connect(self):
        """Open one configured connection and close it even on failure."""
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA busy_timeout = 5000")
            db.execute("PRAGMA foreign_keys = ON")
            yield db
        finally:
            db.close()

    def _write(self, action):
        # A SQLite lock waits five seconds and gets one more attempt (LLD §8).
        for attempt in range(2):
            try:
                with self._connect() as db:
                    with db:
                        action(db)
                return
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() and attempt == 0:
                    continue
                raise StoreError(str(exc)) from exc
            except sqlite3.DatabaseError as exc:
                raise StoreError(str(exc)) from exc

    def migrate(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connect() as db:
                db.execute("PRAGMA journal_mode = WAL")
                db.executescript(SCHEMA)
        except sqlite3.DatabaseError as exc:
            raise StoreError(str(exc)) from exc

    def upsert_facts(self, rows):
        rows = list(rows)
        loaded_at = datetime.now(timezone.utc).isoformat()
        def action(db):
            db.executemany("""INSERT INTO facts
                (company, metric, basis, value, unit, period_end, filed_at, source_url,
                 extraction, validated, loaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(company, metric, basis, period_end, extraction) DO UPDATE SET
                value=excluded.value, unit=excluded.unit, filed_at=excluded.filed_at,
                source_url=excluded.source_url, validated=excluded.validated,
                loaded_at=excluded.loaded_at""", [
                (r.company, r.metric.value, r.basis.value, r.value, r.unit,
                 _iso(r.period_end), _iso(r.filed_at), r.source_url, r.extraction.value,
                 int(r.validated), loaded_at) for r in rows])
        self._write(action)

    def latest_facts(self, company, as_of):
        raise NotImplementedError

    def put_snapshots(self, rows):
        rows = list(rows)
        def action(db):
            db.executemany("""INSERT INTO snapshots
                (venue, market_id, ts, bid, ask, bid_size, ask_size, last,
                 volume_24h, open_interest, liquidity, src)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(venue, market_id, ts) DO UPDATE SET
                bid=excluded.bid, ask=excluded.ask, bid_size=excluded.bid_size,
                ask_size=excluded.ask_size, last=excluded.last,
                volume_24h=excluded.volume_24h, open_interest=excluded.open_interest,
                liquidity=excluded.liquidity, src=excluded.src""", [
                (r.venue.value, r.market_id, _iso(r.ts), r.bid, r.ask, r.bid_size,
                 r.ask_size, r.last, r.volume_24h, r.open_interest, r.liquidity, r.src)
                for r in rows])
        self._write(action)

    def latest_snapshot(self, venue, market_id, as_of):
        raise NotImplementedError

    def snapshot_series(self, venue, market_id, start, end, as_of):
        raise NotImplementedError

    def upsert_market(self, m):
        """Accept MarketMeta, or MarketView when a label is available."""
        if isinstance(m, MarketView):
            meta, label = m.meta, m.label
        elif isinstance(m, MarketMeta):
            meta, label = m, None
        else:
            raise TypeError("upsert_market expects MarketMeta or MarketView")
        content_hash = hashlib.sha256(meta.model_dump_json().encode()).hexdigest()
        label_values = (int(label.relevant), label.factor.value if label.factor else None,
                        label.yes_moves_factor, label.mechanism, label.citation,
                        label.label_source, datetime.now(timezone.utc).isoformat()) if label else (None,) * 7
        def action(db):
            db.execute("""INSERT INTO markets
                (venue, market_id, token_id, title, rules, close_time, kind, outcome,
                 threshold, threshold_unit, group_key, relevant, factor,
                 yes_moves_factor, mechanism, citation, label_source, labeled_at, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(venue, market_id) DO UPDATE SET
                token_id=excluded.token_id, title=excluded.title, rules=excluded.rules,
                close_time=excluded.close_time, kind=excluded.kind, outcome=excluded.outcome,
                threshold=excluded.threshold, threshold_unit=excluded.threshold_unit,
                group_key=excluded.group_key, content_hash=excluded.content_hash,
                relevant=CASE WHEN markets.label_source='hand' AND excluded.label_source='llm'
                    THEN markets.relevant ELSE COALESCE(excluded.relevant, markets.relevant) END,
                factor=CASE WHEN markets.label_source='hand' AND excluded.label_source='llm'
                    THEN markets.factor ELSE COALESCE(excluded.factor, markets.factor) END,
                yes_moves_factor=CASE WHEN markets.label_source='hand' AND excluded.label_source='llm'
                    THEN markets.yes_moves_factor ELSE COALESCE(excluded.yes_moves_factor, markets.yes_moves_factor) END,
                mechanism=CASE WHEN markets.label_source='hand' AND excluded.label_source='llm'
                    THEN markets.mechanism ELSE COALESCE(excluded.mechanism, markets.mechanism) END,
                citation=CASE WHEN markets.label_source='hand' AND excluded.label_source='llm'
                    THEN markets.citation ELSE COALESCE(excluded.citation, markets.citation) END,
                label_source=CASE WHEN markets.label_source='hand' AND excluded.label_source='llm'
                    THEN markets.label_source ELSE COALESCE(excluded.label_source, markets.label_source) END,
                labeled_at=CASE WHEN markets.label_source='hand' AND excluded.label_source='llm'
                    THEN markets.labeled_at ELSE COALESCE(excluded.labeled_at, markets.labeled_at) END""",
                (meta.venue.value, meta.market_id, meta.token_id, meta.title,
                 meta.rules, _iso(meta.close_time), meta.kind.value, meta.outcome,
                 meta.threshold, meta.threshold_unit, meta.group_key,
                 *label_values, content_hash))
        self._write(action)

    def markets(self, factor=None):
        raise NotImplementedError

    def put_macro(self, rows):
        rows = list(rows)
        def action(db):
            db.executemany("""INSERT INTO macro (series_id, obs_date, value, realtime_start)
                VALUES (?, ?, ?, ?) ON CONFLICT(series_id, obs_date, realtime_start)
                DO UPDATE SET value=excluded.value""", [
                (r.series_id, _iso(r.obs_date), r.value, _iso(r.realtime_start)) for r in rows])
        self._write(action)

    def macro_series(self, series_id, start, end, as_of):
        raise NotImplementedError

    def save_run(self, run):
        def action(db):
            db.execute("""INSERT INTO runs
                (run_id, company, as_of, created_at, inputs_json, result_json,
                 data_version, brief_template_md, brief_llm_md, brief_llm_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run.run_id, run.company, _iso(run.as_of), _iso(run.created_at),
                 run.inputs.model_dump_json(), run.model_dump_json(), run.data_version,
                 run.brief.template_md, run.brief.llm_md, run.brief.llm_status))
        self._write(action)

    def get_run(self, run_id):
        raise NotImplementedError

    def update_run_brief(self, run_id, md, status):
        def action(db):
            row = db.execute("SELECT result_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise StoreError(f"run not found: {run_id}")
            run = RunResult.model_validate_json(row[0])
            brief = run.brief.model_copy(update={"llm_md": md, "llm_status": status})
            result = run.model_copy(update={"brief": brief})
            db.execute("""UPDATE runs SET result_json=?, brief_llm_md=?, brief_llm_status=?
                WHERE run_id=?""", (result.model_dump_json(), md, status, run_id))
        self._write(action)

    def save_alert(self, a):
        def action(db):
            db.execute("""INSERT INTO alerts
                (alert_id, run_id, ts, company, rule, dedup_key, severity, payload_json, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(alert_id) DO UPDATE SET run_id=excluded.run_id, ts=excluded.ts,
                company=excluded.company, rule=excluded.rule, dedup_key=excluded.dedup_key,
                severity=excluded.severity, payload_json=excluded.payload_json,
                status=excluded.status""",
                (a.alert_id, a.run_id, _iso(a.ts), a.company, a.rule, a.dedup_key,
                 a.severity, _json(a.payload), a.status))
        self._write(action)

    def alerts(self, company, since):
        raise NotImplementedError

    def cache_get(self, key):
        raise NotImplementedError

    def cache_put(self, entry):
        def action(db):
            db.execute("""INSERT INTO llm_cache
                (cache_key, task, provider, model, response_json, tokens_in, tokens_out, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET task=excluded.task,
                provider=excluded.provider, model=excluded.model,
                response_json=excluded.response_json, tokens_in=excluded.tokens_in,
                tokens_out=excluded.tokens_out, created_at=excluded.created_at""",
                (entry["cache_key"], entry["task"], entry.get("provider"), entry.get("model"),
                 entry["response_json"], entry.get("tokens_in"), entry.get("tokens_out"),
                 _iso(entry["created_at"])))
        self._write(action)

    def data_version(self, as_of):
        raise NotImplementedError
