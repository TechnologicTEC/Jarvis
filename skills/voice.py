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
import subprocess
import tempfile
import threading

from core import config

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_DIR = os.path.join(BASE, "models")

SAMPLE_RATE = 16000
_BLOCK = 1600           # 100ms blocks
_SILENCE_RMS = 0.012    # below this counts as silence
_SILENCE_BLOCKS = 12    # ~1.2s of silence ends the utterance
_MIN_BLOCKS = 4         # ignore instant blips
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
    try:
        import faster_whisper  # noqa: F401
        import sounddevice  # noqa: F401
        return True
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


def record(on_level=None) -> "object":
    """Record one utterance from the default mic. Returns a float32 numpy array.

    `on_level(rms)` is called per 100ms block so the UI can animate.
    """
    global _recording
    import numpy as np
    import sounddevice as sd

    _stop_flag.clear()
    blocks, silence, voiced = [], 0, 0
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
    return " ".join(s.text.strip() for s in segments).strip()


def listen(on_level=None) -> str:
    """Record one utterance and return its transcript."""
    return transcribe(record(on_level=on_level))


# --------------------------------------------------------------------------
# Text to speech
# --------------------------------------------------------------------------

def _tts_engine() -> str:
    return config.get("voice", "tts", default="edge-tts")


def _voice_name() -> str:
    return config.get("voice", "edge_voice", default="en-AU-WilliamNeural")


def speak(text: str) -> dict:
    """Say `text`.

    `voice.tts`:
      edge-tts  nicer voice, needs internet, ~1.5-2.5s before it starts
      pyttsx3   offline Windows SAPI, starts almost instantly, robotic
    edge-tts falls back to pyttsx3 on any failure, so replies still get spoken
    offline.
    """
    text = (text or "").strip()
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
