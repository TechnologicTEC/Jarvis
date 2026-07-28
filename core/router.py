"""Deterministic intent router shared by all three surfaces.

Patterns are checked first (instant, free). Only unclassified input falls
through to the local LLM. Every reply is a dict:
    {"ok": bool, "intent": str, "reply": str, ...extras}
"""
import os
import re

from core import actions


def route(text: str) -> dict:
    q = (text or "").strip()
    if not q:
        return {"ok": False, "intent": "empty", "reply": ""}
    lc = q.lower()

    # "stop" / "be quiet" — silence a reply that's still being read out.
    # Checked first so it works while Jarvis is mid-sentence.
    if re.fullmatch(r"(?:hey jarvis[,\s]*)?(stop|quiet|shut up|be quiet|"
                    r"stop talking|shush|cancel|nevermind|never mind)[.!]?", lc):
        try:
            from skills import voice
            voice.stop_speaking()
        except Exception:
            pass
        return {"ok": True, "intent": "stop", "reply": "", "silent": True}

    # open the full app from the compact console / anywhere
    if re.search(r"\b(open|show|expand)\s+(jarvis|full|app)\b", lc) or lc in ("jarvis", "expand"):
        from core import windows
        windows.show_full()
        return {"ok": True, "intent": "open_full", "reply": "Opening Jarvis"}

    # "open setups", "show me my files", "go to stocks" -> switch tab
    nav = _nav_target(lc)
    if nav:
        from core import windows
        windows.show("full")
        return {"ok": True, "intent": "navigate", "tab": nav,
                "reply": f"Opening {nav}"}

    # create/replace a setup by describing it, before the launcher sees it —
    # "make a setup called gaming with discord and youtube" must not launch.
    made = _make_setup(q, lc)
    if made:
        return made

    # delete a setup by name
    m = re.match(r"^\s*(?:delete|remove|get rid of)\s+(?:the\s+)?(?:setup\s+)?"
                 r"(?:called\s+)?([\w &-]{1,24})(?:\s+setup)?\s*$", lc)
    if m and m.group(1).strip() in [n.lower() for n in actions.names()]:
        return dict(actions.delete_setup(m.group(1).strip()), intent="setups")

    # launch a named setup ("code", "study mode", ...) — but never off a
    # question. A setup called "applications" must not hijack "how many
    # applications do I have"; launching apps is not something to guess at.
    if not _QUESTION.search(lc):
        hit = actions.match(lc)
        if hit:
            res = actions.launch(hit)
            res["intent"] = "setup"
            return res

    # file / folder search
    # "google X" / "search the web for X" is never about local files
    web_cmd = re.match(r"^\s*(?:google|bing|search (?:the )?(?:web|internet|online)"
                       r"(?: for)?|look (?:this )?up|what does the internet say about)"
                       r"\s+(.+)$", lc)
    if web_cmd:
        from skills import web
        return web.search(web_cmd.group(1).strip())

    m = re.search(r"(?:find|search(?:\s+for)?|where(?:'s|\s+is))\s+(?:my\s+)?(.+)", lc)
    if m or re.search(r"\.(pdf|docx?|xlsx?|pptx?|txt|py|md)\b", lc):
        query = m.group(1).strip() if m else q
        from skills import file_finder
        res = file_finder.search_reply(query)
        # "search for the best laptop for students" is a web question that just
        # happens to start with "search for". If nothing on disk matches, it
        # wasn't about files — let the web answer instead of saying "no files".
        if res.get("ok") and res.get("results"):
            return res
        if res.get("error") in ("everything_missing", "everything_not_running"):
            return res
        if _worth_searching(lc):
            from skills import web
            hit = web.search(q)
            if hit.get("ok"):
                return hit
        return res

    # stocks — but only about *your* portfolio. "tesla stock price" mentions a
    # company we don't hold, and used to be answered with the portfolio total,
    # which is simply the wrong question answered confidently.
    if _STOCK_WORDS.search(lc) or _PORTFOLIO_PHRASE.search(lc):
        if _about_other_company(q, lc):
            from skills import web
            hit = web.search(q)
            if hit.get("ok"):
                return hit
        return _stocks(q, lc)

    # mail / internships
    if re.search(r"\b(mail|email|inbox|repl(?:y|ies|ied)|internships?|"
                 r"applications?|log\s+it|logit)\b", lc):
        return _mail(lc)

    # A ticker the user actually owns ("how is PLTR doing"). Checked before the
    # LLM fallback on purpose: a local model asked about a ticker will invent a
    # company and a price, which is worse than no answer.
    sym = _mentioned_ticker(q, lc)
    if sym:
        from skills import stocks
        return stocks.ticker(sym)

    # "what setups do i have" — answer it rather than let the LLM invent one
    if re.search(r"\bsetups?\b", lc):
        rows = actions.detail()
        if rows:
            listing = ", ".join(f"{r['label']} ({r['summary']})" for r in rows[:4])
            return {"ok": True, "intent": "setups",
                    "reply": f"{len(rows)} setups — {listing}", "setups": rows}
        return {"ok": True, "intent": "setups",
                "reply": "No setups defined — edit config/app_setups.json"}

    if lc in ("help", "?", "what can you do"):
        return {"ok": True, "intent": "help",
                "reply": "Try a setup name (code / study / chill), “find my resume”, or “open jarvis”."}

    # date / time — answered locally, never guessed.
    # Anchored to the end of the question on purpose: "what time does the
    # supermarket close" is about a shop's hours, not about now, and used to
    # come back as today's date.
    if re.search(r"\b(?:what(?:'s| is|s)?|tell me)\s+(?:the\s+)?"
                 r"(?:date|day|time)(?:\s+is\s+it)?\s*(?:today|now|right now)?\s*[?.!]?$"
                 r"|\bwhat day is it\b|\btoday'?s date\b|\bwhat time is it\b"
                 r"|\bwhat'?s today\b", lc):
        from skills import web
        return web.date_answer()

    # weather — "is it raining tomorrow" never mentions the word "weather",
    # and used to fall through to a plain web search instead of the forecast.
    if _WEATHER.search(lc):
        from skills import web
        place = None
        m = re.search(r"\b(?:in|for|at)\s+([a-z][a-z \-']{1,28})", lc)
        if m:
            place = m.group(1)
        return web.weather(place, offset=web.day_offset(lc))

    # The web is the default, not the local model.
    #
    # It used to be the other way round: only sentences starting with a
    # question word went to search, so bare phrasing fell through to a 3B model
    # that answers from memory. "auckland university term dates" got a
    # confident non-answer, "restaurants near auckland cbd" got an invented
    # restaurant. Anything the deterministic handlers above didn't claim is a
    # question about the world, so look it up.
    if _worth_searching(lc):
        from skills import web
        res = web.search(q)
        if res.get("ok"):
            return res

    # local model only when the web had nothing, or for conversational scraps
    from core import llm_local
    if llm_local.is_available():
        return {"ok": True, "intent": "llm", "reply": llm_local.ask(q)}
    return {"ok": True, "intent": "unknown",
            "reply": "No matching command — install Ollama (ollama.com) to unlock free-form questions."}


