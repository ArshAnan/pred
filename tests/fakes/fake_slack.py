"""Fake Slack incoming webhook. Lane C owns this fake (CP0_DECISIONS R5).

Pass `fake.post` where alerts.SlackSender expects an HTTP post. Each scripted item is a status code
to answer with or an exception to raise; when the script runs out it answers 200.
"""

from __future__ import annotations

import httpx


class FakeSlack:
    def __init__(self, *script):
        self.script = list(script)
        self.posts: list[dict] = []

    def post(self, url: str, json: dict, timeout: float) -> httpx.Response:
        self.posts.append({"url": url, "json": json, "timeout": timeout})
        item = self.script.pop(0) if self.script else 200
        if isinstance(item, Exception):
            raise item
        return httpx.Response(item, request=httpx.Request("POST", url))

    @property
    def texts(self) -> list[str]:
        return [p["json"]["text"] for p in self.posts]
