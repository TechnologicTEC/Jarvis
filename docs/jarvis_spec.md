# Project Jarvis — Personal Desktop Assistant

## Overview
A Windows background app that launches on startup, sits in the system tray,
and opens a small always-on-top popup when the user double-taps `Esc`. The
popup accepts typed or spoken input, routes it to either a hardcoded action,
a local function, or a local LLM, and can speak its reply back.

**Hard constraint: $0 running cost.** No paid APIs. Everything either runs
locally or uses a free tier the user already has (Gemini, via the existing
Stock_Project key).

---

## Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.11 | matches Stock_Project, easiest for Claude Code to reuse code between the two |
| Tray icon | `pystray` | minimal, just needs an icon + menu (Open, Quit) |
| Popup UI | `pywebview` | renders local HTML/CSS/JS — this is where the Claude Design output goes |
| Global hotkey | `keyboard` (or `pynput` if `keyboard` needs admin rights and that's a problem) | listen for double-`Esc` within ~400ms |
| Intent routing | Custom deterministic router (regex/keyword match), same philosophy as Stock_Project's `chat.py` | fast, free, predictable |
| Local LLM fallback | **Ollama**, model `llama3.2:3b` or `phi3-mini` | free, unlimited, offline — used only when the deterministic router can't classify the request |
| Optional cloud LLM | Gemini free tier (`GEMINI_API_KEY` — can reuse Stock_Project's) | only for the "ask about my stocks" free-form path, to match Stock_Project's own pattern and stay within its quota expectations |
| Speech-to-text | `faster-whisper`, model `tiny.en` or `base.en` | offline, free, runs on CPU fine at these sizes |
| Text-to-speech | `edge-tts` (nicer voices, needs internet, free, no key) with `pyttsx3` as an offline fallback | toggle in settings |
| File/folder search | **Everything** (voidtools, free Windows app) + its `es.exe` CLI, results ranked with `rapidfuzz` | must be installed separately by the user; app should detect if it's missing and prompt |
| Email | Gmail API, OAuth, read-only scope (`gmail.readonly`) | free tier is far beyond personal-use volume |
| Excel read/write | `openpyxl` | local file, no cloud dependency |
| Stock data | Direct read from the Stock_Project Postgres/Supabase DB, reusing `engine/chat_tools.py` | see "Stock integration" section below |
| Config / app-setups | Plain JSON file | user-editable without touching code |
| Autostart | Shortcut in `shell:startup`, or Task Scheduler entry created by an install script | |

---

## Folder structure (proposed)

```
jarvis/
├── main.py                    # entry point — starts tray icon + hotkey listener
├── config/
│   ├── settings.json           # user prefs (voice on/off, TTS engine, hotkey, etc.)
│   └── app_setups.json         # named setups → list of actions
├── core/
│   ├── hotkey_listener.py      # double-Esc detection
│   ├── popup.py                 # pywebview window management
│   ├── router.py                 # deterministic intent classifier
│   ├── llm_local.py               # Ollama wrapper
│   ├── llm_gemini.py               # optional Gemini wrapper (mirrors Stock_Project's chat_llm.py pattern)
│   └── actions.py                  # launches apps/URLs per app_setups.json
├── skills/
│   ├── file_finder.py           # Everything + rapidfuzz wrapper
│   ├── stocks.py                 # imports/reuses Stock_Project's chat_tools.py
│   ├── email_tracker.py           # Gmail polling + classification
│   ├── excel_sync.py               # openpyxl read/write for the internship tracker
│   └── voice.py                     # faster-whisper (STT) + edge-tts/pyttsx3 (TTS)
├── ui/
│   ├── popup.html                # <- Claude Design output goes here
│   ├── popup.css
│   └── popup.js
├── requirements.txt
└── README.md
```

---

## Feature breakdown & build order

### 1. Tray app + popup skeleton (no intelligence yet)
- App starts on boot, shows tray icon with Open/Quit menu.
- Double-`Esc` opens a small always-on-top window near center screen or cursor.
- Typed text input, just echoes back for now. Confirms the whole shell works
  before adding any smarts.

### 2. App-setup launcher
- `app_setups.json` — e.g.:
```json
{
  "school": [
    {"type": "app", "path": "explorer.exe"},
    {"type": "url", "target": "https://canvas.instructure.com", "browser": "chrome"}
  ],
  "code": [
    {"type": "app", "path": "C:\\Users\\YOU\\AppData\\Local\\Programs\\Microsoft VS Code\\Code.exe"},
    {"type": "app", "path": "C:\\path\\to\\claude.exe"},
    {"type": "app", "path": "chrome.exe"}
  ],
  "chill": [
    {"type": "url", "target": "https://youtube.com", "browser": "chrome"}
  ]
}
```
- Router recognizes "open school setup", "code mode", "let's chill", etc., or
  the popup just shows clickable buttons for each setup — probably do both.
- Easy to add more setups later without touching code.

### 3. File/folder finder
- Requires **Everything** installed (free, from voidtools.com) with its CLI
  (`es.exe`) on PATH, or called via full path.
- "find my resume" → query Everything → rapidfuzz scores results against
  "resume" → return top 5 ranked by closeness → clickable to open in Explorer.
- Detect at startup if Everything isn't installed/running and tell the user
  once, don't fail silently.

### 4. Intent router + local LLM fallback
- Deterministic router checks for known patterns first (open a setup, find a
  file, check stocks, check email/internships) — zero cost, instant.
- Anything unclassified goes to Ollama running locally for a free-form answer.
- Router should be easy to extend — a growing list of (pattern → handler)
  pairs, same spirit as Stock_Project's `chat.py`.

### 5. Voice in/out
- Hold-to-talk or a "listening" state after the popup opens (design choice —
  probably simplest to start listening automatically once popup opens, with
  a manual stop/enter to confirm).
- faster-whisper transcribes → same router as typed text.
- Reply optionally spoken back via edge-tts (needs internet) falling back to
  pyttsx3 (offline) — toggle in settings.

### 6. Stock integration
- Reuse `Stock_Project/engine/chat_tools.py` directly — either as a git
  submodule/installed local package, or copy the relevant functions in.
- Connect to the same Postgres/Supabase DB, ideally via a **read-only scoped
  role** (mirroring the `copilot_app` pattern already in Stock_Project) so
  Jarvis can never accidentally write to portfolio data.
- "How are my stocks doing" → deterministic router calls
  `get_portfolio_value()`, `get_todays_movers()`, `get_health_summary()`
  directly — no network hop to the deployed site, no Gemini quota spent.
- Free-form/complex questions can optionally route through Gemini using the
  same tool-calling pattern as `chat_llm.py`, sharing the existing
  `GEMINI_API_KEY`.

### 7. Internship tracker (build last — most moving parts)
- Gmail API, OAuth read-only scope, polls periodically (e.g. every 30–60 min
  via a background thread or scheduled task).
- Classification: does this email look like a response to an application?
  Start simple (keyword/sender heuristics), optionally escalate to the local
  LLM for judgment calls.
- **Do not auto-write silently at first.** Surface a "I think this is a
  response from [Company], want me to log it?" confirmation until the
  classification is trustworthy, then consider full automation.
- On confirmation, updates the tracking Excel file via `openpyxl` — append
  new applications, update status column for responses.

---

## Design step (before Claude Code build)
Use Claude Design to mock up:
1. **Popup — idle/typing state** (small, always-on-top, clean text input)
2. **Popup — listening state** (visual indicator that voice is being captured)
3. **Setup picker view** (school / code / chill / + more, as clickable tiles)

Export the HTML/CSS and drop it into `ui/popup.html` / `popup.css` — Claude
Code should wire real functionality into that markup rather than generating
its own generic UI.

---

## Explicitly out of scope for v1 (mention if asked, don't build unprompted)
- Anything that sends money, trades, or modifies the Stock_Project DB
- Auto-sending emails on the user's behalf
- Cloud sync of settings across machines

---

## Open questions for the user (Claude Code should ask before/while building)
- Exact install paths for VS Code / Claude / Chrome on this machine (for
  `app_setups.json`)
- Preferred Ollama model size, based on actual hardware (CPU-only vs GPU)
- Whether voice should auto-start listening on popup open, or require a
  manual trigger
- Gmail OAuth setup — user will need to create their own Google Cloud
  project + OAuth credentials (free, but a few manual steps)
