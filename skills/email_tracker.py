"""Gmail polling + "is this a reply to an application?" classification.

Read-only by scope: `gmail.readonly` is the only scope requested, so Jarvis
cannot send, delete, or modify mail even if something goes wrong.

Nothing is ever written to the tracker from here. Detection surfaces a
suggestion ("Kami replied — say 'log it'") and `skills/excel_sync.py` only
writes after the user confirms, which is the spec's rule for as long as the
classifier isn't trustworthy.

Classification is deliberately cheap and explainable first — sender/subject
matched against the companies already in the tracker, plus outcome keywords.
The local LLM is only consulted for genuinely ambiguous cases, and only to
judge, never to invent a company.
"""
import base64
import datetime
import os
import re

from core import config

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_PATH = os.path.join(BASE, "config", "gmail_token.json")

# Outcome keywords, most decisive first — order matters, an offer letter also
# says "application".
_OUTCOMES = [
    ("offer", r"\b(pleased to offer|offer of employment|we'd like to offer|"
              r"we would like to offer|job offer|formal offer)\b"),
    # An actual invitation, not any mention of the word. This used to include
    # a bare "interview" and "next steps" — and "Next Steps:" is a heading in
    # almost every acknowledgement email, which is exactly how a "Thanks for
    # Applying!" receipt from ASB got logged as an interview.
    ("interview", r"\b(invit(?:e|ing|ation) (?:you )?(?:to|for)|"
                  r"we(?:'d| would) like to (?:interview|meet|speak|chat)|"
                  r"(?:like to )?schedule (?:an? )?(?:interview|call|chat|time)|"
                  r"book (?:a )?(?:time|slot|interview)|"
                  r"your interview|interview (?:with|on|is|has been)|"
                  r"progress(?:ed|ing)? to (?:the )?(?:next (?:stage|round)|interview)|"
                  r"shortlisted|assessment (?:centre|center|invitation)|"
                  r"coding challenge|hackerrank|codility|"
                  r"complete (?:an? )?(?:online )?(?:test|assessment))\b"),
    ("rejected", r"\b(unfortunately|regret to inform|not (?:be )?(?:progress|proceed)\w*|"
                 r"unsuccessful|not selected|decided not to|other candidates|"
                 r"will not be moving forward)\b"),
    # "Thanks for Applying" and "thank you for taking the time to apply" are
    # the two commonest receipts there are, and neither matched the old
    # wording, which demanded exactly "thank you for applying".
    ("acknowledged", r"(received your application|"
                     r"application (?:has been )?received|we have received|"
                     r"thank(?:s|\s+you)(?:\s+\w+){0,3}?\s+for\b"
                     r"(?:\s+\w+){0,5}?\s+(?:apply(?:ing)?|application|interest))"),
]

# Conditional framing. "If required, we may also email you a link to complete
# online testing" describes what *might* happen, and is not an invitation to
# anything. Judged over the sentence containing the match.
_HEDGE = re.compile(
    r"\b(if|should you|may|might|unless|in the event|where required|"
    r"successful candidates|those selected|candidates who|"
    r"shortlisted candidates|if you are|will be contacted)\b")

_STAGE_FOR = {
    "offer": ("Offer", "Offer"),
    "interview": ("Interview", "Replied"),
    "rejected": ("Rejected", "Rejected"),
    "acknowledged": ("Submitted CV", "Acknowledged"),
}

_NOISE_SENDERS = ("noreply@linkedin", "jobalerts", "no-reply@indeed", "seek.co",
                  "newsletter", "notifications@", "digest", "news@", "media@",
                  "marketing@", "promo", "@substack", "unsubscribe@")