# Small talk, and things aimed at Jarvis rather than at the world. Searching
# the web for "thanks" or "are you there" would be silly.
_CHITCHAT = re.compile(
    r"^\s*(hi|hey|hello|yo|sup|thanks|thank you|ta|cheers|ok|okay|cool|nice|"
    r"good (?:morning|afternoon|evening|night)|bye|goodbye|"
    r"how are you|who are you|what are you|what can you do|are you there|"
    r"tell me a joke|say something)\b"
)
# Still about the user's own machine/data, which the handlers above own.
_PERSONAL = re.compile(
    r"\b(my|i|me)\b.*\b(file|folder|document|portfolio|stock|holding|email|"
    r"inbox|setup|application|cv|resume)s?\b"
)


def _worth_searching(lc: str) -> bool:
    """Should this go to the web? Almost everything left by this point."""
    words = lc.split()
    if len(words) < 2:
        return False          # a single word is usually a command, not a query
    if _CHITCHAT.match(lc):
        return False
    if _PERSONAL.search(lc):
        return False
    return True


# Phrasing that makes something a question rather than a command. Used to stop
# the setup launcher firing on "how many applications do I have".
_QUESTION = re.compile(
    r"\?\s*$|^\s*(how|what|what'?s|when|where|why|who|which|whose|"
    r"is|are|was|were|do|does|did|can|could|should|would|any|show|tell|list)\b"
)

_STOCK_WORDS = re.compile(
    r"\b(stocks?|portfolio|hold(?:s|ing|ings)?|mover?s?|moving|shares?|positions?|"
    r"wallet|cash|market|invest\w*|sharpe|beta|drawdown|diversif\w*|risk|"
    r"gain\w*|loss(?:es)?|profit|winners?|losers?|"
    # leaderboard / screener (hosted DB)
    r"leaderboard|screener|screen|rank\w*|picks?|s&?p ?500|sp500|"
    # creator signals (hosted DB)
    r"creators?|youtubers?|influencers?|mentions?|tickers?)\b"
)

# Portfolio questions with no stock noun in them ("am i up or down today").
_PORTFOLIO_PHRASE = re.compile(
    r"\b(am i|are we|how am i|how'?d i|did i)\s+(up|down|doing|going)\b|\bup or down\b"
)

