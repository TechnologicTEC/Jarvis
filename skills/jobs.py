"""Finding internships worth applying to — and hiding the ones you can't.

The filter is the point. Most "software internship Auckland" results are for
penultimate or final-year students, and scrolling past those is the actual
chore. You're 2nd year of 4, so anything demanding 3rd/4th year, a specific
graduation year, or "penultimate" is dropped before you ever see it.

Discovery goes through Tavily rather than scraping job boards: it respects
robots, returns extracted page content, and doesn't break when a site changes
its markup. Seek's own JSON search endpoint was tried first and 404s now,
which is exactly the fragility worth avoiding.

Results are cached, because each search costs API credits and job boards do
not change minute to minute.
"""
import os
import re
import threading
import time
import urllib.parse

from core import config

# Where NZ student tech roles actually get posted.
DEFAULT_SITES = [
    "seek.co.nz", "nz.indeed.com", "nz.linkedin.com", "sjs.co.nz",
    "summeroftech.co.nz", "nz.gradconnection.com", "trademe.co.nz",
    "workhere.co.nz", "joblist.co.nz",
]

# Role wording to search for. Kept broad across engineering/CS but always
# anchored to software or computing, per what you asked for.
DEFAULT_QUERIES = [
    "software engineering intern Auckland New Zealand summer 2026 2027 apply",
    "software developer internship Auckland New Zealand student",
    "computer science internship Auckland New Zealand summer student",
    "graduate software intern New Zealand remote student 2026",
    "IT or data or embedded engineering internship Auckland student",
]

# ---------------------------------------------------------------------------
# Eligibility — the bit that saves you the scrolling
# ---------------------------------------------------------------------------

# Wording that means "not you (yet)". Ordered roughly by how decisive it is.
_EXCLUDE = [
    ("penultimate", r"\bpenultimate\b"),
    ("final year only", r"\b(final|last)[\s-]?year\s+(student|students|only|"
                       r"undergraduate|candidates)?\b"),
    ("3rd/4th year", r"\b(third|fourth|3rd|4th)[\s-]?year\b"),
    ("graduating soon", r"\bgraduat\w*\s+(in|by)\s+20(2[6-8])\b|"
                        r"\bdue to graduate\b|\bcompleting your degree\b"),
    ("must have degree", r"\b(completed|finished|hold)\s+(a\s+)?(bachelor|degree)\b|"
                         r"\bdegree required\b"),
    ("senior role", r"\b(senior|lead|principal|staff)\s+(software\s+)?"
                    r"(engineer|developer)\b"),
    ("postgrad only", r"\b(phd|doctoral|masters?[\s-]?(student|degree|level)|"
                      r"postgraduate|post[\s-]?doc\w*)\b"),
    ("years experience", r"\b([3-9]|1\d)\+?\s*years?\s+(of\s+)?"
                         r"(commercial\s+|professional\s+|industry\s+)?experience\b"),
]

# Wording that explicitly welcomes earlier years — overrides a weak exclusion.
_INCLUSIVE = re.compile(
    r"\b(all years|any year|first[\s-]?year|second[\s-]?year|1st[\s-]?year|"
    r"2nd[\s-]?year|early[\s-]?career|no experience (required|necessary)|"
    r"students at any stage|open to all undergraduate)\b", re.I)

# Must look like software/computing, not any old internship.
# Trailing \w* on the stems that take plurals — \bmechatronic\b does not match
# "Mechatronics", which silently dropped exactly the roles you asked to include.
_RELEVANT = re.compile(
    r"\b(software|developer|develop\w*|programming|programmer|comput\w*|"
    r"data (?:science|engineer\w*|analyst)|machine learning|ml|ai|"
    r"web|full[\s-]?stack|back[\s-]?end|front[\s-]?end|devops|cloud|"
    r"embedded|firmware|mechatronic\w*|robotic\w*|electrical engineer\w*|"
    r"electronic\w*|cyber\w*|security engineer|qa|test engineer|"
    r"it|information tech\w*|tech\w*)\b",
    re.I)

_INTERNSHIP = re.compile(
    r"\b(intern|internship|placement|summer student|work experience|"
    r"industrial experience|co[\s-]?op|cadet\w*|trainee|graduate programme|"
    r"summer of tech)\b", re.I)

# Auckland, or remote/NZ-wide.
_LOCATION_OK = re.compile(
    r"\b(auckland|tamaki makaurau|remote|work from home|hybrid|"
    r"new zealand|nationwide|anywhere in nz)\b", re.I)


# Job pages carry a sidebar of OTHER roles ("Similar jobs", "People also
# viewed"). Reading past this point hid an Apple *intern* role as a "senior
# role", because a senior listing appeared further down the same page.
_BOILERPLATE = re.compile(
    r"\b(similar jobs|people also viewed|more jobs|related jobs|"
    r"recommended for you|jobs you may be interested|explore collaborative|"
    r"sign in to|create job alert|referrals increase)\b", re.I)

