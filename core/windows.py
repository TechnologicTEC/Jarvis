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
import time

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
# The compact window is positioned once; afterwards it stays where it was left.
_compact_placed = False


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

class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _window_rect():
    """The window's rect as the OS reports it (physical pixels)."""
    try:
        hwnd = ctypes.windll.user32.FindWindowW(None, TITLE)
        if not hwnd:
            return None
        rc = _RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rc))
        return rc
    except Exception:
        return None


_scale = None


def _calibrate():
    """Measure the ratio between the units resize() takes and physical pixels.

    Getting this from the DPI APIs is unreliable: GetDpiForSystem reports 96
    until the process happens to declare DPI awareness and 192 afterwards, so
    the answer depends on when you ask. Meanwhile resize()/move() take logical
    pixels while webview.screens reports physical ones — a 2x error on this
    200%-scaled display, which is what threw the window off-screen. Measuring
    once sidesteps every one of those assumptions.
    """
    global _scale
    if _scale is not None or WIN is None:
        return _scale or 1.0
    try:
        probe_w, probe_h = 600, 400
        WIN.resize(probe_w, probe_h)
        time.sleep(0.25)
        rc = _window_rect()
        if rc:
            got = rc.right - rc.left
            ratio = got / float(probe_w)
            # only trust a sane, near-standard scale
            _scale = ratio if 0.5 <= ratio <= 4.0 else 1.0
        else:
            _scale = 1.0
    except Exception:
        _scale = 1.0
    return _scale


def _screen_size():
    """Screen size in the SAME coordinate space as Window.move()/resize()."""
    phys = None
    try:
        screens = webview.screens
        if screens and screens[0].width and screens[0].height:
            phys = (int(screens[0].width), int(screens[0].height))
    except Exception:
        pass
    if phys is None:
        try:
            user32 = ctypes.windll.user32
            phys = (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
        except Exception:
            return 1440, 900
    scale = _scale or 1.0
    return max(640, int(phys[0] / scale)), max(480, int(phys[1] / scale))


def _compact_pos(w, h, sw, sh):
    """Where the pinned console sits. Centred by default so it can't land
    off-screen; 'corner' parks it bottom-right once you know where it goes."""
    where = config.get("ui", "compact_position", default="centre")
    if where in ("corner", "bottom-right"):
        return max(0, sw - w - 28), max(0, sh - h - 92)
    return max(0, (sw - w) // 2), max(0, (sh - h) // 3)


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
        global _compact_placed
        w, h = _compact_size or _compact_default()
        try:
            WIN.restore()               # leave maximised state before shrinking
            WIN.resize(w, h)
            # Place it once; after that the window stays where the user put it.
            if not _compact_placed:
                WIN.move(*_compact_pos(w, h, sw, sh))
                _compact_placed = True
            WIN.on_top = True
        except Exception:
            pass
    else:
        try:
            WIN.on_top = False
            if config.get("ui", "full_maximised", default=True):
                WIN.maximize()          # fill the screen, DPI-independent
            else:
                w, h = _full_size()
                WIN.restore()
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


def _resize_compact(w, h):
    """Resize only — never move.

    Moving here fought the user: opening the setups menu changes the height,
    and re-positioning on every height change snapped a window they had dragged
    into a corner straight back to the middle. Position is chosen once, when
    compact mode is first entered, and is the user's from then on.
    """
    if WIN is None:
        return
    try:
        WIN.resize(w, h)
    except Exception:
        pass


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
    if MODE == "compact":
        _resize_compact(w, h)
    return {"ok": True, "w": w, "h": h}


# --------------------------------------------------------------------------
# Show / hide
# --------------------------------------------------------------------------

def show(mode: str = None):
    global _visible
    if WIN is None:
        return
    # Show BEFORE sizing: maximize()/move() are no-ops on a hidden window, so
    # doing it the other way round left the window at its default geometry.
    WIN.show()
    _visible = True
    _calibrate()          # needs a visible window; runs once
    set_mode(mode or MODE)
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
