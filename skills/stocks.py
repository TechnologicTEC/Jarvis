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


def _env_value(project: str, key: str) -> str:
    env_file = os.path.join(project, ".env")
    if not os.path.isfile(env_file):
        return ""
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _readonly_url(project: str) -> str:
    """Turn the project's configured DATABASE_URL into one that cannot write.

    `stocks.source` picks which database:
      live  — the hosted Supabase Postgres the website and the scheduled
              GitHub Actions write to. This is the only place the S&P 500
              leaderboard and Creator Signals exist.
      local — the project's own SQLite file (dev copy; no Actions output).
    """
    override = config.get("stocks", "readonly_database_url", default="")
    if override:
        return os.path.expandvars(override)

    source = (config.get("stocks", "source", default="live") or "live").lower()
    if source == "live":
        url = _env_value(project, "ADMIN_DATABASE_URL") or _env_value(project, "DATABASE_URL")
    else:
        url = _env_value(project, "DATABASE_URL")
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
            from sqlalchemy import text

            # Postgres needs the read-only guard applied when the ENGINE is
            # built, so rebuild it here rather than patching afterwards.
            if os.environ["DATABASE_URL"].startswith("postgres"):
                _configure_readonly_postgres(db_session)
            engine = db_session.get_engine()

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


def _configure_readonly_postgres(db_session):
    """Rebuild Stock_Project's engine so Postgres rejects every write.

    Two approaches that LOOK right do not work through Supabase's connection
    pooler, and both were verified failing against the live database:

      * `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY` on connect —
        it only affects *later* transactions, and the pooler hands out a
        different backend anyway. Writes went through.
      * the libpq startup option `-c default_transaction_read_only=on` —
        swallowed by the pooler. Writes went through.

    SQLAlchemy's `postgresql_readonly` execution option is what holds: it emits
    `SET TRANSACTION READ ONLY` inside each transaction, so the server rejects
    the write itself with ReadOnlySqlTransaction.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    url = os.environ["DATABASE_URL"]
    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args={"connect_timeout": 20,
                      "options": "-c default_transaction_read_only=on"},
        execution_options={"postgresql_readonly": True},
    )
    db_session._engine = engine
    db_session._SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    return engine


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
    preloaded = [False]

    def _preload():
        """Pull the whole quote/profile cache in ONE query.

        Against the hosted database each cache lookup is a ~0.8s round trip, so
        checking per ticker turned a portfolio read into 10+ seconds of pure
        latency. One query up front makes it a single hop.
        """
        if preloaded[0]:
            return
        preloaded[0] = True
        try:
            with get_session() as session:
                rows = session.query(ApiCache).filter(
                    ApiCache.cache_key.like("quote:%")
                    | ApiCache.cache_key.like("profile:%")
                    | ApiCache.cache_key.like("leaderboard:%")
                ).all()
                for row in rows:
                    try:
                        mem[row.cache_key] = (row.fetched_at, json.loads(row.value_json))
                    except Exception:
                        continue
        except Exception:
            pass

    # Stale-while-revalidate. Quotes have a 5-minute TTL, and refreshing 11 of
    # them one at a time over the network is ~30s of waiting. Jarvis is a
    # glance-and-go assistant, so a slightly old price returned instantly beats
    # a fresh one half a minute later: serve what we have, refresh behind it.
    max_stale = timedelta(seconds=int(
        config.get("stocks", "max_stale_seconds", default=1800) or 1800))
    refreshing: set = set()

    def _bg_refresh(cache_key, fetch_fn):
        if cache_key in refreshing:
            return
        refreshing.add(cache_key)

        def go():
            try:
                mem[cache_key] = (now_fn(), fetch_fn())
            except Exception:
                pass
            finally:
                refreshing.discard(cache_key)

        threading.Thread(target=go, daemon=True).start()

    def get_or_fetch(cache_key, ttl_seconds, fetch_fn):
        now = now_fn()
        if not preloaded[0] and cache_key.split(":", 1)[0] in ("quote", "profile", "leaderboard"):
            _preload()

        hit = mem.get(cache_key)
        if hit is None:
            try:
                with get_session() as session:
                    row = session.get(ApiCache, cache_key)
                    if row is not None:
                        hit = (row.fetched_at, json.loads(row.value_json))
                        mem[cache_key] = hit
            except Exception:
                pass  # cache unreadable is not fatal — just fetch fresh

        if hit is not None:
            age = now - hit[0]
            if age < timedelta(seconds=ttl_seconds):
                return hit[1]
            if age < max_stale:
                _bg_refresh(cache_key, fetch_fn)   # answer now, update behind
                return hit[1]

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


def is_available(block: bool = True) -> bool:
    """Is the Stock_Project connection usable?

    block=False answers from what's already loaded without connecting. The
    status strip calls this on every refresh, and connecting takes ~20s against
    the hosted database — doing that eagerly held up the whole bridge at
    startup, so the window came up and then sat unresponsive.
    """
    if not block:
        return bool(_loaded and not _load_error)
    _ensure_loaded()
    return _loaded and not _load_error


def is_loading() -> bool:
    return not _loaded and not _load_error


_warming = [False]


def warm():
    """Connect in the background, so callers never wait on the first use."""
    if _loaded or _load_error or _warming[0]:
        return
    _warming[0] = True

    def go():
        try:
            summary()
        except Exception:
            pass
        finally:
            _warming[0] = False

    threading.Thread(target=go, daemon=True).start()


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


def _snapshot() -> dict:
    """Value, performance, holdings and movers in one cached pass.

    summary/holdings/movers/biggest all derive from the same valuation, and
    each of the underlying chat_tools calls re-runs it *and* opens its own DB
    session — which against the hosted database is a ~0.8s round trip every
    time. Gathering once and caching briefly turns four network-bound calls
    into one.
    """
    def produce():
        t = _scoped()
        return {
            "value": t.get_portfolio_value(),
            "perf": t.get_portfolio_performance(),
            "holdings": t.get_holdings(),
            "movers": t.get_todays_movers(),
        }

    ttl = float(config.get("stocks", "snapshot_seconds", default=60) or 60)
    return _cached("snapshot", ttl, produce)


def summary() -> dict:
    """Total value + today's move — the Stocks tab header and 'how are my stocks'."""
    snap = _snapshot()
    val = snap["value"]
    perf = snap["perf"]
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

    # A one-line total is thin for "how are my stocks" — add what's driving it.
    try:
        m = snap["movers"]
        best, worst = m.get("best"), m.get("worst")
        if best and best.get("day_change_pct") is not None:
            bits.append(f"best {best['ticker']} {_pct(best['day_change_pct'])}")
        if worst and worst is not best and worst.get("day_change_pct") is not None:
            bits.append(f"worst {worst['ticker']} {_pct(worst['day_change_pct'])}")
        if snap["holdings"]:
            bits.append(f"{len(snap['holdings'])} holdings")
    except Exception:
        pass

    return {
        "ok": True, "intent": "stocks", "reply": " · ".join(bits),
        "total_value": total, "invested_value": val.get("invested_value"),
        "wallet_balance": val.get("wallet_balance"),
        "day_change": day, "gain_loss_pct": gl_pct,
    }