# Wording that shows a message is actually about *your* application, rather
# than merely mentioning a company. Required when the only evidence is the
# company name appearing in the text: a news email reading "Tencent ... offer
# of employment scandal" otherwise scored as an offer from Tencent, and would
# have written that into the tracker.
_APP_CONTEXT = re.compile(
    r"\bapplication\b|\bapplied\b|\bapplicant\b|"
    r"\b(the (?:role|position|internship|vacancy)|this (?:role|position)|"
    r"candidate|cv|resume|cover letter|recruit\w*|talent team|"
    r"internship|graduate programme|grad program\w*|"
    r"we'd like to (?:interview|meet|speak|chat)|"
    r"invite you|your interview|your candidacy|hiring team)\b")


def credentials_path() -> str:
    """Where the OAuth client file is, being forgiving about how it got there.

    Windows hides known extensions, so saving it as "gmail_credentials.json"
    in a folder that already shows "gmail_credentials" produces
    gmail_credentials.json.json — and Google's own download is named
    client_secret_<id>.apps.googleusercontent.com.json. Accept all of it rather
    than reporting "missing" at a file the user can plainly see.
    """
    import glob

    p = os.path.expandvars(config.get("inbox", "credentials_path",
                                      default="config/gmail_credentials.json"))
    p = os.path.normpath(p if os.path.isabs(p) else os.path.join(BASE, p))
    if os.path.isfile(p):
        return p

    cfg_dir = os.path.join(BASE, "config")
    for pattern in ("gmail_credentials.json.json", "gmail_credentials*.json",
                    "client_secret*.json", "credentials*.json"):
        for hit in sorted(glob.glob(os.path.join(cfg_dir, pattern))):
            if _looks_like_client(hit):
                return hit
    return p          # the canonical path, for the "expected at ..." message


def _looks_like_client(path: str) -> bool:
    try:
        import json
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        node = d.get("installed") or d.get("web") or {}
        return bool(node.get("client_id") and node.get("client_secret"))
    except Exception:
        return False


def has_credentials() -> bool:
    return os.path.isfile(credentials_path())


def is_authorized() -> bool:
    return os.path.isfile(TOKEN_PATH)


def status() -> dict:
    return {
        "credentials": has_credentials(),
        "authorized": is_authorized(),
        "credentials_path": credentials_path(),
    }


