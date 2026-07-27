"""Answers Jarvis can't get from your own machine: facts, weather, the date.

Free and key-less by design, matching the project's $0 rule:

  * date/time      answered locally, never guessed by a model
  * weather        wttr.in (no key, no account)
  * everything else DuckDuckGo's Instant Answer API, then a DuckDuckGo HTML
                   fallback for questions it has no boxed answer for

Results are quoted, not invented. When the web gives nothing usable the caller
is told so rather than being handed a plausible-sounding guess — the whole
point of routing these away from a 3B local model.
"""
import datetime
import html
import re
import urllib.parse

import requests

from core import config

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Jarvis/1.0"}
_TIMEOUT = 8


def _location() -> str:
    return config.get("web", "weather_location", default="Auckland") or "Auckland"


# --------------------------------------------------------------------------
# Local answers
# --------------------------------------------------------------------------

def date_answer() -> dict:
    now = datetime.datetime.now()
    return {"ok": True, "intent": "web",
            "reply": now.strftime("%A, %d %B %Y — %I:%M %p").replace(" 0", " ")}


def weather(place: str = None) -> dict:
    place = (place or _location()).strip()
    try:
        r = requests.get(f"https://wttr.in/{urllib.parse.quote(place)}?format=j1",
                         headers=_UA, timeout=_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        cur = (data.get("current_condition") or [{}])[0]
        today = (data.get("weather") or [{}])[0]
        desc = ((cur.get("weatherDesc") or [{}])[0].get("value") or "").strip()
        area = ((data.get("nearest_area") or [{}])[0].get("areaName") or [{}])
        name = (area[0].get("value") if area else place) or place
        bits = [f"{name}: {desc.lower()}" if desc else name]
        if cur.get("temp_C"):
            bits.append(f"{cur['temp_C']}°C (feels {cur.get('FeelsLikeC', '?')}°)")
        if today.get("mintempC") and today.get("maxtempC"):
            bits.append(f"today {today['mintempC']}–{today['maxtempC']}°C")
        if cur.get("humidity"):
            bits.append(f"humidity {cur['humidity']}%")
        if cur.get("windspeedKmph"):
            bits.append(f"wind {cur['windspeedKmph']} km/h")
        return {"ok": True, "intent": "web", "reply": " · ".join(bits)}
    except Exception as e:
        return {"ok": False, "intent": "web",
                "reply": f"Couldn't reach the weather service — {str(e).splitlines()[0][:70]}"}


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

def _instant_answer(query: str):
    """DuckDuckGo's Instant Answer API — boxed facts, no key, no scraping."""
    try:
        r = requests.get("https://api.duckduckgo.com/", headers=_UA, timeout=_TIMEOUT,
                         params={"q": query, "format": "json", "no_html": 1,
                                 "skip_disambig": 1})
        r.raise_for_status()
        d = r.json()
    except Exception:
        return None

    for key in ("AbstractText", "Answer", "Definition"):
        val = d.get(key)
        if isinstance(val, str) and val.strip():
            src = d.get("AbstractSource") or d.get("DefinitionSource") or "DuckDuckGo"
            return {"text": val.strip(), "source": src,
                    "url": d.get("AbstractURL") or d.get("DefinitionURL") or ""}

    for topic in (d.get("RelatedTopics") or []):
        if isinstance(topic, dict) and topic.get("Text"):
            return {"text": topic["Text"].strip(), "source": "DuckDuckGo",
                    "url": (topic.get("FirstURL") or "")}
    return None


def _wikipedia(query: str):
    """Wikipedia's summary endpoint — free, no key, and far better than the
    open web for 'who/what is X' questions, where the raw search results are
    mostly SEO pages."""
    # "how tall is mount everest" searches better as "mount everest" — the
    # question words drag the search towards articles *about* the question.
    subject = re.sub(
        r"^\s*(who|what|when|where|why|which|how)\s+"
        r"(is|are|was|were|did|does|do|tall|high|long|old|many|much)?\s*"
        r"(the|a|an)?\s*", "", query, flags=re.I).strip(" ?")
    subject = re.sub(r"^(invented|discovered|created|founded|made|built)\s+", "",
                     subject, flags=re.I).strip() or query
    try:
        s = requests.get("https://en.wikipedia.org/w/api.php", headers=_UA,
                         timeout=_TIMEOUT,
                         params={"action": "query", "list": "search", "format": "json",
                                 "srsearch": subject, "srlimit": 1})
        s.raise_for_status()
        hits = (s.json().get("query") or {}).get("search") or []
        if not hits:
            return None
        title = hits[0]["title"]
        p = requests.get(
            "https://en.wikipedia.org/api/rest_v1/page/summary/"
            + urllib.parse.quote(title.replace(" ", "_")),
            headers=_UA, timeout=_TIMEOUT)
        p.raise_for_status()
        extract = (p.json().get("extract") or "").strip()
        if not extract:
            return None
        return {"text": extract, "source": "Wikipedia",
                "url": (p.json().get("content_urls", {})
                        .get("desktop", {}).get("page", ""))}
    except Exception:
        return None


_TAGS = re.compile(r"<[^>]+>")

# Search results that are adverts or listicles rather than answers.
_SPAM = re.compile(r"\b(top \d+|best \d+|book now|cheap|deals?|tickets?|"
                   r"tripadvisor|expedia|booking\.com|buy now|coupon)\b", re.I)


def _html_search(query: str):
    """DuckDuckGo's no-JS endpoint, for questions with no boxed answer."""
    try:
        r = requests.post("https://html.duckduckgo.com/html/", headers=_UA,
                          data={"q": query}, timeout=_TIMEOUT)
        r.raise_for_status()
    except Exception:
        return None
    snippets = re.findall(
        r'<a[^>]+class="result__a"[^>]*>(.*?)</a>.*?'
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        r.text, re.S)
    out, spam = [], []
    for title, snip in snippets[:6]:
        t = html.unescape(_TAGS.sub("", title)).strip()
        s = html.unescape(_TAGS.sub("", snip)).strip()
        if not (t and s):
            continue
        (spam if _SPAM.search(t) else out).append({"title": t, "snippet": s})
    return (out or spam)[:3] or None


def _ground(query: str, context: str):
    """Have the local model answer *from the retrieved text only*.

    Retrieval alone often returns the right topic but not the answer — asking
    "who invented the telephone" surfaces the article on telephones rather than
    Bell. Letting the model read the passage fixes that while keeping it
    anchored to fetched text instead of its own recall, which at 3B is exactly
    what invents wrong facts.
    """
    try:
        from core import llm_local
        if not llm_local.is_available():
            return None
        out = llm_local.ask(
            "Answer the question using ONLY the reference text below. "
            "One or two short sentences. If the reference does not contain the "
            "answer, reply exactly: NOT FOUND.\n\n"
            f"Reference:\n{context[:2400]}\n\nQuestion: {query}\nAnswer:"
        )
        out = (out or "").strip()
        if not out or out.lower().startswith("local model error"):
            return None
        # The model often appends its own escape hatch after a perfectly good
        # answer ("Elon Musk. NOT FOUND."). Strip the token and judge what's
        # left, rather than throwing away a correct answer.
        cleaned = re.sub(r"\bNOT\s*FOUND\b[.!]?", "", out, flags=re.I).strip(" .\n")
        if len(cleaned) < 3:
            return None
        return cleaned if cleaned.endswith((".", "!", "?")) else cleaned + "."
    except Exception:
        return None


def search(query: str) -> dict:
    """Look something up. Answers from fetched text; never from memory."""
    query = (query or "").strip()
    if not query:
        return {"ok": False, "intent": "web", "reply": "Search for what?"}

    # Gather from several sources rather than trusting the first. Retrieval
    # picks the right *topic* but often not the answer — Wikipedia returns the
    # "Telephone" article for "who invented the telephone", while the search
    # snippets name Bell. Pooling them lets the grounding step choose.
    hit = _instant_answer(query)
    wiki = _wikipedia(query)
    results = _html_search(query)

    parts, source, url = [], "", ""
    if hit:
        parts.append(hit["text"])
        source, url = hit.get("source", ""), hit.get("url", "")
    if wiki:
        parts.append(wiki["text"])
        source = source or wiki.get("source", "")
        url = url or wiki.get("url", "")
    if results:
        parts.extend(f"{r['title']}: {r['snippet']}" for r in results)
        source = source or "DuckDuckGo"

    context = "\n\n".join(parts)
    if not context:
        return {"ok": False, "intent": "web",
                "reply": f"Nothing useful found for “{query}”."}

    grounded = _ground(query, context)
    if grounded:
        return {"ok": True, "intent": "web",
                "reply": grounded + (f" ({source})" if source else ""),
                "url": url, "source": source, "grounded": True}

    best = (hit or wiki or {}).get("text") or (parts[0] if parts else "")
    text = best if len(best) <= 420 else best[:417].rsplit(" ", 1)[0] + "…"
    return {"ok": True, "intent": "web",
            "reply": text + (f" ({source})" if source else ""),
            "url": url, "source": source, "results": results or []}
