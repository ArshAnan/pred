"""The only module that opens SQLite.

Reads take an `as_of` time. Writes each run in one short transaction and retry
once when SQLite is busy. Visibility rules are LLD section 4.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path

from airscen.types import (
    Alert,
    Basis,
    Extraction,
    Factor,
    Fact,
    MarketKind,
    MarketLabel,
    MarketMeta,
    Metric,
    MacroObs,
    RunResult,
    Snapshot,
    StoreError,
    Venue,
)

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;

CREATE TABLE facts (
  id INTEGER PRIMARY KEY,
  company TEXT NOT NULL, metric TEXT NOT NULL,
  basis TEXT NOT NULL CHECK (basis IN ('GAAP','ADJUSTED')),
  value REAL NOT NULL, unit TEXT NOT NULL,
  period_end TEXT NOT NULL, filed_at TEXT NOT NULL, source_url TEXT NOT NULL,
  extraction TEXT NOT NULL CHECK (extraction IN ('xbrl','parser','llm','override')),
  validated INTEGER NOT NULL DEFAULT 0, loaded_at TEXT NOT NULL,
  UNIQUE (company, metric, basis, period_end, extraction)
);
CREATE INDEX ix_facts ON facts (company, metric, filed_at);

CREATE TABLE markets (
  venue TEXT NOT NULL, market_id TEXT NOT NULL, token_id TEXT,
  title TEXT NOT NULL, rules TEXT NOT NULL DEFAULT '', close_time TEXT,
  kind TEXT NOT NULL, outcome TEXT, threshold REAL, threshold_unit TEXT, group_key TEXT,
  relevant INTEGER, factor TEXT, yes_moves_factor TEXT, mechanism TEXT, citation TEXT,
  label_source TEXT, labeled_at TEXT, content_hash TEXT NOT NULL,
  PRIMARY KEY (venue, market_id)
);

CREATE TABLE snapshots (
  venue TEXT NOT NULL, market_id TEXT NOT NULL, ts TEXT NOT NULL,
  bid REAL, ask REAL, bid_size REAL, ask_size REAL, last REAL,
  volume_24h REAL, open_interest REAL, liquidity REAL,
  src TEXT NOT NULL DEFAULT 'live',
  PRIMARY KEY (venue, market_id, ts),
  FOREIGN KEY (venue, market_id) REFERENCES markets (venue, market_id)
);

CREATE TABLE macro (
  series_id TEXT NOT NULL, obs_date TEXT NOT NULL, value REAL NOT NULL, realtime_start TEXT NOT NULL,
  PRIMARY KEY (series_id, obs_date, realtime_start)
);

CREATE TABLE runs (
  run_id TEXT PRIMARY KEY, company TEXT NOT NULL, as_of TEXT NOT NULL, created_at TEXT NOT NULL,
  inputs_json TEXT NOT NULL, result_json TEXT NOT NULL, data_version TEXT NOT NULL,
  brief_template_md TEXT NOT NULL, brief_llm_md TEXT,
  brief_llm_status TEXT NOT NULL CHECK (brief_llm_status IN ('pending','ready','failed','rejected','skipped'))
);

CREATE TABLE alerts (
  alert_id TEXT PRIMARY KEY, run_id TEXT REFERENCES runs (run_id),
  ts TEXT NOT NULL, company TEXT NOT NULL, rule TEXT NOT NULL, dedup_key TEXT NOT NULL,
  severity TEXT NOT NULL, payload_json TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, sent_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('pending','sent','failed','suppressed'))
);
CREATE INDEX ix_alerts ON alerts (company, ts);

CREATE TABLE llm_cache (
  cache_key TEXT PRIMARY KEY, task TEXT NOT NULL, provider TEXT, model TEXT,
  response_json TEXT NOT NULL, tokens_in INTEGER, tokens_out INTEGER, created_at TEXT NOT NULL
);
"""

_TABLES = ("facts", "markets", "snapshots", "macro", "runs", "alerts", "llm_cache")
_SOURCE_PRIORITY = {"xbrl": 0, "override": 1, "parser": 2, "llm": 3}
_LABEL_COLUMNS = (
    "relevant", "factor", "yes_moves_factor", "mechanism", "citation", "label_source", "labeled_at",
)


