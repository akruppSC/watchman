from app.errors import ConfigError


def test_config_error_is_an_exception():
    assert issubclass(ConfigError, Exception)
