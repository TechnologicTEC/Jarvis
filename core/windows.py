"""pywebview window management for the full app and the mini popup.

Both windows are created hidden before webview.start() and are shown/hidden
from the tray, the global hotkey, and the js_api bridge. Closing a window
hides it (back to tray) unless we are actually quitting.
"""
import ctypes
import os

import webview

from core import config

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FULL = None
MINI = None
QUITTING = False

# Set by main.py once the global hotkey hook is (or isn't) registered. The mini
# popup is frameless, so if the hook failed there'd be no way to dismiss it —
# the UI reads this and falls back to closing on Esc itself.
HOTKEY_OK = False

_full_visible = False
_mini_visible = False

FULL_TITLE = "Jarvis"
MINI_TITLE = "Jarvis Mini"


def _ui(name: str) -> str:
    return os.path.join(BASE, "ui", name)


def create_windows(api):
    global FULL, MINI
    fw, fh = config.get("ui", "full_size", default=[1180, 760])
    mw, mh = config.get("ui", "mini_size", default=[470, 400])
    FULL = webview.create_window(
        FULL_TITLE, _ui("popup.html"), js_api=api,
        width=fw, height=fh, min_size=(980, 640),
        hidden=True, background_color="#070d20",
    )
    MINI = webview.create_window(
        MINI_TITLE, _ui("popup-mini.html"), js_api=api,
        width=mw, height=mh, hidden=True,
        frameless=True, on_top=True, easy_drag=True,
        background_color="#050a16",
    )
    FULL.events.closing += _on_full_closing
    MINI.events.closing += _on_mini_closing


def _on_full_closing():
    if QUITTING:
        return True
    hide_full()
    return False  # cancel the close, we just hid it


def _on_mini_closing():
    if QUITTING:
        return True
    hide_mini()
    return False


def show_full():
    global _full_visible
    if FULL is None:
        return
    FULL.show()
    try:
        FULL.restore()
    except Exception:
        pass
    _full_visible = True


def hide_full():
    global _full_visible
    if FULL is not None:
        FULL.hide()
    _full_visible = False


def show_mini():
    global _mini_visible
    if MINI is None:
        return
    MINI.show()
    _mini_visible = True
    try:
        MINI.evaluate_js(
            "(function(){var i=document.getElementById('jc-input');if(i){i.focus();}})()"
        )
    except Exception:
        pass


def hide_mini():
    global _mini_visible
    if MINI is not None:
        MINI.hide()
    _mini_visible = False


def toggle_mini():
    if _mini_visible:
        hide_mini()
    else:
        show_mini()


def _foreground_title() -> str:
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value
    except Exception:
        return ""


def on_double_esc():
    """Global double-Esc: acts on whichever Jarvis window is in front,
    otherwise summons the mini popup."""
    title = _foreground_title()
    if title == MINI_TITLE:
        hide_mini()
    elif title == FULL_TITLE:
        hide_full()
    elif _mini_visible:
        hide_mini()
    else:
        show_mini()


def quit_all():
    global QUITTING
    QUITTING = True
    for w in (FULL, MINI):
        if w is not None:
            try:
                w.destroy()
            except Exception:
                pass
