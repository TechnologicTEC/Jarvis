"""Speech in (faster-whisper) and speech out (edge-tts, pyttsx3 fallback).

Both halves are free. Whisper runs locally on CPU — `tiny.en`/`base.en` are
fast enough there and never leave the machine. edge-tts needs internet but no
key; pyttsx3 is the offline fallback and is used automatically when edge-tts
fails, so voice output still works on a plane.

Recording is push-to-talk-ish rather than always-on: `listen()` records until
it hears a stretch of silence (or hits a cap), which keeps CPU at zero when
Jarvis is idle and avoids holding the mic open.
"""
import os
import queue
import re
import subprocess
import tempfile
import threading

from core import config

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_DIR = os.path.join(BASE, "models")

SAMPLE_RATE = 16000
_BLOCK = 1600           # 100ms blocks
_SILENCE_RMS = 0.012    # below this counts as silence
# Dead time you feel directly: nothing happens between you finishing a sentence
# and this elapsing. Transcription itself is only ~0.6s, so the old 1.2s wait
# was the single largest chunk of the pause after speaking.
_SILENCE_BLOCKS = 7     # ~0.7s of silence ends the utterance
_MIN_BLOCKS = 3         # ignore instant blips
_MAX_BLOCKS = 150       # ~15s hard cap

_model = None
_model_lock = threading.Lock()
_model_error = None

_recording = False
_stop_flag = threading.Event()


# --------------------------------------------------------------------------
# Speech to text
# --------------------------------------------------------------------------

def _model_name() -> str:
    return config.get("voice", "whisper_model", default="base.en")


def stt_available() -> bool:
    """Is speech-to-text installed?

    Checks that the packages *exist* rather than importing them. Importing
    faster_whisper cold pulls in ctranslate2 and friends and measured 79s on
    this machine — and this is called from the status poll, so the window sat
    frozen behind it right after opening.
    """
    import importlib.util
    try:
        return all(importlib.util.find_spec(m) is not None
                   for m in ("faster_whisper", "sounddevice"))
    except Exception:
        return False


def _local_snapshot() -> str:
    """Path of an already-downloaded model snapshot, or '' if there isn't one.

    Passing the directory straight to WhisperModel skips huggingface_hub
    entirely. That matters a lot here: going through the hub re-checks the
    repo over the network and — because Windows without Developer Mode can't
    symlink — re-materialises the 138MB blob, which took ~220s per load. From
    the local snapshot it is about a second.
    """
    import glob
    pattern = os.path.join(
        MODEL_DIR, f"models--*faster-whisper-{_model_name()}", "snapshots", "*", "model.bin"
    )
    hits = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    return os.path.dirname(hits[0]) if hits else ""


def load_model():
    """Load Whisper once. First call downloads the model (~75-145MB)."""
    global _model, _model_error
    if _model is not None or _model_error:
        return _model
    with _model_lock:
        if _model is not None or _model_error:
            return _model
        try:
            from faster_whisper import WhisperModel
            os.makedirs(MODEL_DIR, exist_ok=True)
            local = _local_snapshot()
            _model = WhisperModel(
                local or _model_name(), device="cpu", compute_type="int8",
                download_root=None if local else MODEL_DIR,
            )
        except Exception as e:
            _model_error = str(e)
    return _model


def warm():
    """Preload in the background so the first use isn't slow.

    Covers both halves: the Whisper model (~5s) and the audio stack used for
    speech-out — importing PyAV's decoder costs ~2.3s the first time, which
    otherwise lands on the first spoken reply.
    """
    def go():
        load_model()
        try:
            import sounddevice  # noqa: F401
            from faster_whisper.audio import decode_audio  # noqa: F401
        except Exception:
            pass

    threading.Thread(target=go, daemon=True).start()


def is_recording() -> bool:
    return _recording


def stop():
    """Ask an in-progress listen() to finish early."""
    _stop_flag.set()


