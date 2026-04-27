"""
slow_lane/slow_lane_manager.py
Manages both LinkedIn and Indeed slow lanes.
Each runs in its own async loop in a background thread.
Can be toggled on/off independently via settings.
"""

import asyncio
import json
import threading
from typing import Callable, Optional
from core.settings_store import get_store


class SlowLaneManager:
    """
    Runs LinkedIn + Indeed Easy Apply in background threads.
    Human-paced, logged in, completely separate from fast tracks.
    """

    def __init__(self, status_callback: Optional[Callable] = None):
        self.store = get_store()
        self.status_cb = status_callback or (lambda lane, s, m: None)
        self._stop_event = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self.running = False
        self.lane_statuses: dict[str, dict] = {}

    def start(self):
        """Start enabled slow lanes."""
        if self.running:
            return

        slow_platforms = self.store.get("slow_platforms", [])
        if isinstance(slow_platforms, str):
            try:
                slow_platforms = json.loads(slow_platforms)
            except Exception:
                slow_platforms = []

        self._stop_event.clear()
        self.running = True

        if "LinkedIn Easy Apply" in slow_platforms:
            self._start_lane("linkedin")

        if "Indeed Easy Apply" in slow_platforms:
            self._start_lane("indeed")

        print(f"[SlowLane] Started: {list(self._threads.keys())}")

    def stop(self):
        """Stop all slow lanes."""
        self._stop_event.set()
        self.running = False
        for name, thread in self._threads.items():
            thread.join(timeout=5)
        self._threads.clear()
        print("[SlowLane] All lanes stopped")

    def toggle_lane(self, lane: str, enable: bool):
        """Enable or disable a specific lane at runtime."""
        if enable and lane not in self._threads:
            self._start_lane(lane)
        elif not enable and lane in self._threads:
            # Can't stop just one lane without its own event — mark as disabled
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
