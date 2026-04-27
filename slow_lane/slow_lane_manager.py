"""
slow_lane/slow_lane_manager.py
Manages both LinkedIn and Indeed slow lanes.
Each runs in its own async loop in a background thread.
Only starts lanes that are explicitly enabled in settings.
"""

import asyncio
import json
import threading
from typing import Callable, Optional
from core.settings_store import get_store


class SlowLaneManager:
    """
    Runs LinkedIn + Indeed Easy Apply in background threads.
    Only starts lanes that are checked in Settings → Automation.
    """

    def __init__(self, status_callback: Optional[Callable] = None):
        self.store = get_store()
        self.status_cb = status_callback or (lambda lane, s, m: None)
        self._stop_event = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self.running = False
        self.lane_statuses: dict[str, dict] = {}

    def _get_enabled_platforms(self) -> list[str]:
        """Read slow_platforms from DB and return as list."""
        slow_platforms = self.store.get("slow_platforms", [])
        if isinstance(slow_platforms, str):
            try:
                slow_platforms = json.loads(slow_platforms)
            except Exception:
                slow_platforms = []
        if not isinstance(slow_platforms, list):
            return []
        return slow_platforms

    def start(self):
        """Start only the slow lanes that are enabled in settings."""
        if self.running:
            return

        enabled = self._get_enabled_platforms()

        # If neither is enabled, don't start anything — no threads, no errors
        if not enabled:
            print("[SlowLane] No slow lanes enabled — skipping")
            self.running = True  # Mark running so stop() works cleanly
            return

        self._stop_event.clear()
        self.running = True

        if "LinkedIn Easy Apply" in enabled:
            self._start_lane("linkedin")
        else:
            print("[SlowLane] LinkedIn Easy Apply disabled — not starting")

        if "Indeed Easy Apply" in enabled:
            self._start_lane("indeed")
        else:
            print("[SlowLane] Indeed Easy Apply disabled — not starting")

        if self._threads:
            print(f"[SlowLane] Started: {list(self._threads.keys())}")
        else:
            print("[SlowLane] No lanes started (all disabled)")

    def stop(self):
        """Stop all slow lanes."""
        self._stop_event.set()
        self.running = False
        for name, thread in self._threads.items():
            thread.join(timeout=5)
        self._threads.clear()
        self.lane_statuses.clear()
        print("[SlowLane] All lanes stopped")

    def toggle_lane(self, lane: str, enable: bool):
        """Enable or disable a specific lane at runtime."""
        if enable and lane not in self._threads:
            self._start_lane(lane)
        elif not enable and lane in self._threads:
            self.store.set(f"slow_lane_{lane}_enabled", False)

    def get_status(self) -> dict:
        return {
            "running": self.running,
            "lanes": list(self._threads.keys()),
            "statuses": self.lane_statuses.copy(),
        }

    def _start_lane(self, lane: str):
        def _run_linkedin():
            from slow_lane.linkedin_easy_apply import LinkedInSlowLane
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            worker = LinkedInSlowLane(
                stop_event=self._stop_event,
                status_callback=lambda s, m: self._on_status("linkedin", s, m),
            )
            try:
                loop.run_until_complete(worker.run())
            finally:
                loop.close()

        def _run_indeed():
            from slow_lane.indeed_easy_apply import IndeedSlowLane
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            worker = IndeedSlowLane(
                stop_event=self._stop_event,
                status_callback=lambda s, m: self._on_status("indeed", s, m),
            )
            try:
                loop.run_until_complete(worker.run())
            finally:
                loop.close()

        target = _run_linkedin if lane == "linkedin" else _run_indeed
        t = threading.Thread(target=target, daemon=True, name=f"SlowLane-{lane}")
        t.start()
        self._threads[lane] = t
        self._on_status(lane, "starting", f"{lane.title()} slow lane launching...")

    def _on_status(self, lane: str, status: str, message: str):
        self.lane_statuses[lane] = {"status": status, "message": message}
        self.status_cb(lane, status, message)
