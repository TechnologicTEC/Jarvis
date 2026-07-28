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

### Startup order

The window goes on screen first; everything slow follows behind it. Measured
from launch: **window visible and usable in ~1s**, wake word listening about
12s later. Setups are read from a JSON file, so the launcher works the moment
the window paints — which is the point, since that's what you reach for at
login.

Getting there meant fixing what the freshly-opened window itself was waiting
on. Two calls it makes on boot were quietly blocking the bridge:

- `voice_status` **took 79s** — `stt_available()` proved faster-whisper was
  installed by importing it, which cold-pulls ctranslate2 and friends. It now
  checks the package *exists* (`importlib.util.find_spec`) without importing.
  `wake.available()` had the same bug.
- `status`/`stocks_summary` connected to the hosted stock database (~20s).
  Both now answer from what's already loaded and kick the connection off in the
  background, so the header shows "portfolio connecting…" and fills in later.

The Ollama reachability check is cached for 15s too — the header polls it, and
a dead endpoint costs the full timeout every time.

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

**Pin** (📌 in the compact header, `ui.pin_compact`) keeps the console above
other windows when you click away. It is applied through Win32 `SetWindowPos`
rather than pywebview's `on_top`, which never actually cleared the topmost
style — so the pin could not be released, and the full-size window was silently
floating over everything too. The window is also located by process rather than
by title: `FindWindowW(None, "Jarvis")` matches on title alone and, with a
leftover instance running, was pinning a window belonging to a different
process.

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

  Handles **which day**: "tomorrow", "on Friday", "the day after tomorrow".
  Previously every question fetched a one-day forecast and answered with
  *today*, so "what's the weather tomorrow" quietly disagreed with your phone.
  It also answers questions that never say the word "weather" — "is it raining
  tomorrow", "do I need an umbrella", "how hot is it" — which used to fall
  through to a general web search and come back with something unrelated.
- **everything else** pools **Tavily** (if a key is set), DuckDuckGo's Instant
  Answer API, Wikipedia and DuckDuckGo results — all four fetched **in
  parallel** — then has the local model answer **from that fetched text only**.

**Tavily is the big win.** It returns extracted page *content* and often a
direct answer, where DuckDuckGo gives ~30-word snippets — and thin snippets
are exactly what the grounding step runs short of. When it answers directly
the local model is skipped entirely: measured **2.3–2.7s** with a key against
**7.9s** without.