def authorize() -> dict:
    """Run the one-time OAuth consent flow. Opens the user's browser."""
    if not has_credentials():
        return {"ok": False, "reply":
                "Missing OAuth client file — see README 'Inbox setup'. "
                f"Expected at {credentials_path()}"}
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_secrets_file(credentials_path(), SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
        return {"ok": True, "reply": "Gmail connected (read-only)"}
    except Exception as e:
        return {"ok": False, "reply": f"Gmail authorisation failed — {e}"}


class NeedsReauth(Exception):
    """The saved token can't be refreshed — only consent will fix it."""


def _retire_token():
    """Move a revoked token aside so the app stops claiming to be connected.

    Renamed rather than deleted: it costs nothing to keep, and a token file is
    the sort of thing worth being able to look at afterwards.
    """
    try:
        if os.path.isfile(TOKEN_PATH):
            dead = TOKEN_PATH + ".revoked"
            if os.path.exists(dead):
                os.remove(dead)
            os.replace(TOKEN_PATH, dead)
    except Exception:
        pass    # never let cleanup mask the real error


def _service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            # invalid_grant means the refresh token itself is dead, not
            # merely expired. Google issues refresh tokens that die after
            # SEVEN DAYS while the OAuth consent screen is still in "Testing"
            # — which is why this reappears about weekly. Publishing the app
            # (Google Cloud Console -> Audience -> Publish app) stops it.
            # Nothing here can recover it; the user has to consent again.
            if "invalid_grant" in str(e).lower():
                # Google has revoked this refresh token; it will never work
                # again. Setting it aside (rather than leaving it in place)
                # makes is_authorized() report the truth, which is what puts
                # the Connect button back. Leaving it meant the UI said
                # "gmail connected" over a dead token, with no way to reconnect.
                _retire_token()
                raise NeedsReauth(
                    "Gmail needs reconnecting — Google revoked the saved "
                    "login. Click Connect Gmail to sign in again."
                ) from e
            raise
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# --------------------------------------------------------------------------
# Classification — pure, so it is testable without Gmail
# --------------------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _is_hedged(text: str, pos: int) -> bool:
    """Is the sentence containing this match conditional rather than actual?"""
    start = max(text.rfind(".", 0, pos), text.rfind("\n", 0, pos),
                text.rfind(";", 0, pos)) + 1
    end = len(text)
    for ch in (".", "\n", ";"):
        i = text.find(ch, pos)
        if i != -1:
            end = min(end, i)
    return bool(_HEDGE.search(text[start:end]))


def classify(sender: str, subject: str, snippet: str, companies) -> dict:
    """Match one message to a tracked company and an outcome.

    Returns {company, outcome, confidence, why}. company is None when the
    message isn't about anything in the tracker — Jarvis never invents one.
    """
    blob = f"{subject}\n{snippet}".lower()
    sender_lc = (sender or "").lower()

    if any(n in sender_lc for n in _NOISE_SENDERS):
        return {"company": None, "outcome": None, "confidence": 0.0,
                "why": "bulk/job-board sender"}

    domain = ""
    m = re.search(r"@([\w.-]+)", sender_lc)
    if m:
        domain = m.group(1)
    domain_core = _norm(domain.split(".")[0]) if domain else ""

    matched, why, by_domain = None, [], False
    for c in companies:
        cn = _norm(c)
        if not cn:
            continue
        if domain_core and (cn == domain_core or cn in domain_core or domain_core in cn):
            matched, why, by_domain = c, [f"sender domain {domain}"], True
            break
    if not matched:
        for c in companies:
            if re.search(r"\b" + re.escape(c.lower()) + r"\b", blob):
                matched, why = c, ["company named in subject/body"]
                break
    if not matched:
        return {"company": None, "outcome": None, "confidence": 0.0,
                "why": "no tracked company matched"}

    # A name in the text is weak evidence on its own — anyone can mention a
    # company. Demand wording that shows it concerns your application.
    if not by_domain and not _APP_CONTEXT.search(blob):
        return {"company": None, "outcome": None, "confidence": 0.0,
                "why": f"mentions {matched} but reads as unrelated to an application"}

    outcome = None
    for name, pattern in _OUTCOMES:
        m = re.search(pattern, blob)
        if not m:
            continue
        # A promise about what may happen later is not the thing happening.
        # Only the good-news outcomes need this: a rejection stated
        # conditionally is still a rejection worth surfacing.
        if name in ("interview", "offer") and _is_hedged(blob, m.start()):
            continue
        outcome = name
        why.append(f"{name} wording")
        break

    if outcome is None:
        return {"company": matched, "outcome": None, "confidence": 0.35,
                "why": ", ".join(why) + ", but no outcome wording"}

    # domain match + clear wording is about as good as heuristics get
    confidence = 0.9 if by_domain else 0.7
    return {"company": matched, "outcome": outcome, "confidence": confidence,
            "why": ", ".join(why)}


def suggested_update(outcome: str):
    return _STAGE_FOR.get(outcome or "", (None, None))


# --------------------------------------------------------------------------
# Gmail polling
# --------------------------------------------------------------------------

def _header(payload, name):
    for h in payload.get("headers", []):
        if h.get("name", "").lower() == name:
            return h.get("value", "")
    return ""


def _body_snippet(msg) -> str:
    snippet = msg.get("snippet", "") or ""
    try:
        payload = msg.get("payload", {})
        parts = payload.get("parts") or [payload]
        for p in parts:
            if p.get("mimeType") == "text/plain":
                data = p.get("body", {}).get("data")
                if data:
                    text = base64.urlsafe_b64decode(data).decode("utf-8", "ignore")
                    return (snippet + "\n" + text)[:4000]
    except Exception:
        pass
    return snippet


def check_replies(max_results=40) -> dict:
    """Scan recent mail for replies about tracked applications. Read-only."""
    from skills import excel_sync

    if not is_authorized():
        return {"ok": False, "error": "not_authorized", "hits": [],
                "reply": "Gmail isn't connected yet — see README 'Inbox setup'."}
    names = excel_sync.companies()
    if not names:
        return {"ok": False, "error": "no_tracker", "hits": [],
                "reply": "No applications in the tracker to match against."}

    days = int(config.get("inbox", "lookback_days", default=21) or 21)
    after = (datetime.date.today() - datetime.timedelta(days=days)).strftime("%Y/%m/%d")
    try:
        svc = _service()
        listing = svc.users().messages().list(
            userId="me", q=f"after:{after} -category:promotions", maxResults=max_results,
        ).execute()
        hits = []
        for ref in listing.get("messages", []):
            msg = svc.users().messages().get(
                userId="me", id=ref["id"], format="full").execute()
            payload = msg.get("payload", {})
            sender = _header(payload, "from")
            subject = _header(payload, "subject")
            verdict = classify(sender, subject, _body_snippet(msg), names)
            if verdict["company"] and verdict["outcome"]:
                stage, st = suggested_update(verdict["outcome"])
                hits.append({
                    "id": ref["id"], "company": verdict["company"],
                    "outcome": verdict["outcome"], "confidence": verdict["confidence"],
                    "why": verdict["why"], "sender": sender, "subject": subject,
                    "date": _header(payload, "date"),
                    "suggested_stage": stage, "suggested_status": st,
                })
        # newest-looking first, strongest match first
        hits.sort(key=lambda h: -h["confidence"])
        set_pending(hits)
        if not hits:
            return {"ok": True, "hits": [],
                    "reply": f"No new replies about your {len(names)} application(s)."}
        top = hits[0]
        more = len(hits) - 1
        return {"ok": True, "hits": hits,
                "reply": f"{top['company']} {top['outcome']}"
                         + (f" (+{more} more)" if more else "")
                         + " — say “log it” to update the tracker"}
    except NeedsReauth as e:
        # A dead token is a one-click fix, so say that instead of showing the
        # raw OAuth error, which reads like a fault in Jarvis.
        return {"ok": False, "error": "needs_reauth", "hits": [],
                "needs_reauth": True, "reply": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e), "hits": [],
                "reply": f"Gmail check failed — {str(e).splitlines()[0][:110]}"}


