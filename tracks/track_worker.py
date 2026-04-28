"""
tracks/track_worker.py
One isolated application track. Pulls a job from the pool, researches it,
generates content, fills the form, submits. Completely isolated per track.
Uses Playwright with stealth for human-like behavior.
Paused applications step aside without blocking the track.

Supports:
- Workday (full account creation + multi-step form)
- Greenhouse
- Lever
- The Muse (click-through to real ATS)
- Generic ATS fallback
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

MIN_KEYSTROKE_MS = 40
MAX_KEYSTROKE_MS = 160

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
                "job_title":    job.title,
                "company_name": job.company,
                "job_url":      job.url,
                "ats_url":      job.ats_url,
                "platform":     job.platform,
                "score":        job.score,
                "track_id":     self.track_id,
                "status":       "researching",
            })

            # 2. Research the company
            self._status("researching", f"Researching {job.company}...")
            research = await research_company(job.company, job.title, job.description)
            insight = synthesize(research)

            # 3. Generate cover letter + humanize
            self._status("writing", f"Writing cover letter for {job.company}...")
            cover_letter = generate_cover_letter(
                job.title, job.company, job.description, insight, app_id
            )
            cover_letter, ai_score, attempts = ensure_humanized(
                cover_letter, job.company, job.title
            )

            self.store.update_application(app_id, {
                "cover_letter": cover_letter,
                "status": "applying",
            })

            # 4. Manual approval gate
            review_mode = self.store.get("review_mode", True)
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
                    self.stop_event.set()
                    return
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

                asyncio.create_task(
                    self._post_submission_actions(app_id, job, insight, cover_letter)
                )

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
        """Show approval dialog on main Qt thread and wait for user response."""
        from ui.approval_queue import request_approval

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

        loop = asyncio.get_event_loop()
        action, edited_cl = await loop.run_in_executor(
            None, lambda: request_approval(job_data, insight, cover_letter)
        )
        return action, edited_cl

    async def _fill_and_submit_form(self, url: str, job: Job, insight: dict,
                                     cover_letter: str, app_id: int) -> bool:
        """
        Navigate to application URL and fill/submit the form.
        Routes to the correct handler based on ATS platform detected.
        """
        if not self.page:
            return False

        try:
            await self.page.goto(url, wait_until="networkidle", timeout=30000)
            await self._delay(2, 4)

            profile = self.store.get_profile() or {}
            platform = job.platform or ""
            current_url = self.page.url

            # ── Workday ───────────────────────────────────────────────────────
            from tracks.workday_handler import fill_workday_application, is_workday_url
            if is_workday_url(current_url) or is_workday_url(url):
                return await fill_workday_application(
                    self.page, current_url, job.title, job.company,
                    insight, cover_letter, app_id
                )

            # ── Greenhouse ────────────────────────────────────────────────────
            elif "greenhouse" in current_url or "greenhouse" in platform:
                return await self._fill_greenhouse(job, insight, cover_letter, profile, app_id)

            # ── Lever ─────────────────────────────────────────────────────────
            elif "lever" in current_url or "lever" in platform:
                return await self._fill_lever(job, insight, cover_letter, profile, app_id)

            # ── The Muse landing page — click through to real ATS ─────────────
            elif "themuse.com" in current_url:
                apply_btn = await self.page.query_selector(
                    "a[data-automation-id='applyButton'], "
                    "a:has-text('Apply on Company Site'), "
                    "a:has-text('Apply Now'), "
                    "button:has-text('Apply')"
                )
                if apply_btn:
                    try:
                        async with self.page.expect_navigation(timeout=15000):
                            await apply_btn.click()
                    except Exception:
                        await apply_btn.click()
                        await asyncio.sleep(3)

                    await self._delay(2, 3)
                    new_url = self.page.url
                    print(f"[Track {self.track_id}] Muse → {new_url[:70]}")

                    # Re-detect ATS after redirect
                    if is_workday_url(new_url):
                        return await fill_workday_application(
                            self.page, new_url, job.title, job.company,
                            insight, cover_letter, app_id
                        )
                    elif "greenhouse" in new_url:
                        return await self._fill_greenhouse(job, insight, cover_letter, profile, app_id)
                    elif "lever" in new_url:
                        return await self._fill_lever(job, insight, cover_letter, profile, app_id)
                    else:
                        return await self._fill_generic(job, insight, cover_letter, profile, app_id)
                else:
                    print(f"[Track {self.track_id}] Muse: no Apply button found on page")
                    return False

            # ── Generic fallback ──────────────────────────────────────────────
            else:
                return await self._fill_generic(job, insight, cover_letter, profile, app_id)

        except Exception as e:
            print(f"[Track {self.track_id}] Form fill error: {e}")
            if app_id:
                self.store.update_application(app_id, {
                    "status": "paused",
                    "paused_reason": f"Form fill error: {str(e)[:200]}"
                })
            return False

    async def _fill_greenhouse(self, job: Job, insight: dict, cover_letter: str,
                                profile: dict, app_id: int) -> bool:
        """Fill a Greenhouse ATS application form."""
        try:
            await self.page.wait_for_selector("form#application_form, #application-form", timeout=10000)
            await self._delay(1, 2)

            await self._fill_field_by_id("first_name", profile.get("full_name", "").split()[0] if profile.get("full_name") else "")
            await self._fill_field_by_id("last_name", profile.get("full_name", "").split()[-1] if profile.get("full_name") else "")
            await self._fill_field_by_id("email", profile.get("email", ""))
            await self._fill_field_by_id("phone", profile.get("phone", ""))

            cover_area = await self.page.query_selector("textarea[name*='cover'], textarea[id*='cover']")
            if cover_area:
                await cover_area.fill(cover_letter)
                await self._delay(0.5, 1)

            resume_input = await self.page.query_selector("input[type='file'][name*='resume']")
            if resume_input and profile.get("resume_path"):
                try:
                    await resume_input.set_input_files(profile["resume_path"])
                    await self._delay(1, 2)
                except Exception:
                    pass

            await self._fill_custom_questions(job, insight, cover_letter, profile)

            submit_btn = await self.page.query_selector(
                "input[type='submit'], button[type='submit'], button:has-text('Submit Application')"
            )
            if submit_btn:
                await self._delay(1, 2)
                await submit_btn.click()
                await self._delay(3, 5)
                return True

        except Exception as e:
            print(f"[Track {self.track_id}] Greenhouse error: {e}")

        return False

    async def _fill_lever(self, job: Job, insight: dict, cover_letter: str,
                           profile: dict, app_id: int) -> bool:
        """Fill a Lever ATS application form."""
        try:
            await self.page.wait_for_selector(".application-form, form", timeout=10000)
            await self._delay(1, 2)

            await self._fill_field_by_placeholder("Full name", profile.get("full_name", ""))
            await self._fill_field_by_placeholder("Email", profile.get("email", ""))
            await self._fill_field_by_placeholder("Phone", profile.get("phone", ""))

            linkedin = profile.get("linkedin_url", "")
            if linkedin:
                await self._fill_field_by_placeholder("LinkedIn", linkedin)

            portfolio = profile.get("portfolio_url", "")
            if portfolio:
                await self._fill_field_by_placeholder("Website", portfolio)

            cover_area = await self.page.query_selector(
                "textarea[placeholder*='cover'], textarea[data-field='comments']"
            )
            if cover_area:
                await cover_area.fill(cover_letter)
                await self._delay(0.5, 1)

            resume_input = await self.page.query_selector("input[type='file']")
            if resume_input and profile.get("resume_path"):
                try:
                    await resume_input.set_input_files(profile["resume_path"])
                    await self._delay(1, 2)
                except Exception:
                    pass

            submit_btn = await self.page.query_selector(
                "button[type='submit'], button:has-text('Submit Application'), button:has-text('Apply')"
            )
            if submit_btn:
                await self._delay(1, 2)
                await submit_btn.click()
                await self._delay(3, 5)

                confirm = await self.page.query_selector(
                    ".success-message, h2:has-text('Application received'), h1:has-text('Thank you')"
                )
                return bool(confirm)

        except Exception as e:
            print(f"[Track {self.track_id}] Lever error: {e}")

        return False

    async def _fill_generic(self, job: Job, insight: dict, cover_letter: str,
                             profile: dict, app_id: int) -> bool:
        """Fill a generic / unknown ATS form using heuristic field mapping."""
        try:
            inputs = await self.page.query_selector_all(
                "input:not([type='hidden']):not([type='submit']):not([type='file'])"
                ":not([type='checkbox']):not([type='radio']),"
                "textarea"
            )

            for inp in inputs:
                try:
                    label_text = await self._get_label_text(inp)
                    value = self._map_field_value(label_text.lower(), profile, cover_letter, insight)
                    if value:
                        tag = (await inp.evaluate("el => el.tagName")).lower()
                        if tag == "textarea":
                            await inp.fill(value)
                        else:
                            await self._type_human(inp, value)
                        await self._delay(0.3, 0.8)
                except Exception:
                    continue

            file_input = await self.page.query_selector("input[type='file']")
            if file_input and profile.get("resume_path"):
                try:
                    await file_input.set_input_files(profile["resume_path"])
                    await self._delay(1, 2)
                except Exception:
                    pass

            submit_btn = await self.page.query_selector(
                "input[type='submit'], button[type='submit'],"
                "button:has-text('Submit'), button:has-text('Apply')"
            )
            if submit_btn:
                await self._delay(1, 2)
                await submit_btn.click()
                await self._delay(3, 5)
                return True

        except Exception as e:
            print(f"[Track {self.track_id}] Generic form error: {e}")

        return False

    async def _fill_custom_questions(self, job: Job, insight: dict,
                                      cover_letter: str, profile: dict):
        """Find and answer custom application questions using AI."""
        try:
            question_selectors = [
                "label:not([for*='resume']):not([for*='name']):not([for*='email']):not([for*='phone'])",
            ]
            for selector in question_selectors:
                labels = await self.page.query_selector_all(selector)
                for label_el in labels[:10]:
                    try:
                        question_text = await label_el.inner_text()
                        if len(question_text.strip()) < 10:
                            continue

                        label_for = await label_el.get_attribute("for")
                        if label_for:
                            inp = await self.page.query_selector(f"#{label_for}, [name='{label_for}']")
                        else:
                            inp = await label_el.evaluate_handle(
                                "el => el.nextElementSibling"
                            )

                        if not inp:
                            continue

                        tag = await inp.evaluate("el => el.tagName.toLowerCase()")
                        if tag not in ("input", "textarea", "select"):
                            continue

                        answer = generate_form_answer(
                            question_text, job.title, job.company, insight
                        )
                        if answer:
                            if tag == "select":
                                try:
                                    await inp.select_option(label=answer)
                                except Exception:
                                    pass
                            else:
                                await inp.fill(answer)
                            await self._delay(0.5, 1.5)

                    except Exception:
                        continue
        except Exception as e:
            print(f"[Track {self.track_id}] Custom questions error: {e}")

    async def _post_submission_actions(self, app_id: int, job: Job,
                                        insight: dict, cover_letter: str):
        """Background tasks after submission."""
        try:
            from extra_effort.people_finder import find_contacts_for_application
            contacts = await find_contacts_for_application(
                job.company, job.title, app_id, insight
            )
            if contacts:
                print(f"[Track {self.track_id}] Found {len(contacts)} contacts for {job.company}")
        except Exception as e:
            print(f"[Track {self.track_id}] People finder error: {e}")

        try:
            from email_handler.gmail_sender import send_cold_email_for_application
            send_cold_email_for_application(app_id, job.company, job.title, insight)
        except Exception as e:
            print(f"[Track {self.track_id}] Cold email error: {e}")

    # ── Field helpers ──────────────────────────────────────────────────────────

    async def _fill_field_by_id(self, field_id: str, value: str):
        if not value:
            return
        try:
            el = await self.page.query_selector(f"#{field_id}, [name='{field_id}']")
            if el:
                await el.fill(value)
                await self._delay(0.3, 0.8)
        except Exception:
            pass

    async def _fill_field_by_placeholder(self, placeholder: str, value: str):
        if not value:
            return
        try:
            el = await self.page.query_selector(
                f"input[placeholder*='{placeholder}'], textarea[placeholder*='{placeholder}']"
            )
            if el:
                await el.fill(value)
                await self._delay(0.3, 0.8)
        except Exception:
            pass

    async def _get_label_text(self, field_el) -> str:
        try:
            field_id = await field_el.get_attribute("id")
            if field_id:
                label = await self.page.query_selector(f"label[for='{field_id}']")
                if label:
                    return await label.inner_text()
            placeholder = await field_el.get_attribute("placeholder") or ""
            name = await field_el.get_attribute("name") or ""
            return placeholder or name
        except Exception:
            return ""

    def _map_field_value(self, field_name: str, profile: dict,
                          cover_letter: str, insight: dict) -> str:
        full_name = profile.get("full_name", "") or ""
        mapping = {
            "first":      full_name.split()[0] if full_name else "",
            "last":       full_name.split()[-1] if full_name else "",
            "name":       full_name,
            "email":      profile.get("email", ""),
            "phone":      profile.get("phone", ""),
            "city":       (profile.get("address") or "").split(",")[0].strip(),
            "linkedin":   profile.get("linkedin_url", ""),
            "github":     profile.get("github_url", ""),
            "website":    profile.get("portfolio_url", ""),
            "portfolio":  profile.get("portfolio_url", ""),
            "cover":      cover_letter,
            "letter":     cover_letter,
            "gpa":        profile.get("gpa", ""),
            "graduation": profile.get("graduation_date", ""),
            "authorized": "Yes",
            "sponsor":    "No",
            "salary":     str(profile.get("salary_min", 20)),
        }
        for key, value in mapping.items():
            if key in field_name and value:
                return str(value)

        stored = self.store.find_learned_answer(field_name)
        return stored or ""

    async def _type_human(self, element, text: str):
        try:
            await element.click()
            for char in text:
                delay = random.randint(MIN_KEYSTROKE_MS, MAX_KEYSTROKE_MS)
                await element.type(char, delay=delay)
                if random.random() < 0.03:
                    await asyncio.sleep(random.uniform(0.1, 0.4))
        except Exception:
            try:
                await element.fill(text)
            except Exception:
                pass

    async def _delay(self, min_s: float, max_s: float):
        await asyncio.sleep(random.uniform(min_s, max_s))

    # ── Browser lifecycle ──────────────────────────────────────────────────────

    async def _init_browser(self):
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self.context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=f"/tmp/autoapplyai_track_{self.track_id}",
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            self.page = await self.context.new_page()
            await self.page.set_viewport_size({"width": 1280, "height": 800})
            print(f"[Track {self.track_id}] Browser initialized")
        except Exception as e:
            print(f"[Track {self.track_id}] Browser init failed: {e}")
            self.page = None

    async def _close_browser(self):
        try:
            if self.context:
                await self.context.close()
            if hasattr(self, "_playwright") and self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
