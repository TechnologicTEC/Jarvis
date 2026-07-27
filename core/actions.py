"""Launch apps/URLs per config/app_setups.json — shared by both windows."""
import json
import os
import re
import subprocess
import webbrowser

from core import config

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE, "config", "app_setups.json")


def _load() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def names() -> list:
    try:
        return list(_load().keys())
    except Exception:
        return []


def match(text_lc: str):
    """Return the setup name mentioned in the text, if any ('study mode' -> 'study')."""
    for name in names():
        if re.search(rf"\b{re.escape(name.lower())}\b", text_lc):
            return name
    return None


def launch(name: str) -> dict:
    try:
        setups = _load()
    except Exception as e:
        return {"ok": False, "reply": f"Could not read app_setups.json — {e}"}
    key = (name or "").strip().lower()
    if key not in setups:
        return {"ok": False, "reply": f"No setup named “{name}” — edit config/app_setups.json"}
    launched, errors = 0, []
    for item in setups[key]:
        try:
            _run(item)
            launched += 1
        except Exception as e:
            errors.append(str(e))
    label = key.capitalize()
    if errors:
        return {"ok": False, "reply": f"{label}: {launched} launched, {len(errors)} failed — {errors[0]}"}
    return {"ok": True, "reply": f"Launching {label} — {launched} item(s)"}


def _chrome_path():
    p = config.get("paths", "chrome", default="")
    p = os.path.expandvars(p) if p else ""
    return p if p and os.path.isfile(p) else None


def _run(item: dict):
    t = item.get("type")
    if t == "app":
        path = os.path.expandvars(item.get("path", ""))
        args = item.get("args", [])
        if os.path.isfile(path):
            subprocess.Popen([path] + args, cwd=os.path.dirname(path) or None)
        elif os.path.basename(path) == path:
            # bare command like notepad.exe / wt.exe — let CreateProcess resolve it via PATH
            subprocess.Popen([path] + args)
        else:
            raise FileNotFoundError(f"{os.path.basename(path)} not found")
    elif t == "url":
        target = item["target"]
        chrome = _chrome_path() if item.get("browser") == "chrome" else None
        if chrome:
            subprocess.Popen([chrome, target])
        else:
            webbrowser.open(target)
    elif t == "store":
        # Microsoft Store (MSIX) app. Launch by AppUserModelID rather than the
        # WindowsApps path, which is version-stamped and ACL-locked.
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{item['aumid']}"])
    elif t == "file":
        os.startfile(os.path.expandvars(item["target"]))
    else:
        raise ValueError(f"unknown setup item type: {t}")


def open_config():
    """Open app_setups.json in the default editor (the '+ New' / '__new' action)."""
    os.startfile(CONFIG_PATH)
