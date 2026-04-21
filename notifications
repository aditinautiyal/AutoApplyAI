"""
notifications/inbox_monitor_loop.py
Runs inbox monitoring in a background thread.
Checks Gmail every 15 minutes for employer responses.
Categorizes and logs them. Creates notifications for interviews/info requests.
"""

import threading
import time
from typing import Optional
from core.settings_store import get_store


class InboxMonitorLoop:
    """Periodic Gmail inbox check running in background thread."""

    CHECK_INTERVAL = 15 * 60  # 15 minutes

    def __init__(self, on_new_response=None):
        self.store = get_store()
        self.on_new_response = on_new_response or (lambda r: None)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.running = False
        self.last_check = None
        self.response_count = 0

    def start(self):
        """Start monitoring in background thread."""
        gmail_token = self.store.get("gmail_token")
        if not gmail_token:
            print("[InboxMonitor] Gmail not connected — skipping inbox monitoring")
            return

        self._stop_event.clear()
        self.running = True
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="InboxMonitor"
        )
        self._thread.start()
        print("[InboxMonitor] Started — checking every 15 minutes")

    def stop(self):
        self._stop_event.set()
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                self._check_once()
            except Exception as e:
                print(f"[InboxMonitor] Error: {e}")

            # Sleep in 30-second chunks so stop_event is responsive
            for _ in range(self.CHECK_INTERVAL // 30):
                if self._stop_event.is_set():
                    break
                time.sleep(30)

    def _check_once(self):
        from email_handler.gmail_sender import InboxMonitor
        monitor = InboxMonitor()
        monitor.run_check()
        self.last_check = time.strftime("%H:%M:%S")
        print(f"[InboxMonitor] Check complete at {self.last_check}")

    def get_status(self) -> dict:
        return {
            "running": self.running,
            "last_check": self.last_check,
        }
