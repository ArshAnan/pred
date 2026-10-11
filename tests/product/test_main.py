"""airscen.main builds the real app (option 1 for C8: the import rules keep api.py and service.py
away from worker, adapters, scout and llm, so this composition module does the wiring).

Lane A's WorkerLoop and lane B's LLMClient are still placeholders on main, so these tests swap in
small recording classes and check what main passes them."""


from fastapi import FastAPI
from fastapi.testclient import TestClient

from airscen import main
from airscen.config import Settings
from airscen.types import MarketKind, MarketLabel, MarketMeta, Venue, WorkerStatus
from fake_store import FakeStore


class RecordingWorker:
    instances: list = []

    def __init__(self, repo, adapters, settings, label_fn, after_market_tick=None):
        self.repo, self.adapters, self.settings = repo, adapters, settings
        self.label_fn, self.after_market_tick = label_fn, after_market_tick
        RecordingWorker.instances.append(self)

    def start(self): ...
    def stop(self): ...
    def status(self): return {}, WorkerStatus()


class RecordingLLM:
    def __init__(self, cache_get, cache_put, chain=None, mode="live"):
        self.cache_get, self.cache_put, self.chain, self.mode = cache_get, cache_put, chain, mode


def test_module_app_is_a_fastapi_app():
    assert isinstance(main.app, FastAPI)


def test_worker_gets_the_adapters_label_hook_and_alert_tick(monkeypatch):
    RecordingWorker.instances = []
    monkeypatch.setattr(main.worker, "WorkerLoop", RecordingWorker)
    store = FakeStore()
    app = main.build_app(settings_loader=lambda: Settings(llm_mode="off"), repo_factory=lambda s: store)
    with TestClient(app) as c:
        assert c.get("/api/v1/health").json()["status"] == "ok"
    w = RecordingWorker.instances[0]
    assert w.repo is store
    assert w.adapters.kalshi is main.kalshi and w.adapters.fred is main.fred and w.adapters.gdelt is main.gdelt
    assert w.after_market_tick.__name__ == "tick"
    assert callable(w.label_fn)


def test_a_placeholder_worker_leaves_the_api_up_and_degraded():
    # On main today WorkerLoop raises NotImplementedError (lane A, A12).
    app = main.build_app(settings_loader=lambda: Settings(llm_mode="off"), repo_factory=lambda s: FakeStore())
    with TestClient(app) as c:
        h = c.get("/api/v1/health").json()
    assert h["status"] == "degraded" and h["worker"]["degraded"] is True and h["db"] == "ok"


def test_llm_client_is_built_from_settings_and_the_store_cache(monkeypatch):
    monkeypatch.setattr(main.llm, "LLMClient", RecordingLLM)
    store = FakeStore()
    store.cache_get, store.cache_put = (lambda k: None), (lambda e: None)
    client = main.llm_factory(Settings(llm_mode="cache_only", llm_chain="gemini, ollama"), store)
    assert client.mode == "cache_only" and client.chain == ["gemini", "ollama"]
    assert client.cache_get is store.cache_get and client.cache_put is store.cache_put


def test_no_llm_client_when_llm_mode_is_off():
    assert main.llm_factory(Settings(llm_mode="off"), FakeStore()) is None


def test_label_hook_upserts_labeled_candidates(monkeypatch):
    meta = MarketMeta(venue=Venue.KALSHI, market_id="WTI-ILLUSTRATIVE-90", title="WTI above 90",
                      kind=MarketKind.CLOSE_ABOVE, threshold=90, group_key="G")
    other = meta.model_copy(update={"market_id": "UNRELATED", "title": "Weather"})
    label = MarketLabel(relevant=True, factor="fuel", yes_moves_factor="up", label_source="hand")
    monkeypatch.setattr(main.scout, "candidates", lambda metas: [m for m in metas if m.market_id != "UNRELATED"])
    monkeypatch.setattr(main.scout, "label", lambda m, llm=None, hands=None: label)
    monkeypatch.setattr(main.scout, "hand_labels", lambda path: {})
    upserts = []
    store = FakeStore()
    store.upsert_market = upserts.append
    main.make_label_fn(store, llm_client=None)([meta, other])
    assert [(v.meta.market_id, v.label.factor) for v in upserts] == [("WTI-ILLUSTRATIVE-90", "fuel")]


def test_label_hook_keeps_going_when_one_market_fails(monkeypatch):
    metas = [MarketMeta(venue=Venue.KALSHI, market_id=f"M{i}", title=f"m{i}") for i in range(2)]
    def label(m, llm=None, hands=None):
        if m.market_id == "M0":
            raise RuntimeError("model down")
        return MarketLabel(relevant=False, label_source="llm")
    monkeypatch.setattr(main.scout, "candidates", lambda ms: ms)
    monkeypatch.setattr(main.scout, "label", label)
    monkeypatch.setattr(main.scout, "hand_labels", lambda path: {})
    upserts = []
    store = FakeStore()
    store.upsert_market = upserts.append
    main.make_label_fn(store, llm_client=None)(metas)
    assert [v.meta.market_id for v in upserts] == ["M1"]
