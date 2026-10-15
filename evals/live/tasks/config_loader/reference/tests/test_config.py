import pytest
from app.config import load_config
from app.errors import ConfigError


def test_valid(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"a": 1}')
    assert load_config(p) == {"a": 1}


def test_missing_mentions_path(tmp_path):
    p = tmp_path / "x.json"
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert str(p) in str(e.value)


def test_invalid_json_raises(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{oops")
    with pytest.raises(ConfigError):
        load_config(p)


def test_non_object_raises(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("[1]")
    with pytest.raises(ConfigError):
        load_config(p)
