"""
tracks/track_manager.py
Manages N parallel application tracks.
Each track is fully isolated. Track count adjustable at runtime.
"""

import asyncio
import threading
from typing import Callable, Optional
from tracks.track_worker import TrackWorker
from core.settings_store import get_store


class TrackManager:
    """
    Runs N TrackWorker instances concurrently.
    Tracks can be added/removed at runtime.
    """

    def __init__(self, status_callback: Optional[Callable] = None):
        self.store = get_store()
        self.status_cb = status_callback or (lambda t, s, m: None)
        self._stop_event = threading.Event()
        self._workers: dict[int, TrackWorker] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self.running = False

        # Track statuses for UI
        self.track_statuses: dict[int, dict] = {}

    def start(self, num_tracks: Optional[int] = None):
        """Start the track manager in a background thread."""
        if self.running:
            return
        if num_tracks is None:
            num_tracks = self.store.get("track_count", 2)

        self._stop_event.clear()
        self.running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(num_tracks,),
            daemon=True,
            name="TrackManager"
        )
        self._thread.start()
        print(f"[TrackManager] Started with {num_tracks} tracks")

    def stop(self):
        """Gracefully stop all tracks."""
        self._stop_event.set()
        self.running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._cancel_all)
        if self._thread:
            self._thread.join(timeout=10)
        print("[TrackManager] Stopped")

    def set_track_count(self, count: int):
        """Adjust number of running tracks at runtime."""
        self.store.set("track_count", count)
        if self.running:
            current = len(self._workers)
            if count > current:
                for i in range(current + 1, count + 1):
                    self._start_track(i)
            elif count < current:
                for i in range(count + 1, current + 1):
                    self._stop_track(i)

    def get_status(self) -> dict:
        """Return current status of all tracks."""
        return {
            "running": self.running,
            "track_count": len(self._workers),
            "tracks": self.track_statuses.copy(),
        }

    def _run_loop(self, num_tracks: int):
        """Run async event loop in background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_main(num_tracks))
        finally:
            self._loop.close()

    async def _async_main(self, num_tracks: int):
        """Start N tracks and keep them running."""
        tasks = []
        for i in range(1, num_tracks + 1):
            worker = TrackWorker(
                track_id=i,
                stop_event=self._stop_event,
                status_callback=self._on_track_status,
            )
            self._workers[i] = worker
            task = asyncio.create_task(worker.run(), name=f"Track-{i}")
            self._tasks[i] = task
            tasks.append(task)

        await asyncio.gather(*tasks, return_exceptions=True)

    def _on_track_status(self, track_id: int, status: str, message: str):
        """Receive status update from a track worker."""
        self.track_statuses[track_id] = {
            "status": status,
            "message": message,
        }
        self.status_cb(track_id, status, message)

    def _start_track(self, track_id: int):
        """Add a new track at runtime."""
        if not self._loop:
            return
        worker = TrackWorker(
            track_id=track_id,
            stop_event=self._stop_event,
            status_callback=self._on_track_status,
        )
        self._workers[track_id] = worker
        future = asyncio.run_coroutine_threadsafe(worker.run(), self._loop)
        print(f"[TrackManager] Added Track {track_id}")

    def _stop_track(self, track_id: int):
        """Remove a track at runtime."""
        if track_id in self._tasks:
            self._loop.call_soon_threadsafe(self._tasks[track_id].cancel)
            del self._tasks[track_id]
            del self._workers[track_id]
            print(f"[TrackManager] Removed Track {track_id}")

    def _cancel_all(self):
        for task in self._tasks.values():
            task.cancel()
