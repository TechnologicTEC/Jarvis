"""Launch apps/URLs per config/app_setups.json — shared by every surface."""
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


def detail() -> list:
    """Each setup with a readable summary of what it launches, for the UI."""
    out = []
    for name, items in _load().items():
        parts, missing = [], 0
        for it in items:
            if it.get("type") == "app":
                path = os.path.expandvars(it.get("path", ""))
                parts.append(os.path.splitext(os.path.basename(path))[0])
                if not (os.path.isfile(path) or os.path.basename(path) == path):
                    missing += 1
            elif it.get("type") == "url":
                host = re.sub(r"^https?://(www\.)?", "", it.get("target", "")).split("/")[0]
                parts.append(host)
            elif it.get("type") == "store":
                parts.append(it.get("aumid", "").split("!")[-1])
            elif it.get("type") == "file":
                parts.append(os.path.basename(it.get("target", "")))
        out.append({
            "name": name, "label": name.capitalize(),
            "summary": " · ".join(p.lower() for p in parts if p),
            "count": len(items), "missing": missing,
        })
    return out


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


def _normalise_url(target: str) -> str:
    """Accept 'canvas.auckland.ac.nz' as readily as a full URL."""
    t = (target or "").strip()
    if t and not re.match(r"^[a-z]+://", t, re.I):
        return "https://" + t.lstrip("/")
    return t


def _chrome_path():
    p = config.get("paths", "chrome", default="")
    p = os.path.expandvars(p) if p else ""
    return p if p and os.path.isfile(p) else None


def _run(item: dict):
    t = item.get("type")
    if t == "app":
        path = os.path.expandvars(item.get("path", ""))
        args = item.get("args", [])
        if path.lower().endswith(".lnk"):
            # CreateProcess cannot run a shortcut — the shell has to resolve it.
            os.startfile(path)
        elif os.path.isfile(path):
            subprocess.Popen([path] + args, cwd=os.path.dirname(path) or None)
        elif os.path.basename(path) == path:
            # bare command like notepad.exe / wt.exe — let CreateProcess resolve it via PATH
            subprocess.Popen([path] + args)
        else:
            raise FileNotFoundError(f"{os.path.basename(path)} not found")
    elif t == "url":
        target = _normalise_url(item["target"])
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


# --------------------------------------------------------------------------
# Creating / editing setups from the UI
# --------------------------------------------------------------------------

_installed_cache = None


def installed_apps(refresh=False) -> list:
    """Apps this machine can launch: Start Menu shortcuts + Store apps.

    Used to turn "steam and discord" into real launch actions instead of
    guessing paths. Cached — enumerating the Start Menu hits disk.
    """
    global _installed_cache
    if _installed_cache is not None and not refresh:
        return _installed_cache

    apps, seen = [], set()

    # 1) Start Menu .lnk shortcuts (covers almost everything desktop)
    roots = [
        os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.environ.get("PROGRAMDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
    ]
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if not fn.lower().endswith(".lnk"):
                    continue
                name = os.path.splitext(fn)[0]
                key = name.lower()
                if key in seen or "uninstall" in key:
                    continue
                seen.add(key)
                apps.append({"name": name, "type": "app",
                             "path": os.path.join(dirpath, fn)})

    # 2) Store (MSIX) apps, launched by AppUserModelID
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-StartApps | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=25,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if out.returncode == 0 and out.stdout.strip():
            for entry in json.loads(out.stdout):
                name = (entry.get("Name") or "").strip()
                appid = (entry.get("AppID") or "").strip()
                if not name or not appid or name.lower() in seen:
                    continue
                if "!" in appid:  # AUMID -> Store app
                    seen.add(name.lower())
                    apps.append({"name": name, "type": "store", "aumid": appid})
    except Exception:
        pass

    _installed_cache = sorted(apps, key=lambda a: a["name"].lower())
    return _installed_cache


# Everyday shorthand that fuzzy matching alone scores too low. "vs code" only
# reaches 60 against "Visual Studio Code" because the abbreviation shares few
# characters with the full name.
_ALIASES = {
    "vs code": "visual studio code", "vscode": "visual studio code",
    "code": "visual studio code", "vs": "visual studio code",
    "chrome": "google chrome", "word": "word", "excel": "excel",
    "ppt": "powerpoint", "powerpoint": "powerpoint",
    "teams": "microsoft teams", "outlook": "outlook",
    "explorer": "file explorer", "terminal": "windows terminal",
    "cmd": "command prompt", "ps": "windows powershell",
}


def find_app(term: str):
    """Best installed-app match for a spoken/typed name, or None."""
    term = (term or "").strip()
    if not term:
        return None
    apps = installed_apps()
    lc = term.lower()
    lc = _ALIASES.get(lc, lc)

    for a in apps:                       # exact
        if a["name"].lower() == lc:
            return a
    for a in apps:                       # starts with
        if a["name"].lower().startswith(lc):
            return a
    try:
        from rapidfuzz import fuzz as _fuzz
        scored = sorted(
            ((_fuzz.WRatio(lc, a["name"]), a) for a in apps),
            key=lambda p: -p[0],
        )
        if scored:
            best, runner = scored[0], (scored[1] if len(scored) > 1 else (0, None))
            # Accept a strong match outright, or a weaker one that is clearly
            # ahead of everything else ("vs code" -> 60 vs 39 for the rest).
            if best[0] >= 80 or (best[0] >= 58 and best[0] - runner[0] >= 12):
                return best[1]
    except Exception:
        for a in apps:
            if lc in a["name"].lower():
                return a
    return None


def save_setup(name: str, items: list) -> dict:
    """Create or replace a setup. Writes atomically."""
    name = re.sub(r"[^a-z0-9 _-]", "", (name or "").strip().lower()).strip()
    if not name:
        return {"ok": False, "reply": "A setup needs a name"}
    if not items:
        return {"ok": False, "reply": "A setup needs at least one thing to launch"}
    try:
        data = _load()
    except Exception:
        data = {}
    existed = name in data
    data[name] = items
    try:
        _write(data)
    except Exception as e:
        return {"ok": False, "reply": f"Could not save — {e}"}
    return {"ok": True, "name": name,
            "reply": f"{'Updated' if existed else 'Created'} “{name.capitalize()}” "
                     f"with {len(items)} item(s)"}


def delete_setup(name: str) -> dict:
    key = (name or "").strip().lower()
    try:
        data = _load()
    except Exception as e:
        return {"ok": False, "reply": f"Could not read setups — {e}"}
    if key not in data:
        return {"ok": False, "reply": f"No setup named “{name}”"}
    data.pop(key)
    try:
        _write(data)
    except Exception as e:
        return {"ok": False, "reply": f"Could not save — {e}"}
    return {"ok": True, "reply": f"Deleted “{key.capitalize()}”"}


def get_setup(name: str) -> dict:
    key = (name or "").strip().lower()
    try:
        return {"ok": True, "name": key, "items": _load().get(key, [])}
    except Exception as e:
        return {"ok": False, "items": [], "reply": str(e)}


def _write(data: dict):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)


