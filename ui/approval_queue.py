"""
ui/approval_queue.py
Thread-safe bridge between background track workers and the main Qt thread.
Background thread puts a request in the queue and blocks on threading.Event.
Main Qt thread (via QTimer) picks it up, shows the dialog, sets the event.
"""

import queue
import threading

_request_queue = queue.Queue()


def request_approval(job_data: dict, insight: dict, cover_letter: str) -> tuple[str, str]:
    """
    Called from background async thread. BLOCKS until user responds.
    Returns (action, cover_letter). action = 'approve' | 'skip' | 'stop'
    """
    done = threading.Event()
    result = {"action": "skip", "cover_letter": cover_letter}
    _request_queue.put((job_data, insight, cover_letter, done, result))
    done.wait()  # Block background thread until dialog closes
    return result["action"], result["cover_letter"]


def get_pending_request():
    """
    Called from main Qt thread via QTimer.
    Returns (job_data, insight, cover_letter, done_event, result) or None.
    """
    try:
        return _request_queue.get_nowait()
    except queue.Empty:
        return None
