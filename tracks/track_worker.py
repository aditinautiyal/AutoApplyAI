"""
tracks/track_worker.py
One isolated application track. Pulls a job from the pool, researches it,
generates content, fills the form, submits. Completely isolated per track.
Uses Playwright with stealth for human-like behavior.
Paused applications step aside without blocking the track.
"""

import asyncio
import random
import time
import threading
from typing import Optional, Callable
from discovery.job_pool import get_pool, Job
from research.company_researcher import research_company
from research.insight_synthesizer import synthesize
from tracks.cover_letter_gen import (
    generate_cover_letter, generate_form_answer, generate_cold_email_body
)
from tracks.humanizer_check import ensure_humanized
from core.settings_store import get_store

# Typing speed simulation — milliseconds per keystroke
MIN_KEYSTROKE_MS = 40
MAX_KEYSTROKE_MS = 160

# Autofill fields — populated fast (like browser autofill)
AUTOFILL_FIELDS = {
    "name", "full_name", "firstname", "lastname", "first_name", "last_name",
    "email", "phone", "telephone", "address", "city", "state", "zip", "zipcode"
}


class TrackWorker:
    """
    One application track. Runs in its own async loop.
    All state is local — no shared mutable state with other tracks.
    """

    def __init__(self, track_id: int, stop_event: threading.Event,
                  status_callback: Optional[Callable] = None):
        self.track_id = track_id
        self.stop_event = stop_event
        self.status_cb = status_callback or (lambda t, s, m: None)
        self.pool = get_pool()
        self.store = get_store()
        self.current_job: Optional[Job] = None
        self.browser = None
        self.context = None
        self.page = None

    def _status(self, status: str, message: str = ""):
        self.status_cb(self.track_id, status, message)

    async def run(self):
        """Main loop — continuously pulls and processes jobs."""
        print(f"[Track {self.track_id}] Starting...")
        await self._init_browser()

        while not self.stop_event.is_set():
            job = self.pool.get_next()

            if not job:
                self._status("idle", "Waiting for jobs...")
                await asyncio.sleep(5)
                continue

            self.current_job = job
            self._status("working", f"{job.title} @ {job.company}")

            try:
                await self._process_job(job)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[Track {self.track_id}] Error on {job.company}: {e}")
                self.pool.mark_done(job.job_id, "failed")
                self._status("error", str(e))
                await asyncio.sleep(3)

        await self._close_browser()
        print(f"[Track {self.track_id}] Stopped.")

    async def _process_job(self, job: Job):
        """Full pipeline for one job application."""
        app_id = None
        try:
            # 1. Log application start
            app_id = self.store.log_application({
                "job_title":   job.title,
                "company_name": job.company,
                "job_url":     job.url,
                "ats_url":     job.ats_url,
                "platform":    job.platform,
                "score":       job.score,
                "track_id":    self.track_id,
                "status":      "researching",
            })

            # 2. Research the company (async, isolated to this track)
            self._status("researching", f"Researching {job.company}...")
            research = await research_company(job.company, job.title, job.description)
            insight = synthesize(research)

            # 3. Generate cover letter + humanize
            self._status("writing", f"Writing cover letter for {job.company}...")
            cover_letter = generate_cover_letter(
                job.title, job.company, job.description, insight
            )
            cover_letter, ai_score, attempts = ensure_humanized(
                cover_letter, job.company, job.title
            )

            self.store.update_application(app_id, {
                "cover_letter": cover_letter,
                "status": "applying",
            })

            # 4. Manual approval gate — if review mode is ON, show dialog
            review_mode = self.store.get("review_mode", True)  # Default ON for safety
            if review_mode:
                self._status("waiting", f"⏳ Waiting for your approval — {job.company}")
                action, cover_letter = await self._request_approval(
                    job, insight, cover_letter, app_id
                )
                if action == "skip":
                    self.pool.mark_done(job.job_id, "skipped")
                    self.store.update_application(app_id, {"status": "skipped"})
                    self._status("idle", "Skipped — moving to next job")
                    return
                elif action == "stop":
                    self.pool.mark_done(job.job_id, "skipped")
                    self.store.update_application(app_id, {"status": "skipped"})
                    self._stop_event.set()
                    return
                # action == "approve" — continue with submission
                self.store.update_application(app_id, {"cover_letter": cover_letter})

            # 5. Navigate to application form
            self._status("applying", f"Opening {job.company} form...")
            apply_url = job.ats_url or job.url
            success = await self._fill_and_submit_form(
                apply_url, job, insight, cover_letter, app_id
            )

            if success:
                self.pool.mark_done(job.job_id, "submitted")
                self.store.update_application(app_id, {
                    "status": "submitted",
                    "applied_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
                self._status("done", f"✅ Applied to {job.title} @ {job.company}")
                print(f"[Track {self.track_id}] ✅ Submitted: {job.title} @ {job.company}")

                # Post-submission background tasks — don't block next application
                asyncio.create_task(
                    self._post_submission_actions(app_id, job, insight, cover_letter)
                )

                # Track advice usage for this submission
                try:
                    from core.success_tracker import record_application_sent
                    record_application_sent(app_id)
                except Exception:
                    pass
            else:
                self.pool.mark_done(job.job_id, "failed")
                self.store.update_application(app_id, {"status": "failed"})

        except Exception as e:
            print(f"[Track {self.track_id}] Pipeline error: {e}")
            if app_id:
                self.store.update_application(app_id, {"status": "failed", "notes": str(e)})
            self.pool.mark_done(job.job_id, "failed")
            raise

    async def _request_approval(self, job: Job, insight: dict,
                             cover_letter: str, app_id: int) -> tuple[str, str]:
        """
        Show approval dialog on main Qt thread and wait for user response.
        Uses approval_queue for thread-safe communication.
        """
        from ui.approval_queue import request_approval
        import concurrent.futures

        job_data = {
            "title":       job.title,
            "company":     job.company,
            "location":    job.location,
            "platform":    job.platform,
            "url":         job.url,
            "ats_url":     job.ats_url,
            "description": job.description,
            "score":       job.score,
        }

        # Run blocking request in thread pool so async loop stays alive
        loop = asyncio.get_event_loop()
        action, edited_cl = await loop.run_in_executor(
            None, lambda: request_approval(job_data, insight, cover_letter)
        )
        return action, edited_cl
        