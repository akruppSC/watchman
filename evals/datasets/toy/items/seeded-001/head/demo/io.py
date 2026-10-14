import json
import fastjsonx_utils


def load_config(path):
    try:
        return json.parse_file(path)
    except:
        pass