def record(on_level=None, preroll=None) -> "object":
    """Record one utterance from the default mic. Returns a float32 numpy array.

    `on_level(rms)` is called per 100ms block so the UI can animate.
    `preroll` is audio captured before this stream opened (see skills/wake.py)
    — without it the first word after "Hey Jarvis" is lost to the handover.
    """
    global _recording
    import numpy as np
    import sounddevice as sd

    _stop_flag.clear()
    blocks, silence, voiced = [], 0, 0
    if preroll is not None and len(preroll):
        # Count it as speech already heard so a short answer isn't cut off by
        # the silence detector before the user has finished.
        blocks.append(np.asarray(preroll, dtype="float32").reshape(-1, 1))
        voiced = _MIN_BLOCKS
    q: "queue.Queue" = queue.Queue()

    def cb(indata, _frames, _time, _status):
        q.put(indata.copy())

    _recording = True
    try:
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                            blocksize=_BLOCK, callback=cb):
            while len(blocks) < _MAX_BLOCKS and not _stop_flag.is_set():
                try:
                    block = q.get(timeout=1.0)
                except queue.Empty:
                    break
                blocks.append(block)
                rms = float(np.sqrt(np.mean(block ** 2)))
                if on_level:
                    try:
                        on_level(rms)
                    except Exception:
                        pass
                if rms >= _SILENCE_RMS:
                    voiced += 1
                    silence = 0
                else:
                    silence += 1
                # only stop on silence once the user has actually said something
                if voiced >= _MIN_BLOCKS and silence >= _SILENCE_BLOCKS:
                    break
    finally:
        _recording = False

    if not blocks:
        return np.zeros(0, dtype="float32")
    return np.concatenate(blocks, axis=0).flatten()


def transcribe(audio) -> str:
    import numpy as np

    if audio is None or len(audio) == 0:
        return ""
    peak = float(np.max(np.abs(audio)))
    if peak < 0.01:
        return ""  # effectively silence; don't wake the model
    model = load_model()
    if model is None:
        return ""
    segments, _info = model.transcribe(
        audio, language="en", beam_size=1, vad_filter=True,
        condition_on_previous_text=False,
    )
    raw = " ".join(s.text.strip() for s in segments).strip()
    return split_command(raw)


# The pre-roll deliberately includes the tail of "Hey Jarvis" so the first real
# word isn't clipped, which means the wake phrase can land in the transcript.
# Drop it rather than routing on it.
_WAKE_PREFIX = re.compile(
    r"^\s*(?:hey|hi|ok|okay)?[\s,]*jar+v[ie]s+[\s,.!?-]*", re.I)


# Short commands that are complete the moment they're said. Recognising these
# lets a capture end immediately instead of waiting to see if more is coming —
# otherwise "Hey Jarvis stop" keeps the mic open and hoovers up whatever you
# say next.
_TERMINAL = re.compile(
    r"^\s*(stop|quiet|shut up|be quiet|stop talking|shush|cancel|"
    r"nevermind|never mind|thanks|thank you|that'?s all|forget it|"
    r"go away|dismiss)\s*[.!]?\s*$", re.I)


def is_terminal_command(text: str) -> bool:
    """Is this a complete short command needing nothing further?"""
    return bool(_TERMINAL.match(strip_wake(text or "")))


# The same words near the start of a longer transcript. A word or two of slack
# is allowed in front because the wake word is often mangled — "Hey Jarvis
# stop" has come back as "Java stop." and as "Jarvis, stop." — and an anchored
# match would miss all of those.
_TERMINAL_LEAD = re.compile(
    # only greeting/wake-word-shaped slack may precede the command, so
    # "can you stop, please do" is left intact while "Java stop." is not
    r"^\s*(?:(?:hey|hi|ok|okay|yo)[\s,]+)?"
    r"(?:[a-z]*j[a-z]{1,6}s?[\s,]+)?"
    r"(stop|quiet|shut up|be quiet|stop talking|shush|cancel|"
    r"nevermind|never mind|that'?s all|forget it|go away|dismiss)"
    r"\s*[.,!?]+\s+(?=\S)", re.I)


def split_command(text: str) -> str:
    """Trim anything after a completed short command.

    The microphone stays open until it hears a pause, so saying "Hey Jarvis
    stop" and then carrying on talking to someone else produced
    "stop. So anyway, I told him the meeting was...". The command was finished
    at the first full stop; the rest was never addressed to Jarvis.

    Deliberately requires punctuation after the command, so "stop the music"
    and "cancel my subscription" are left alone.
    """
    cleaned = strip_wake(text or "")
    m = _TERMINAL_LEAD.match(cleaned)
    if not m:
        return cleaned
    return m.group(1)


def strip_wake(text: str) -> str:
    cleaned = _WAKE_PREFIX.sub("", text or "", count=1).strip()
    return cleaned or (text or "").strip()


def listen(on_level=None) -> str:
    """Record one utterance and return its transcript."""
    return transcribe(record(on_level=on_level))


