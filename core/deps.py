"""The lifecycle of the things Jarvis leans on: started with it, stopped with it.

Jarvis is the reason Ollama is running for most of a session, so closing Jarvis
and leaving it in the background just moves the chore — the X button ends it.
The other half of that bargain is starting it again, otherwise quitting Jarvis
once leaves the local model unavailable for the rest of the session.

Processes get asked politely before being killed: a close request lets an app
save its state on the way out.

  ui.quit_stops     what the X button closes    (default ollama, everything)
  llm.autostart     start Ollama with Jarvis    (default true)
"""
import shutil
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


def ollama_running() -> bool:
    return is_running("ollama.exe") or is_running("ollama app.exe")


def start_ollama(wait: float = 12.0) -> str:
    """Start Ollama if it isn't up, and wait until it answers.

    Launches the tray app rather than `ollama serve`: it is what the Startup
    shortcut runs, it spawns the server itself, and it leaves the same tray
    icon behind, so a session Jarvis started looks like one Windows started.

    Returns a short description of what happened, for the log.
    """
    if ollama_running():
        return "already running"

    exe = shutil.which("ollama app.exe") or shutil.which("ollama")
    if not exe:
        return "not installed"
    try:
        # DETACHED_PROCESS: Ollama must outlive this call, and under pythonw
        # an inherited handle would otherwise keep a console-less child tied
        # to us. Nothing here reads its output.
        subprocess.Popen([exe], creationflags=_NO_WINDOW | 0x00000008,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
    except Exception as e:
        return f"failed to launch — {type(e).__name__}"

    # Running is not the same as ready: the API takes a moment to bind.
    url = config.get("llm", "ollama_url", default="http://127.0.0.1:11434")
    end = time.time() + wait
    while time.time() < end:
        time.sleep(0.5)
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=2):
                return "started"
        except Exception:
            continue
    return "started, not answering yet" if ollama_running() else "did not start"


def ensure_started() -> str:
    """Bring up what Jarvis needs, if the user wants that. Never raises."""
    if not config.get("llm", "autostart", default=True):
        return "autostart off"
    try:
        return start_ollama()
    except Exception as e:
        return f"error — {type(e).__name__}"


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
