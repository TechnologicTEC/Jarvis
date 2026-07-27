"""The pywebview js_api bridge — one instance shared by BOTH windows.

The HTML calls e.g. `pywebview.api.route(text)` and gets a structured dict
back. No logic lives here; it delegates to the router/actions/skills.
"""
from core import actions, router, windows


class JarvisApi:
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

    def open_setups_file(self):
        try:
            actions.open_config()
            return {"ok": True, "reply": "Opening app_setups.json"}
        except Exception as e:
            return {"ok": False, "reply": f"Error — {e}"}

    # ---- window control ----
    def open_full(self):
        windows.show_full()
        return {"ok": True}

    def hide_full(self):
        windows.hide_full()
        return {"ok": True}

    def hide_mini(self):
        windows.hide_mini()
        return {"ok": True}

    def hotkey_active(self):
        """False means the UI must offer its own way to dismiss the mini."""
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

    # ---- stocks ----
    def stocks_summary(self):
        from skills import stocks
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

    # ---- status strip in the full app header ----
    def status(self):
        from core import llm_local
        from skills import file_finder, stocks
        return {
            "ok": True,
            "ollama": llm_local.is_available(),
            "everything": file_finder.is_available(),
            "stocks": stocks.is_available(),
            "gmail_connected": False,
        }