def movers(limit: int = 3) -> dict:
    """Today's movers — the top and bottom few, not just one of each."""
    m = _snapshot()["movers"]
    ranked = [r for r in (m.get("ranked_desc") or []) if r.get("day_change_pct") is not None]
    if not ranked:
        return {"ok": True, "intent": "stocks",
                "reply": "No live price moves right now — the market may be closed."}

    ups = [r for r in ranked if r["day_change_pct"] > 0][:limit]
    downs = [r for r in ranked if r["day_change_pct"] < 0][-limit:]

    def fmt(rows):
        return ", ".join(f"{r['ticker']} {_pct(r['day_change_pct'])}" for r in rows)

    parts = []
    if ups:
        parts.append(f"Gainers: {fmt(ups)}")
    if downs:
        parts.append(f"Fallers: {fmt(list(reversed(downs)))}")
    green, red = len(ups), len(downs)
    parts.append(f"{green} up, {red} down of {len(ranked)}")
    return {"ok": True, "intent": "stocks", "reply": " · ".join(parts),
            "ranked": ranked}


def holdings() -> dict:
    hs = _snapshot()["holdings"]
    if not hs:
        return {"ok": True, "intent": "stocks", "reply": "No holdings on record.", "holdings": []}
    top = ", ".join(f"{h['ticker']} {h['weight_pct']:.0f}%" for h in hs[:4] if h.get("weight_pct"))
    return {"ok": True, "intent": "stocks",
            "reply": f"{len(hs)} holdings — {top}", "holdings": hs}


def biggest(limit: int = 3) -> dict:
    """Largest positions — with the runners-up, since 'biggest' invites context."""
    hs = _snapshot()["holdings"]
    if not hs:
        return {"ok": True, "intent": "stocks", "reply": "No holdings on record."}
    h = hs[0]
    bits = [f"Biggest is {h['ticker']} at {_money(h['market_value'])}"]
    if h.get("weight_pct") is not None:
        bits.append(f"{h['weight_pct']:.1f}% of the portfolio")
    if h.get("day_change_pct") is not None:
        bits.append(f"{_pct(h['day_change_pct'])} today")
    if h.get("gain_loss_pct") is not None:
        bits.append(f"{_pct(h['gain_loss_pct'])} overall")
    reply = ", ".join(bits) + "."
    others = hs[1:max(1, limit)]
    if others:
        reply += " Then " + ", ".join(
            f"{o['ticker']} at {o['weight_pct']:.1f}%" for o in others
            if o.get("weight_pct") is not None) + "."
    return {"ok": True, "intent": "stocks", "reply": reply, "holding": h, "holdings": hs}


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