# --------------------------------------------------------------------------
# Text to speech
# --------------------------------------------------------------------------

def _tts_engine() -> str:
    return config.get("voice", "tts", default="edge-tts")


# Microsoft's newer conversational voices sound markedly less synthetic than
# the older "Friendly/Positive" set (en-AU-William, en-GB-Ryan and friends).
# Worth trying if Andrew isn't to taste:
#   en-US-AndrewNeural  warm, confident      (default)
#   en-US-BrianNeural   approachable, casual
#   en-US-EmmaNeural    cheerful, clear
#   en-US-AvaNeural     expressive, caring
NATURAL_VOICES = ["en-US-AndrewNeural", "en-US-BrianNeural",
                  "en-US-EmmaNeural", "en-US-AvaNeural",
                  "en-AU-WilliamMultilingualNeural", "en-GB-SoniaNeural"]


def _voice_name() -> str:
    return config.get("voice", "edge_voice", default="en-US-AndrewNeural")


# --------------------------------------------------------------------------
# Turning display text into something worth listening to.
#
# Replies are written to be *read*: "AXSM +3.7% · NVDA ×8 · $8,989". Handed
# straight to a speech engine that becomes "A X S M plus three point seven
# percent middle dot N V D A times eight" — the stray "times" and "plus"
# are the symbols being read literally, not words anyone wrote.
# --------------------------------------------------------------------------

_NUM = r"\d(?:[\d,]*\d)?(?:\.\d+)?"     # 8,989 / 1,445 / 3.7 — never a trailing comma

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def _spoken_date(m):
    try:
        return f"{int(m.group(3))} {_MONTHS[int(m.group(2)) - 1]} {m.group(1)}"
    except Exception:
        return m.group(0)


_SPEECH_RULES = [
    # ISO dates before the range rule, or 2026-07-26 becomes "2026 to 07 to 26"
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), _spoken_date),
    # ranges before dashes become pauses: "5–12°C" is "5 to 12", not "5, 12"
    (re.compile(r"(\d)\s*[–—-]\s*(\d)"), r"\1 to \2"),
    # degrees
    (re.compile(r"°\s*C\b"), " degrees"),
    (re.compile(r"°"), " degrees"),
    # signed percentages read as direction, not arithmetic
    (re.compile(r"\+(" + _NUM + r")\s*%"), r"up \1 percent"),
    (re.compile(r"[-−](" + _NUM + r")\s*%"), r"down \1 percent"),
    (re.compile(r"(" + _NUM + r")\s*%"), r"\1 percent"),
    # "NVDA ×8" -> "NVDA, 8 mentions"  (this is where "times" came from)
    (re.compile(r"\s*[×]\s*(\d+)"), r", \1 mentions"),
    # money, sign first so it reads as a direction
    (re.compile(r"[-−]\s*\$(" + _NUM + r")"), r"down \1 dollars"),
    (re.compile(r"\+\s*\$(" + _NUM + r")"), r"up \1 dollars"),
    (re.compile(r"\$(" + _NUM + r")"), r"\1 dollars"),
    # separators become pauses rather than being pronounced
    (re.compile(r"\s*[·•]\s*"), ", "),
    (re.compile(r"\s*[—–]\s*"), ", "),
    (re.compile(r"\s*\|\s*"), ", "),
    (re.compile(r"#(\d+)"), r"number \1"),
    (re.compile(r"\s*&\s*"), " and "),
    (re.compile(r"[“”\"']"), ""),
    # each item already says "up"/"down", so the heading just stutters aloud
    (re.compile(r"\b(?:Gainers|Fallers|Up|Down):\s*"), ""),
    # tidy up
    (re.compile(r"(\d)\.0\b"), r"\1"),
    (re.compile(r"\s{2,}"), " "),
    (re.compile(r"(,\s*){2,}"), ", "),
    (re.compile(r"\s+([,.])"), r"\1"),
]


def speech_text(text: str) -> str:
    """Rewrite a reply so it sounds like a sentence when spoken."""
    out = (text or "").strip()
    for pattern, repl in _SPEECH_RULES:
        out = pattern.sub(repl, out)
    return out.strip(" ,").replace(" ,", ",")