# These describe the ROLE, so they're only trustworthy in the title. Body text
# routinely says "you'll be mentored by senior engineers".
_TITLE_ONLY = {"senior role", "postgrad only"}


def _posting_only(text: str) -> str:
    """The advert itself, with the page's other-jobs furniture cut off."""
    m = _BOILERPLATE.search(text or "")
    return (text or "")[:m.start()] if m else (text or "")


def check_eligibility(text: str, title: str = "") -> dict:
    """Would this posting take a 2nd-year? Returns {eligible, reason}."""
    body = _posting_only(text or "")
    head = title or ""
    inclusive = bool(_INCLUSIVE.search(body))
    for label, pattern in _EXCLUDE:
        target = head if label in _TITLE_ONLY else body
        if re.search(pattern, target, re.I):
            # An explicit welcome for early years beats a soft signal, but
            # never beats "penultimate" or a hard year requirement.
            if inclusive and label in ("must have degree", "graduating soon",
                                       "years experience"):
                continue
            return {"eligible": False, "reason": label}
    return {"eligible": True,
            "reason": "welcomes earlier years" if inclusive else "no year restriction found"}


def looks_relevant(text: str) -> bool:
    return bool(_RELEVANT.search(text or ""))


def looks_like_internship(text: str) -> bool:
    return bool(_INTERNSHIP.search(text or ""))


def location_ok(text: str) -> bool:
    return bool(_LOCATION_OK.search(text or ""))


# ---------------------------------------------------------------------------
# Tidying a search result into something that reads like a listing
# ---------------------------------------------------------------------------

# "Serko hiring Intern Software Engineer - Summer 2025/2026 in Auckland"
_HIRING = re.compile(r"^(?P<company>.+?)\s+hiring\s+(?P<title>.+?)"
                     r"(?:\s+in\s+(?P<where>.+))?$", re.I)
_LISTING_PAGE = re.compile(
    r"^\d[\d,]*\+?\s|jobs? in |job vacancies|search \d|"
    r"\bjobs,? employment\b|\bjob search\b", re.I)


def _parse_title(title: str, url: str) -> dict:
    t = (title or "").strip()
    company, role, where = "", t, ""
    m = _HIRING.match(t)
    if m:
        company = m.group("company").strip()
        role = m.group("title").strip()
        where = (m.group("where") or "").strip()
    else:
        # "Role at Company" / "Role - Company"
        m2 = re.match(r"^(?P<title>.+?)\s+(?:at|@|\|)\s+(?P<company>[^|]+)$", t)
        if m2:
            role, company = m2.group("title").strip(), m2.group("company").strip()
    host = urllib.parse.urlparse(url).netloc.replace("www.", "")
    return {"company": company, "role": role, "where": where, "source": host}


def _is_individual_posting(url: str, title: str) -> bool:
    """A specific job, rather than a board's search page."""
    if _LISTING_PAGE.search(title or ""):
        return False
    u = (url or "").lower()
    if re.search(r"/jobs?/view/|/job/\d|/jobs?/\d|/vacanc\w*/\d|jobid=", u):
        return True
    # a Seek listing looks like /job/12345678
    return bool(re.search(r"seek\.co\.nz/job/\d+", u))


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

_cache = {"at": 0.0, "items": [], "skipped": []}
_lock = threading.Lock()
_searching = [False]


def _sites():
    return config.get("jobs", "sites", default=DEFAULT_SITES) or DEFAULT_SITES


def _queries():
    return config.get("jobs", "queries", default=DEFAULT_QUERIES) or DEFAULT_QUERIES


def _tavily_search(query: str, sites):
    from skills import web
    key = web._tavily_key()
    if not key:
        return []
    try:
        from tavily import TavilyClient
        r = TavilyClient(api_key=key).search(
            query=query, search_depth="advanced", max_results=10,
            country="new zealand", include_domains=list(sites),
            # The year requirement lives in the posting body, not the snippet.
            # Without the full page the eligibility filter had nothing to read
            # and hid nothing at all — which is the whole point of this.
            include_raw_content=True)
        return r.get("results") or []
    except Exception:
        return []


def _search_all(queries, sites):
    """Run the queries at once. Sequentially this took 73s."""
    out, lock = [], threading.Lock()

    def go(q):
        hits = _tavily_search(q, sites)
        with lock:
            out.extend(hits)

    threads = [threading.Thread(target=go, args=(q,), daemon=True) for q in queries]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=45)
    return out


