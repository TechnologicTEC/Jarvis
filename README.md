# Jarvis — personal desktop assistant

**One app, two sizes, one backend.** The full window is home base (nav rail:
Ask / Files / Stocks / Inbox / Setups / Settings); double-space shrinks it to a
pinned always-on-top console you can leave in the corner while you work. Both
are the *same window* running the same page, so there is no state to keep in
sync. A tray host keeps it alive and owns the global hotkey. All input funnels
through `core/router.py`. Full spec: `docs/jarvis_spec.md`.

## Install

```powershell
pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

That creates Desktop and Start Menu shortcuts (with a real icon — pin it to the
taskbar if you like) and a Startup entry so Jarvis is already running, and the
global hotkey already listening, when you log in. `scripts\install.ps1
-Uninstall` removes all three; nothing else on the system is touched.

**You don't need to publish anything.** Publishing to the Microsoft Store costs
money and exists to distribute software to strangers — irrelevant for a personal
tool. The shortcuts above give you the native-app experience (desktop icon,
taskbar, autostart) with none of that. A standalone `Jarvis.exe` is possible
later via PyInstaller; it's deferred because it would need rebuilding after
every code change while the app is still growing.

**Use the shortcut, not a terminal.** The shortcuts run `pythonw.exe`, which
has no console — and that is not just cosmetic. Started from a terminal, numpy's
MKL runtime installs a console handler and aborts the whole process the moment
that window closes:

```
forrtl: error (200): program aborting due to window-CLOSE event
```

Jarvis then dies silently in the background, which looks exactly like "the wake
word stopped working" or "it ignored me". If you do want a terminal for
debugging, leave it open. Either way `jarvis.log` in the project folder records
startup and background activity, which is the only visibility you get under
`pythonw`.

```powershell
pythonw main.py         # no console (what the shortcuts use)
python  main.py --tray  # with a console, for debugging
```

## The two sizes

| | Full | Compact |
|---|---|---|
| Size | maximised, fills the screen | ~440×158, always-on-top |
| Shows | Everything — nav rail, tabs, composer | The command console only |
| Switch to it | double-space, or `⇱` in the header | double-space, or the tray |

`ui.full_maximised: false` gives a windowed full mode instead;
`ui.compact_position: "corner"` parks the console bottom-right rather than
centred.

**On DPI scaling** — this display is 2880×1800 at 200%, and window placement is
easy to get wrong there: `resize()`/`move()` take *logical* pixels while
`webview.screens` and `GetWindowRect` report *physical* ones, so centring with
the physical width threw both windows off the bottom-right of the screen. The
DPI APIs aren't a reliable fix either — `GetDpiForSystem()` returns 96 until
the process happens to declare awareness and 192 after, so the answer depends
on when you ask. Jarvis instead measures the ratio once, on first show, by
resizing to a known size and reading back the rect.

The window is frameless in both (the design draws its own header), so the
header is a `.pywebview-drag-region` — drag it to move the window. In compact
mode the page reports its rendered height back to Python, which resizes the
window to hug the card exactly, so there is no dead border around it.

- **Double-Esc** anywhere summons Jarvis (or hides it if it's already in front).
  It opens the app itself, not a separate popup — there is only one window now.
- Double-space is ignored while you're mid-command, so typing two spaces in the
  console never resizes the window.
- Tray icon → Open Jarvis / Pin to corner / Hide / Quit. Closing hides to the
  tray; only **Quit** exits.

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
| 7 | Gmail internship tracker + Excel | ✅ built — **needs your one-time OAuth setup** |

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
  configured path no longer exists. Click to launch, or **create and edit them
  in the app** (see below).
- **Settings** — working toggles (voice, spoken replies, auto-listen, model
  preload) that write straight to `settings.json`, plus read-only status for
  Ollama, Everything, Stock_Project and Gmail.

## Stocks — live data, leaderboard and creator signals

`stocks.source` decides which database Jarvis reads:

- **`live`** (default) — the hosted Supabase Postgres that
  [the site](https://delta247-investment-project.hf.space/) and the scheduled
  GitHub Actions write to. This is the **only** place the S&P 500 leaderboard
  and Creator Signals exist; the local SQLite copy has zero rows for both.
- **`local`** — the project's own SQLite file. Portfolio only, no Actions output.

What you can ask for:

| Ask | Source |
|---|---|
| "what's the leaderboard", "top picks" | weekly S&P 500 screen (503 names, ranked, scored) |
| "how is MU ranked" | that ticker's rank/score/recommendation |
| "creator mentions" | most-mentioned tickers across tracked creators |
| "recent creator videos" | latest videos and the tickers each discussed |

Each live query is a ~0.8s round trip to us-east-1 and the underlying data only
refreshes weekly (leaderboard) or daily (creators), so results are cached in
memory for 15 minutes. A cold portfolio read is ~30s — every per-ticker cache
lookup becomes a network hop — so `main.py` warms it in the background at
startup and the first real question answers in about a second.

## Stocks integration — how the read-only guarantee works

`skills/stocks.py` reuses Stock_Project's own `engine/chat_tools.py` rather than
reimplementing anything, but it can never write your portfolio:

- **SQLite** (your current `DATABASE_URL`) is opened in URI `mode=ro`, so the
  driver itself rejects writes. There is a test for this — a deliberate UPDATE
  must fail.
- **Postgres** (the live source) uses SQLAlchemy's `postgresql_readonly`
  execution option, which emits `SET TRANSACTION READ ONLY` inside each
  transaction. Verified against the live database: an `UPDATE` is rejected with
  `ReadOnlySqlTransaction`.

  Two approaches that *look* right were tested and **do not work** through
  Supabase's connection pooler — both let writes through:
  `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY` on connect (it only
  affects later transactions, and the pooler hands out a different backend),
  and the libpq startup option `-c default_transaction_read_only=on` (swallowed
  by the pooler). Don't "simplify" to either of those.
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

## Creating setups without editing JSON

**Just ask.** "Make a setup called gaming with discord and youtube", "new setup
music: spotify and youtube", "create a writing setup that opens notepad and
google docs" — typed or spoken. "Delete gaming" removes one. Creation is
checked *before* the launcher, so asking to make a setup never launches
anything, and plain "code" still launches as before.

Or use the UI: **Setups → + new setup** (also the **+ New** card on the home
row), then describe it in plain language — *"discord and
youtube"*, *"gaming: steam, twitch and spotify"*, *"vs code, chrome and
github.com"*. Jarvis resolves each part into a real launch action and shows the
result as chips you can remove before saving. **Edit** on any row reopens it
with its current items; **✕** deletes (click twice to confirm).

Resolution is deterministic first, and that ordering is deliberate: the
description is split, then each part is matched against your **actually
installed** apps (Start Menu shortcuts + Store apps — 186 on this machine),
then against known sites, then treated as a URL if it looks like one. The local
model is only ever asked to *split* a woolly description into names — it never
supplies a path, so a hallucination can't produce a launcher that silently
fails. Anything unresolved is reported rather than guessed at.

Common shorthand is handled: `vs code` only scores 60 against "Visual Studio
Code" by fuzzy matching, so there is an alias table, and weaker matches are
accepted only when clearly ahead of the runner-up.

## Quick files

The chips beside the search bar are real files from `ui.quick_files` in
`settings.json` — currently `Tech_CV.pdf` and `Internships.xlsx`. Click one to
open it in its default app. A chip whose file is missing turns red and is
marked `?` rather than failing silently when clicked.

## Asking about the world

"What's the date", "what's the weather", "who invented the telephone", "how
tall is Mount Everest" — these go to the internet, not to the local model, and
all of it is free and key-less:

- **date/time** answered locally, never guessed.
- **weather** from Open-Meteo, geocoded (no key, no account). Set
  `web.weather_location` for the default. Asking for Auckland used to answer
  for "Newton" — wttr.in reports the nearest *weather station*, so it named a
  suburb; Open-Meteo geocodes the place you asked for.
- **everything else** pools DuckDuckGo's Instant Answer API, Wikipedia, and
  DuckDuckGo's HTML results, then has the local model answer **from that
  fetched text only**.

That last step matters. Retrieval alone finds the right topic but often not the
answer — Wikipedia returns the "Telephone" article for "who invented the
telephone". Letting the model read the passage fixes that while keeping it
anchored to text it was handed, which is the opposite of asking a 3B model to
recall facts. If the text doesn't contain the answer, you get the source
passage rather than an invention.

Expect 6-13s for these: it's a couple of HTTP round trips plus local
inference.

## Stopping it talking

Say **"stop"**, **"be quiet"**, or **"Hey Jarvis, stop"** — or just say the
wake word again, which silences the current reply before it starts listening.
That also stops Jarvis transcribing its own voice.

## Voice

Manual trigger, never always-on: the mic only opens when you ask for it.

- **Compact console** — the `listen` toggle, or press **`v`**.
- **Full app** — click the mic ring, or press **`v`**.
- `v`, not Alt: Alt fires on every alt-tab, so switching windows was toggling
  the mic. `v` is ignored while you're mid-command.
- **You don't press anything to send.** Recording ends by itself after ~1.2s of
  silence, transcribes, routes and answers — one Alt to start, then just talk
  and stop. It's capped at 15s so nothing holds the mic open. The transcript
  goes through the **same `route()`** as typed text, and the reply is spoken
  back (`voice.speak_replies`).
- STT is `faster-whisper base.en` on CPU — local, free, offline. The ~140MB
  model downloads on first use into `models/` (gitignored).
- TTS is edge-tts (free, no key, needs internet) with pyttsx3 as the offline
  fallback — `speak()` falls back on its own if edge-tts fails.

### One stream, no handover

When the wake word fires, the recording continues on **the same audio stream**
rather than closing it and opening another. That gap used to be a few hundred
milliseconds landing exactly where your question starts, so "Hey Jarvis what is
the weather in Auckland" arrived as "the weather in Auckland" — and a mangled
question produced a confidently wrong answer. Measured with real audio through
the microphone, the full question now survives.

The speech/silence gate is measured against the room instead of a fixed number.
A fixed threshold either recorded the full 15s cap in a quiet room with a
low-gain mic (nothing ever registered as speech, so the end was never
detected) or cut you off in a loud one.

The pre-roll keeps ~0.5s from before the trigger, so the wake word itself can
land in the transcript; `strip_wake()` removes a leading "Hey Jarvis" rather
than routing on it.

### "Hey Jarvis" — hands-free

Set `voice.wake_word` (or the toggle in **Settings**) and Jarvis listens for
**"Hey Jarvis"**, then starts capturing what you say next — no keypress at all.

It runs on openWakeWord's pretrained `hey_jarvis` ONNX model, entirely locally.
Tested against synthesised speech it scored 0.99 on "Hey Jarvis" phrasings and
**0.000** on near-misses like "hey there, can you help me" — 7/7 with no false
positives — at about **4% of one CPU core**.

It is **off by default on purpose**: it holds the microphone open the whole
time it runs. Audio is fed frame by frame into the local model and discarded —
nothing is recorded, stored or sent anywhere — and the wake listener releases
the mic before the real capture starts, so the two never fight over the device.

### Why speech-out isn't instant

edge-tts is a network service, and `save()` waits for the whole clip: about 4s,
which is why replies used to land ~5s after the text. Jarvis now streams the
chunks and plays through sounddevice instead of spawning PowerShell, which
gets it to **~1.7s** warm. Most of what's left is the round trip to Microsoft.

If you'd rather have it near-instant than nice-sounding, set `voice.tts` to
`pyttsx3` — that's the offline Windows voice, which starts speaking
immediately but sounds robotic.

Two things worth knowing about model loading. Going through huggingface_hub
re-checks the repo over the network *and*, because Windows without Developer
Mode can't symlink, re-materialises the 138MB blob — about **220s per load**.
Jarvis therefore hands `WhisperModel` the already-downloaded snapshot directory
directly, which loads in ~5s. `main.py` also warms the model in a background
thread at startup, so the first Alt-to-talk doesn't pay even that — at the cost
of roughly 300MB resident for the session (the tray app sits at ~540MB with it
loaded, ~210MB without). Set `voice.preload_model` to `false` to trade that back
for a ~5s wait on first use.

## Inbox setup (one-time, free)

Everything except the Google authorisation is built and working. The tracker
side already reads your spreadsheet; only Gmail access needs you:

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create
   a project (any name).
2. **APIs & Services → Library →** enable **Gmail API**.
3. **APIs & Services → OAuth consent screen →** External, fill in the app name
   and your email. Add **yourself** as a Test user — that's what lets an
   unverified personal app sign in.
4. **Credentials → Create credentials → OAuth client ID → Desktop app.**
   Download the JSON.
5. Save it as `config/gmail_credentials.json` (gitignored).
6. Open Jarvis → **Inbox → connect gmail** and approve. The token is written to
   `config/gmail_token.json`, also gitignored.

The only scope requested is `gmail.readonly`, so Jarvis cannot send, delete, or
modify mail even if something goes wrong.

**How detection works.** The tray host polls every `inbox.poll_minutes`
(default 45). Each recent message is matched against the companies *already in
your tracker* — by sender domain first, then by the company being named — and
then against outcome wording (offer / interview / rejected / acknowledged).
Bulk senders (LinkedIn, Seek, Indeed, newsletters) are dropped first. Jarvis
never invents a company: no tracker match means no suggestion.

The **Inbox dot in the nav rail means "something changed you haven't seen"** —
it only lights when there are unseen detections, and clears when you open the
tab. It is not a decoration that is always on.

**Nothing is written without you.** A detection only ever produces a
`Kami — interview  [log it]` card. The Excel write happens on that click (or
saying "log it") and nowhere else. Before the first write of a session it takes
a timestamped backup beside the file, only touches the Stage/Status cells of an
existing company row, and fails with a clear message if the workbook is open in
Excel rather than half-writing.

One caveat worth knowing: the tracker lives in OneDrive, and openpyxl rewrites
the whole workbook on save. Plain data survives fine, but charts, images and
some conditional formatting would not — worth keeping in mind if that sheet
ever grows beyond a plain table.

## Known rough edges

- The local model is `llama3.2:3b` on CPU (no CUDA GPU here). It is fine for
  quick questions but will still get facts wrong; the system prompt tells it to
  decline rather than invent prices, dates or filenames. Anything about your
  portfolio, files or email is answered deterministically, never by the model.
- "why is my portfolio down" loads FinBERT and per-ticker news (~25s cold), so
  plain "what's moving" deliberately routes to the fast movers answer instead.
- Everything must be running **in your user session** for `es.exe` to work — the
  Windows service alone runs in session 0 and `es.exe` cannot reach it.
