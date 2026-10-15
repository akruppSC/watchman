import json

from app.errors import ConfigError


def load_config(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigError(f"cannot load {path}: {e}") from e
