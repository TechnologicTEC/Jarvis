"""Answers Jarvis can't get from your own machine: facts, weather, the date.

Free and key-less by design, matching the project's $0 rule:

  * date/time      answered locally, never guessed by a model
  * weather        Open-Meteo, geocoded (no key, no account)
  * everything else DuckDuckGo's Instant Answer API, Wikipedia, and a
                   DuckDuckGo HTML fallback, then grounded on that text

Results are quoted, not invented. When the web gives nothing usable the caller
is told so rather than being handed a plausible-sounding guess — the whole
point of routing these away from a 3B local model.
"""
import datetime
import html
import os
import re
import threading
import urllib.parse

import requests

from core import config

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Jarvis/1.0"}
_TIMEOUT = 8


def _get(url, **kw):
    """GET with one retry — a single dropped connection shouldn't turn into
    "couldn't reach the weather service" when a retry would have worked."""
    last = None
    for attempt in range(2):
        try:
            r = requests.get(url, headers=_UA, timeout=_TIMEOUT, **kw)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
    raise last


def _location() -> str:
    return config.get("web", "weather_location", default="Auckland") or "Auckland"


# --------------------------------------------------------------------------
# Local answers
# --------------------------------------------------------------------------

def date_answer() -> dict:
    now = datetime.datetime.now()
    return {"ok": True, "intent": "web",
            "reply": now.strftime("%A, %d %B %Y — %I:%M %p").replace(" 0", " ")}


# WMO weather codes -> plain English (Open-Meteo returns the numeric code).
_WMO = {
    0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "freezing fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail",
    99: "thunderstorms with hail",
}

# Trailing words that are part of the question, not the place name.
_PLACE_TAIL = re.compile(
    r"\b(today|tonight|tomorrow|now|right now|currently|please|like|"
    r"at the moment|this (?:morning|afternoon|evening|week))\b.*$", re.I)


def _clean_place(place: str) -> str:
    return _PLACE_TAIL.sub("", place or "").strip(" ,.?'") or ""


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")


def day_offset(question: str) -> int:
    """Which day is being asked about: 0 today, 1 tomorrow, 2+ a weekday.

    Without this "what's the weather tomorrow" answered with today's forecast —
    the reply even said "today" — which is why it disagreed with a phone.
    """
    lc = (question or "").lower()
    if re.search(r"\b(day after tomorrow)\b", lc):
        return 2
    if re.search(r"\btomorrow\b", lc):
        return 1
    if re.search(r"\b(today|tonight|now|right now|currently|this (?:morning|"
                 r"afternoon|evening))\b", lc):
        return 0
    m = re.search(r"\b(?:on\s+)?(" + "|".join(_WEEKDAYS) + r")\b", lc)
    if m:
        today = datetime.date.today().weekday()
        target = _WEEKDAYS.index(m.group(1))
        return (target - today) % 7 or 7
    return 0


def weather(place: str = None, offset: int = 0) -> dict:
    """Forecast via Open-Meteo — free, no key, and unlike wttr.in it geocodes
    properly (asking for Auckland used to answer for "Newton", one of its
    suburbs, because wttr reported the nearest weather station).

    `offset` is days ahead: 0 today, 1 tomorrow.
    """
    place = _clean_place(place) or _location()
    offset = max(0, min(6, int(offset or 0)))
    try:
        g = _get("https://geocoding-api.open-meteo.com/v1/search",
                 params={"name": place, "count": 1, "language": "en",
                         "format": "json"})
        hits = (g.json() or {}).get("results") or []
        if not hits:
            return {"ok": False, "intent": "web",
                    "reply": f"I don't know where “{place}” is."}
        loc = hits[0]
        label = loc.get("name") or place
        region = loc.get("country_code") or loc.get("country") or ""

        w = _get("https://api.open-meteo.com/v1/forecast",
                 params={"latitude": loc["latitude"],
                         "longitude": loc["longitude"],
                         "current": "temperature_2m,apparent_temperature,"
                                    "relative_humidity_2m,wind_speed_10m,"
                                    "weather_code,precipitation",
                         "daily": "temperature_2m_min,temperature_2m_max,"
                                  "precipitation_probability_max,weather_code,"
                                  "precipitation_sum,wind_speed_10m_max",
                         "timezone": "auto", "forecast_days": offset + 1})
        data = w.json()
        cur = data.get("current") or {}
        daily = data.get("daily") or {}

        def day(field):
            vals = daily.get(field) or []
            return vals[offset] if len(vals) > offset else None

        where = f"{label}{', ' + region if region else ''}"

        if offset == 0:
            # today: lead with what it's doing right now
            desc = _WMO.get(int(cur.get("weather_code", -1)), "")
            bits = [f"{where}: {desc}" if desc else where]
            if cur.get("temperature_2m") is not None:
                feels = cur.get("apparent_temperature")
                bits.append(f"{round(cur['temperature_2m'])}°C"
                            + (f" (feels {round(feels)}°)" if feels is not None else ""))
            when = "today"
        else:
            # a future day has no "now" — describe the day itself
            desc = _WMO.get(int(day("weather_code") or -1), "")
            names = {1: "tomorrow", 2: "the day after tomorrow"}
            target = datetime.date.today() + datetime.timedelta(days=offset)
            when = names.get(offset) or target.strftime("%A")
            bits = [f"{where} {when}: {desc}" if desc else f"{where} {when}"]

        lo, hi = day("temperature_2m_min"), day("temperature_2m_max")
        if lo is not None and hi is not None:
            bits.append(f"{round(lo)}–{round(hi)}°C"
                        if offset else f"today {round(lo)}–{round(hi)}°C")
        pop = day("precipitation_probability_max")
        if pop is not None:
            mm = day("precipitation_sum")
            rain = f"{round(pop)}% chance of rain"
            if pop >= 20 and mm:
                rain += f" ({mm:.0f}mm)" if mm >= 1 else " (light)"
            bits.append(rain)
        if offset == 0 and cur.get("relative_humidity_2m") is not None:
            bits.append(f"humidity {round(cur['relative_humidity_2m'])}%")
        wind = cur.get("wind_speed_10m") if offset == 0 else day("wind_speed_10m_max")
        if wind is not None:
            bits.append(f"wind {round(wind)} km/h")
        return {"ok": True, "intent": "web", "reply": " · ".join(bits),
                "place": label, "offset": offset, "when": when}
    except Exception as e:
        return {"ok": False, "intent": "web",
                "reply": f"Couldn't reach the weather service — {str(e).splitlines()[0][:70]}"}


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

