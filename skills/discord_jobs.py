"""Reading a Discord jobs channel.

Servers like SESA post new internships daily, hours before they surface in
search — that's the point of reading it rather than waiting for a crawler.

Uses Discord's REST API directly with `requests`: fetching recent messages from
one channel needs a single GET, so there's no reason to run a gateway client
and hold a websocket open.

Read-only. The bot needs `View Channel` and `Read Message History` on that
channel and nothing else — it never posts, edits, or joins voice.

Setup (only you can do this part):
  1. https://discord.com/developers/applications -> New Application -> Bot
  2. copy the bot token into `inbox`-style local settings:
       config/settings.local.json  ->  {"discord": {"bot_token": "..."}}
     (never settings.json — that file is committed and the repo is public)
  3. enable "Message Content Intent" on the Bot page, or message bodies come
     back empty
  4. invite it with scope=bot and permissions "View Channel" +
     "Read Message History"
  5. right-click the jobs channel -> Copy Channel ID -> `discord.channel_ids`
"""
import re
import time

import requests

from core import config

API = "https://discord.com/api/v10"


def _token() -> str:
    import os
    return (config.get("discord", "bot_token", default="")
            or os.environ.get("DISCORD_BOT_TOKEN", ""))


def _channels() -> list:
    ids = config.get("discord", "channel_ids", default=[]) or []
    return [str(c).strip() for c in ids if str(c).strip()]


def is_configured() -> bool:
    return bool(_token() and _channels())


def status() -> dict:
    return {"token": bool(_token()), "channels": len(_channels()),
            "configured": is_configured()}


def _headers():
    return {"Authorization": f"Bot {_token()}",
            "User-Agent": "Jarvis (personal internship tracker, read-only)"}


def check_access() -> dict:
    """Confirm the token works and the channels are readable, with a clear
    message when they aren't — the usual causes are a missing invite or the
    Message Content Intent being switched off."""
    if not _token():
        return {"ok": False, "reply": "No Discord bot token set — see the "
                                      "README 'Discord jobs feed'."}
    try:
        me = requests.get(f"{API}/users/@me", headers=_headers(), timeout=12)
    except Exception as e:
        return {"ok": False, "reply": f"Discord unreachable — {str(e)[:70]}"}
    if me.status_code == 401:
        return {"ok": False, "reply": "Discord rejected the bot token."}
    if not me.ok:
        return {"ok": False, "reply": f"Discord error {me.status_code}"}
    name = (me.json() or {}).get("username", "bot")

    if not _channels():
        return {"ok": False, "reply": f"Signed in as {name}, but no "
                                      "discord.channel_ids configured."}
    readable, problems = [], []
    for cid in _channels():
        r = requests.get(f"{API}/channels/{cid}/messages", headers=_headers(),
                         params={"limit": 1}, timeout=12)
        if r.ok:
            readable.append(cid)
        elif r.status_code == 403:
            problems.append(f"{cid}: bot can't see that channel")
        elif r.status_code == 404:
            problems.append(f"{cid}: no such channel")
        else:
            problems.append(f"{cid}: error {r.status_code}")
    reply = f"Signed in as {name} · {len(readable)}/{len(_channels())} channel(s) readable"
    if problems:
        reply += " · " + "; ".join(problems[:2])
    return {"ok": bool(readable), "reply": reply, "readable": readable}


def _fetch(channel_id: str, limit: int):
    out, before = [], None
    while len(out) < limit:
        params = {"limit": min(100, limit - len(out))}
        if before:
            params["before"] = before
        try:
            r = requests.get(f"{API}/channels/{channel_id}/messages",
                             headers=_headers(), params=params, timeout=15)
        except Exception:
            break
        if not r.ok:
            break
        batch = r.json() or []
        if not batch:
            break
        out.extend(batch)
        before = batch[-1]["id"]
        if len(batch) < params["limit"]:
            break
    return out


_URL = re.compile(r"https?://[^\s<>|)\]]+")
# The feed posts titles as "Company - Role"; embeds carry the same in .title
_TITLE = re.compile(r"^\s*(?P<company>[^-–—]{2,60}?)\s*[-–—]\s*(?P<role>.{3,110})\s*$")

# Posts start with a tag row — "Sydney | Intern", "🇳🇿 Auckland  Intern" — which
# looks exactly like "Company - Role" to a naive split, and was being read as
# company "Sydney", role "Intern".
_TAG_WORDS = re.compile(
    r"^(intern(ship)?|grad(uate)?|junior|entry[\s-]?level|part[\s-]?time|"
    r"full[\s-]?time|contract|casual|remote|hybrid|onsite|new|hot|"
    r"auckland|wellington|christchurch|hamilton|sydney|melbourne|brisbane|"
    r"perth|adelaide|canberra|nz|new zealand|australia|aus|multiple|various)$",
    re.I)


