"""Jarvis tray host - owns the hotkey listener, the tray icon, and the window.

Run:  python main.py           (opens the full app + tray)
      python main.py --tray    (starts silent in the tray; used by autostart)
"""
import argparse
import os
import threading
import time

import webview

from api import JarvisApi
from core import config, single_instance, windows
from core.hotkey_listener import HotkeyListener

# pystray and PIL are imported inside _build_tray(), not here. They are only
# needed by the tray icon, which already runs after the window is on screen,
# and on a cold cache every import is thousands of small disk reads competing
# with the rest of the login. Nothing above is optional; those two were.

TRAY = None
HOTKEY = None
API = None

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE, "jarvis.log")


def log(msg: str):
    """Print, and always append to jarvis.log.

    Under pythonw there is no console at all, so a file is the only way to see
    what happened. Launching with a console is worse than useless here: numpy's
    MKL runtime installs a console handler and aborts the whole process the
    moment that window closes ("forrtl: error (200)"), which silently killed
    Jarvis in the background.
    """
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    try:
        print(line)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _tray_image():
    """Use the real app icon so the tray matches the shortcuts."""
    from PIL import Image, ImageDraw
    ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "jarvis.ico")
    if os.path.isfile(ico):
        try:
            return Image.open(ico)
        except Exception:
            pass
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 60, 60), radius=16, fill=(92, 198, 232, 255))
    d.rounded_rectangle((20, 20, 44, 44), radius=7, fill=(7, 13, 32, 255))
    return img


def _quit(icon, _item):
    windows.quit_all()  # destroys windows -> webview.start() returns in main()
    icon.stop()


def _build_tray():
    import pystray
    menu = pystray.Menu(
        pystray.MenuItem("Open Jarvis", lambda i, it: windows.show("full"), default=True),
        pystray.MenuItem("Pin to corner", lambda i, it: windows.show("compact")),
        pystray.MenuItem("Hide", lambda i, it: windows.hide()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", _quit),
    )
    return pystray.Icon("jarvis", _tray_image(), "Jarvis", menu)


def _after_start(show_full_now: bool):
    """Runs once the webview event loop is up.

    The window goes on screen FIRST and everything else follows on a worker
    thread. It used to be the other way round — the hotkey hook, the tray icon
    and the wake-word model (a synchronous ONNX load) all ran ahead of
    show_full(), so at boot, with the disk busy, the screen stayed empty for
    minutes. Setups are read straight from a JSON file, so they work the
    moment the window paints, which is what you actually want at login.
    """
    if show_full_now:
        windows.show_full()
    threading.Thread(target=_background_init, daemon=True).start()


def _background_init():
    """Everything that can make you wait, done after the window is usable.

    The heavy parts (Whisper, the wake model, the stock connection) wait a
    little longer still: at login they'd otherwise be competing for the disk
    with every other startup program, which is exactly when you want the window
    responsive so you can hit a setup.
    """
    global TRAY, HOTKEY
    started = time.time()

    TRAY = _build_tray()
    threading.Thread(target=TRAY.run, daemon=True).start()

    HOTKEY = HotkeyListener(
        windows.on_double_esc,
        window_ms=config.get("hotkey", "double_esc_ms", default=400),
    )
    windows.HOTKEY_OK = HOTKEY.start()
    if not windows.HOTKEY_OK:
        log("global hotkey unavailable (keyboard hook failed) - use the tray menu")

    # Give the rest of the login rush a head start before loading models and
    # opening network connections. Nothing below is needed to click a setup.
    quiet = float(config.get("ui", "defer_heavy_seconds", default=20) or 0)
    if quiet > 0:
        time.sleep(quiet)

    # Load Whisper now, in the background, so the first Alt-to-talk doesn't
    # pay the ~5s model load. Costs ~300MB resident for the whole session -
    # set voice.preload_model false to trade that back for a slower first use.
    if (config.get("voice", "enabled", default=True)
            and config.get("voice", "preload_model", default=True)):
        try:
            from skills import voice
            voice.warm()
        except Exception:
            pass

    # Always-on "Hey Jarvis". Opt-in: it holds the microphone open.
    # On its own thread because wake.start() loads an ONNX model synchronously,
    # and nothing else should queue behind that.
    if config.get("voice", "enabled", default=True) and \
            config.get("voice", "wake_word", default=False):
        def _start_wake():
            try:
                from skills import wake
                if wake.start(API._on_wake):
                    log('listening for "Hey Jarvis"')
                else:
                    log("wake word unavailable (mic busy or model missing)")
            except Exception as e:
                log(f"wake word failed: {str(e)[:120]}")
        threading.Thread(target=_start_wake, daemon=True).start()

    # Warm the stocks path too. Against the hosted DB a cold portfolio read is
    # ~30s (per-ticker cache lookups are network round trips, plus live
    # quotes); doing it now means the first question answers in ~1s.
    if config.get("stocks", "warm_on_start", default=True):
        def _warm_stocks():
            try:
                from skills import stocks
                stocks.summary()
            except Exception as e:
                log(f"stocks warm-up failed: {str(e)[:120]}")
        threading.Thread(target=_warm_stocks, daemon=True).start()

    _start_inbox_poller()
    log(f"background init finished in {time.time() - started:.1f}s")


def _start_inbox_poller():
    """Background Gmail poll from the tray host. Detection only - it surfaces
    a suggestion and never writes the tracker without an explicit 'log it'."""
    minutes = int(config.get("inbox", "poll_minutes", default=45) or 45)
    if minutes <= 0:
        return

    def loop():
        from skills import email_tracker
        while not windows.QUITTING:
            time.sleep(minutes * 60)
            if windows.QUITTING:
                return
            try:
                if not email_tracker.is_authorized():
                    continue
                res = email_tracker.check_replies()
                hits = res.get("hits") or []
                if hits:
                    log(f"inbox: {len(hits)} reply(ies) detected - "
                          f"{hits[0]['company']} {hits[0]['outcome']}")
            except Exception as e:
                log(f"inbox poll failed: {str(e)[:120]}")

    threading.Thread(target=loop, daemon=True).start()


def main():
    # Stamped first so the log distinguishes "Jarvis was slow" from "Windows
    # started Jarvis late" — with a dozen startup items competing, the second
    # is usually the real story.
    log(f"starting (python up at +{time.process_time():.1f}s cpu)")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tray", action="store_true", help="start hidden in the tray")
    args = ap.parse_args()

    # Refuse to run twice: two copies fight over the mic, the hotkey and the
    # tray icon. Hand the request to the copy that's already up instead.
    if not single_instance.acquire():
        single_instance.summon_existing()
        log("another instance is already running - summoned it instead")
        return

    show_full_now = (not args.tray) and config.get("ui", "open_full_on_start", default=True)

    global API
    API = JarvisApi()
    api = API
    windows.create_windows(api)
    try:
        webview.start(_after_start, show_full_now)
    finally:
        if HOTKEY is not None:
            HOTKEY.stop()
        if TRAY is not None:
            TRAY.stop()
        single_instance.release()


if __name__ == "__main__":
    main()

