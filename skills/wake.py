"""Always-on "Hey Jarvis" wake word (openWakeWord, local, free).

Off by default — it holds the microphone open for as long as it runs, which is
a real privacy and battery consideration, so it is opt-in via
`voice.wake_word` in settings.json.

Nothing is recorded or sent anywhere while it listens: audio is fed frame by
frame into a small local ONNX model and discarded. Only once the wake word
scores above the threshold does the normal (bounded, 15s) capture start.

The wake stream releases the microphone before that capture begins and resumes
afterwards, so the two never fight over the device.
"""
import threading
import time

from core import config

SAMPLE_RATE = 16000
FRAME = 1280           # 80ms — openWakeWord's expected chunk

_model = None
_thread = None
_running = False
_paused = False
_lock = threading.Lock()
_last_fire = 0.0
_last_score = 0.0
_frames_seen = 0
_drops = 0
_peak_since_reset = [0.0]

# Rolling buffer of the most recent audio, kept so the recorder can be handed
# what was said *during* the switch-over. Between the wake word firing and the
# recorder opening its own stream there is a gap of a few hundred milliseconds,
# and people start their sentence immediately after "Hey Jarvis" — so the first
# word or two was being lost, which is what produced garbled questions.
PREROLL_SECONDS = 2.0
_preroll: list = []
_preroll_lock = threading.Lock()


def _preroll_push(frame):
    max_frames = int(PREROLL_SECONDS * SAMPLE_RATE / FRAME)
    with _preroll_lock:
        _preroll.append(frame)
        if len(_preroll) > max_frames:
            del _preroll[:-max_frames]


def take_preroll(seconds: float = 0.55):
    """Audio captured just before/while the wake word fired, as float32.

    Returned once and then cleared, so it can't leak into a later recording.
    """
    import numpy as np
    want = int(seconds * SAMPLE_RATE / FRAME)
    with _preroll_lock:
        frames = list(_preroll[-want:]) if _preroll else []
        _preroll.clear()
    if not frames:
        return None
    pcm = np.concatenate([np.squeeze(f) for f in frames]).astype("float32")
    return pcm / 32768.0


def clear_preroll():
    with _preroll_lock:
        _preroll.clear()


def _model_name() -> str:
    return config.get("voice", "wake_model", default="hey_jarvis_v0.1")


def _threshold() -> float:
    return float(config.get("voice", "wake_threshold", default=0.55) or 0.55)


def is_enabled() -> bool:
    return bool(config.get("voice", "wake_word", default=False))


def is_running() -> bool:
    return _running


def last_score() -> float:
    return _last_score


def available() -> bool:
    """Is the wake word installed? Checks for the packages without importing
    them — importing openwakeword is slow, and status polling must not pay
    that (see the same note in skills/voice.py)."""
    import importlib.util
    try:
        return all(importlib.util.find_spec(m) is not None
                   for m in ("openwakeword", "sounddevice"))
    except Exception:
        return False


def load():
    """Load the wake model once (~1s, small ONNX graph)."""
    global _model
    if _model is not None:
        return _model
    with _lock:
        if _model is not None:
            return _model
        from openwakeword.model import Model
        _model = Model(wakeword_models=[_model_name()], inference_framework="onnx")
    return _model


def pause():
    """Release the mic (the main capture is about to use it)."""
    global _paused
    _paused = True


def resume():
    global _paused
    _paused = False


# Utterance capture, continuing on the wake stream (see the comment at the
# firing site). Mirrors the thresholds in skills/voice.py.
CAPTURE_SILENCE_RMS = 0.012
CAPTURE_SILENCE_FRAMES = 9        # ~0.7s of silence closes the utterance
CAPTURE_MAX_FRAMES = 188          # ~15s hard cap
CAPTURE_LEAD_FRAMES = 6           # ~0.5s kept from before the trigger

_level_cb = None


def set_level_callback(fn):
    """UI hook: called with an RMS level per frame while capturing."""
    global _level_cb
    _level_cb = fn


def _silence_self() -> bool:
    """Cut Jarvis's own playback the instant the wake word lands.

    Returns whether it was actually speaking, so the caller can discard the
    pre-roll — that lead is from *before* detection, so if Jarvis was talking
    it contains Jarvis, not you.
    """
    try:
        from skills import voice
        speaking = voice.is_speaking()
        voice.stop_speaking()
        if speaking:
            # let the device drain so the tail isn't recorded as speech
            time.sleep(0.12)
        return speaking
    except Exception:
        return False


