import json


def load_config(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