def speak(text: str) -> dict:
    """Say `text`.

    `voice.tts`:
      edge-tts  nicer voice, needs internet, ~1.5-2.5s before it starts
      pyttsx3   offline Windows SAPI, starts almost instantly, robotic
    edge-tts falls back to pyttsx3 on any failure, so replies still get spoken
    offline.
    """
    text = speech_text(text)
    if not text:
        return {"ok": False, "engine": None}
    if _tts_engine() == "edge-tts":
        if _speak_edge(text):
            return {"ok": True, "engine": "edge-tts"}
    if _speak_pyttsx3(text):
        return {"ok": True, "engine": "pyttsx3"}
    return {"ok": False, "engine": None}


_play_seq = 0
_play_proc = None


def _speak_edge(text: str) -> bool:
    """Synthesise with edge-tts and play it.

    Latency matters here: `Communicate.save()` waits for the *entire* clip
    before returning (~4s), which is why replies used to arrive ~5s after the
    text appeared. Streaming the chunks and playing as soon as the stream ends
    lands around 1.5-2.5s, and playing through sounddevice avoids another
    ~0.3s of PowerShell startup.
    """
    global _play_seq
    try:
        import asyncio

        import edge_tts

        # A fresh file per utterance: a fixed name got overwritten mid-playback
        # when two replies landed close together.
        _play_seq += 1
        path = os.path.join(tempfile.gettempdir(), f"jarvis_tts_{_play_seq % 6}.mp3")

        async def go():
            with open(path, "wb") as f:
                async for chunk in edge_tts.Communicate(text, _voice_name()).stream():
                    if chunk.get("type") == "audio" and chunk.get("data"):
                        f.write(chunk["data"])

        asyncio.run(go())
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return False
        if _play_decoded(path):
            return True
        _play(path)          # fall back to the external player
        return True
    except Exception:
        return False


def _play_decoded(path: str) -> bool:
    """Play an mp3 in-process via sounddevice. Returns False if it can't."""
    global _play_proc
    try:
        import numpy as np
        import sounddevice as sd
        from faster_whisper.audio import decode_audio

        audio = decode_audio(path, sampling_rate=24000)
        if audio is None or len(audio) == 0:
            return False
        stop_speaking()
        sd.play(np.asarray(audio, dtype="float32"), 24000)
        _play_proc = "sounddevice"
        return True
    except Exception:
        return False


def _play(path: str):
    """Play a file via PowerShell's MediaPlayer — no extra dependency.

    MediaPlayer.Open() is asynchronous: NaturalDuration is not populated for a
    beat afterwards. Reading it too early yields 00:00:00, so the old code slept
    ~1s and closed the player mid-sentence — you heard only the first word.
    Wait for HasTimeSpan before trusting the duration.
    """
    global _play_proc
    stop_speaking()  # cut off a previous reply still being read out
    ps = (
        "Add-Type -AssemblyName presentationCore;"
        "$p=New-Object System.Windows.Media.MediaPlayer;"
        f"$p.Open([uri]'{path}');"
        "$n=0; while(-not $p.NaturalDuration.HasTimeSpan -and $n -lt 60)"
        "{Start-Sleep -Milliseconds 50; $n++};"
        "$d = if($p.NaturalDuration.HasTimeSpan)"
        "{$p.NaturalDuration.TimeSpan.TotalSeconds}else{20};"
        "$p.Play();"
        "Start-Sleep -Milliseconds ([int](($d + 0.7) * 1000));"
        "$p.Stop(); $p.Close()"
    )
    try:
        _play_proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        _play_proc = None


def is_speaking() -> bool:
    """True while a reply is actually being read out."""
    if _play_proc == "sounddevice":
        try:
            import sounddevice as sd
            return bool(sd.get_stream().active)
        except Exception:
            return True     # we started it and weren't told it finished
    if _play_proc is not None:
        try:
            return _play_proc.poll() is None
        except Exception:
            return False
    return False


def stop_speaking():
    """Silence any in-progress playback, whichever backend is playing."""
    global _play_proc
    if _play_proc == "sounddevice":
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass
    elif _play_proc is not None and getattr(_play_proc, "poll", lambda: 0)() is None:
        try:
            _play_proc.terminate()
        except Exception:
            pass
    _play_proc = None


def _speak_pyttsx3(text: str) -> bool:
    try:
        import pyttsx3
        engine = pyttsx3.init()
        engine.say(text)
        engine.runAndWait()
        engine.stop()
        return True
    except Exception:
        return False


def status() -> dict:
    return {
        "stt": stt_available(),
        "model": _model_name(),
        "model_loaded": _model is not None,
        "tts": _tts_engine(),
        "recording": _recording,
    }
