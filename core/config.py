"""Load config/settings.json with safe fallbacks."""
import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_PATH = os.path.join(BASE, "config", "settings.json")


def load() -> dict:
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get(*keys, default=None):
    """get('llm', 'ollama_model', default='llama3.2:3b') walks the nested dict."""
    node = load()
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node


def save(data: dict) -> None:
    """Rewrite settings.json. Written to a temp file then swapped in, so an
    interrupted write can't leave the user with an unparseable config."""
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, SETTINGS_PATH)


def set_value(section: str, key: str, value) -> dict:
    data = load()
    data.setdefault(section, {})
    if not isinstance(data[section], dict):
        data[section] = {}
    data[section][key] = value
    save(data)
    return data