# ---------------------------------------------------------------------------
# S&P 500 leaderboard and Creator Signals.
#
# Both are produced by Stock_Project's scheduled GitHub Actions and only exist
# in the hosted database — the local SQLite copy has none of it. Each live
# query is a ~0.8s round trip to us-east-1, and the underlying data refreshes
# weekly (leaderboard) or daily (creators), so results are cached in memory.
# ---------------------------------------------------------------------------

_mem: dict = {}


_refreshing: set = set()


def _cached(key: str, ttl: float, produce):
    """Cache with stale-while-revalidate.

    Recomputing a portfolio snapshot costs ~18s against the hosted database, so
    letting it expire would hand that wait to whoever asked next. Once we have
    a value, answer from it immediately and refresh behind the scenes; only the
    very first call (absorbed by the startup warm-up) ever blocks.
    """
    import time

    hit = _mem.get(key)
    now = time.time()
    if hit:
        age = now - hit[0]
        if age < ttl:
            return hit[1]
        if key not in _refreshing:
            _refreshing.add(key)

            def go():
                try:
                    _mem[key] = (time.time(), produce())
                except Exception:
                    pass
                finally:
                    _refreshing.discard(key)

            threading.Thread(target=go, daemon=True).start()
        return hit[1]
    value = produce()
    _mem[key] = (time.time(), value)
    return value


def leaderboard(limit: int = 5, ticker_filter: str = None) -> dict:
    """The weekly ranked S&P 500 screen from the Actions run."""
    _scoped()

    def produce():
        from engine import screener
        return screener.load_leaderboard()

    data = _cached("leaderboard", 900, produce)
    if not data:
        return {"ok": False, "intent": "stocks",
                "reply": "No S&P 500 leaderboard cached yet — the weekly Action may not have run."}
    rows = data.get("rows") or []
    if not rows:
        return {"ok": False, "intent": "stocks", "reply": "The leaderboard is empty."}

    if ticker_filter:
        sym = ticker_filter.upper()
        hit = next((r for r in rows if (r.get("ticker") or "").upper() == sym), None)
        if not hit:
            return {"ok": True, "intent": "stocks",
                    "reply": f"{sym} isn't in the S&P 500 leaderboard.", "rows": rows}
        return {"ok": True, "intent": "stocks", "rows": rows, "generated": data.get("generated_at"),
                "reply": f"{sym} ranks #{hit.get('rank')} of {len(rows)} — "
                         f"score {hit.get('score')}, {hit.get('recommendation')}"}

    top = rows[:max(1, limit)]
    listing = ", ".join(
        f"#{r.get('rank')} {r.get('ticker')} {r.get('score')} ({r.get('recommendation')})"
        for r in top)
    when = (data.get("generated_at") or "")[:10]
    return {"ok": True, "intent": "stocks", "rows": rows, "generated": data.get("generated_at"),
            "reply": f"S&P 500 leaderboard{' · ' + when if when else ''} — {listing}"}


def creator_leaderboard(limit: int = 5) -> dict:
    """Most-mentioned tickers across the tracked creators' recent videos."""
    _scoped()

    def produce():
        from engine import creator_signals
        return creator_signals.mention_leaderboard()

    rows = _cached("creator_lb", 900, produce) or []
    if not rows:
        return {"ok": True, "intent": "stocks",
                "reply": "No creator mentions in the tracking window yet.", "rows": []}
    top = rows[:max(1, limit)]

    def label(r):
        t = r.get("ticker")
        n = r.get("mentions") or r.get("count") or r.get("videos")
        return f"{t} {n} mentions" if n else str(t)

    return {"ok": True, "intent": "stocks", "rows": rows,
            "reply": "Creator mentions — " + ", ".join(label(r) for r in top)}


def creator_recent(limit: int = 5) -> dict:
    """The latest creator videos and the tickers they mentioned."""
    _scoped()

    def produce():
        from engine import creator_signals
        return creator_signals.recent_signals()

    rows = _cached("creator_recent", 900, produce) or []
    if not rows:
        return {"ok": True, "intent": "stocks",
                "reply": "No recent creator signals.", "rows": []}
    out = []
    for v in rows[:max(1, limit)]:
        # `mentions` is a list of dicts, one per ticker discussed in the video
        tickers = [m.get("ticker") for m in (v.get("mentions") or []) if m.get("ticker")]
        line = f"{v.get('creator', '?')}: “{(v.get('title') or '')[:48]}”"
        if tickers:
            line += " — " + ", ".join(tickers[:4])
        out.append(line)
    return {"ok": True, "intent": "stocks", "rows": rows,
            "reply": f"{len(rows)} recent creator videos. " + " · ".join(out)}


def ticker(sym: str) -> dict:
    sym = (sym or "").strip().upper()
    h = next((x for x in _snapshot()["holdings"] if x.get("ticker") == sym), None)
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
