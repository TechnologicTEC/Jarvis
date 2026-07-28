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
    ("interview", r"\b(interview|meet with|schedule a (?:call|chat|time)|"
                  r"next (?:steps|stage|round)|shortlist(?:ed)?|"
                  r"assessment|coding challenge|hackerrank|codility)\b"),
    ("rejected", r"\b(unfortunately|regret to inform|not (?:be )?(?:progress|proceed)\w*|"
                 r"unsuccessful|not selected|decided not to|other candidates|"
                 r"will not be moving forward)\b"),
    ("acknowledged", r"\b(received your application|thank you for applying|"
                     r"application (?:has been )?received|we have received|"
                     r"thanks for your interest)\b"),
]

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


def _service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# --------------------------------------------------------------------------
# Classification — pure, so it is testable without Gmail
# --------------------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


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
        if re.search(pattern, blob):
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
    res = excel_sync.update_stage(
        hit.get("company"), stage=hit.get("suggested_stage"),
        status=hit.get("suggested_status"))
    if res.get("ok"):
        _pending = [p for p in _pending
                    if p.get("company") != hit.get("company")]
        if _pending:
            res["reply"] += f" · {len(_pending)} more pending"
    res["intent"] = "mail"
    return res
