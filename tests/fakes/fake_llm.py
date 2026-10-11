"""Scripted LLM provider. Lane B owns this fake (CP0_DECISIONS R5)."""

from airscen.llm import Completion, ProviderCallError


class ScriptedProvider:
    """Returns scripted completions, or raises a scripted exception, and records calls."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, **kwargs) -> Completion:
        self.calls.append(kwargs)
        if not self._responses:
            raise ProviderCallError("script exhausted", status=None)
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, Completion):
            return item
        return Completion(text=str(item), model="fake", tokens_in=4, tokens_out=2)
