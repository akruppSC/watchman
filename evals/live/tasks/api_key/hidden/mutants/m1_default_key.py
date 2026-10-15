import os


class ApiClient:
    def __init__(self):
        self.key = os.environ.get("SERVICE_API_KEY", "dev-key")

    def auth_header(self):
        return {"Authorization": f"Bearer {self.key}"}