def _capture(q, drop_lead: bool = False):
    """Read the already-open stream until the speaker stops. float32 audio.

    The speech/silence gate is relative to the room rather than a fixed number.
    A fixed threshold either runs the full 15s cap in a quiet room with a
    low-gain mic (nothing ever counts as speech, so the end is never detected)
    or cuts you off in a loud one.
    """
    import numpy as np

    with _preroll_lock:
        lead = [] if drop_lead else list(_preroll[-CAPTURE_LEAD_FRAMES:])
        recent = [float(np.sqrt(np.mean((np.squeeze(f).astype("float32") / 32768.0) ** 2)))
                  for f in _preroll[-20:]]
        if drop_lead:
            _preroll.clear()      # it holds Jarvis's voice, not the user's
    floor = min(recent) if recent else 0.0
    gate = max(0.006, min(CAPTURE_SILENCE_RMS, floor * 4 + 0.004))
    if drop_lead:
        # The room's noise floor was measured while a speaker was playing, so
        # it reads high; fall back to the plain threshold.
        gate = CAPTURE_SILENCE_RMS
        # discard whatever is already queued — it is the tail of the reply
        try:
            while True:
                q.get_nowait()
        except Exception:
            pass

    frames = list(lead)
    silence = voiced = 0
    peak = 0.0
    while len(frames) < CAPTURE_MAX_FRAMES and _running:
        try:
            frame = q.get(timeout=1.0)
        except Exception:
            break
        frames.append(frame)
        block = np.squeeze(frame).astype("float32") / 32768.0
        rms = float(np.sqrt(np.mean(block ** 2)))
        peak = max(peak, rms)
        if _level_cb:
            try:
                _level_cb(rms)
            except Exception:
                pass
        if rms >= gate:
            voiced += 1
            silence = 0
        else:
            silence += 1
        if voiced >= 2 and silence >= CAPTURE_SILENCE_FRAMES:
            break
        # Nothing said at all: give up early rather than banking 15s of silence.
        if voiced == 0 and len(frames) > 38:
            break

        # A short command like "stop" is finished as soon as it's said. Peek at
        # the audio during the first pause and, if that's all it was, close the
        # microphone now — waiting for the full silence window meant whatever
        # you said next was recorded too.
        # NB: do not transcribe here to detect a finished command. Whisper on
        # this thread blocks frame consumption for ~1s, the queue backs up and
        # the stream desyncs — tried it, and captures came back empty. A
        # trailing command is trimmed after the fact instead (voice.split_command).
    if not frames:
        return None
    pcm = np.concatenate([np.squeeze(f) for f in frames]).astype("float32")
    return pcm / 32768.0


def start(on_wake) -> bool:
    """Begin listening for the wake word. `on_wake()` runs on a worker thread."""
    global _thread, _running
    if _running:
        return True
    # A previous listener may still be unwinding. Without this wait, start()
    # saw _running as True, returned early without spawning anything, and the
    # old thread then exited — leaving nothing listening at all.
    if _thread is not None and _thread.is_alive():
        _thread.join(timeout=2.0)
    # Starting implies un-pausing. A listener left paused by a previous capture
    # would otherwise sit in its sleep loop forever, silently never listening.
    resume()
    if not available():
        return False
    try:
        load()
    except Exception:
        return False

    _running = True

    def loop():
        global _last_fire, _last_score, _running, _frames_seen, _drops
        import queue as _queue

        import numpy as np
        import sounddevice as sd

        cooldown = float(config.get("voice", "wake_cooldown", default=3.0) or 3.0)
        try:
            while _running:
                if _paused:
                    time.sleep(0.15)
                    continue
                q: "_queue.Queue" = _queue.Queue(maxsize=64)

                def cb(indata, _frames, _time, status):
                    # Capture must never wait on inference. Blocking reads meant
                    # that whenever the app was busy (model loading, a network
                    # call) frames were lost, and openWakeWord needs contiguous
                    # audio — dropped frames are why the wake word fired only
                    # sometimes. The callback just enqueues.
                    if status:
                        globals()["_drops"] = _drops + 1
                    try:
                        q.put_nowait(indata.copy())
                    except Exception:
                        globals()["_drops"] = _drops + 1

                try:
                    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                        dtype="int16", blocksize=FRAME, callback=cb):
                        while _running and not _paused:
                            try:
                                frame = q.get(timeout=0.5)
                            except Exception:
                                continue
                            _frames_seen += 1
                            _preroll_push(frame)
                            scores = _model.predict(np.squeeze(frame))
                            score = max(scores.values()) if scores else 0.0
                            _last_score = float(score)
                            if score > _peak_since_reset[0]:
                                _peak_since_reset[0] = float(score)
                            now = time.time()
                            if score >= _threshold() and (now - _last_fire) > cooldown:
                                _last_fire = now
                                _model.reset()
                                # Silence Jarvis BEFORE recording, not after.
                                # stop_speaking() used to run in the wake
                                # handler, which fires only once the utterance
                                # is already captured — so the microphone spent
                                # that whole time recording Jarvis's own reply
                                # and "Hey Jarvis stop" came back as
                                # "stop this train was...".
                                was_speaking = _silence_self()
                                # Keep reading THIS stream straight into the
                                # recording. Closing it and opening another
                                # left a few hundred ms of dead air exactly
                                # where the question starts, which is why words
                                # went missing ("what is the weather in
                                # Auckland" came back as "the weather...").
                                utterance = _capture(q, drop_lead=was_speaking)
                                pause()
                                try:
                                    on_wake(utterance)
                                except Exception:
                                    pass
                                break
                except Exception:
                    time.sleep(0.6)   # device busy/unplugged — retry shortly
        finally:
            _running = False

    _thread = threading.Thread(target=loop, daemon=True)
    _thread.start()
    return True


def stop(wait: bool = True):
    """Stop listening. Waits for the audio thread to release the microphone,
    so a following start() (or a recording) doesn't race it for the device."""
    global _running
    _running = False
    if wait and _thread is not None and _thread.is_alive():
        _thread.join(timeout=2.0)


def status() -> dict:
    return {
        "available": available(),
        "enabled": is_enabled(),
        "running": _running,
        "paused": _paused,
        "model": _model_name(),
        "threshold": _threshold(),
        "last_score": round(_last_score, 3),
        "peak_score": round(_peak_since_reset[0], 3),
        "frames": _frames_seen,
        "dropped": _drops,
    }


def reset_peak():
    """Clear the running peak — used by the Settings mic test."""
    _peak_since_reset[0] = 0.0
