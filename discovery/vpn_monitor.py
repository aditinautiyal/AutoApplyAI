"""
discovery/vpn_monitor.py
Monitors Google search availability and shows a popup reminder
when rate limiting is detected (time to switch VPN location).

Runs as a background thread. Shows a Windows notification + logs
to the dashboard activity log via the signals system.
"""

import asyncio
import threading
import time
import random
import httpx


# ─── Simple test query ────────────────────────────────────────────────────────

TEST_QUERIES = [
    "software engineer intern site:greenhouse.io",
    "machine learning intern 2025",
    "AI intern site:lever.co",
]

CHECK_INTERVAL = 15 * 60   # Check every 15 minutes
CONSECUTIVE_FAILS_BEFORE_ALERT = 2   # Alert after 2 failed checks in a row


def _check_google_available() -> tuple[bool, str]:
    """
    Returns (is_available, reason).
    Tests a simple Google search and checks for rate-limit signals.
    """
    query = random.choice(TEST_QUERIES)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"
        ),
    }

    try:
        resp = httpx.get(
            "https://www.google.com/search",
            params={"q": query, "num": 3},
            headers=headers,
            timeout=10,
            follow_redirects=True,
        )

        # 429 = explicit rate limit
        if resp.status_code == 429:
            return False, "429 rate limit"

        # CAPTCHA page
        if "sorry/index" in resp.url.path or "captcha" in resp.text.lower():
            return False, "CAPTCHA triggered"

        # Unusual traffic page
        if "unusual traffic" in resp.text.lower():
            return False, "unusual traffic detected"

        # Got a real result
        if resp.status_code == 200 and "<h3" in resp.text:
            return True, "OK"

        # Empty result (rate limited but no explicit signal)
        if resp.status_code == 200 and "<h3" not in resp.text:
            return False, "empty results (likely blocked)"

        return False, f"status {resp.status_code}"

    except Exception as e:
        return False, f"request failed: {e}"


def _show_windows_notification(title: str, message: str):
    """Show a Windows toast notification."""
    try:
        from win10toast import ToastNotifier
        toaster = ToastNotifier()
        toaster.show_toast(title, message, duration=10, threaded=True)
        return
    except ImportError:
        pass

    # Fallback: Windows balloon tip via ctypes (no extra packages needed)
    try:
        import ctypes
        # Just flash the taskbar — simple and always works
        ctypes.windll.user32.FlashWindowEx  # noqa
    except Exception:
        pass


def _show_qt_popup(message: str):
    """Show a popup in the running PyQt6 app."""
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
        from PyQt6.QtCore import Qt
        app = QApplication.instance()
        if app:
            msg = QMessageBox()
            msg.setWindowTitle("⚠️ VPN Switch Needed")
            msg.setText(message)
            msg.setIcon(QMessageBox.Icon.Warning)
            msg.setWindowFlags(
                msg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint
            )
            msg.exec()
    except Exception:
        pass


def _log_to_dashboard(message: str):
    """Append message to the dashboard activity log via signals."""
    try:
        from main import signals
        signals.log_message.emit(f"⚠️ VPN: {message}")
    except Exception:
        pass


class VPNMonitor:
    """
    Background thread that checks Google availability every 15 minutes.
    Shows a popup + dashboard log entry when rate limiting is detected.
    """

    def __init__(self):
        self._thread = None
        self._stop_event = threading.Event()
        self.running = False
        self._consecutive_fails = 0
        self._alerted = False   # Don't spam — one alert per block event

    def start(self):
        if self.running:
            return
        self._stop_event.clear()
        self.running = True
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="VPNMonitor"
        )
        self._thread.start()
        print("[VPNMonitor] Started — checking Google every 15 min")

    def stop(self):
        self._stop_event.set()
        self.running = False

    def _loop(self):
        # Wait 2 minutes after startup before first check
        # (let everything else initialize first)
        for _ in range(12):
            if self._stop_event.is_set():
                return
            time.sleep(10)

        while not self._stop_event.is_set():
            self._check_once()

            # Sleep in 30s chunks so stop_event is responsive
            for _ in range(CHECK_INTERVAL // 30):
                if self._stop_event.is_set():
                    break
                time.sleep(30)

    def _check_once(self):
        available, reason = _check_google_available()

        if available:
            if self._consecutive_fails > 0:
                print(f"[VPNMonitor] ✅ Google available again")
                _log_to_dashboard("Google available — discovery resuming normally")
            self._consecutive_fails = 0
            self._alerted = False
            return

        # Google not available
        self._consecutive_fails += 1
        print(f"[VPNMonitor] ❌ Google blocked ({reason}) — "
              f"{self._consecutive_fails} consecutive fail(s)")

        if self._consecutive_fails >= CONSECUTIVE_FAILS_BEFORE_ALERT and not self._alerted:
            self._alerted = True
            message = (
                f"Google is rate-limiting your IP ({reason}).\n\n"
                f"Switch to a different VPN server location and Google\n"
                f"ATS search will resume automatically next cycle.\n\n"
                f"(API discovery — The Muse, Remotive — is still running fine.)"
            )

            # Log to dashboard
            _log_to_dashboard(
                f"Google blocked ({reason}) — switch VPN location to resume Google ATS search"
            )

            # Show Windows notification
            _show_windows_notification(
                "AutoApplyAI — Switch VPN Location",
                f"Google blocked ({reason}). Switch VPN server to resume."
            )

            # Show popup in app
            _show_qt_popup(message)


# Singleton
_monitor_instance = None


def get_vpn_monitor() -> VPNMonitor:
    global _monitor_instance
    if _monitor_instance is None:
        _monitor_instance = VPNMonitor()
    return _monitor_instance
