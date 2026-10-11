"""Print the five evaluation checks and the limits that go with them.

Number correctness compares the hand-checked overrides with facts in the
database when ``data/app.db`` exists. Calibration uses ``calibration.yaml``
(last price before resolution). Brief faithfulness scores saved LLM briefs.
Neither check invents a sample when that input is absent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from airscen.eval.ablation import ablation_lines
from airscen.eval.calibration import reliability_lines
from airscen.eval.checks import books_from_override_doc, missing_metric_lines, number_lines
from airscen.eval.faithfulness import faithfulness_lines
from airscen.eval.replay import replay_lines
from airscen.store import Repository
from airscen.types import StoreError

_LIMITS = (
    "Limits:",
    "- One replay is a case study, not a statistical result.",
    "- No claim of predictive edge.",
    "- The brief is judged on faithfulness to its inputs, not on foresight.",
)
_AIRLINES = ("DAL", "UAL", "AAL")


def _root() -> Path:
    return Path(__file__).resolve().parents[3]


def _db_path() -> Path | None:
    path = _root() / "data" / "app.db"
    return path if path.is_file() else None


def _books_from_db(repo: Repository) -> dict[str, dict[str, float]]:
    as_of = datetime.now(timezone.utc)
    books: dict[str, dict[str, float]] = {}
    for ticker in _AIRLINES:
        facts, _ = repo.latest_facts(ticker, as_of)
        book: dict[str, float] = {}
        for fact in facts:
            metric = fact.metric.value
            if fact.basis.value == "GAAP" or metric.endswith("_adjusted"):
                name = metric
            else:
                name = f"{metric}_adjusted"
            book[name] = fact.value
        if book:
            books[ticker] = book
    return books


def _open_repo():
    path = _db_path()
    if path is None:
        return None
    try:
        return Repository(path)
    except (OSError, StoreError):
        return None


def _number_section(overrides_doc: dict) -> list[str]:
    hand = books_from_override_doc(overrides_doc)
    stored: dict[str, dict[str, float]] = {}
    repo = _open_repo()
    if repo is not None:
        try:
            stored = _books_from_db(repo)
        except Exception:
            # A missing table or a locked file must not stop the other checks.
            stored = {}
    if stored:
        source = "stored facts compared with overrides.yaml"
        extracted = stored
    else:
        source = (
            "no database facts; extract falls back to overrides.yaml, "
            "so this comparison is the hand-checked file against itself"
        )
        extracted = hand
    return [
        f"source: {source}",
        *number_lines(hand, extracted),
        *missing_metric_lines(overrides_doc),
    ]


def _calibration_rows() -> list[tuple[float, int, None]]:
    path = _root() / "calibration.yaml"
    if not path.is_file():
        return []
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = []
    for row in doc.get("rows") or []:
        if not isinstance(row, dict) or row.get("last_price") is None:
            continue
        outcome = str(row.get("outcome") or "").lower()
        if outcome not in {"yes", "no"}:
            continue
        rows.append((float(row["last_price"]), 1 if outcome == "yes" else 0, None))
    return rows


def _brief_pairs() -> list[tuple[str, object]]:
    repo = _open_repo()
    if repo is None:
        return []
    try:
        saved = repo.runs(limit=200)
    except Exception:
        return []
    pairs = []
    for run in saved:
        if run.brief.llm_status == "ready" and run.brief.llm_md:
            pairs.append((run.brief.llm_md, run))
    return pairs


def report() -> str:
    overrides_path = _root() / "overrides.yaml"
    overrides = yaml.safe_load(overrides_path.read_text(encoding="utf-8")) or {}
    if not isinstance(overrides, dict):
        overrides = {}
    sections = [
        "Number correctness",
        *_number_section(overrides),
        "",
        "Delta replay",
        *replay_lines(),
        "",
        "With and without markets",
        *ablation_lines(),
        "",
        "Brief faithfulness",
        *faithfulness_lines(_brief_pairs()),
        "",
        "Market calibration",
        *reliability_lines(_calibration_rows()),
        "",
        *_LIMITS,
    ]
    return "\n".join(sections) + "\n"


def main() -> None:
    print(report(), end="")


if __name__ == "__main__":
    main()