> **Put the key in `config/settings.local.json`, never `config/settings.json`.**
> The latter is committed and this repo is public. `settings.local.json` is
> gitignored, is layered over the tracked file at load time, and is where
> `set_setting()` automatically routes anything whose name looks like a secret.
> A free key comes from [tavily.com](https://tavily.com); `TAVILY_API_KEY` in
> the environment works too.

Without a key everything still works — Tavily is skipped silently.

`web.search_depth` defaults to `advanced`, which reads more of each page and
handles local/specific questions ("term dates", "opening hours") much better.
It costs 2 API credits per search against `basic`'s 1 — on the 1000-credit free
tier that's ~500 searches a month rather than ~1000. Measured side by side the
two returned the same answers, so drop to `basic` if you ever run short.
`web.country` (default `new zealand`) biases results locally.

### The web is the default, not the model

Anything the deterministic handlers don't claim now goes to search. It used to
be the other way round — only sentences starting with a question word were
searched, so bare phrasing fell through to the 3B local model answering from
memory. That's where the wrong answers came from:

| Asked | Used to do | Now |
|---|---|---|
| "search for the best laptop for students" | searched your **files**, found none | searches the web |
| "what time does countdown close" | replied with **today's date** | shop hours |
| "tesla stock price" | replied with **your portfolio total** | Tesla's price |
| "auckland university term dates" | model: "I don't have access" | the actual dates |
| "restaurants near auckland cbd" | model **invented** a restaurant | real listings |
| "best programming language for beginners" | model guessed | sourced answer |

Small talk ("hi", "thanks") and anything about *your* files/portfolio still
never hits the network. A file search that finds nothing now falls through to
the web, since "search for X" is often a question rather than a filename.

That last step matters. Retrieval alone finds the right topic but often not the
answer — Wikipedia returns the "Telephone" article for "who invented the
telephone". Letting the model read the passage fixes that while keeping it
anchored to text it was handed, which is the opposite of asking a 3B model to
recall facts. If the text doesn't contain the answer, you get the source
passage rather than an invention.

Expect 6-13s for these: it's a couple of HTTP round trips plus local
inference.

## Knowing it heard you

The orb on **Ask** is the indicator, not decoration:

| State | Orb |
|---|---|
| listening | bars track your voice level, ring speeds up, halo breathes |
| transcribing | bars settle to a slow violet pulse — "got that" |
| thinking | amber pulse, ring spins fast, shows the acknowledgement |
| answering | green, quicker rhythm |
| idle | back to its slow drift, halo off |

This works for **"Hey Jarvis" too**, not just clicking the mic. The window
polls only while it is showing a session, so one the backend started on its
own used to run to completion invisibly — Jarvis heard you, answered and spoke,
and the screen never moved. A light watcher now notices a session it did not
start and attaches to it.

As soon as it has your words — before the answer exists — it says so, both on
screen and aloud ("I'm on that now", "Looking that up now"). A web question
takes several seconds, and silence in that gap reads as "it didn't hear me".
The acknowledgement is spoken on its own thread so it adds nothing to the
wait, and the real reply cuts it off when it arrives. Short questions that
answer instantly get a short "Sure." instead, so it isn't still talking when
the answer lands. Turn it off with `voice.acknowledge`.

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

### What it says vs what it shows

Replies are written to be *read*: `AXSM +3.7% · NVDA ×8 · $8,989`. Fed straight
to a speech engine that came out as "A X S M **plus** three point seven percent
**middle dot** N V D A **times** eight" — the stray "times" and "plus" were
symbols being pronounced, not words anyone wrote. `speech_text()` rewrites the
line for the ear only; the screen keeps the compact version:

| On screen | Spoken |
|---|---|
| `+3.7%` / `-5.0%` | "up 3.7 percent" / "down 5 percent" |
| `NVDA ×8` | "NVDA, 8 mentions" |
| `-$127.19` | "down 127.19 dollars" |
| `·` `—` `\|` | a pause |
| `5–12°C` | "5 to 12 degrees" |
| `2026-07-26` | "26 July 2026" |
| `#1`, `S&P` | "number 1", "S and P" |

### Voice

Default is `en-US-AndrewNeural` — one of Microsoft's newer conversational
voices, which sound markedly less synthetic than the older set (the previous
`en-AU-WilliamNeural` among them). Others worth trying, via `voice.edge_voice`:
`en-US-BrianNeural` (casual), `en-US-EmmaNeural` (clear), `en-US-AvaNeural`
(expressive).

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

## Jobs — finding internships you can actually apply for

**Jobs** tab, or ask: "find me some internships", "any new internships",
"who's hiring". Click a listing to open it.

The filter is the point. You're 2nd year of 4, and most "software internship
Auckland" results want penultimate or final-year students — scrolling past
those is the actual chore. Anything demanding **penultimate**, 3rd/4th year, a
graduation year, a completed degree, years of experience, or postgrad study is
dropped before you see it, and the count of what was hidden is shown so you
know it's working rather than silently missing things.

Scope is software and computing, broadly read: developer, data, ML, web,
DevOps, cloud, embedded, firmware, mechatronics, robotics, cyber, QA, IT.
Marketing, nursing and civil internships are filtered out. Location must be
Auckland, remote, hybrid or NZ-wide. Roles at companies already in your tracker
are marked *already applied* and sorted last.

Two implementation notes worth keeping:

- Discovery is **Tavily**, not scraping. Seek's own JSON search endpoint was
  tried first and 404s now, which is exactly the fragility to avoid; Tavily
  respects robots and returns extracted page content.
- Eligibility is read from the **full posting** (`include_raw_content`), not
  the snippet. With snippets alone the filter had nothing to read and hid
  nothing at all. The page is then truncated at "Similar jobs" / "People also
  viewed" — a sidebar of *other* roles was getting Apple's intern posting
  hidden as a "senior role". Seniority and postgrad are judged from the title
  only, since bodies routinely say "mentored by senior engineers".

Searches run in parallel (73s → ~2s) and are cached for `jobs.cache_hours`
(default 6), because each one costs Tavily credits.

### Dismissing what you've read

Each listing has a **seen** button. The row goes immediately and the posting
never comes back — boards repost the same roles for weeks, and re-reading them
is the thing this is meant to save you. Dismissals are kept in
`config/jobs_seen.json` (gitignored) so they survive a restart, keyed by URL.
**show all (n)** in the header brings them back.

### Where it looks

Two passes:

1. **Job boards** — seek.co.nz, nz.indeed.com, nz.linkedin.com, sjs.co.nz,
   summeroftech.co.nz, nz.gradconnection.com, trademe.co.nz, workhere.co.nz,
   joblist.co.nz. Results are restricted to these domains.
2. **Company careers pages** — the companies in your database, searched with no
   domain restriction, because plenty advertise only on their own site and a
   board search structurally cannot see those.

The second pass rotates: `jobs.company_batch` (default 10) companies per
refresh, each cached for `jobs.company_ttl_hours` (default 48), oldest first.
Checking all 80 every time would exhaust the Tavily allowance, so the reply
reports coverage — "career pages: 20/80 companies, 60 still to check" — rather
than implying the whole list was searched. Run refresh a few times to sweep it.

A careers landing page ("EROAD Careers") is kept but labelled *careers page —
check yourself* and sorted below real postings, since it's a pointer rather
than a role. Listicles, bootcamp ads and social posts are dropped.

### Closed and out-of-date postings

Boards keep old adverts up for years, and a dead link wastes more of your time
than an ineligible one — you only find out after clicking through. Hidden when:

- the page says **no longer accepting applications**, applications closed,
  position filled, or lists it under "past internships";
- it was **posted more than ~5 months ago** ("1 year ago", "11 months ago");
- it names a **season that has finished**. NZ summer internships run
  November–February, so in July 2026 a "2025 - 2026" advert is stale while
  "2026/2027" is the live one.

Verdicts stick, because Tavily doesn't always return the same amount of a page
and a role doesn't stop being closed because this fetch was shorter.

### Your companies list

Set `jobs.companies_file` to that spreadsheet (first column, or a column headed
`Company`). Those companies get their own targeted searches and rank above
generic board hits — one that has hired students before is a better lead. It
isn't on this machine, so nothing is wired to it yet.

### The Discord job feed

Not built: reading a Discord server needs a bot token and the bot invited to
that server, which only you can do. If you want it, create an application at
[discord.com/developers](https://discord.com/developers/applications), add a
bot, give it *Read Messages* and *Read Message History* on the jobs channel,
and say so — the postings would flow through the same eligibility filter.

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
Bulk senders (LinkedIn, Seek, Indeed, newsletters, news desks) are dropped
first. Jarvis never invents a company: no tracker match means no suggestion.

A company name appearing in the text is treated as **weak** evidence and now
also requires wording showing the message concerns an application ("your
application", "the role", "candidate", "internship"…). Without that guard a
news email reading *"Tencent … offer of employment scandal"* classified as an
offer from Tencent — and that would have been written into the real
spreadsheet. A match on the sender's domain still stands on its own.

The classifier is covered by a test over both directions — six genuine replies
(acknowledgement, interview, rejection, offer, coding challenge, a forwarded
thread) and six that must stay silent (job alerts, newsletters, a receipt, a
security alert, personal mail, and that news article).

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
