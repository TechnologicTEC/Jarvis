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
        global _last_fire, _last_score, _running
        import numpy as np
        import sounddevice as sd

        cooldown = float(config.get("voice", "wake_cooldown", default=3.0) or 3.0)
        try:
            while _running:
                if _paused:
                    time.sleep(0.15)
                    continue
                try:
                    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                        dtype="int16", blocksize=FRAME) as stream:
                        while _running and not _paused:
                            frame, _overflow = stream.read(FRAME)
                            scores = _model.predict(np.squeeze(frame))
                            score = max(scores.values()) if scores else 0.0
                            _last_score = float(score)
                            now = time.time()
                            if score >= _threshold() and (now - _last_fire) > cooldown:
                                _last_fire = now
                                try:
                                    on_wake()
                                except Exception:
                                    pass
                                # the handler takes the mic from here
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
    }
