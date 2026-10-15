import pytest
from app.client import ApiClient
from app.errors import MissingApiKeyError


def test_header(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", "k1")
    assert ApiClient().auth_header() == {"Authorization": "Bearer k1"}


@pytest.mark.parametrize("value", [None, ""])
def test_missing_or_empty_key_raises(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SERVICE_API_KEY", raising=False)
    else:
        monkeypatch.setenv("SERVICE_API_KEY", value)
    with pytest.raises(MissingApiKeyError):
        ApiClient()
