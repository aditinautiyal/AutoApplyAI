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
        Show approval dialog on the main thread and wait for user response.
        Returns (action, cover_letter). Runs dialog via Qt signal safely.
        """
        import asyncio
        from PyQt6.QtWidgets import QApplication

        # Build job data dict for the dialog
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

        result = {"action": "skip", "cover_letter": cover_letter}
        event = asyncio.Event()

        def show_dialog():
            from ui.approval_dialog import ApprovalDialog
            dialog = ApprovalDialog(job_data, insight, cover_letter)
            dialog.exec()
            action, edited_cl = dialog.get_result()
            result["action"] = action
            result["cover_letter"] = edited_cl
            event.set()

        # Schedule dialog on main Qt thread
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(
            QApplication.instance().metaObject().invokeMethod,
        ) if False else None  # Placeholder — use direct approach below

        # Run on main thread via QTimer
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, show_dialog)

        # Wait for user response (check every 0.5s)
        while not event.is_set():
            await asyncio.sleep(0.5)

        return result["action"], result["cover_letter"]

    async def _post_submission_actions(self, app_id: int, job: Job,
                                        insight: dict, cover_letter: str):
        """
        Runs after a successful submission — in background, doesn't block next job.
        1. Find contacts who could supplement this application (people finder)
        2. Send cold email if recruiter address findable
        3. Update advice success tracking
        """
        try:
            # 1. People finder — find contacts at this company
            print(f"[Track {self.track_id}] Finding contacts for {job.company}...")
            from extra_effort.people_finder import find_contacts_for_application
            await find_contacts_for_application(
                company=job.company,
                job_title=job.title,
                application_id=app_id,
                insight=insight,
            )
        except Exception as e:
            print(f"[Track {self.track_id}] People finder error: {e}")

        try:
            # 2. Cold email — auto-send if Gmail connected and email findable
            gmail_token = self.store.get("gmail_token")
            if gmail_token:
                from email_handler.gmail_sender import GmailClient, _find_recruiter_email
                from tracks.cover_letter_gen import generate_cold_email_body

                recruiter_email = _find_recruiter_email(job.company)
                if recruiter_email:
                    subject, body = generate_cold_email_body(
                        company=job.company,
                        job_title=job.title,
                        insight=insight,
                    )
                    gmail = GmailClient()
                    if gmail.is_connected():
                        sent = gmail.send_email(recruiter_email, subject, body)
                        if sent:
                            self.store.update_application(app_id, {
                                "notes": f"Cold email sent to {recruiter_email}"
                            })
                            print(f"[Track {self.track_id}] 📧 Cold email sent to {job.company}")
        except Exception as e:
            print(f"[Track {self.track_id}] Cold email error: {e}")

    async def _fill_and_submit_form(self, url: str, job: Job, insight: dict,
                                     cover_letter: str, app_id: int) -> bool:
        """Navigate to URL and fill the application form with human-like behavior."""
        try:
            # New page for each application — completely isolated
            page = await self.context.new_page()

            # Random viewport size
            await page.set_viewport_size({
                "width": random.randint(1280, 1920),
                "height": random.randint(800, 1080)
            })

            await page.goto(url, wait_until="networkidle", timeout=30000)
            await self._human_delay(1.5, 3.0)
            await self._human_scroll(page)

            # Find all form fields
            fields = await page.query_selector_all("input, textarea, select")

            profile = self.store.get_profile() or {}

            for form_field in fields:
                try:
                    field_type = await form_field.get_attribute("type") or "text"
                    field_name = (
                        await form_field.get_attribute("name") or
                        await form_field.get_attribute("id") or
                        await form_field.get_attribute("placeholder") or ""
                    ).lower()
                    tag = (await form_field.evaluate("el => el.tagName")).lower()

                    if field_type in ("submit", "button", "hidden", "checkbox", "radio"):
                        continue

                    value = await self._get_field_value(
                        field_name, field_type, tag,
                        profile, job, insight, cover_letter
                    )

                    if value:
                        is_autofill = any(af in field_name for af in AUTOFILL_FIELDS)

                        if tag == "select":
                            await form_field.select_option(label=value)
                        elif tag == "textarea":
                            await self._type_human(page, form_field, value, fast=False)
                        elif is_autofill:
                            # Personal info fields — fast like autofill
                            await self._type_human(page, form_field, value, fast=True)
                        else:
                            await self._type_human(page, form_field, value, fast=False)

                        await self._human_delay(0.3, 1.0)

                except Exception as e:
                    print(f"[Track {self.track_id}] Field error: {e}")
                    continue

            # Check for unknown fields that need user input
            unknown_fields = await self._detect_unknown_fields(page, profile)
            if unknown_fields:
                await self._handle_unknown_fields(unknown_fields, job, app_id)
                await page.close()
                return False  # Will resume when user provides info

            # Look for file upload (resume)
            await self._handle_resume_upload(page, profile)

            # Handle GitHub OAuth if needed
            await self._handle_github_oauth(page)

            await self._human_delay(1.0, 2.5)

            # Submit
            submit_btn = await page.query_selector(
                "button[type='submit'], input[type='submit'], button:has-text('Submit'), button:has-text('Apply')"
            )
            if submit_btn:
                await submit_btn.scroll_into_view_if_needed()
                await self._human_delay(0.5, 1.5)
                await submit_btn.click()
                await self._human_delay(2.0, 4.0)
                await page.close()
                return True
            else:
                await page.close()
                return False

        except Exception as e:
            print(f"[Track {self.track_id}] Form error: {e}")
            try:
                await page.close()
            except Exception:
                pass
            return False

    async def _get_field_value(self, field_name: str, field_type: str,
                                tag: str, profile: dict, job: Job,
                                insight: dict, cover_letter: str) -> str:
        """Determine what value to put in a form field."""
        fn = field_name.lower()

        # Personal info — use profile data directly
        mapping = {
            "name": profile.get("full_name", ""),
            "full_name": profile.get("full_name", ""),
            "first": profile.get("full_name", "").split()[0] if profile.get("full_name") else "",
            "last": profile.get("full_name", "").split()[-1] if profile.get("full_name") else "",
            "email": profile.get("email", ""),
            "phone": profile.get("phone", ""),
            "address": profile.get("address", ""),
            "linkedin": profile.get("linkedin_url", ""),
            "github": profile.get("github_url", ""),
            "portfolio": profile.get("portfolio_url", ""),
            "website": profile.get("portfolio_url", ""),
            "gpa": profile.get("gpa", ""),
            "graduation": profile.get("graduation_date", ""),
            "university": "",  # From resume parsed
            "work_auth": profile.get("work_auth", ""),
            "authorized": "Yes",
            "sponsor": "No",
            "salary": f"{profile.get('salary_min', 20)}-{profile.get('salary_max', 35)} per hour",
        }

        for key, value in mapping.items():
            if key in fn and value:
                return str(value)

        # Cover letter field
        if any(kw in fn for kw in ["cover", "letter", "motivation", "statement"]):
            return cover_letter

        # Resume path handled separately via file upload

        # Check learned answers
        stored = self.store.find_learned_answer(field_name)
        if stored:
            return stored

        # Generate answer for unknown question fields
        if tag == "textarea" or (tag == "input" and field_type == "text" and len(field_name) > 3):
            if any(skip in fn for skip in ["captcha", "token", "csrf", "honeypot"]):
                return ""
            answer = generate_form_answer(
                question=field_name,
                job_title=job.title,
                company=job.company,
                insight=insight,
            )
            return answer

        return ""

    async def _type_human(self, page, element, text: str, fast: bool = False):
        """Type text with human-like speed and occasional typos."""
        await element.click()
        await element.fill("")  # Clear first

        if fast:
            # Autofill-style — quick
            await element.type(text, delay=random.randint(10, 30))
            return

        for char in text:
            # Occasional typo + correction
            if random.random() < 0.03 and char.isalpha():
                typo = random.choice("qwertyuiopasdfghjklzxcvbnm")
                await element.type(typo, delay=random.randint(MIN_KEYSTROKE_MS, MAX_KEYSTROKE_MS))
                await asyncio.sleep(random.uniform(0.1, 0.3))
                await page.keyboard.press("Backspace")
                await asyncio.sleep(random.uniform(0.05, 0.15))

            delay = random.randint(MIN_KEYSTROKE_MS, MAX_KEYSTROKE_MS)
            await element.type(char, delay=delay)

            # Occasional pause mid-word
            if random.random() < 0.05:
                await asyncio.sleep(random.uniform(0.3, 0.8))

    async def _human_scroll(self, page):
        """Realistic scroll behavior — reads the page like a human."""
        await page.evaluate("""
            () => {
                const totalHeight = document.body.scrollHeight;
                const steps = Math.floor(Math.random() * 4) + 2;
                for (let i = 0; i < steps; i++) {
                    const target = (totalHeight / steps) * i;
                    window.scrollTo({top: target, behavior: 'smooth'});
                }
            }
        """)
        await asyncio.sleep(random.uniform(0.8, 2.0))

    async def _human_delay(self, min_s: float, max_s: float):
        await asyncio.sleep(random.uniform(min_s, max_s))

    async def _handle_resume_upload(self, page, profile: dict):
        """Find file input and upload resume PDF."""
        resume_path = profile.get("resume_path")
        if not resume_path:
            return
        try:
            file_input = await page.query_selector("input[type='file']")
            if file_input:
                await file_input.set_input_files(resume_path)
                await self._human_delay(1.0, 2.0)
        except Exception as e:
            print(f"[Track {self.track_id}] Resume upload error: {e}")

    async def _handle_github_oauth(self, page):
        """Auto-handle GitHub OAuth buttons if GitHub token available."""
        store = self.store
        github_token = store.get("github_token")
        if not github_token:
            return
        try:
            github_btn = await page.query_selector(
                "a[href*='github.com/login'], button:has-text('GitHub'), a:has-text('GitHub')"
            )
            if github_btn:
                print(f"[Track {self.track_id}] Handling GitHub OAuth...")
                await github_btn.click()
                await self._human_delay(2.0, 4.0)
                # GitHub login handled via stored session in browser context
        except Exception:
            pass

    async def _detect_unknown_fields(self, page, profile: dict) -> list:
        """Detect fields we can't fill that need user clarification."""
        unknown = []
        # Look for visible required fields still empty
        try:
            required_empty = await page.query_selector_all(
                "input[required]:not([value]), textarea[required]:empty"
            )
            for el in required_empty:
                label_text = await el.get_attribute("placeholder") or await el.get_attribute("name") or ""
                if label_text and not any(af in label_text.lower() for af in AUTOFILL_FIELDS):
                    unknown.append(label_text)
        except Exception:
            pass
        return unknown

    async def _handle_unknown_fields(self, unknown_fields: list, job: Job, app_id: int):
        """Pause this application and notify user."""
        self.pool.pause_job(job.job_id, "unknown_fields")
        self.store.update_application(app_id, {
            "status": "paused",
            "paused_reason": f"Unknown fields: {', '.join(unknown_fields[:3])}"
        })
        message = f"Application to {job.title} at {job.company} needs your input:\n" + \
                  "\n".join(f"• {f}" for f in unknown_fields[:5])
        self.store.add_notification(
            notif_type="clarification",
            title=f"Input needed: {job.company}",
            message=message,
            application_id=app_id,
        )
        print(f"[Track {self.track_id}] ⏸ Paused {job.company} — needs user input")

    async def _init_browser(self):
        """Initialize stealth Playwright browser."""
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
        )
        # Try to apply stealth
        try:
            from playwright_stealth import stealth_async
            self._stealth = stealth_async
        except ImportError:
            self._stealth = None

        self.context = await self.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768},
            locale="en-US",
        )

    async def _close_browser(self):
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
