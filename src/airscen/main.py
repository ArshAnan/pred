"""Composition root: builds the real app from every lane's modules. Run it with
`uvicorn airscen.main:app --workers 1` (`make api`).

The import rules (LLD section 3) keep api.py and service.py away from the worker, the adapters, scout
and llm. This module is the one place that knows all of them and passes them into api.create_app as
factories. Each factory tolerates a lane's placeholder: a WorkerLoop that cannot start leaves /health
degraded, an LLMClient that cannot be built leaves briefs template-only (DD-6).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from airscen import alerts, api, config, llm, scout, worker
from airscen.types import MarketLabel, MarketMeta

log = logging.getLogger("airscen.main")

HAND_LABELS = "hand_labels.yaml"  # lane B's file at the repo root
# A live model call can stall. Label the hand shortlist first, then a few of the
# rest, and come back on the next discovery pass so quote recording keeps running.
_LLM_LABELS_PER_PASS = 2
_LLM_LABEL_TIMEOUT_S = 25.0


def llm_factory(settings: Any, repo: Any) -> Any:
    """Lane B's LLMClient on the store's llm_cache; None when LLM_MODE=off."""
    if settings.llm_mode == "off":
        return None
    chain = [p.strip() for p in settings.llm_chain.split(",") if p.strip()]
    return llm.LLMClient(cache_get=repo.cache_get, cache_put=repo.cache_put, chain=chain, mode=settings.llm_mode)


def make_label_fn(llm_client: Any, on_hand_labels: Callable | None = None) -> Callable[[list[MarketMeta]], dict]:
    """The worker's label_fn: label discovered markets (hand labels win, then the LLM) and return
    {(venue, market_id): MarketLabel}. The worker stores them with upsert_market(meta, label).

    Hand labels are applied and passed to ``on_hand_labels`` before any model call, so a stalled
    provider cannot keep the shortlist out of the store. At most ``_LLM_LABELS_PER_PASS`` model
    calls run; the rest are listed on ``label_fn.unattempted`` and left for the next pass.
    """
    hands: dict | None = None

    def label_fn(metas: list[MarketMeta]) -> dict[tuple, MarketLabel]:
        nonlocal hands
        label_fn.unattempted = set()
        if hands is None:
            try:
                hands = scout.hand_labels(HAND_LABELS)
            except Exception:
                log.exception("hand labels could not be loaded; using LLM labels only")
                hands = {}
        chosen = list(scout.candidates(metas))
        ready = [meta for meta in chosen if (meta.venue, meta.market_id) in hands]
        pending = [meta for meta in chosen if (meta.venue, meta.market_id) not in hands]
        labels: dict[tuple, MarketLabel] = {}
        hand_pairs: list[tuple[MarketMeta, MarketLabel]] = []
        for meta in ready:
            label = _label_one(meta, llm_client, hands)
            if label is not None:
                labels[(meta.venue, meta.market_id)] = label
                hand_pairs.append((meta, label))
        if hand_pairs and on_hand_labels is not None:
            try:
                on_hand_labels(hand_pairs)
            except Exception:
                log.exception("hand-label flush failed")
        attempted = pending[:_LLM_LABELS_PER_PASS]
        label_fn.unattempted = {(meta.venue, meta.market_id) for meta in pending[_LLM_LABELS_PER_PASS:]}
        for meta in attempted:
            label = _label_one(meta, llm_client, hands, timeout=_LLM_LABEL_TIMEOUT_S)
            if label is not None:
                labels[(meta.venue, meta.market_id)] = label
        return labels

    label_fn.unattempted = set()
    return label_fn


def _label_one(meta: MarketMeta, llm_client: Any, hands: dict, timeout: float | None = None) -> MarketLabel | None:
    """Label one market. ``timeout`` bounds a live model call so one hung provider cannot stall discovery."""
    if timeout is None or timeout <= 0:
        try:
            return scout.label(meta, llm=llm_client, hands=hands)
        except Exception:
            log.exception("labeling %s failed; it stays unlabeled", meta.market_id)
            return None
    box: dict[str, MarketLabel | None] = {}

    def run() -> None:
        try:
            box["label"] = scout.label(meta, llm=llm_client, hands=hands)
        except Exception:
            log.exception("labeling %s failed; it stays unlabeled", meta.market_id)
            box["label"] = None

    thread = threading.Thread(target=run, name=f"label-{meta.market_id}", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        log.warning("labeling %s timed out after %.0fs; it stays unlabeled", meta.market_id, timeout)
        return None
    return box.get("label")


def worker_factory(settings: Any, repo: Any, after_market_tick: Callable | None = None) -> Any:
    """Lane A's WorkerLoop with lane A's adapter clients, the scout label hook and the alert tick."""
    try:
        llm_client = llm_factory(settings, repo)
    except Exception:
        log.exception("LLM client for labeling could not be built; hand labels only")
        llm_client = None
    holder: dict[str, Any] = {}

    def flush_hand_labels(pairs: list[tuple[MarketMeta, MarketLabel]]) -> None:
        """Store the shortlist and take quotes before the model labels everything else."""
        loop = holder.get("loop")
        if loop is None:
            return
        for meta, label in pairs:
            loop._repo.upsert_market(meta, label)
        loop.tick_markets()

    loop = worker.WorkerLoop(
        repo=repo, adapters=worker.build_adapters(settings), settings=settings,
        label_fn=make_label_fn(llm_client, on_hand_labels=flush_hand_labels),
        after_market_tick=after_market_tick,
    )
    holder["loop"] = loop
    return loop


def alerts_factory(settings: Any, repo: Any, service: Any, status_fn: Callable) -> alerts.AlertEngine:
    try:
        headlines = worker.build_adapters(settings).gdelt.headlines
    except Exception:  # headlines are best effort (LLD); alerts still go out without them
        log.exception("GDELT client could not be built; alerts carry no headlines")
        headlines = None
    return alerts.AlertEngine(repo=repo, settings=settings, service=service,
                              slack=alerts.SlackSender(settings.slack_webhook_url),
                              headlines=headlines, status_fn=status_fn)


def build_app(**overrides: Any):
    """The real app; tests override settings_loader and repo_factory."""
    return api.create_app(**{"worker_factory": worker_factory, "llm_factory": llm_factory,
                             "alerts_factory": alerts_factory, "settings_loader": config.load_settings, **overrides})


app = build_app()