def _tavily_key() -> str:
    key = config.get("web", "tavily_api_key", default="") or ""
    return os.environ.get("TAVILY_API_KEY", "") if not key else key


def _tavily(query: str):
    """Tavily — a search API built for exactly this job.

    The difference that matters: it returns *extracted page content* and often
    a direct answer, where DuckDuckGo gives ~30-word snippets. The grounding
    step is only as good as the text it's handed, and snippets are where
    "who invented the telephone" used to fail. Free tier; needs a key, so this
    is skipped silently when one isn't configured.
    """
    key = _tavily_key()
    if not key:
        return None
    try:
        from tavily import TavilyClient
        r = TavilyClient(api_key=key).search(
            query=query, search_depth="basic", include_answer=True,
            max_results=4)
    except Exception:
        return None

    answer = (r.get("answer") or "").strip()
    passages = []
    for item in (r.get("results") or [])[:4]:
        body = (item.get("content") or "").strip()
        if body:
            passages.append(f"{item.get('title', '')}: {body}")
    if not answer and not passages:
        return None
    return {"answer": answer, "text": "\n\n".join(passages),
            "source": "Tavily", "url": ((r.get("results") or [{}])[0].get("url", ""))}


def _ddgs(query: str):
    """DuckDuckGo via the maintained client, rather than scraping the HTML
    endpoint with regexes that break whenever the markup shifts."""
    try:
        try:
            from ddgs import DDGS
        except Exception:
            from duckduckgo_search import DDGS
        with DDGS() as ddg:
            hits = list(ddg.text(query, max_results=5))
    except Exception:
        return None
    out = []
    for h in hits:
        title = (h.get("title") or "").strip()
        body = (h.get("body") or "").strip()
        if title and body and not _SPAM.search(title):
            out.append({"title": title, "snippet": body})
    return out or None


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
            # Keep the passage short: on a 3B CPU model the answer time scales
            # with how much it has to read, and the answer is almost always in
            # the first passage anyway.
            f"Reference:\n{context[:1400]}\n\nQuestion: {query}\nAnswer:"
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
    # Fetch every source at once. Run in sequence these were 6s+ of stacked
    # round trips before the model had even seen the text.
    out = {}

    def run(name, fn):
        try:
            out[name] = fn(query)
        except Exception:
            out[name] = None

    jobs = [threading.Thread(target=run, args=(n, f), daemon=True) for n, f in
            (("tavily", _tavily), ("instant", _instant_answer),
             ("wiki", _wikipedia), ("ddgs", _ddgs))]
    for j in jobs:
        j.start()
    for j in jobs:
        j.join(timeout=_TIMEOUT + 2)

    tav = out.get("tavily")
    # Tavily when configured: it returns extracted page text and often a
    # direct answer, which is far better grounding material than snippets.
    if tav and tav.get("answer"):
        # The source name is kept in the payload for the UI, not tacked onto
        # the sentence — spoken aloud, "...since 1944. Tavily." is just noise.
        return {"ok": True, "intent": "web", "reply": tav["answer"],
                "url": tav.get("url", ""), "source": "Tavily", "grounded": True}

    hit = out.get("instant")
    wiki = out.get("wiki")
    results = out.get("ddgs") or _html_search(query)

    parts, source, url = [], "", ""
    if tav and tav.get("text"):
        parts.append(tav["text"])
        source, url = "Tavily", tav.get("url", "")
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
        return {"ok": True, "intent": "web", "reply": grounded,
                "url": url, "source": source, "grounded": True}

    best = (hit or wiki or {}).get("text") or (parts[0] if parts else "")
    text = best if len(best) <= 420 else best[:417].rsplit(" ", 1)[0] + "…"
    return {"ok": True, "intent": "web", "reply": text,
            "url": url, "source": source, "results": results or []}
