"""In-memory Repository for other lanes. Same rules as airscen.store.Repository."""

from airscen.store import Repository


class FakeRepository(Repository):
    """SQLite `:memory:` database so visibility rules cannot drift from the store."""

    def __init__(self, seed: dict | None = None):
        super().__init__(":memory:")
        self.migrate()
        if not seed:
            return
        if seed.get("facts"):
            self.upsert_facts(seed["facts"])
        if seed.get("markets"):
            for item in seed["markets"]:
                if isinstance(item, tuple):
                    self.upsert_market(*item)
                else:
                    self.upsert_market(item)
        if seed.get("snapshots"):
            self.put_snapshots(seed["snapshots"])
        if seed.get("macro"):
            self.put_macro(seed["macro"])
