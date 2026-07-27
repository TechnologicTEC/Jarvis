"""One window, two sizes.

Jarvis is a single frameless pywebview window running ui/popup.html. It has two
modes rather than two windows, so state, the loaded model and the backend are
shared and there is nothing to keep in sync:

    full     the home base — nav rail, tabs, composer
    compact  the same app shrunk to the pinned command console, always-on-top,
             parked in a screen corner

Double-space toggles. The window is frameless in both modes (the design draws
its own header), so drag handles are marked with `.pywebview-drag-region`
rather than using easy_drag, which would otherwise make the whole full-size
page draggable.
"""
import ctypes
import os

import webview

from core import config

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WIN = None
QUITTING = False
HOTKEY_OK = False

TITLE = "Jarvis"

MODE = "full"
_visible = False
# Set once the compact view has measured itself in the DOM, so the window can
# be sized to the card exactly — no dead space around it.
_compact_size = None


def _ui(name: str) -> str:
    return os.path.join(BASE, "ui", name)


def _full_size():
    w, h = config.get("ui", "full_size", default=[1180, 760])
    return int(w), int(h)


def _compact_default():
    w, h = config.get("ui", "compact_size", default=[440, 150])
    return int(w), int(h)


def create_windows(api):
    global WIN
    fw, fh = _full_size()
    WIN = webview.create_window(
        TITLE, _ui("popup.html"), js_api=api,
        width=fw, height=fh, min_size=(430, 140),
        hidden=True, frameless=True, easy_drag=False,
        background_color="#070d20", resizable=True,
    )
    WIN.events.closing += _on_closing


def _on_closing():
    if QUITTING:
        return True
    hide()
    return False  # closing just hides to tray


# --------------------------------------------------------------------------
# Mode switching
# --------------------------------------------------------------------------

def _screen_size():
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDPIAware()
        return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    except Exception:
        return 1920, 1080


def set_mode(mode: str, announce=True):
    """Switch between 'full' and 'compact'."""
    global MODE
    if WIN is None:
        return MODE
    mode = "compact" if mode == "compact" else "full"
    MODE = mode

    # Resize BEFORE telling the page to switch layout: the compact view
    # measures itself once it renders, and measuring inside a full-size window
    # reports the wrong height.
    sw, sh = _screen_size()
    if mode == "compact":
        w, h = _compact_size or _compact_default()
        try:
            WIN.resize(w, h)
            WIN.move(max(0, sw - w - 28), max(0, sh - h - 92))
            WIN.on_top = True
        except Exception:
            pass
    else:
        w, h = _full_size()
        try:
            WIN.on_top = False
            WIN.resize(w, h)
            WIN.move(max(0, (sw - w) // 2), max(0, (sh - h) // 2 - 20))
        except Exception:
            pass

    if announce:
        try:
            WIN.evaluate_js(
                "(function(){if(window.jarvisSetLayout)"
                "window.jarvisSetLayout('%s');})()" % mode
            )
        except Exception:
            pass
    return MODE


def toggle_mode():
    return set_mode("full" if MODE == "compact" else "compact")


def set_compact_height(h: int):
    """Called from JS once the compact card has rendered, so the window hugs
    the card exactly and no dead space shows around it.

    Height only — the width is whatever was configured. Letting the page report
    a width raced the resize and fed back the full-size window's width.
    """
    global _compact_size
    w = (_compact_size or _compact_default())[0]
    h = max(90, min(600, int(h)))
    _compact_size = (w, h)
    if MODE == "compact" and WIN is not None:
        sw, sh = _screen_size()
        try:
            WIN.resize(w, h)
            WIN.move(max(0, sw - w - 28), max(0, sh - h - 92))
        except Exception:
            pass
    return {"ok": True, "w": w, "h": h}


# --------------------------------------------------------------------------
# Show / hide
# --------------------------------------------------------------------------

def show(mode: str = None):
    global _visible
    if WIN is None:
        return
    if mode:
        set_mode(mode)
    WIN.show()
    try:
        WIN.restore()
    except Exception:
        pass
    _visible = True
    _focus_input()


def _focus_input():
    try:
        WIN.evaluate_js(
            "(function(){if(window.jarvisFocus)window.jarvisFocus();})()"
        )
    except Exception:
        pass


def hide():
    global _visible
    if WIN is not None:
        WIN.hide()
    _visible = False


def is_visible() -> bool:
    return _visible


# Back-compat names used elsewhere in the app.
def show_full():
    show("full")


def show_mini():
    show("compact")


def hide_full():
    hide()


def hide_mini():
    hide()


def _foreground_is_jarvis() -> bool:
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value == TITLE
    except Exception:
        return False


def on_double_esc():
    """Global double-Esc: summon Jarvis, or dismiss it if it's already in front.

    Opens the app itself (at whatever size was last used) rather than a
    separate popup — there is only one window now.
    """
    if _visible and _foreground_is_jarvis():
        hide()
    else:
        show()


def quit_all():
    global QUITTING
    QUITTING = True
    if WIN is not None:
        try:
            WIN.destroy()
        except Exception:
            pass
