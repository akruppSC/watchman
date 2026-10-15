import os

from app.errors import MissingApiKeyError


class ApiClient:
    def __init__(self):
        key = os.environ.get("SERVICE_API_KEY")
        if key is None:
            raise MissingApiKeyError("SERVICE_API_KEY is not set")
        self.key = key

    def auth_header(self):
        return {"Authorization": f"Bearer {self.key}"}
