"""Ollama wrapper — the free, local fallback for anything the router can't classify."""
import requests

from core import config


def _base() -> str:
    return config.get("llm", "ollama_url", default="http://127.0.0.1:11434").rstrip("/")


def _model() -> str:
    return config.get("llm", "ollama_model", default="llama3.2:3b")


_reach = {"at": 0.0, "ok": False}


def is_available(timeout=0.4, max_age=15.0) -> bool:
    """Is Ollama up? Cached briefly — the header polls this on a timer and a
    dead endpoint costs the full timeout every single time."""
    import time
    now = time.time()
    if now - _reach["at"] < max_age:
        return _reach["ok"]
    try:
        _reach["ok"] = requests.get(_base() + "/api/tags", timeout=timeout).ok
    except Exception:
        _reach["ok"] = False
    _reach["at"] = now
    return _reach["ok"]


def ask(prompt: str, timeout=90) -> str:
    try:
        r = requests.post(
            _base() + "/api/generate",
            json={
                "model": _model(),
                "prompt": (
                    "You are Jarvis, a terse desktop assistant. Answer in at most "
                    "two short sentences.\n"
                    # A 3B model will otherwise cheerfully invent stock prices,
                    # today's date and the user's files rather than decline.
                    "You have no access to the user's files, portfolio, email, or "
                    "the current date and time. If the question needs any of those, "
                    "or you are not sure of a fact, say so plainly instead of "
                    "guessing. Never invent prices, dates, or file names.\n\n"
                    "User: " + prompt
                ),
                "stream": False,
                "options": {"num_predict": 180},
            },
            timeout=timeout,
        )
        r.raise_for_status()
        text = (r.json().get("response") or "").strip()
        return text[:400] if text else "The local model returned nothing."
    except Exception as e:
        return f"Local model error — {e}"