def _is_tag_line(line: str) -> bool:
    """A row of chips rather than a title."""
    bits = [b.strip(" *_`#•·|") for b in re.split(r"[|·•]", line) if b.strip()]
    if not bits or len(line) > 60:
        return False
    clean = [re.sub(r"[^\w\s-]", "", b).strip() for b in bits]   # drop emoji
    clean = [c for c in clean if c]
    return bool(clean) and all(_TAG_WORDS.match(c) for c in clean)


def _texts(msg):
    """Everything worth reading: the message plus any embed fields."""
    parts = [msg.get("content") or ""]
    for e in msg.get("embeds") or []:
        for k in ("title", "description", "url"):
            if e.get(k):
                parts.append(str(e[k]))
        for f in e.get("fields") or []:
            parts.append(f"{f.get('name', '')} {f.get('value', '')}")
        if (e.get("footer") or {}).get("text"):
            parts.append(e["footer"]["text"])
        if (e.get("author") or {}).get("name"):
            parts.append(e["author"]["name"])
    return "\n".join(p for p in parts if p)


def _parse(msg) -> dict:
    blob = _texts(msg)
    urls = _URL.findall(blob)
    url = ""
    for u in urls:
        # prefer the actual advert over a tracking or invite link
        if not re.search(r"discord\.(gg|com)|tenor|giphy", u):
            url = u.rstrip(".,)")
            break
    company, role = "", ""
    for raw in blob.splitlines():
        line = raw.strip().strip("*_`# ")          # markdown bold/heading
        if not line or _URL.match(line) or _is_tag_line(line):
            continue
        m = _TITLE.match(line)
        if m and not _is_tag_line(m.group("company")):
            company = m.group("company").strip()
            role = m.group("role").strip()
            break
        if not role and 4 < len(line) < 120:
            role = line
    return {"company": company, "role": role or "(untitled)", "url": url,
            "text": blob, "at": msg.get("timestamp", "")}


def recent(limit: int = 120) -> dict:
    """Postings from the configured channels, run through the same filters as
    everything else — eligibility, Auckland, freshness."""
    from skills import jobs

    if not is_configured():
        return {"ok": False, "items": [], "skipped": [],
                "reply": "Discord isn't set up — see README 'Discord jobs feed'."}

    dismissed = set(jobs._load_seen())
    remembered = jobs._load_verdicts()
    applied = jobs._applied_companies()
    known_lc = {k.lower() for k in jobs.known_companies()}

    items, skipped, seen_urls = [], [], set()
    for cid in _channels():
        for msg in _fetch(cid, limit):
            p = _parse(msg)
            if not p["url"] or p["url"] in seen_urls:
                continue
            seen_urls.add(p["url"])
            if p["url"] in dismissed:
                continue
            blob = p["text"]
            if not jobs.looks_like_internship(blob) or not jobs.looks_relevant(blob):
                continue
            if not jobs.location_ok(blob):
                continue          # the feed carries as much Sydney as Auckland

            elig = jobs.check_eligibility(blob, title=f"{p['role']} {p['company']}")
            fresh = jobs.check_freshness(blob, p["role"])
            if not fresh["open"]:
                elig = {"eligible": False, "reason": fresh["reason"]}
            prior = remembered.get(p["url"])
            if elig["eligible"] and prior:
                elig = {"eligible": False, "reason": prior}

            row = {
                "role": p["role"][:110], "company": p["company"][:60],
                "where": "", "source": "discord", "url": p["url"],
                "score": 0.95,     # a human posted it today; rank it high
                "reason": elig["reason"], "posted": p["at"][:10],
                "already_applied": p["company"].strip().lower() in applied,
                "known_hirer": p["company"].strip().lower() in known_lc,
                "careers_page": False,
            }
            (items if elig["eligible"] else skipped).append(row)

    # confirm the survivors are actually still open, same as the board results
    if items and config.get("jobs", "verify_open", default=True):
        verdicts = jobs._verify_all(items)
        live = []
        for row in items:
            v = verdicts.get(row["url"], {})
            if v.get("checked") and not v.get("open"):
                row["reason"] = v["reason"]
                skipped.append(row)
                jobs._remember_verdict(row["url"], v["reason"])
            else:
                live.append(row)
        items = live

    items.sort(key=lambda r: (r["already_applied"], -r["score"]))
    return {"ok": True, "items": items, "skipped": skipped,
            "reply": f"{len(items)} from Discord"
                     + (f" · {len(skipped)} filtered out" if skipped else "")}
