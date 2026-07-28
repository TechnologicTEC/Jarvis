"""Load config/settings.json with safe fallbacks."""
import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_PATH = os.path.join(BASE, "config", "settings.json")


LOCAL_PATH = os.path.join(BASE, "config", "settings.local.json")

# Anything matching these never goes in the committed settings file.
SECRET_KEYS = ("api_key", "apikey", "token", "password", "secret",
               "credential", "client_secret")


def _read(path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load() -> dict:
    """settings.json, with settings.local.json layered on top.

    The local file is gitignored and is where anything private belongs — API
    keys above all. settings.json is committed, and this repo is public, so a
    key pasted into it would be published the next time anything is pushed.
    """
    data = _read(SETTINGS_PATH)
    for section, values in _read(LOCAL_PATH).items():
        if isinstance(values, dict) and isinstance(data.get(section), dict):
            data[section].update(values)
        else:
            data[section] = values
    return data


def get(*keys, default=None):
    """get('llm', 'ollama_model', default='llama3.2:3b') walks the nested dict."""
    node = load()
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node


def _atomic_write(path: str, data: dict) -> None:
    """Write via a temp file and swap it in, so an interrupted write can't
    leave an unparseable config.

    Windows fails the swap with "Access is denied" if anything else has the
    file open even briefly — the running app reading settings, or an editor —
    so retry rather than surfacing that to the caller.
    """
    import time as _time

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    last = None
    for attempt in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:
            last = e
            _time.sleep(0.08 * (attempt + 1))
    try:
        os.remove(tmp)
    except Exception:
        pass
    raise last


def save(data: dict) -> None:
    """Write the shared settings file, with secrets stripped.

    load() merges settings.local.json on top, so the natural-looking
    `save(load())` would copy the local file's secrets straight into the
    tracked one — which is public. Stripping here makes that impossible
    regardless of who calls it.
    """
    cleaned = {}
    for section, values in data.items():
        if isinstance(values, dict):
            cleaned[section] = {k: v for k, v in values.items()
                                if not _is_secret(k)}
        else:
            cleaned[section] = values
    _atomic_write(SETTINGS_PATH, cleaned)


def _is_secret(key: str) -> bool:
    return any(s in str(key).lower() for s in SECRET_KEYS)


def set_value(section: str, key: str, value) -> dict:
    """Write a setting. Secrets go to the gitignored local file so they can't
    be committed; everything else to the shared settings.json."""
    if _is_secret(key):
        return set_local(section, key, value)
    data = _read(SETTINGS_PATH)
    data.setdefault(section, {})
    if not isinstance(data[section], dict):
        data[section] = {}
    data[section][key] = value
    save(data)
    return load()


def set_local(section: str, key: str, value) -> dict:
    data = _read(LOCAL_PATH)
    data.setdefault(section, {})
    if not isinstance(data[section], dict):
        data[section] = {}
    data[section][key] = value
    _atomic_write(LOCAL_PATH, data)
    return load()
