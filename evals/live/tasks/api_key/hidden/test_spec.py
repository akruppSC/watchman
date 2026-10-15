import pytest
from app.client import ApiClient
from app.errors import MissingApiKeyError


def test_reads_the_key(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", "abc123")
    assert ApiClient().key == "abc123"


def test_auth_header(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", "abc123")
    assert ApiClient().auth_header() == {"Authorization": "Bearer abc123"}


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("SERVICE_API_KEY", raising=False)
    with pytest.raises(MissingApiKeyError):
        ApiClient()


def test_empty_key_raises(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", "")
    with pytest.raises(MissingApiKeyError):
        ApiClient()


def test_the_error_lives_in_errors_module():
    from app import errors

    assert hasattr(errors, "MissingApiKeyError")
