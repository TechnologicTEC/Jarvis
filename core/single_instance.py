"""One Jarvis at a time.

Autostart plus a desktop shortcut is an easy way to end up with two copies
running: the second one fights the first for the microphone, the global hotkey
and the tray icon. A named Windows mutex is the cheapest reliable check — it is
owned by the process and released automatically if that process dies, so a
crash can't leave a stale lock behind (unlike a pid file).

When a second copy starts it asks the first to show itself and then exits, so
double-clicking the shortcut behaves like clicking a taskbar button.
"""
import ctypes
import ctypes.wintypes as wt

MUTEX_NAME = "Global\\JarvisAssistant_SingleInstance"
WINDOW_TITLE = "Jarvis"

ERROR_ALREADY_EXISTS = 183
_handle = None


def acquire() -> bool:
    """True if we're the only instance. False means another one already runs."""
    global _handle
    try:
        kernel32 = ctypes.windll.kernel32
        _handle = kernel32.CreateMutexW(None, wt.BOOL(True), MUTEX_NAME)
        return kernel32.GetLastError() != ERROR_ALREADY_EXISTS
    except Exception:
        return True  # never block startup over this


def release():
    global _handle
    if _handle:
        try:
            ctypes.windll.kernel32.ReleaseMutex(_handle)
            ctypes.windll.kernel32.CloseHandle(_handle)
        except Exception:
            pass
        _handle = None


def summon_existing() -> bool:
    """Bring the already-running Jarvis to the front."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, 9)      # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False
