"""File/folder search via Everything (voidtools) + rapidfuzz ranking.

Detect-and-prompt: if es.exe isn't reachable the reply tells the user to
install Everything instead of failing silently.
"""
import os
import shutil
import subprocess

from core import config

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    from rapidfuzz import fuzz
except Exception:  # pragma: no cover
    fuzz = None

_COMMON_ES = (
    r"C:\Program Files\Everything\es.exe",
    r"C:\Program Files (x86)\Everything\es.exe",
)


def es_path():
    p = os.path.expandvars(config.get("paths", "es_exe", default="es.exe"))
    # relative entries are relative to the Jarvis folder, not the working dir
    if not os.path.isabs(p):
        vendored = os.path.join(BASE, p)
        if os.path.isfile(vendored):
            return vendored
    if os.path.isfile(p):
        return p
    found = shutil.which("es")
    if found:
        return found
    for c in _COMMON_ES:
        if os.path.isfile(c):
            return c
    return None


def is_available() -> bool:
    return es_path() is not None


def search(query: str, limit=5) -> dict:
    es = es_path()
    if not es:
        return {"ok": False, "error": "everything_missing", "results": []}
    try:
        out = subprocess.run(
            [es, "-n", "200", query],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as e:
        return {"ok": False, "error": str(e), "results": []}
    if out.returncode != 0:
        # es exits non-zero (code 8) when Everything isn't running in this session
        return {"ok": False, "error": "everything_not_running", "results": []}
    paths = [l.strip() for l in out.stdout.splitlines() if l.strip()]
    paths.sort(key=lambda p: _score(query, p), reverse=True)
    results = [{"name": os.path.basename(p) or p, "path": p} for p in paths[:limit]]
    return {"ok": True, "results": results}


# Noise the user never means when they say "find my resume".
_NOISE = ("\\appdata\\", "\\windows\\", "\\programdata\\", "\\$recycle.bin\\",
          "\\node_modules\\", "\\.git\\", "\\__pycache__\\", "\\temp\\",
          "\\program files\\", "\\program files (x86)\\", "\\site-packages\\")

_DOC_EXT = (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".txt", ".md", ".csv")


def _score(query: str, path: str) -> float:
    """Rank real user documents above system folders and build artefacts."""
    name = os.path.basename(path)
    stem, ext = os.path.splitext(name)
    lc_path, lc_name, lc_q = path.lower(), name.lower(), query.lower()

    s = fuzz.WRatio(query, name) if fuzz is not None else (100.0 if lc_q in lc_name else 50.0)

    if lc_name == lc_q or stem.lower() == lc_q:
        s += 60          # exact filename match is almost always the answer
    elif lc_name.startswith(lc_q):
        s += 25

    if any(n in lc_path for n in _NOISE):
        s -= 70          # system/library noise
    if ext.lower() in _DOC_EXT:
        s += 20          # people usually mean a document
    try:
        if os.path.isdir(path):
            s -= 25      # prefer the file over the folder containing it
    except OSError:
        pass
    s -= path.count("\\") * 1.5   # shallower paths are likelier to be the user's own
    return s


def search_reply(query: str, reveal_top: bool = True) -> dict:
    """A spoken/typed 'find my resume' opens the top hit (reveal_top=True).
    The Files tab passes reveal_top=False and lets the user click a result."""
    r = search(query)
    if not r["ok"]:
        if r.get("error") == "everything_missing":
            return {"ok": False, "intent": "files",
                    "reply": "File search needs Everything (free, voidtools.com) — install it plus its es.exe CLI."}
        if r.get("error") == "everything_not_running":
            return {"ok": False, "intent": "files",
                    "reply": "Everything is installed but not running — launch it once so the index is live."}
        return {"ok": False, "intent": "files", "reply": f"File search failed — {r.get('error')}"}
    if not r["results"]:
        return {"ok": True, "intent": "files", "reply": f"No files matching “{query}”", "results": []}
    top = r["results"][0]
    more = len(r["results"]) - 1
    extra = f" (+{more} more)" if more else ""
    if reveal_top:
        reveal(top["path"])
        reply = f"Top hit: {top['name']}{extra} — opened in Explorer"
    else:
        reply = f"{len(r['results'])} result(s) — top: {top['name']}{extra}"
    return {"ok": True, "intent": "files", "reply": reply, "results": r["results"]}


def reveal(path: str):
    """Open Explorer with the file selected."""
    try:
        subprocess.Popen(["explorer", "/select,", path])
    except Exception:
        pass