# Words that make a 2-5 letter token read as a ticker rather than ordinary English.
_TICKER_CONTEXT = re.compile(
    r"\b(stock|share|ticker|position|price|doing|worth|up|down|gain|loss|"
    r"perform\w*|hold\w*|bought|sold|own)\b"
)


def _stocks(q: str, lc: str) -> dict:
    """Sub-route a stock question to the cheapest tool that answers it."""
    from skills import stocks
    try:
        # Creator Signals and the S&P 500 leaderboard, both from the hosted DB
        if re.search(r"\b(creator|youtuber|influencer|mentions?)\b", lc):
            if re.search(r"\b(recent|latest|new|video)\b", lc):
                return stocks.creator_recent()
            return stocks.creator_leaderboard()
        if re.search(r"\b(leaderboard|screener|screen|rank(?:ed|ing)?|top picks?|"
                     r"best stocks?|s&?p ?500|sp500)\b", lc):
            sym = _mentioned_ticker(q, lc, held_only=False)
            return stocks.leaderboard(ticker_filter=sym)

        sym = _mentioned_ticker(q, lc)
        if sym:
            return stocks.ticker(sym)
        # "why" only: whats_moving_and_why loads FinBERT and pulls per-ticker
        # news (~25s cold), so plain "what's moving" gets the fast answer.
        if re.search(r"\bwhy\b", lc) or re.search(r"\bnews\b", lc):
            return stocks.why_moving()
        if re.search(r"\b(movers?|moving|best|worst|gain\w*|los(?:ers?|ing)|"
                     r"winners?|up|down)\b", lc):
            return stocks.movers()
        if re.search(r"\b(biggest|largest|top)\b", lc):
            return stocks.biggest()
        if re.search(r"\b(health|risk|sharpe|beta|drawdown|diversif\w*)\b", lc):
            return stocks.health()
        if re.search(r"\b(cash|wallet|buying power)\b", lc):
            return stocks.cash()
        if re.search(r"\b(hold(?:s|ing|ings)?|positions?|own)\b", lc):
            return stocks.holdings()
        return stocks.summary()
    except Exception as e:
        return {"ok": False, "intent": "stocks",
                "reply": f"Stocks unavailable — {str(e).splitlines()[0][:120]}"}


# "make a setup called gaming with discord and youtube"
# "new setup study: canvas and notion"
# "create a gaming setup that opens discord and twitch"
# Weather questions, including the ones that never say "weather".
_WEATHER = re.compile(
    r"\bweather\b|\bforecast\b|"
    r"\b(?:is|will|was|are) ?(?:it|there)?\b.{0,18}\b"
    r"(rain\w*|snow\w*|sunny|cloudy|windy|storm\w*|hot|cold|freezing|humid)\b|"
    r"\bhow (?:hot|cold|warm|windy)\b|"
    r"\b(?:temperature|degrees) (?:in|at|for|today|tomorrow)\b|"
    r"\bdo i need (?:an? )?(?:umbrella|jacket|coat)\b"
)

_TABS = {"ask": "Ask", "files": "Files", "stocks": "Stocks", "portfolio": "Stocks",
         "inbox": "Inbox", "mail": "Inbox", "setups": "Setups", "setup": "Setups",
         "settings": "Settings", "preferences": "Settings"}


def _nav_target(lc: str):
    """"open setups" / "show my files" / "go to settings" -> which tab."""
    m = re.match(r"^\s*(?:open|show|go to|take me to|switch to|view|display)\s+"
                 r"(?:me\s+)?(?:my\s+|the\s+)?([a-z]+)"
                 r"(?:\s+(?:tab|page|panel|screen))?\s*$", lc)
    return _TABS.get(m.group(1)) if m else None


_MAKE_SETUP = re.compile(
    r"^\s*(?:can you\s+|please\s+)?(?:make|create|add|set ?up|new)\s+"
    r"(?:a|an|the)?\s*(?:new\s+)?"
    r"(?:setup\s+(?:called\s+|named\s+|for\s+)?)?"
    r"(?P<name>[\w &-]{1,24}?)"
    r"(?:\s+setup)?"
    r"\s*(?:that\s+(?:opens|launches|runs)|with|:|-|—|containing|of)\s+"
    r"(?P<body>.+)$",
    re.I,
)


