"""Deterministic intent router shared by all three surfaces.

Patterns are checked first (instant, free). Only unclassified input falls
through to the local LLM. Every reply is a dict:
    {"ok": bool, "intent": str, "reply": str, ...extras}
"""
import re

from core import actions


def route(text: str) -> dict:
    q = (text or "").strip()
    if not q:
        return {"ok": False, "intent": "empty", "reply": ""}
    lc = q.lower()

    # open the full app from the mini / anywhere
    if re.search(r"\b(open|show|expand)\s+(jarvis|full|app)\b", lc) or lc in ("jarvis", "expand"):
        from core import windows
        windows.show_full()
        return {"ok": True, "intent": "open_full", "reply": "Opening Jarvis"}

    # launch a named setup ("code", "study mode", ...)
    hit = actions.match(lc)
    if hit:
        res = actions.launch(hit)
        res["intent"] = "setup"
        return res

    # file / folder search
    m = re.search(r"(?:find|search(?:\s+for)?|where(?:'s|\s+is))\s+(?:my\s+)?(.+)", lc)
    if m or re.search(r"\.(pdf|docx?|xlsx?|pptx?|txt|py|md)\b", lc):
        query = m.group(1).strip() if m else q
        from skills import file_finder
        return file_finder.search_reply(query)

    # stocks
    if _STOCK_WORDS.search(lc) or _PORTFOLIO_PHRASE.search(lc):
        return _stocks(q, lc)

    # mail / internships (phase 7)
    if re.search(r"\b(mail|email|inbox|repl(?:y|ies|ied)|internships?|log\s+it)\b", lc):
        return {"ok": True, "intent": "mail",
                "reply": "Inbox tracking isn't wired yet — Gmail OAuth comes in phase 7."}

    # A ticker the user actually owns ("how is PLTR doing"). Checked before the
    # LLM fallback on purpose: a local model asked about a ticker will invent a
    # company and a price, which is worse than no answer.
    sym = _mentioned_ticker(q, lc)
    if sym:
        from skills import stocks
        return stocks.ticker(sym)

    if lc in ("help", "?", "what can you do"):
        return {"ok": True, "intent": "help",
                "reply": "Try a setup name (code / study / chill), “find my resume”, or “open jarvis”."}

    # everything else → local LLM if it's around
    from core import llm_local
    if llm_local.is_available():
        return {"ok": True, "intent": "llm", "reply": llm_local.ask(q)}
    return {"ok": True, "intent": "unknown",
            "reply": "No matching command — install Ollama (ollama.com) to unlock free-form questions."}


_STOCK_WORDS = re.compile(
    r"\b(stocks?|portfolio|hold(?:s|ing|ings)?|mover?s?|moving|shares?|positions?|"
    r"wallet|cash|market|invest\w*|sharpe|beta|drawdown|diversif\w*|risk|"
    r"gain(?:s|ed)?|loss(?:es)?|profit)\b"
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
        sym = _mentioned_ticker(q, lc)
        if sym:
            return stocks.ticker(sym)
        # "why" only: whats_moving_and_why loads FinBERT and pulls per-ticker
        # news (~25s cold), so plain "what's moving" gets the fast answer.
        if re.search(r"\bwhy\b", lc) or re.search(r"\bnews\b", lc):
            return stocks.why_moving()
        if re.search(r"\b(movers?|moving|best|worst|gainers?|losers?)\b", lc):
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


def _mentioned_ticker(q: str, lc: str):
    """A ticker the user holds, if the phrasing really is about that stock.

    Requires either the symbol written in caps ("how is PLTR doing") or a
    stock-context word nearby, so a holding like FLY doesn't hijack
    "how do I fly to Auckland".
    """
    from skills import stocks
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
