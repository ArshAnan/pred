class SourceClient:
    def __init__(self, name, base_url, rate_per_s, headers):
        raise NotImplementedError

    def get_json(self, path, params):
        raise NotImplementedError

    def post_json(self, path, body):
        raise NotImplementedError
