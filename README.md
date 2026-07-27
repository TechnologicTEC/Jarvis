# Jarvis — personal desktop assistant

Three surfaces, one backend: the **full app** (nav rail: Ask / Files / Stocks /
Inbox / Setups / Settings), the **mini popup** (double-`Esc`, always-on-top),
and the **system tray** host that owns both. All input funnels through
`core/router.py`. Full spec: `docs/jarvis_spec.md`.

## Run

```powershell
pip install -r requirements.txt
python main.py          # opens the full app + tray icon
python main.py --tray   # start silent in the tray (what autostart uses)
```

- **Double-Esc** anywhere → mini popup (Esc again hides it; double-Esc inside a
  Jarvis window hides that window).
- Tray icon → Open Jarvis / Open Mini / Quit. Closing a window hides it to the
  tray; only tray **Quit** exits.
- `scripts\install_autostart.ps1` adds a Startup shortcut (`pythonw --tray`).

## Configure

- `config/app_setups.json` — named setups → list of `{"type": "app"|"url"|"file", ...}`
  actions. The "+ New" card / `__new` menu item opens this file. `%VAR%` env
  vars are expanded in paths.
- `config/settings.json` — hotkey timing, Ollama model/url, chrome/es.exe paths.

## Status (build order from the spec)

| Phase | Feature | State |
|---|---|---|
| 1 | Tray + both windows + js_api bridge | ✅ built |
| 2 | Setup launcher (`core/actions.py`) | ✅ built |
| 3 | File finder (Everything + rapidfuzz) | ✅ built — Everything installed, `es.exe` vendored |
| 4 | Router + Ollama fallback | ✅ built — `llama3.2:3b` pulled |
| 6 | Stocks (Stock_Project `chat_tools`) | ✅ built — read-only, see below |
| 5 | Voice (faster-whisper + edge-tts) | ✅ built — `base.en`, manual trigger |
| 7 | Gmail internship tracker + Excel | ⏳ not built — needs Google Cloud OAuth setup |

The full app header shows honest status dots for ollama / everything / gmail, and
a live portfolio figure. Nothing in the UI is placeholder data any more.

## The full app's tabs

All six nav tabs are built and read from the live backend:

- **Ask** — composer, mic ring, and the orb. Free-form Q&A and voice.
- **Files** — live Everything search as you type, with the real indexed count
  (~1.6M items here). Click a result to reveal it in Explorer.
- **Stocks** — total / today / overall / invested / wallet, plus every holding
  with its day and lifetime move and a weight bar.
- **Inbox** — states plainly that phase 7 isn't connected and what it needs.
- **Setups** — every setup, what it launches, and a warning count if any
  configured path no longer exists. Click to launch.
- **Settings** — working toggles (voice, spoken replies, auto-listen, model
  preload) that write straight to `settings.json`, plus read-only status for
  Ollama, Everything, Stock_Project and Gmail.

## Stocks integration — how the read-only guarantee works

`skills/stocks.py` reuses Stock_Project's own `engine/chat_tools.py` rather than
reimplementing anything, but it can never write your portfolio:

- **SQLite** (your current `DATABASE_URL`) is opened in URI `mode=ro`, so the
  driver itself rejects writes. There is a test for this — a deliberate UPDATE
  must fail.
- **Postgres**, if you ever switch `DATABASE_URL` over, gets every connection
  pinned to `TRANSACTION READ ONLY` — the equivalent of the `copilot_app`
  least-privilege role pattern in `scripts/setup_app_role.py`.
- It never calls `auth.apply_login()` or `init_db()`; both write (user upsert,
  `last_login_at`, DDL). The portfolio owner is resolved with a plain SELECT
  and set as the scoping context directly.

One consequence worth knowing: Stock_Project's `engine/cache.py` writes every
freshly fetched quote back to `api_cache`, which a read-only connection blocks —
and the exception would otherwise take the whole quote down with it. Jarvis
installs a shim (`_install_readonly_cache_shim`) that keeps the cache *reads*
and holds its own fetches in memory for the same TTL instead of persisting them.
**Stock_Project itself is not modified.**

Configure it under `stocks` in `settings.json` (`project_path`, `user_email`).
Note `user_email` matters: the DB's bootstrap user (`local@localhost`) owns no
holdings, so an unscoped read would look like an empty portfolio.

## Voice

Manual trigger, never always-on: the mic only opens when you ask for it.

- **Mini popup** — the `listen` toggle (or Alt). Space stops recording early.
- **Full app** — click the mic ring, or press Alt.
- Recording ends automatically on ~1.2s of silence, capped at 15s, so nothing
  holds the mic open. The transcript goes through the **same `route()`** as
  typed text, and the reply is spoken back (`voice.speak_replies`).
- STT is `faster-whisper base.en` on CPU — local, free, offline. The ~140MB
  model downloads on first use into `models/` (gitignored).
- TTS is edge-tts (free, no key, needs internet) with pyttsx3 as the offline
  fallback — `speak()` falls back on its own if edge-tts fails.

Two things worth knowing about model loading. Going through huggingface_hub
re-checks the repo over the network *and*, because Windows without Developer
Mode can't symlink, re-materialises the 138MB blob — about **220s per load**.
Jarvis therefore hands `WhisperModel` the already-downloaded snapshot directory
directly, which loads in ~5s. `main.py` also warms the model in a background
thread at startup, so the first Alt-to-talk doesn't pay even that — at the cost
of roughly 300MB resident for the session (the tray app sits at ~540MB with it
loaded, ~210MB without). Set `voice.preload_model` to `false` to trade that back
for a ~5s wait on first use.

## Known rough edges

- The local model is `llama3.2:3b` on CPU (no CUDA GPU here). It is fine for
  quick questions but will still get facts wrong; the system prompt tells it to
  decline rather than invent prices, dates or filenames. Anything about your
  portfolio, files or email is answered deterministically, never by the model.
- "why is my portfolio down" loads FinBERT and per-ticker news (~25s cold), so
  plain "what's moving" deliberately routes to the fast movers answer instead.
- Everything must be running **in your user session** for `es.exe` to work — the
  Windows service alone runs in session 0 and `es.exe` cannot reach it.
