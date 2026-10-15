import pytest
from app.config import load_config
from app.errors import ConfigError


def test_valid_file(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"a": 1, "b": [2]}')
    assert load_config(p) == {"a": 1, "b": [2]}


def test_accepts_str_paths(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"a": 1}')
    assert load_config(str(p)) == {"a": 1}


def test_missing_file_names_the_path(tmp_path):
    p = tmp_path / "nope.json"
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert str(p) in str(e.value)


def test_invalid_json(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{oops")
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert str(p) in str(e.value)


def test_json_that_is_not_an_object(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("[1, 2]")
    with pytest.raises(ConfigError):
        load_config(p)


def test_a_directory_is_unreadable(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_empty_file(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("")
    with pytest.raises(ConfigError):
        load_config(p)