# Detected-but-unconfirmed replies. "log it" consumes from here, so the write
# path always corresponds to something the user was actually shown.
_pending: list = []


_seen_ids: set = set()


def set_pending(hits):
    global _pending
    _pending = list(hits or [])


def unseen_count() -> int:
    """Detections the user hasn't looked at — drives the Inbox badge, which
    should mean 'something changed' rather than 'this tab exists'."""
    return sum(1 for h in _pending if h.get("id") not in _seen_ids)


def mark_seen():
    for h in _pending:
        if h.get("id"):
            _seen_ids.add(h["id"])


def get_pending() -> list:
    return list(_pending)


def log_it(hit: dict = None) -> dict:
    """Write a detected reply into the tracker. Only ever called on confirmation."""
    from skills import excel_sync

    global _pending
    if hit is None:
        if not _pending:
            return {"ok": False, "intent": "mail",
                    "reply": "Nothing pending — ask “any replies” first."}
        hit = _pending[0]
    # `row` wins when the UI has already resolved which application this is;
    # otherwise the role narrows several applications to one company.
    res = excel_sync.update_stage(
        hit.get("company"), stage=hit.get("suggested_stage"),
        status=hit.get("suggested_status"),
        role=hit.get("role") or hit.get("subject") or "",
        row=hit.get("row"))
    if res.get("ok"):
        _pending = [p for p in _pending
                    if p.get("company") != hit.get("company")]
        if _pending:
            res["reply"] += f" · {len(_pending)} more pending"
    res["intent"] = "mail"
    return res