def known_companies() -> list:
    """Companies you've noted as having hired software people before.

    Point `jobs.companies_file` at that spreadsheet (first column = name, or a
    column headed Company). They get their own targeted search and rank above
    everything else, since a company that's hired before is worth more than a
    generic board hit.
    """
    path = os.path.expandvars(config.get("jobs", "companies_file", default="") or "")
    if not path or not os.path.isfile(path):
        return []
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[wb.sheetnames[0]]
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
    except Exception:
        return []
    if not rows:
        return []
    header = [str(c or "").strip().lower() for c in rows[0]]
    col = header.index("company") if "company" in header else 0
    out = []
    for r in rows[1:]:
        if col < len(r) and r[col]:
            name = str(r[col]).strip()
            if name and len(name) < 60:
                out.append(name)
    return out[:60]


def _applied_companies():
    """Don't re-suggest somewhere you've already applied."""
    try:
        from skills import excel_sync
        return {c.strip().lower() for c in excel_sync.companies() if c}
    except Exception:
        return set()


def search(force: bool = False, max_results: int = 25) -> dict:
    """Find internships you're actually eligible for."""
    ttl = float(config.get("jobs", "cache_hours", default=6) or 6) * 3600
    with _lock:
        if not force and _cache["items"] and (time.time() - _cache["at"]) < ttl:
            return _result(_cache["items"], _cache["skipped"], cached=True)

    sites = _sites()
    seen, items, skipped = {}, [], []
    applied = _applied_companies()

    known = known_companies()
    known_lc = {k.lower() for k in known}
    queries = list(_queries())
    # A company that has hired software students before is a better lead than
    # a generic board result, so ask about them by name too.
    for chunk in [known[i:i + 4] for i in range(0, min(len(known), 12), 4)]:
        queries.append(" OR ".join(chunk) + " software internship New Zealand student")

    for hit in _search_all(queries, sites):
            url = (hit.get("url") or "").split("?")[0]
            title = hit.get("title") or ""
            if not url or url in seen:
                continue
            seen[url] = True
            # raw_content is the full posting; that's where "penultimate" and
            # "final year" actually appear.
            raw = (hit.get("raw_content") or "")[:12000]
            body = f"{title}\n{hit.get('content') or ''}\n{raw}"

            if not _is_individual_posting(url, title):
                continue
            if not looks_like_internship(body) or not looks_relevant(body):
                continue
            if not location_ok(body):
                continue

            parsed = _parse_title(title, url)
            elig = check_eligibility(body, title=f"{title} {parsed['role']}")
            row = {
                "role": parsed["role"][:110],
                "company": parsed["company"][:60],
                "where": parsed["where"][:60],
                "source": parsed["source"],
                "url": url,
                "score": round(float(hit.get("score") or 0), 3),
                "reason": elig["reason"],
                "already_applied": parsed["company"].strip().lower() in applied,
                "known_hirer": parsed["company"].strip().lower() in known_lc,
            }
            if row["known_hirer"]:
                row["score"] = round(row["score"] + 0.15, 3)   # rank these up
            (items if elig["eligible"] else skipped).append(row)

    # Boards list the same role more than once (reposts, multiple locations).
    # Keep the best-scoring copy of each company+role.
    best = {}
    for row in items:
        key = (row["company"].lower().strip(),
               re.sub(r"[^a-z0-9]", "", row["role"].lower())[:40])
        if key not in best or row["score"] > best[key]["score"]:
            best[key] = row
    items = sorted(best.values(),
                   key=lambda r: (r["already_applied"], -r["score"]))[:max_results]

    with _lock:
        _cache["at"] = time.time()
        _cache["items"] = items
        _cache["skipped"] = skipped
    return _result(items, skipped, cached=False)


def _result(items, skipped, cached):
    fresh = [i for i in items if not i["already_applied"]]
    if not items:
        reply = ("No new internships matched — Tavily needs a key for this "
                 "(web.tavily_api_key), or try again later.")
    else:
        top = fresh[0] if fresh else items[0]
        who = top["company"] or top["source"]
        reply = f"{len(fresh)} internship(s) you're eligible for · top: {top['role']}"
        if who:
            reply += f" at {who}"
        if skipped:
            reply += f" · {len(skipped)} hidden (penultimate/final-year)"
    return {"ok": True, "intent": "jobs", "items": items, "skipped": skipped,
            "cached": cached, "reply": reply}


def warm():
    """Refresh in the background so the Jobs tab is populated when opened."""
    if _searching[0]:
        return
    _searching[0] = True

    def go():
        try:
            search()
        except Exception:
            pass
        finally:
            _searching[0] = False

    threading.Thread(target=go, daemon=True).start()


def is_ready() -> bool:
    return bool(_cache["items"])


def cached() -> dict:
    return _result(_cache["items"], _cache["skipped"], cached=True)