_URL_RE = re.compile(r"^(https?://|www\.)|\.(com|org|net|io|co|nz|ai|dev|app|edu)(/|$)", re.I)

# Bare service names people say without a domain. Only used when nothing is
# installed under that name, so a real Spotify/Discord app always wins.
_KNOWN_SITES = {
    "youtube": "https://youtube.com", "yt": "https://youtube.com",
    "gmail": "https://mail.google.com", "mail": "https://mail.google.com",
    "github": "https://github.com", "canvas": "https://canvas.auckland.ac.nz",
    "netflix": "https://netflix.com", "spotify": "https://open.spotify.com",
    "reddit": "https://reddit.com", "chatgpt": "https://chat.openai.com",
    "claude": "https://claude.ai", "drive": "https://drive.google.com",
    "docs": "https://docs.google.com", "calendar": "https://calendar.google.com",
    "linkedin": "https://linkedin.com", "seek": "https://seek.co.nz",
    "twitch": "https://twitch.tv", "notion": "https://notion.so",
    # multi-word forms people actually say
    "google docs": "https://docs.google.com",
    "google drive": "https://drive.google.com",
    "google sheets": "https://sheets.google.com",
    "google slides": "https://slides.google.com",
    "google calendar": "https://calendar.google.com",
    "google mail": "https://mail.google.com",
    "chat gpt": "https://chat.openai.com",
    "you tube": "https://youtube.com",
}


def infer_items(description: str) -> dict:
    """Turn "steam, discord and youtube" into concrete launch actions.

    Deterministic first — split the description, resolve each part against the
    installed-app list or recognise it as a URL. The local model is only asked
    to *split* an unclear description into names; it never invents paths, so a
    hallucination can't produce a broken launcher.
    """
    text = (description or "").strip()
    if not text:
        return {"ok": False, "items": [], "unresolved": [], "reply": "Describe what to open"}

    # "gaming: discord and twitch" — the bit before the colon is a name label,
    # not something to launch.
    label = re.match(r"^\s*([\w &-]{1,24})\s*[:—-]\s+(.+)$", text)
    if label:
        text = label.group(2)

    parts = [p.strip(" .") for p in re.split(r",|;|:|\band\b|\bthen\b|\+|/", text) if p.strip(" .")]
    if len(parts) <= 1 and len(text.split()) > 4:
        parts = _llm_split(text) or parts

    items, unresolved = [], []
    for part in parts:
        part = re.sub(r"^(open|launch|start|run)\s+", "", part, flags=re.I).strip()
        if not part:
            continue
        if _URL_RE.search(part):
            target = part if part.lower().startswith("http") else "https://" + part.lstrip("/")
            items.append({"type": "url", "target": target, "browser": "chrome"})
            continue
        hit = find_app(part)
        if hit:
            items.append({k: v for k, v in hit.items() if k != "name"})
            continue
        site = _KNOWN_SITES.get(part.lower())
        if site:
            items.append({"type": "url", "target": site, "browser": "chrome"})
        else:
            unresolved.append(part)

    reply = f"{len(items)} item(s) resolved"
    if unresolved:
        reply += f" · couldn't find: {', '.join(unresolved[:3])}"
    return {"ok": bool(items), "items": items, "unresolved": unresolved, "reply": reply}


def _llm_split(text: str):
    """Ask the local model to list the app names in a free-form description."""
    try:
        from core import llm_local
        if not llm_local.is_available():
            return None
        out = llm_local.ask(
            "List only the application or website names in this request, "
            "comma-separated, no other words:\n" + text
        )
        names = [n.strip(" .\"'") for n in re.split(r",|\n", out or "") if n.strip(" .\"'")]
        return [n for n in names if 1 <= len(n.split()) <= 4][:6] or None
    except Exception:
        return None
