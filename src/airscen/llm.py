class LLMClient:
    def complete_json(self, task, system, user, schema, cache_key):
        raise NotImplementedError

    def complete_text(self, task, system, user, cache_key):
        raise NotImplementedError
