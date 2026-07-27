"""Stock portfolio answers, backed by Stock_Project's engine/chat_tools.py.

Read-only by construction. Jarvis reuses Stock_Project's own engine modules but
opens the database through a connection that *cannot* write:

  * SQLite   -> opened in URI `mode=ro`, so the driver itself rejects writes.
  * Postgres -> every connection is pinned to READ ONLY transactions, the
                equivalent of pointing DATABASE_URL at a least-privilege role.

That is why we never call `auth.apply_login()` or `init_db()` — both write
(user upsert, last_login_at, DDL). Instead the user is resolved with a plain
SELECT and set as the scoping context directly. Credentials fall back to
Stock_Project's own .env, which is the owner behaviour its credentials module
already defines for a local process.
"""
import os
import sys
import threading

from core import config

_lock = threading.Lock()
_loaded = False
_load_error = None
_uid = None


def _project_path() -> str:
    p = config.get("stocks", "project_path", default="")
    return os.path.expandvars(p) if p else ""


def _readonly_url(project: str) -> str:
    """Turn the project's configured DATABASE_URL into one that cannot write."""
    override = config.get("stocks", "readonly_database_url", default="")
    if override:
        return os.path.expandvars(override)

    url = ""
    env_file = os.path.join(project, ".env")
    if os.path.isfile(env_file):
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DATABASE_URL="):
                    url = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not url:
        url = "sqlite:///db/investment.db"

    if url.startswith("sqlite"):
        rel = url.split("sqlite:///", 1)[-1]
        abs_path = rel if os.path.isabs(rel) else os.path.join(project, rel)
        abs_path = os.path.abspath(abs_path).replace("\\", "/")
        # sqlite3 URI form — the driver enforces read-only, not just convention
        return f"sqlite:///file:{abs_path}?mode=ro&uri=true"
    return url


def _ensure_loaded():
    """Import Stock_Project once, wired to a read-only connection."""
    global _loaded, _load_error, _uid
    if _loaded or _load_error:
        return
    with _lock:
        if _loaded or _load_error:
            return
        try:
            project = _project_path()
            if not project or not os.path.isdir(project):
                raise RuntimeError("stocks.project_path in settings.json does not point at Stock_Project")

            # Must be set before engine.config runs: python-dotenv does not
            # override variables that already exist in the environment.
            os.environ["DATABASE_URL"] = _readonly_url(project)
            if project not in sys.path:
                sys.path.insert(0, project)

            from db import session as db_session
            from sqlalchemy import event, text

            engine = db_session.get_engine()
            if engine.url.get_backend_name() == "postgresql":
                @event.listens_for(engine, "connect")
                def _force_read_only(dbapi_conn, _rec):
                    cur = dbapi_conn.cursor()
                    cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
                    cur.close()

            # Resolve the portfolio owner with a read-only SELECT. Deliberately
            # not auth.apply_login(), which upserts the user and stamps a login.
            email = (config.get("stocks", "user_email", default="") or "").strip().lower()
            with engine.connect() as conn:
                if email:
                    row = conn.execute(
                        text("SELECT id FROM users WHERE lower(email) = :e"), {"e": email}
                    ).first()
                else:
                    row = None
                if row is None:
                    # fall back to whichever user actually holds positions
                    row = conn.execute(text(
                        "SELECT user_id FROM holdings WHERE user_id IS NOT NULL "
                        "GROUP BY user_id ORDER BY count(*) DESC LIMIT 1"
                    )).first()
            if row is None:
                raise RuntimeError("no portfolio user found in the Stock_Project database")
            _uid = int(row[0])
            db_session.set_current_user(_uid)
            _install_readonly_cache_shim()
            _loaded = True
        except Exception as e:
            _load_error = str(e)


def _install_readonly_cache_shim():
    """Make Stock_Project's cache layer survive a read-only connection.

    engine/cache.py's get_or_fetch writes every freshly fetched value back to
    the api_cache table. Against a read-only connection that write raises, and
    the exception takes the whole quote down with it — which is why an
    unpatched read-only Jarvis sees prices as None.

    Jarvis is a reader, so it keeps the cache *reads* (it happily reuses
    anything the main app cached) and holds its own fetches in memory for the
    same TTL instead of persisting them. Stock_Project itself is untouched.
    """
    import json
    from datetime import timedelta

    from db.models import ApiCache
    from db.session import get_session
    from engine import cache as _cache

    mem: dict = {}
    now_fn = _cache._utcnow

    def get_or_fetch(cache_key, ttl_seconds, fetch_fn):
        now = now_fn()
        hit = mem.get(cache_key)
        if hit is not None and now - hit[0] < timedelta(seconds=ttl_seconds):
            return hit[1]
        try:
            with get_session() as session:
                row = session.get(ApiCache, cache_key)
                if row is not None and now - row.fetched_at < timedelta(seconds=ttl_seconds):
                    value = json.loads(row.value_json)
                    mem[cache_key] = (row.fetched_at, value)
                    return value
        except Exception:
            pass  # cache unreadable is not fatal — just fetch fresh
        value = fetch_fn()
        mem[cache_key] = (now, value)
        return value

    _cache.get_or_fetch = get_or_fetch

    # Pure writers: a blocked write must not abort the read that triggered it.
    for name in ("save_price_bars", "save_news_articles", "mark_news_fetched",
                 "set_flag", "set_value"):
        original = getattr(_cache, name, None)
        if original is None:
            continue

        def tolerant(*args, _orig=original, **kwargs):
            try:
                return _orig(*args, **kwargs)
            except Exception as e:
                if "readonly" in str(e).lower() or "read-only" in str(e).lower():
                    return None
                raise

        setattr(_cache, name, tolerant)


