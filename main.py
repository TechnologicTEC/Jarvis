"""Jarvis tray host — owns the hotkey listener, the tray icon, and both windows.

Run:  python main.py           (opens the full app + tray)
      python main.py --tray    (starts silent in the tray; used by autostart)
"""
import argparse
import os
import threading
import time

import pystray
import webview
from PIL import Image, ImageDraw

from api import JarvisApi
from core import config, windows
from core.hotkey_listener import HotkeyListener

TRAY = None
HOTKEY = None


def _tray_image():
    """Use the real app icon so the tray matches the shortcuts."""
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
    menu = pystray.Menu(
        pystray.MenuItem("Open Jarvis", lambda i, it: windows.show("full"), default=True),
        pystray.MenuItem("Pin to corner", lambda i, it: windows.show("compact")),
        pystray.MenuItem("Hide", lambda i, it: windows.hide()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", _quit),
    )
    return pystray.Icon("jarvis", _tray_image(), "Jarvis", menu)


def _after_start(show_full_now: bool):
    """Runs once the webview event loop is up."""
    global TRAY, HOTKEY
    HOTKEY = HotkeyListener(
        windows.on_double_esc,
        window_ms=config.get("hotkey", "double_esc_ms", default=400),
    )
    windows.HOTKEY_OK = HOTKEY.start()
    if not windows.HOTKEY_OK:
        print("[jarvis] global hotkey unavailable (keyboard hook failed) — use the tray menu")
    TRAY = _build_tray()
    threading.Thread(target=TRAY.run, daemon=True).start()

    # Load Whisper now, in the background, so the first Alt-to-talk doesn't
    # pay the ~5s model load. Costs ~300MB resident for the whole session —
    # set voice.preload_model false to trade that back for a slower first use.
    if (config.get("voice", "enabled", default=True)
            and config.get("voice", "preload_model", default=True)):
        try:
            from skills import voice
            voice.warm()
        except Exception:
            pass

    _start_inbox_poller()

    if show_full_now:
        windows.show_full()


def _start_inbox_poller():
    """Background Gmail poll from the tray host. Detection only — it surfaces
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
                    print(f"[jarvis] inbox: {len(hits)} reply(ies) detected — "
                          f"{hits[0]['company']} {hits[0]['outcome']}")
            except Exception as e:
                print(f"[jarvis] inbox poll failed: {str(e)[:120]}")

    threading.Thread(target=loop, daemon=True).start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tray", action="store_true", help="start hidden in the tray")
    args = ap.parse_args()

    show_full_now = (not args.tray) and config.get("ui", "open_full_on_start", default=True)

    api = JarvisApi()
    windows.create_windows(api)
    try:
        webview.start(_after_start, show_full_now)
    finally:
        if HOTKEY is not None:
            HOTKEY.stop()
        if TRAY is not None:
            TRAY.stop()


if __name__ == "__main__":
    main()
