"""Stopping the things Jarvis leans on, when Jarvis itself is closing.

Jarvis is the reason Ollama and Everything are running for most of a session,
so closing Jarvis and leaving them in the background just moves the chore. The
X button ends the lot.

Everything gets asked politely first: it holds a file index in memory and
writes it out on a clean exit, so killing it outright makes the next start slow
while it rebuilds. Ollama has nothing to save, but the same courtesy costs
nothing.

Turn any of it off with ui.quit_stops (a list) or by emptying it.
"""
import os
import subprocess
import time

from core import config

# Under pythonw there is no console; without this flag every taskkill would
# flash one on screen.
_NO_WINDOW = 0x08000000

# Logical name -> the image names that make up that app. Ollama runs two: the
# tray app and the server it spawns, and stopping only the first leaves the
# model resident in memory.
_APPS = {
    "ollama": ("ollama app.exe", "ollama.exe"),
    "everything": ("Everything.exe",),
}


def _run(args, timeout=8) -> bool:
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout,
                           creationflags=_NO_WINDOW)
        return p.returncode == 0
    except Exception:
        return False


def is_running(image: str) -> bool:
    try:
        p = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
                           capture_output=True, text=True, timeout=8,
                           creationflags=_NO_WINDOW)
        return image.lower() in (p.stdout or "").lower()
    except Exception:
        return False


def service_for(image: str) -> str:
    """The Windows service running from this image, if it is one.

    Everything is normally installed as a LocalSystem service in session 0, not
    as an app in yours. taskkill cannot touch it however hard it tries, and
    neither can Everything's own -exit: Windows blocks messages from a normal
    process to a higher-integrity one. Knowing it is a service is the
    difference between "needs admin" and a silent failure.
    """
    # tasklist /svc, not a PowerShell CIM query: same answer in 0.3s rather
    # than 5s, and this runs while the user is waiting for the app to exit.
    try:
        p = subprocess.run(["tasklist", "/svc", "/FI", f"IMAGENAME eq {image}", "/NH"],
                           capture_output=True, text=True, timeout=8,
                           creationflags=_NO_WINDOW)
        for line in (p.stdout or "").splitlines():
            if image.lower() not in line.lower():
                continue
            # "Everything.exe   3496   Everything"  ->  the trailing column
            parts = line.split()
            if len(parts) >= 3 and parts[-1] not in ("N/A", "N/D"):
                return parts[-1]
        return ""
    except Exception:
        return ""


def stop_image(image: str) -> str:
    """Ask, wait, then insist. Returns what happened, for the log."""
    if not is_running(image):
        return "not running"

    # Services first. A LocalSystem service ignores taskkill from an
    # unelevated process no matter how many times it is asked, and the retry
    # loop below was spending four seconds proving that on every quit.
    svc = service_for(image)
    if svc:
        if _run(["net", "stop", svc], timeout=10) and not is_running(image):
            return f"service {svc} stopped"
        return f"running as service '{svc}' — needs admin to stop"

    # No /F first: a close request lets an app save its state on the way out.
    _run(["taskkill", "/IM", image])
    for _ in range(5):
        time.sleep(0.25)
        if not is_running(image):
            return "closed"
    _run(["taskkill", "/IM", image, "/F"])
    time.sleep(0.3)
    return "killed" if not is_running(image) else "would not stop"


def apps_to_stop() -> list:
    names = config.get("ui", "quit_stops", default=["ollama", "everything"])
    if isinstance(names, str):
        names = [names]
    return [str(n).strip().lower() for n in (names or []) if str(n).strip()]


def stop_dependencies() -> dict:
    """Stop everything configured, and report per app. Never raises: this runs
    while the window is closing, and a failure here must not block the exit."""
    out = {}
    for name in apps_to_stop():
        for image in _APPS.get(name, ()):
            try:
                out[image] = stop_image(image)
            except Exception as e:
                out[image] = f"error: {type(e).__name__}"
    return out