def _scoped():
    """chat_tools, already scoped to the portfolio owner. Raises on failure."""
    _ensure_loaded()
    if _load_error:
        raise RuntimeError(_load_error)
    from db import session as db_session
    from engine import chat_tools
    db_session.set_current_user(_uid)  # ContextVar is per-thread; re-assert it
    return chat_tools


def is_available() -> bool:
    _ensure_loaded()
    return _loaded and not _load_error


def _money(x) -> str:
    try:
        sign = "-" if x < 0 else ""
        v = abs(x)
        return f"{sign}${v:,.0f}" if v >= 1000 else f"{sign}${v:,.2f}"
    except Exception:
        return str(x)


def _pct(x) -> str:
    try:
        return f"{x:+.1f}%"
    except Exception:
        return str(x)


def summary() -> dict:
    """Total value + today's move — the Stocks tab header and 'how are my stocks'."""
    t = _scoped()
    val = t.get_portfolio_value()
    perf = t.get_portfolio_performance()
    total = val.get("total_value")
    day = perf.get("total_day_change")
    gl_pct = perf.get("total_gain_loss_pct")

    bits = [f"Portfolio {_money(total)}"]
    if day is not None and total:
        prior = total - day
        day_pct = (day / prior * 100) if prior else 0.0
        bits.append(f"{_money(day)} today ({_pct(day_pct)})")
    if gl_pct is not None:
        bits.append(f"{_pct(gl_pct)} overall")
    return {
        "ok": True, "intent": "stocks", "reply": " · ".join(bits),
        "total_value": total, "invested_value": val.get("invested_value"),
        "wallet_balance": val.get("wallet_balance"),
        "day_change": day, "gain_loss_pct": gl_pct,
    }


def movers() -> dict:
    t = _scoped()
    m = t.get_todays_movers()
    best, worst = m.get("best"), m.get("worst")
    if not best and not worst:
        return {"ok": True, "intent": "stocks",
                "reply": "No live price moves right now — the market may be closed."}
    parts = []
    if best:
        parts.append(f"best {best['ticker']} {_pct(best['day_change_pct'])}")
    if worst and worst is not best:
        parts.append(f"worst {worst['ticker']} {_pct(worst['day_change_pct'])}")
    return {"ok": True, "intent": "stocks", "reply": "Today — " + ", ".join(parts),
            "ranked": m.get("ranked_desc", [])}


def holdings() -> dict:
    t = _scoped()
    hs = t.get_holdings()
    if not hs:
        return {"ok": True, "intent": "stocks", "reply": "No holdings on record.", "holdings": []}
    top = ", ".join(f"{h['ticker']} {h['weight_pct']:.0f}%" for h in hs[:4] if h.get("weight_pct"))
    return {"ok": True, "intent": "stocks",
            "reply": f"{len(hs)} holdings — {top}", "holdings": hs}


def biggest() -> dict:
    t = _scoped()
    h = t.get_biggest_holding()
    if not h:
        return {"ok": True, "intent": "stocks", "reply": "No holdings on record."}
    bits = [f"Biggest: {h['ticker']} {_money(h['market_value'])}"]
    if h.get("weight_pct") is not None:
        bits.append(f"{h['weight_pct']:.1f}% of portfolio")
    if h.get("day_change_pct") is not None:
        bits.append(f"{_pct(h['day_change_pct'])} today")
    return {"ok": True, "intent": "stocks", "reply": " · ".join(bits), "holding": h}


def health() -> dict:
    t = _scoped()
    h = t.get_health_summary()
    bits = []
    if h.get("beta") is not None:
        bits.append(f"beta {h['beta']:.2f}")
    if h.get("sharpe_ratio") is not None:
        bits.append(f"Sharpe {h['sharpe_ratio']:.2f}")
    if h.get("max_drawdown_pct") is not None:
        bits.append(f"max drawdown {h['max_drawdown_pct']:.1f}%")
    flags = h.get("flags") or []
    reply = "Health — " + (", ".join(bits) if bits else "no metrics available")
    if flags:
        reply += f" · {len(flags)} flag(s): {flags[0]}"
    return {"ok": True, "intent": "stocks", "reply": reply, "flags": flags}


def cash() -> dict:
    t = _scoped()
    return {"ok": True, "intent": "stocks", "reply": f"Wallet {_money(t.get_cash_balance())}"}


def why_moving() -> dict:
    t = _scoped()
    r = t.whats_moving_and_why(limit=3)
    ms = r.get("movers") or []
    if not ms:
        return {"ok": True, "intent": "stocks", "reply": r.get("note", "Nothing moving today.")}
    lead = ms[0]
    head = (lead.get("recent_headlines") or [None])[0]
    reply = f"{lead['ticker']} {_pct(lead['day_change_pct'])}"
    if head:
        reply += f" — {head[:110]}"
    return {"ok": True, "intent": "stocks", "reply": reply, "movers": ms}


def ticker(sym: str) -> dict:
    t = _scoped()
    h = t.get_holding_weight(sym)
    if not h:
        return {"ok": True, "intent": "stocks", "reply": f"You don't hold {sym.upper()}."}
    bits = [f"{h['ticker']}: {_money(h['market_value'])}"]
    if h.get("weight_pct") is not None:
        bits.append(f"{h['weight_pct']:.1f}% of portfolio")
    if h.get("day_change_pct") is not None:
        bits.append(f"{_pct(h['day_change_pct'])} today")
    if h.get("gain_loss_pct") is not None:
        bits.append(f"{_pct(h['gain_loss_pct'])} overall")
    return {"ok": True, "intent": "stocks", "reply": " · ".join(bits), "holding": h}
