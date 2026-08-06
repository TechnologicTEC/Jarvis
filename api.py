"""The pywebview js_api bridge — one instance shared by BOTH windows.

The HTML calls e.g. `pywebview.api.route(text)` and gets a structured dict
back. No logic lives here; it delegates to the router/actions/skills.
"""
import threading
import time

from core import actions, router, windows


class JarvisApi:
    def __init__(self):
        # Voice runs on a worker thread so the window stays responsive and can
        # animate the timer/level while Whisper records and transcribes.
        self._v_lock = threading.Lock()
        self._v = {"state": "idle", "seconds": 0.0, "level": 0.0,
                   "transcript": "", "reply": "", "error": "", "ack": ""}

    # ---- the one entry point both windows funnel text through ----
    def route(self, text):
        try:
            return router.route(text or "")
        except Exception as e:
            return {"ok": False, "intent": "error", "reply": f"Error — {e}"}

    # ---- setups ----
    def launch_setup(self, name):
        try:
            return actions.launch(name)
        except Exception as e:
            return {"ok": False, "reply": f"Error — {e}"}

    def get_setups(self):
        return {"ok": True, "order": actions.names()}

    def get_setups_detail(self):
        """Each setup plus a readable summary of what it launches — Setups tab."""
        try:
            return {"ok": True, "setups": actions.detail()}
        except Exception as e:
            return {"ok": False, "setups": [], "reply": str(e)}

    def infer_setup(self, description):
        """'steam and discord' -> concrete launch actions, for the + New flow."""
        try:
            return actions.infer_items(description or "")
        except Exception as e:
            return {"ok": False, "items": [], "unresolved": [], "reply": str(e)}

    def save_setup(self, name, items):
        try:
            return actions.save_setup(name, items or [])
        except Exception as e:
            return {"ok": False, "reply": f"Could not save — {e}"}

    def delete_setup(self, name):
        try:
            return actions.delete_setup(name)
        except Exception as e:
            return {"ok": False, "reply": f"Could not delete — {e}"}

    def get_setup(self, name):
        return actions.get_setup(name)

    def list_installed_apps(self, term=""):
        """Installed apps, optionally filtered — powers the app picker."""
        try:
            apps = actions.installed_apps()
            t = (term or "").strip().lower()
            if t:
                apps = [a for a in apps if t in a["name"].lower()]
            return {"ok": True, "apps": apps[:40], "total": len(apps)}
        except Exception as e:
            return {"ok": False, "apps": [], "reply": str(e)}

    def open_setups_file(self):
        try:
            actions.open_config()
            return {"ok": True, "reply": "Opening app_setups.json"}
        except Exception as e:
            return {"ok": False, "reply": f"Error — {e}"}

    # ---- window control ----
    def set_layout(self, mode):
        """Switch the single window between 'full' and 'compact'."""
        return {"ok": True, "mode": windows.set_mode(mode, announce=False)}

    def toggle_layout(self):
        return {"ok": True, "mode": windows.toggle_mode()}

    def set_compact_height(self, h):
        """The compact card reports its rendered height so the window hugs it."""
        return windows.set_compact_height(h)

    def hide_window(self):
        windows.hide()
        return {"ok": True}

    def quit_app(self):
        """The X button: end Jarvis and what it started.

        Runs on a worker thread because destroy() ends the webview loop, and
        doing that from inside a js_api call kills the bridge mid-reply — the
        UI then hangs waiting for a response that can never arrive.
        """
        import threading

        def go():
            time.sleep(0.15)      # let this call return first
            windows.quit_all(stop_deps=True)

        threading.Thread(target=go, daemon=True).start()
        return {"ok": True, "reply": "Closing Jarvis…"}

    def open_full(self):
        windows.show("full")
        return {"ok": True}

    # kept so older call sites keep working
    def hide_full(self):
        windows.hide()
        return {"ok": True}

    def hide_mini(self):
        windows.hide()
        return {"ok": True}

    def get_pinned(self):
        return {"ok": True, "pinned": windows.pinned(),
                "topmost": windows.is_topmost()}

    def set_pinned(self, on):
        state = windows.set_pinned(bool(on))
        return {"ok": True, "pinned": state,
                "reply": "Pinned on top" if state else "Unpinned"}

    def hotkey_active(self):
        return {"ok": True, "active": bool(windows.HOTKEY_OK)}

    # ---- files ----
    def search_files(self, query):
        """Files tab: list results only — opening is the user's click."""
        from skills import file_finder
        return file_finder.search_reply(query or "", reveal_top=False)

    def open_path(self, path):
        from skills import file_finder
        file_finder.reveal(path)
        return {"ok": True}

    def quick_files(self):
        """The pinned shortcuts beside the search bar — real files, not samples."""
        import os
        from core import config
        out = []
        for entry in (config.get("ui", "quick_files", default=[]) or []):
            path = os.path.expandvars(entry.get("path", ""))
            out.append({
                "label": entry.get("label") or os.path.basename(path),
                "path": path, "exists": os.path.isfile(path),
            })
        return {"ok": True, "files": out}

    def open_file(self, path):
        """Open a file in its default application (not Explorer)."""
        import os
        try:
            p = os.path.expandvars(path or "")
            if not os.path.isfile(p):
                return {"ok": False, "reply": f"Not found: {os.path.basename(p)}"}
            os.startfile(p)
            return {"ok": True, "reply": f"Opening {os.path.basename(p)}"}
        except Exception as e:
            return {"ok": False, "reply": f"Could not open — {e}"}

    # ---- stocks ----
    def stocks_summary(self):
        """Never blocks the first time. The header calls this on boot, and a
        cold connect is ~20s against the hosted database — long enough to hold
        up other bridge calls and make the freshly-opened window feel dead."""
        from skills import stocks
        if stocks.is_loading():
            stocks.warm()          # get it going, answer immediately
            return {"ok": False, "loading": True, "reply": "connecting…"}
        try:
            return stocks.summary()
        except Exception as e:
            return {"ok": False, "reply": f"Stocks unavailable — {e}"}

    def stocks_holdings(self):
        from skills import stocks
        try:
            return stocks.holdings()
        except Exception as e:
            return {"ok": False, "reply": f"Stocks unavailable — {e}", "holdings": []}

    # ---- inbox / internship tracker ----
    def inbox_state(self):
        from skills import email_tracker, excel_sync
        try:
            apps = excel_sync.read_applications()
            return {
                "ok": True,
                "gmail": email_tracker.status(),
                "tracker_ok": bool(apps.get("ok")),
                "tracker_error": apps.get("error", ""),
                "applications": apps.get("applications", []),
                "pending": email_tracker.get_pending(),
            }
        except Exception as e:
            return {"ok": False, "reply": str(e), "applications": [], "pending": []}

    def inbox_unseen(self):
        from skills import email_tracker
        try:
            return {"ok": True, "unseen": email_tracker.unseen_count()}
        except Exception:
            return {"ok": True, "unseen": 0}

    def inbox_mark_seen(self):
        from skills import email_tracker
        email_tracker.mark_seen()
        return {"ok": True}

    def inbox_connect(self):
        """One-time OAuth consent. Opens the browser; user-initiated only."""
        from skills import email_tracker
        return email_tracker.authorize()

    def inbox_check(self):
        from skills import email_tracker
        try:
            return email_tracker.check_replies()
        except Exception as e:
            return {"ok": False, "reply": f"Check failed — {e}", "hits": []}

    def inbox_log(self, company=None):
        """Confirm a detected reply into the tracker. Never called automatically."""
        from skills import email_tracker
        hit = None
        if company:
            hit = next((h for h in email_tracker.get_pending()
                        if h.get("company") == company), None)
            if hit is None:
                return {"ok": False, "reply": f"No pending reply for {company}"}
        return email_tracker.log_it(hit)

    # ---- settings ----
    def get_settings(self):
        from core import config
        return {"ok": True, "settings": config.load()}

    def set_setting(self, section, key, value):
        from core import config
        try:
            config.set_value(section, key, value)
            return {"ok": True, "reply": f"Saved {section}.{key}"}
        except Exception as e:
            return {"ok": False, "reply": f"Could not save — {e}"}

    def open_settings_file(self):
        import os
        from core import config
        try:
            os.startfile(config.SETTINGS_PATH)
            return {"ok": True, "reply": "Opening settings.json"}
        except Exception as e:
            return {"ok": False, "reply": f"Error — {e}"}

    def files_index_count(self):
        from skills import file_finder
        return {"ok": True, "count": file_finder.index_count()}

    # ---- voice ----
    def _vset(self, **kw):
        with self._v_lock:
            self._v.update(kw)

    def voice_start(self, captured=None):
        """Begin one listen->transcribe->route cycle on a worker thread.

        `captured` is audio the wake listener already recorded on its own
        stream; when present we skip straight to transcription instead of
        opening the microphone again.
        """
        from skills import voice
        with self._v_lock:
            if self._v["state"] in ("recording", "transcribing"):
                return {"ok": False, "reply": "Already listening"}
            self._v = {"state": "transcribing" if captured is not None else "recording",
                       "seconds": 0.0, "level": 0.0,
                       "transcript": "", "reply": "", "error": "", "ack": ""}

        def work():
            started = time.time()
            try:
                def level(rms):
                    self._vset(level=min(1.0, rms * 18), seconds=time.time() - started)

                if captured is not None:
                    audio = captured        # already recorded on the wake stream
                else:
                    try:
                        from skills import wake
                        wake.pause()
                    except Exception:
                        pass
                    audio = voice.record(on_level=level)
                self._vset(state="transcribing", seconds=time.time() - started)
                text = voice.transcribe(audio)
                if not text:
                    self._vset(state="done", transcript="",
                               reply="Didn't catch that — try again")
                    return
                # Distinct states so the UI can stop saying "listening" the
                # moment you've actually stopped talking.
                ack = _pick_ack(text)
                self._vset(transcript=text, state="thinking", ack=ack)
                # Answer straight away that it heard you. Routing can take
                # several seconds (a web question is ~8s), and silence in that
                # gap reads as "it didn't hear me". Spoken on its own thread so
                # it doesn't add to the wait; the real reply cuts it off.
                if ack and _speak_enabled() and _ack_enabled():
                    threading.Thread(target=voice.speak, args=(ack,),
                                     daemon=True).start()
                result = router.route(text) or {}
                reply = result.get("reply", "")
                speaking = bool(reply) and not result.get("silent") and _speak_enabled()
                self._vset(state="speaking" if speaking else "done", reply=reply,
                           nav=result.get("tab", ""), silent=bool(result.get("silent")))
                if speaking:
                    voice.speak(reply)
                    self._vset(state="done")
            except Exception as e:
                self._vset(state="error", error=str(e)[:200],
                           reply=f"Voice failed — {str(e).splitlines()[0][:110]}")
            finally:
                try:
                    from skills import wake
                    if wake.is_enabled():
                        wake.resume()
                except Exception:
                    pass

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def voice_poll(self):
        with self._v_lock:
            return dict(self._v, ok=True)

    def voice_stop(self):
        from skills import voice
        voice.stop()
        return {"ok": True}

    def voice_status(self):
        from skills import voice, wake
        return dict(voice.status(), ok=True, wake=wake.status())

    def set_wake(self, enabled):
        """Turn the always-on 'Hey Jarvis' listener on or off, live."""
        from core import config
        from skills import wake
        config.set_value("voice", "wake_word", bool(enabled))
        if enabled:
            ok = wake.start(self._on_wake)
            return {"ok": ok, "reply": "Listening for “Hey Jarvis”" if ok
                    else "Wake word unavailable — check the microphone"}
        wake.stop()
        return {"ok": True, "reply": "Wake word off"}

    def _on_wake(self, captured=None):
        """Wake word heard. The listener has already recorded the question on
        its own stream, so this just surfaces the window and processes it.

        Silencing first is what makes "Hey Jarvis, stop" work mid-sentence —
        and stops Jarvis transcribing its own voice.
        """
        try:
            from skills import voice
            voice.stop_speaking()
            if not windows.is_visible():
                windows.show("compact")
            self.voice_start(captured=captured)
        except Exception:
            pass

    def speak(self, text):
        from skills import voice
        return voice.speak(text or "")

    # ---- internship hunting ----
    def jobs_list(self, refresh=False):
        from skills import jobs
        try:
            if not refresh and not jobs.is_ready():
                jobs.warm()                     # answer now, populate behind
                return {"ok": True, "loading": True, "items": [], "skipped": [],
                        "reply": "searching job boards…"}
            return jobs.search(force=bool(refresh))
        except Exception as e:
            return {"ok": False, "items": [], "skipped": [],
                    "reply": f"Job search failed — {str(e).splitlines()[0][:110]}"}

    def jobs_seen(self, url, role=""):
        from skills import jobs
        return jobs.mark_seen(url, role or "")

    def jobs_unsee(self, url=None):
        from skills import jobs
        return jobs.unmark_seen(url)

    def open_url(self, url):
        import webbrowser
        try:
            if str(url).startswith(("http://", "https://")):
                webbrowser.open(url)
                return {"ok": True}
        except Exception:
            pass
        return {"ok": False}

    # ---- status strip in the full app header ----
    def status(self):
        """Cheap by design — the header polls this every minute, and nothing
        here may block on a network connection or a model load. `stocks` used
        to connect to the hosted database from here (~20s), which held up the
        whole bridge at startup: the window appeared and then sat unresponsive."""
        from core import llm_local
        from skills import file_finder, stocks
        return {
            "ok": True,
            "ollama": llm_local.is_available(),
            "everything": file_finder.is_available(),
            "stocks": stocks.is_available(block=False),
            "stocks_loading": stocks.is_loading(),
            "gmail_connected": _gmail_connected(),
        }


def _gmail_connected() -> bool:
    try:
        from skills import email_tracker
        return email_tracker.is_authorized()
    except Exception:
        return False


def _speak_enabled() -> bool:
    from core import config
    return bool(config.get("voice", "speak_replies", default=True))


def _ack_enabled() -> bool:
    from core import config
    return bool(config.get("voice", "acknowledge", default=True))


# Rotated so it doesn't sound like a recording. Kept short: this is spoken
# while the real answer is still being worked out.
_ACKS = ("I'm on that now.", "On it.", "Let me check.",
         "Looking that up now.", "Give me a second.", "Checking that.")
_ACK_FAST = ("Sure.", "One moment.")
_ack_n = [0]


def _pick_ack(text: str) -> str:
    """A short acknowledgement, biased short when the answer will be quick."""
    import re as _re
    _ack_n[0] += 1
    lc = (text or "").lower()
    # portfolio/file/setup answers come back almost instantly — a long
    # "let me look that up" would still be talking when the answer lands
    quick = bool(_re.search(r"\b(stock|portfolio|holding|cash|wallet|"
                            r"setup|open|launch|stop)\w*\b", lc))
    pool = _ACK_FAST if quick else _ACKS
    return pool[_ack_n[0] % len(pool)]
