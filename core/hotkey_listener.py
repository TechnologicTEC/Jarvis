"""Double-Esc detection via a global low-level keyboard hook.

Uses the `keyboard` package. If hooks fail (some setups need admin rights),
start() returns False and the app still works from the tray menu — the spec's
fallback plan is to swap in pynput if this becomes a problem.
"""
import time

try:
    import keyboard
except Exception:  # pragma: no cover - import can fail without admin rights
    keyboard = None


class HotkeyListener:
    def __init__(self, callback, window_ms=400):
        self.callback = callback
        self.window = window_ms / 1000.0
        self._last = 0.0
        self._hook = None

    def start(self) -> bool:
        if keyboard is None:
            return False
        try:
            self._hook = keyboard.on_press_key("esc", self._on_esc)
            return True
        except Exception:
            return False

    def _on_esc(self, _event):
        now = time.monotonic()
        if now - self._last <= self.window:
            self._last = 0.0
            try:
                self.callback()
            except Exception:
                pass
        else:
            self._last = now

    def stop(self):
        if keyboard is not None and self._hook is not None:
            try:
                keyboard.unhook(self._hook)
            except Exception:
                pass
            self._hook = None