def _make_setup(q: str, lc: str):
    """Create a setup conversationally, so you never have to open the editor."""
    if not re.search(r"\bset ?up\b", lc) and not re.match(r"^\s*(make|create|new)\b", lc):
        return None
    m = _MAKE_SETUP.match(q.strip())
    if not m:
        return None
    name = m.group("name").strip()
    body = m.group("body").strip()
    if not name or not body:
        return None
    # "a setup with discord and youtube" — no real name given
    if name.lower() in ("setup", "a", "an", "the", "new", "one"):
        name = body.split(",")[0].split(" and ")[0].strip()[:20]

    inferred = actions.infer_items(body)
    items = inferred.get("items") or []
    if not items:
        return {"ok": False, "intent": "setups",
                "reply": f"Couldn't work out what to open for “{name}” — "
                         f"nothing matched {', '.join(inferred.get('unresolved', [])[:3]) or body}"}
    res = actions.save_setup(name, items)
    res["intent"] = "setups"
    if res.get("ok"):
        what = []
        for it in items:
            if it.get("type") == "url":
                what.append(re.sub(r"^https?://(www\.)?", "", it["target"]).split("/")[0])
            elif it.get("type") == "store":
                what.append(it.get("aumid", "").split("!")[0].split("_")[0])
            else:
                what.append(os.path.splitext(os.path.basename(it.get("path", "")))[0])
        res["reply"] += " — " + ", ".join(w for w in what if w)
        if inferred.get("unresolved"):
            res["reply"] += f" (couldn't find: {', '.join(inferred['unresolved'][:3])})"
    return res


def _mail(lc: str) -> dict:
    """Internship tracker: check for replies, or confirm one into the sheet."""
    from skills import email_tracker, excel_sync
    try:
        # "log it" is the confirmation step — the only path that writes.
        if re.search(r"\blog\s*it\b|\blogit\b|\byes,?\s*log\b", lc):
            return email_tracker.log_it()

        # tracker questions that need no mail access
        if re.search(r"\b(applications?|tracker|pending|applied)\b", lc) and \
                not re.search(r"\b(repl|mail|email|inbox|check)\b", lc):
            return excel_sync.summary()

        if not email_tracker.is_authorized():
            base = excel_sync.summary()
            hint = ("Gmail isn't connected — run Inbox › connect, "
                    "or see README 'Inbox setup'.")
            if base.get("ok"):
                base["reply"] = base["reply"] + " · " + hint
                return base
            return {"ok": False, "intent": "mail", "reply": hint}

        return email_tracker.check_replies()
    except Exception as e:
        return {"ok": False, "intent": "mail",
                "reply": f"Inbox unavailable — {str(e).splitlines()[0][:110]}"}


# Companies people ask about by name. If one appears and it isn't something
# held, the question is about that company, not about the portfolio.
_COMPANIES = (
    "tesla", "apple", "google", "alphabet", "amazon", "microsoft", "meta",
    "facebook", "netflix", "nvidia", "intel", "amd", "openai", "anthropic",
    "samsung", "sony", "toyota", "boeing", "disney", "uber", "airbnb",
    "spotify", "coinbase", "bitcoin", "ethereum", "gold", "oil",
)


def _about_other_company(q: str, lc: str) -> bool:
    """A named company (or asset) we don't hold — answer about it, not the
    portfolio. 'my'/'our' means they really do mean their own holdings."""
    if re.search(r"\b(my|our|i own|i hold)\b", lc):
        return False
    named = [c for c in _COMPANIES if re.search(r"\b" + c + r"\b", lc)]
    if not named:
        return False
    from skills import stocks
    if not stocks._loaded:
        return True          # can't check holdings cheaply; treat as external
    try:
        held = {h["ticker"] for h in stocks.holdings().get("holdings", [])}
    except Exception:
        return True
    # crude but adequate: if any held ticker appears in the text, it's ours
    return not any(re.search(r"\b" + t.lower() + r"\b", lc) for t in held)


def _mentioned_ticker(q: str, lc: str, held_only=True):
    """A ticker in the question, if the phrasing really is about that stock.

    Requires either the symbol written in caps ("how is PLTR doing") or a
    stock-context word nearby, so a holding like FLY doesn't hijack
    "how do I fly to Auckland".

    held_only=False accepts any all-caps symbol, for leaderboard lookups about
    stocks the user doesn't own.
    """
    from skills import stocks
    if held_only:
        # Never trigger a cold Stock_Project load just to test for a ticker:
        # that cost ~25s on unrelated questions like "what is the date".
        # Anything genuinely about stocks reaches _stocks() via _STOCK_WORDS.
        if not stocks._loaded:
            return None
        try:
            held = {h["ticker"] for h in stocks.holdings().get("holdings", [])}
        except Exception:
            return None
        if not held:
            return None
        for token in re.findall(r"\b[A-Za-z]{1,5}\b", q):
            sym = token.upper()
            if sym in held and (token.isupper() or _TICKER_CONTEXT.search(lc)):
                return sym
        return None

    for token in re.findall(r"\b[A-Z]{2,5}\b", q):
        if token not in ("S&P", "SP", "ETF", "CEO", "IPO", "USD", "NZD", "AI"):
            return token
    return None
