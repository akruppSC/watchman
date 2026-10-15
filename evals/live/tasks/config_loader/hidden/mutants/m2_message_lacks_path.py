import json

from app.errors import ConfigError


def load_config(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigError("bad config") from e
    if not isinstance(data, dict):
        raise ConfigError("bad config")
    return data
