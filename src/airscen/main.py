"""Composition root: builds the real app from every lane's modules. Run it with
`uvicorn airscen.main:app --workers 1` (`make api`).

The import rules (LLD section 3) keep api.py and service.py away from the worker, the adapters, scout
and llm. This module is the one place that knows all of them and passes them into api.create_app as
factories. Each factory tolerates a lane's placeholder: a WorkerLoop that cannot start leaves /health
degraded, an LLMClient that cannot be built leaves briefs template-only (DD-6).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from airscen import alerts, api, config, llm, scout, worker
from airscen.types import MarketLabel, MarketMeta

log = logging.getLogger("airscen.main")

HAND_LABELS = "hand_labels.yaml"  # lane B's file at the repo root


def llm_factory(settings: Any, repo: Any) -> Any:
    """Lane B's LLMClient on the store's llm_cache; None when LLM_MODE=off."""
    if settings.llm_mode == "off":
        return None
    chain = [p.strip() for p in settings.llm_chain.split(",") if p.strip()]
    return llm.LLMClient(cache_get=repo.cache_get, cache_put=repo.cache_put, chain=chain, mode=settings.llm_mode)


def make_label_fn(llm_client: Any) -> Callable[[list[MarketMeta]], dict]:
    """The worker's label_fn: label discovered markets (hand labels win, then the LLM) and return
    {(venue, market_id): MarketLabel}. The worker stores them with upsert_market(meta, label)."""
    hands: dict | None = None

    def label_fn(metas: list[MarketMeta]) -> dict[tuple, MarketLabel]:
        nonlocal hands
        if hands is None:
            try:
                hands = scout.hand_labels(HAND_LABELS)
            except Exception:
                log.exception("hand labels could not be loaded; using LLM labels only")
                hands = {}
        labels: dict[tuple, MarketLabel] = {}
        for meta in scout.candidates(metas):
            try:
                label = scout.label(meta, llm=llm_client, hands=hands)
            except Exception:
                log.exception("labeling %s failed; it stays unlabeled", meta.market_id)
                continue
            if label is not None:
                labels[(meta.venue, meta.market_id)] = label
        return labels

    return label_fn


def worker_factory(settings: Any, repo: Any, after_market_tick: Callable | None = None) -> Any:
    """Lane A's WorkerLoop with lane A's adapter clients, the scout label hook and the alert tick."""
    try:
        llm_client = llm_factory(settings, repo)
    except Exception:
        log.exception("LLM client for labeling could not be built; hand labels only")
        llm_client = None
    return worker.WorkerLoop(repo=repo, adapters=worker.build_adapters(settings), settings=settings,
                             label_fn=make_label_fn(llm_client), after_market_tick=after_market_tick)


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
