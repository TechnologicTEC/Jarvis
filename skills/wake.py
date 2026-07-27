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
    try:
        import openwakeword  # noqa: F401
        import sounddevice  # noqa: F401
        return True
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


def start(on_wake) -> bool:
    """Begin listening for the wake word. `on_wake()` runs on a worker thread."""
    global _thread, _running
    if _running:
        return True
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
                            scores = _model.predict(np.squeeze(frame))
                            score = max(scores.values()) if scores else 0.0
                            _last_score = float(score)
                            if score > _peak_since_reset[0]:
                                _peak_since_reset[0] = float(score)
                            now = time.time()
                            if score >= _threshold() and (now - _last_fire) > cooldown:
                                _last_fire = now
                                # Pause BEFORE handing over, or the outer loop
                                # reopens the stream and races the recorder for
                                # the mic, losing the start of what you say.
                                pause()
                                _model.reset()
                                try:
                                    on_wake()
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


def stop():
    global _running
    _running = False


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
