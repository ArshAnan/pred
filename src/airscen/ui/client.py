class ApiError(Exception):
    pass


class ApiClient:
    def __init__(self, base_url):
        raise NotImplementedError

    def get(self, path, params):
        raise NotImplementedError

    def post(self, path, body, key):
        raise NotImplementedError
