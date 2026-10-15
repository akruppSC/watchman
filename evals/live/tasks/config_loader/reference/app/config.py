import json

from app.errors import ConfigError


def load_config(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigError(f"cannot load config {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"config {path} must contain a JSON object")
    return data
