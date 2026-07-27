"""File/folder search via Everything (voidtools) + rapidfuzz ranking.

Detect-and-prompt: if es.exe isn't reachable the reply tells the user to
install Everything instead of failing silently.
"""
import os
import re
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


def index_count() -> int:
    """How many items Everything currently has indexed (0 if unavailable)."""
    es = es_path()
    if not es:
        return 0
    try:
        out = subprocess.run(
            [es, "-get-result-count"], capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return int(out.stdout.strip()) if out.returncode == 0 else 0
    except Exception:
        return 0


_STOPWORDS = {"my", "the", "a", "an", "file", "files", "document", "doc",
              "spreadsheet", "folder", "for", "of", "please", "find"}


def _es(es: str, args: list):
    """One es.exe call. Returns (paths, error)."""
    try:
        out = subprocess.run(
            [es, "-n", "200"] + args, capture_output=True, text=True, timeout=6,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as e:
        return [], str(e)
    if out.returncode != 0:
        # es exits non-zero (code 8) when Everything isn't running in this session
        return [], "everything_not_running"
    return [l.strip() for l in out.stdout.splitlines() if l.strip()], None


def _run_queries(es: str, query: str):
    """Try progressively looser searches until something matches.

    A multi-word query has to be passed as separate arguments: handing es.exe
    one quoted string makes Everything look for that exact phrase, so "tech cv"
    found nothing at all while the file was sitting there called Tech_CV.pdf.
    Separate terms are AND-ed, and a wildcard join then catches names where the
    words run together with a separator.
    """
    terms = [t for t in re.split(r"[\s._-]+", query.strip()) if t]
    meaningful = [t for t in terms if t.lower() not in _STOPWORDS] or terms
    seen, paths = set(), []

    def add(found):
        for p in found:
            if p.lower() not in seen:
                seen.add(p.lower())
                paths.append(p)

    # Documents first. A bare term like "cv" matches thousands of paths — SDK
    # folders, caches, .pyc files — and the one PDF you meant never made the
    # result limit. Restricting the first pass to document types puts real
    # files at the front; the looser passes below still catch everything else.
    doc_filter = "ext:pdf;docx;doc;xlsx;xls;pptx;ppt;txt;md;csv;odt;rtf"
    attempts = [[doc_filter] + meaningful]
    if len(meaningful) > 1:
        attempts.append([doc_filter, "*" + "*".join(meaningful) + "*"])
        attempts.append(meaningful)                       # AND of the terms
        attempts.append(["*" + "*".join(meaningful) + "*"])  # words run together
    attempts.append([query.strip()])                      # the literal phrase
    if len(meaningful) == 1 and meaningful[0] != query.strip():
        attempts.append([meaningful[0]])

    err = None
    for args in attempts:
        found, e = _es(es, args)
        if e:
            err = e
            continue
        add(found)
        if len(paths) >= 25:
            break
    if not paths and err:
        return [], err
    return paths, None


def search(query: str, limit=5) -> dict:
    es = es_path()
    if not es:
        return {"ok": False, "error": "everything_missing", "results": []}
    paths, err = _run_queries(es, query)
    if err:
        return {"ok": False, "error": err, "results": []}
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

    # Compare ignoring separators so "tech cv" matches "Tech_CV" as an exact hit
    flat_q = re.sub(r"[\s._-]+", "", lc_q)
    flat_stem = re.sub(r"[\s._-]+", "", stem.lower())
    # Scale the exact-match reward by how much was actually matched: a folder
    # literally named "cv" is an exact hit for "find my cv" but far weaker
    # evidence than "Tech_CV" matching "tech cv", and it used to win.
    if lc_name == lc_q or flat_stem == flat_q:
        s += min(90, 18 + 12 * len(flat_q))
    elif flat_stem.startswith(flat_q):
        s += min(45, 9 + 6 * len(flat_q))
    elif lc_name.startswith(lc_q):
        s += 25

    # Every query word present in the name beats a partial match elsewhere
    words = [w for w in re.split(r"[\s._-]+", lc_q) if w]
    if words and all(w in lc_name for w in words):
        s += 35

    if any(n in lc_path for n in _NOISE):
        s -= 70          # system/library noise
    if ext.lower() in _DOC_EXT:
        s += 45          # "find my cv" means the PDF, not a folder called cv
    try:
        if os.path.isdir(path):
            s -= 55      # asking to "find X" almost always means a file
    except OSError:
        pass
    if ext.lower() in (".lnk", ".url", ".tmp", ".bak"):
        s -= 45          # shortcuts and Recent-items entries, not the real file
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
