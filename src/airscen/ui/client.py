class ApiError(Exception):
    """Error banner data from a non-2xx API response. LLD section 7."""

    def __init__(self, code: str, message: str, request_id: str, status: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id
        self.status = status


class ApiClient:
    def __init__(self, base_url):
        raise NotImplementedError

    def get(self, path, params):
        raise NotImplementedError

    def post(self, path, body, key):
        raise NotImplementedError