def market_content_hash(meta: MarketMeta) -> str:
    """Hash the text discovery uses to decide whether a market needs a new label."""
    close = _iso(meta.close_time) if meta.close_time is not None else ""
    payload = f"{meta.title}\n{meta.rules}\n{close}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_dt(text: str | None) -> datetime | None:
    if text is None:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _parse_date(text: str) -> date:
    return date.fromisoformat(text)


def _differs(left: float, right: float) -> bool:
    scale = max(abs(left), abs(right))
    if scale == 0:
        return False
    return abs(left - right) / scale > 0.005


def _enum_value(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


class Repository:
    def __init__(self, path):
        if path == ":memory:":
            self.path = ":memory:"
        else:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            self.path = str(target)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._execute("PRAGMA journal_mode = WAL")
        self._execute("PRAGMA busy_timeout = 5000")
        self._execute("PRAGMA foreign_keys = ON")

    def migrate(self) -> None:
        with self._lock:
            names = {
                row[0]
                for row in self._execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if set(_TABLES).issubset(names):
                return
            self._executescript(SCHEMA)

    def ping(self) -> bool:
        try:
            with self._lock:
                self._execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    def upsert_facts(self, rows: list[Fact]) -> None:
        loaded = _iso(datetime.now(timezone.utc))

        def work() -> None:
            for fact in rows:
                self._execute(
                    """
                    INSERT INTO facts (
                      company, metric, basis, value, unit, period_end, filed_at,
                      source_url, extraction, validated, loaded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (company, metric, basis, period_end, extraction) DO UPDATE SET
                      value = excluded.value,
                      unit = excluded.unit,
                      filed_at = excluded.filed_at,
                      source_url = excluded.source_url,
                      validated = excluded.validated,
                      loaded_at = excluded.loaded_at
                    """,
                    (
                        fact.company,
                        fact.metric.value,
                        fact.basis.value,
                        fact.value,
                        fact.unit,
                        fact.period_end.isoformat(),
                        _iso(fact.filed_at),
                        fact.source_url,
                        fact.extraction.value,
                        1 if fact.validated else 0,
                        loaded,
                    ),
                )

        self._transaction(work)

    def latest_facts(self, company: str, as_of: datetime) -> tuple[list[Fact], list[str]]:
        rows = self._fetchall(
            """
            SELECT * FROM facts
            WHERE company = ? AND validated = 1 AND filed_at <= ?
            """,
            (company, _iso(as_of)),
        )
        grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault((row["metric"], row["basis"]), []).append(row)
        facts: list[Fact] = []
        conflict = False
        for key in sorted(grouped):
            winner, flagged = _pick_fact(grouped[key])
            facts.append(_fact_from_row(winner))
            conflict = conflict or flagged
        return facts, (["FACT_CONFLICT"] if conflict else [])

    def put_snapshots(self, rows: list[Snapshot]) -> None:
        def work() -> None:
            for snap in rows:
                self._execute(
                    """
                    INSERT INTO snapshots (
                      venue, market_id, ts, bid, ask, bid_size, ask_size, last,
                      volume_24h, open_interest, liquidity, src
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (venue, market_id, ts) DO UPDATE SET
                      bid = excluded.bid, ask = excluded.ask,
                      bid_size = excluded.bid_size, ask_size = excluded.ask_size,
                      last = excluded.last, volume_24h = excluded.volume_24h,
                      open_interest = excluded.open_interest, liquidity = excluded.liquidity,
                      src = excluded.src
                    """,
                    (
                        snap.venue.value,
                        snap.market_id,
                        _iso(snap.ts),
                        snap.bid,
                        snap.ask,
                        snap.bid_size,
                        snap.ask_size,
                        snap.last,
                        snap.volume_24h,
                        snap.open_interest,
                        snap.liquidity,
                        snap.src,
                    ),
                )

        self._transaction(work)

    def latest_snapshot(
        self,
        venue,
        market_id: str,
        as_of: datetime,
        stale_after_s: float = 300,
    ) -> tuple[Snapshot | None, bool]:
        venue_s = _enum_value(venue)
        row = self._fetchone(
            """
            SELECT * FROM snapshots
            WHERE venue = ? AND market_id = ? AND ts <= ?
            ORDER BY ts DESC LIMIT 1
            """,
            (venue_s, market_id, _iso(as_of)),
        )
        if row is None:
            return None, False
        snap = _snapshot_from_row(row)
        age = (as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)) - snap.ts
        return snap, age.total_seconds() > stale_after_s

    def snapshot_series(self, venue, market_id: str, start: datetime, end: datetime, as_of: datetime) -> list[Snapshot]:
        venue_s = _enum_value(venue)
        ceiling = min(_iso(end), _iso(as_of))
        rows = self._fetchall(
            """
            SELECT * FROM snapshots
            WHERE venue = ? AND market_id = ? AND ts >= ? AND ts <= ?
            ORDER BY ts
            """,
            (venue_s, market_id, _iso(start), ceiling),
        )
        return [_snapshot_from_row(row) for row in rows]

    def newest_snapshot_ts(self) -> datetime | None:
        row = self._fetchone("SELECT MAX(ts) AS ts FROM snapshots")
        if row is None or row["ts"] is None:
            return None
        return _parse_dt(row["ts"])

    def upsert_market(self, m: MarketMeta, label: MarketLabel | None = None) -> None:
        def work() -> None:
            existing = self._execute(
                "SELECT * FROM markets WHERE venue = ? AND market_id = ?",
                (m.venue.value, m.market_id),
            ).fetchone()
            labels = _resolve_label(existing, label)
            self._execute(
                """
                INSERT INTO markets (
                  venue, market_id, token_id, title, rules, close_time, kind, outcome,
                  threshold, threshold_unit, group_key, relevant, factor, yes_moves_factor,
                  mechanism, citation, label_source, labeled_at, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (venue, market_id) DO UPDATE SET
                  token_id = excluded.token_id,
                  title = excluded.title,
                  rules = excluded.rules,
                  close_time = excluded.close_time,
                  kind = excluded.kind,
                  outcome = excluded.outcome,
                  threshold = excluded.threshold,
                  threshold_unit = excluded.threshold_unit,
                  group_key = excluded.group_key,
                  relevant = excluded.relevant,
                  factor = excluded.factor,
                  yes_moves_factor = excluded.yes_moves_factor,
                  mechanism = excluded.mechanism,
                  citation = excluded.citation,
                  label_source = excluded.label_source,
                  labeled_at = excluded.labeled_at,
                  content_hash = excluded.content_hash
                """,
                (
                    m.venue.value,
                    m.market_id,
                    m.token_id,
                    m.title,
                    m.rules,
                    _iso(m.close_time) if m.close_time else None,
                    m.kind.value,
                    m.outcome,
                    m.threshold,
                    m.threshold_unit,
                    m.group_key,
                    labels["relevant"],
                    labels["factor"],
                    labels["yes_moves_factor"],
                    labels["mechanism"],
                    labels["citation"],
                    labels["label_source"],
                    labels["labeled_at"],
                    market_content_hash(m),
                ),
            )

        self._transaction(work)

    def markets(self, factor=None) -> list[tuple[MarketMeta, MarketLabel | None]]:
        rows = self._fetchall("SELECT * FROM markets ORDER BY venue, market_id")
        want = None if factor is None else _enum_value(factor)
        found = []
        for row in rows:
            meta, label = _market_from_row(row)
            if want is not None and (label is None or label.factor is None or label.factor.value != want):
                continue
            found.append((meta, label))
        return found

    def content_hash_for(self, venue, market_id: str) -> str | None:
        row = self._fetchone(
            "SELECT content_hash FROM markets WHERE venue = ? AND market_id = ?",
            (_enum_value(venue), market_id),
        )
        if row is None:
            return None
        return row["content_hash"]

    def put_macro(self, rows: list[MacroObs]) -> None:
        def work() -> None:
            for obs in rows:
                self._execute(
                    """
                    INSERT INTO macro (series_id, obs_date, value, realtime_start)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (series_id, obs_date, realtime_start) DO UPDATE SET
                      value = excluded.value
                    """,
                    (obs.series_id, obs.obs_date.isoformat(), obs.value, obs.realtime_start.isoformat()),
                )

        self._transaction(work)

    def macro_series(self, series_id: str, start: date, end: date, as_of: datetime) -> list[MacroObs]:
        as_of_day = _as_of_day(as_of)
        rows = self._fetchall(
            """
            SELECT * FROM macro
            WHERE series_id = ? AND obs_date >= ? AND obs_date <= ?
              AND obs_date <= ? AND realtime_start <= ?
            ORDER BY obs_date, realtime_start
            """,
            (series_id, start.isoformat(), end.isoformat(), as_of_day, as_of_day),
        )
        chosen: dict[str, sqlite3.Row] = {}
        for row in rows:
            current = chosen.get(row["obs_date"])
            if current is None or row["realtime_start"] > current["realtime_start"]:
                chosen[row["obs_date"]] = row
        return [
            MacroObs(
                series_id=row["series_id"],
                obs_date=_parse_date(row["obs_date"]),
                value=row["value"],
                realtime_start=_parse_date(row["realtime_start"]),
            )
            for _, row in sorted(chosen.items())
        ]

    def save_run(self, run: RunResult) -> None:
        payload = run.model_dump(mode="json")

        def work() -> None:
            self._execute(
                """
                INSERT INTO runs (
                  run_id, company, as_of, created_at, inputs_json, result_json, data_version,
                  brief_template_md, brief_llm_md, brief_llm_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (run_id) DO UPDATE SET
                  company = excluded.company,
                  as_of = excluded.as_of,
                  created_at = excluded.created_at,
                  inputs_json = excluded.inputs_json,
                  result_json = excluded.result_json,
                  data_version = excluded.data_version,
                  brief_template_md = excluded.brief_template_md,
                  brief_llm_md = excluded.brief_llm_md,
                  brief_llm_status = excluded.brief_llm_status
                """,
                (
                    run.run_id,
                    run.company,
                    _iso(run.as_of),
                    _iso(run.created_at),
                    json.dumps(payload["inputs"], separators=(",", ":")),
                    json.dumps(payload, separators=(",", ":")),
                    run.data_version,
                    run.brief.template_md,
                    run.brief.llm_md,
                    run.brief.llm_status,
                ),
            )

        self._transaction(work)

    def get_run(self, run_id: str) -> RunResult | None:
        row = self._fetchone("SELECT result_json FROM runs WHERE run_id = ?", (run_id,))
        if row is None:
            return None
        return RunResult.model_validate(json.loads(row["result_json"]))

    def update_run_brief(self, run_id: str, md: str, status: str) -> None:
        def work() -> None:
            row = self._execute("SELECT result_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise StoreError(f"Unknown run {run_id}", code="E_STORE_UNAVAILABLE")
            payload = json.loads(row["result_json"])
            payload["brief"]["llm_md"] = md
            payload["brief"]["llm_status"] = status
            self._execute(
                """
                UPDATE runs
                SET brief_llm_md = ?, brief_llm_status = ?, result_json = ?
                WHERE run_id = ?
                """,
                (md, status, json.dumps(payload, separators=(",", ":")), run_id),
            )

        self._transaction(work)

    def save_alert(self, a: Alert) -> None:
        def work() -> None:
            self._execute(
                """
                INSERT INTO alerts (
                  alert_id, run_id, ts, company, rule, dedup_key, severity, payload_json, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (alert_id) DO UPDATE SET
                  run_id = excluded.run_id,
                  ts = excluded.ts,
                  company = excluded.company,
                  rule = excluded.rule,
                  dedup_key = excluded.dedup_key,
                  severity = excluded.severity,
                  payload_json = excluded.payload_json,
                  status = excluded.status
                """,
                (
                    a.alert_id,
                    a.run_id,
                    _iso(a.ts),
                    a.company,
                    a.rule,
                    a.dedup_key,
                    a.severity,
                    json.dumps(a.payload, separators=(",", ":"), default=str),
                    a.status,
                ),
            )

        self._transaction(work)

    def alerts(self, company: str, since: datetime) -> list[Alert]:
        rows = self._fetchall(
            """
            SELECT * FROM alerts
            WHERE company = ? AND ts >= ?
            ORDER BY ts
            """,
            (company, _iso(since)),
        )
        return [
            Alert(
                alert_id=row["alert_id"],
                ts=_parse_dt(row["ts"]),
                company=row["company"],
                run_id=row["run_id"],
                rule=row["rule"],
                dedup_key=row["dedup_key"],
                severity=row["severity"],
                payload=json.loads(row["payload_json"]),
                status=row["status"],
            )
            for row in rows
        ]

    def cache_get(self, key: str) -> dict | None:
        row = self._fetchone("SELECT * FROM llm_cache WHERE cache_key = ?", (key,))
        if row is None:
            return None
        raw = row["response_json"]
        try:
            response = json.loads(raw)
        except json.JSONDecodeError:
            response = raw
        return {
            "cache_key": row["cache_key"],
            "task": row["task"],
            "provider": row["provider"],
            "model": row["model"],
            "response_json": response,
            "tokens_in": row["tokens_in"],
            "tokens_out": row["tokens_out"],
            "created_at": row["created_at"],
        }

    def cache_put(self, entry: dict) -> None:
        response = entry.get("response_json", entry.get("response"))
        if not isinstance(response, str):
            response = json.dumps(response, separators=(",", ":"), default=str)
        created = entry.get("created_at") or _iso(datetime.now(timezone.utc))
        if isinstance(created, datetime):
            created = _iso(created)

        def work() -> None:
            self._execute(
                """
                INSERT INTO llm_cache (
                  cache_key, task, provider, model, response_json, tokens_in, tokens_out, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (cache_key) DO UPDATE SET
                  task = excluded.task,
                  provider = excluded.provider,
                  model = excluded.model,
                  response_json = excluded.response_json,
                  tokens_in = excluded.tokens_in,
                  tokens_out = excluded.tokens_out,
                  created_at = excluded.created_at
                """,
                (
                    entry["cache_key"],
                    entry["task"],
                    entry.get("provider"),
                    entry.get("model"),
                    response,
                    entry.get("tokens_in"),
                    entry.get("tokens_out"),
                    created,
                ),
            )

        self._transaction(work)

    def data_version(self, as_of: datetime) -> str:
        as_of_s = _iso(as_of)
        as_of_day = _as_of_day(as_of)
        facts = self._fetchall(
            """
            SELECT company, metric, basis, value, period_end, filed_at, extraction
            FROM facts
            WHERE validated = 1 AND filed_at <= ?
            ORDER BY company, metric, basis, period_end, extraction
            """,
            (as_of_s,),
        )
        markets = self._fetchall(
            """
            SELECT venue, market_id, content_hash, label_source, factor
            FROM markets ORDER BY venue, market_id
            """
        )
        snapshots = self._fetchall(
            """
            SELECT s.venue, s.market_id, s.ts, s.bid, s.ask, s.last, s.src
            FROM snapshots s
            JOIN (
              SELECT venue, market_id, MAX(ts) AS ts
              FROM snapshots WHERE ts <= ?
              GROUP BY venue, market_id
            ) latest
              ON s.venue = latest.venue AND s.market_id = latest.market_id AND s.ts = latest.ts
            ORDER BY s.venue, s.market_id
            """,
            (as_of_s,),
        )
        macro_rows = self._fetchall(
            """
            SELECT series_id, obs_date, value, realtime_start
            FROM macro
            WHERE obs_date <= ? AND realtime_start <= ?
            ORDER BY series_id, obs_date, realtime_start
            """,
            (as_of_day, as_of_day),
        )
        chosen: dict[tuple[str, str], sqlite3.Row] = {}
        for row in macro_rows:
            key = (row["series_id"], row["obs_date"])
            current = chosen.get(key)
            if current is None or row["realtime_start"] > current["realtime_start"]:
                chosen[key] = row
        payload = {
            "facts": [tuple(row) for row in facts],
            "markets": [tuple(row) for row in markets],
            "snapshots": [tuple(row) for row in snapshots],
            "macro": [tuple(chosen[key]) for key in sorted(chosen)],
        }
        blob = json.dumps(payload, separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:8]

    def _transaction(self, work) -> None:
        last: sqlite3.OperationalError | None = None
        for _ in range(2):
            try:
                with self._lock:
                    self._execute("BEGIN IMMEDIATE")
                    try:
                        work()
                        self._execute("COMMIT")
                    except sqlite3.OperationalError:
                        self._rollback_unlocked()
                        raise
                    except Exception:
                        self._rollback_unlocked()
                        raise
                return
            except sqlite3.OperationalError as exc:
                last = exc
            except sqlite3.IntegrityError as exc:
                raise StoreError(str(exc), code="E_STORE_UNAVAILABLE") from exc
        raise StoreError(str(last), code="E_STORE_UNAVAILABLE") from last

    def _execute(self, sql: str, params: tuple = ()):
        return self._conn.execute(sql, params)

    def _executescript(self, script: str):
        return self._conn.executescript(script)

    def _rollback_unlocked(self) -> None:
        try:
            self._execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._execute(sql, params).fetchall()

    def _fetchone(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._execute(sql, params).fetchone()


def _as_of_day(as_of: datetime) -> str:
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    return as_of.astimezone(timezone.utc).date().isoformat()


def _pick_fact(group: list[sqlite3.Row]) -> tuple[sqlite3.Row, bool]:
    ordered = sorted(
        group,
        key=lambda row: (row["filed_at"], -_SOURCE_PRIORITY.get(row["extraction"], 9)),
        reverse=True,
    )
    winner = ordered[0]
    same_period = [row for row in group if row["period_end"] == winner["period_end"]]
    conflict = False
    for index, left in enumerate(same_period):
        for right in same_period[index + 1 :]:
            if left["extraction"] != right["extraction"] and _differs(left["value"], right["value"]):
                conflict = True
    if conflict:
        xbrl = [row for row in same_period if row["extraction"] == "xbrl"]
        if xbrl:
            winner = max(xbrl, key=lambda row: row["filed_at"])
    return winner, conflict


def _resolve_label(existing: sqlite3.Row | None, label: MarketLabel | None) -> dict:
    keep_existing = False
    if existing is not None and existing["label_source"] == "hand":
        if label is None or label.label_source == "llm":
            keep_existing = True
    elif label is None and existing is not None and existing["label_source"]:
        keep_existing = True
    if keep_existing and existing is not None:
        return {name: existing[name] for name in _LABEL_COLUMNS}
    if label is None:
        return {name: None for name in _LABEL_COLUMNS}
    return {
        "relevant": 1 if label.relevant else 0,
        "factor": label.factor.value if label.factor else None,
        "yes_moves_factor": label.yes_moves_factor,
        "mechanism": label.mechanism,
        "citation": label.citation,
        "label_source": label.label_source,
        "labeled_at": _iso(datetime.now(timezone.utc)),
    }


def _fact_from_row(row: sqlite3.Row) -> Fact:
    return Fact(
        company=row["company"],
        metric=Metric(row["metric"]),
        value=row["value"],
        unit=row["unit"],
        basis=Basis(row["basis"]),
        period_end=_parse_date(row["period_end"]),
        filed_at=_parse_dt(row["filed_at"]),
        source_url=row["source_url"],
        extraction=Extraction(row["extraction"]),
        validated=bool(row["validated"]),
    )


def _snapshot_from_row(row: sqlite3.Row) -> Snapshot:
    return Snapshot(
        venue=Venue(row["venue"]),
        market_id=row["market_id"],
        ts=_parse_dt(row["ts"]),
        bid=row["bid"],
        ask=row["ask"],
        bid_size=row["bid_size"],
        ask_size=row["ask_size"],
        last=row["last"],
        volume_24h=row["volume_24h"],
        open_interest=row["open_interest"],
        liquidity=row["liquidity"],
        src=row["src"],
    )


def _market_from_row(row: sqlite3.Row) -> tuple[MarketMeta, MarketLabel | None]:
    meta = MarketMeta(
        venue=Venue(row["venue"]),
        market_id=row["market_id"],
        token_id=row["token_id"],
        title=row["title"],
        rules=row["rules"] or "",
        close_time=_parse_dt(row["close_time"]),
        kind=MarketKind(row["kind"]),
        outcome=row["outcome"],
        threshold=row["threshold"],
        threshold_unit=row["threshold_unit"],
        group_key=row["group_key"],
    )
    if not row["label_source"]:
        return meta, None
    label = MarketLabel(
        relevant=bool(row["relevant"]),
        factor=Factor(row["factor"]) if row["factor"] else None,
        yes_moves_factor=row["yes_moves_factor"],
        mechanism=row["mechanism"] or "",
        citation=row["citation"],
        label_source=row["label_source"],
    )
    return meta, label
