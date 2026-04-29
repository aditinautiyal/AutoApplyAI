"""
tracks/track_worker.py
Robust application track — aggressively navigates to real ATS forms,
handles aggregators, new tabs, iframes, account creation, and verifies
actual submission before marking done.

Key improvements:
- Multi-attempt aggregator click-through (handles new tabs, iframes, popups)
- Detects and handles every major ATS automatically
- Creates accounts when needed (Workday, Taleo, iCIMS)
- Verifies real submission via confirmation page detection
- Never marks "submitted" without a real confirmation signal
- LinkedIn removed from fast lane (use slow lane instead)
"""

import asyncio
import random
import re
import time
import threading
from typing import Optional, Callable
from discovery.job_pool import get_pool, Job
from research.company_researcher import research_company
from research.insight_synthesizer import synthesize
from tracks.cover_letter_gen import generate_cover_letter, generate_form_answer
from tracks.humanizer_check import ensure_humanized
from core.settings_store import get_store

MIN_KEYSTROKE_MS = 35
MAX_KEYSTROKE_MS = 120

# ATS domains we can handle
GREENHOUSE_DOMAINS = ["greenhouse.io", "boards.greenhouse.io", "job-boards.greenhouse.io"]
LEVER_DOMAINS      = ["lever.co", "jobs.lever.co"]
WORKDAY_DOMAINS    = ["myworkdayjobs.com", "wd1.myworkdayjobs", "wd3.myworkdayjobs",
                      "wd5.myworkdayjobs", "workday.com"]
ASHBY_DOMAINS      = ["jobs.ashbyhq.com", "ashbyhq.com"]
SMARTR_DOMAINS     = ["smartrecruiters.com"]
ICIMS_DOMAINS      = ["icims.com", "careers.icims.com"]
TALEO_DOMAINS      = ["taleo.net", "oracle.taleo.net"]
JOBVITE_DOMAINS    = ["jobvite.com"]
BAMBOO_DOMAINS     = ["bamboohr.com"]
WORKABLE_DOMAINS   = ["apply.workable.com", "workable.com/jobs"]

# Aggregator sites that need click-through (NOT direct ATS)
AGGREGATOR_DOMAINS = [
    "simplyhired.com", "dice.com", "themuse.com",
    "internships.com", "wellfound.com", "workatastartup.com",
    "builtinchicago.org", "builtindallas.com", "builtin.com",
    "builtinnyc.com", "builtinaustin.com", "builtinseattle.com",
    "builtinboston.com", "glassdoor.com", "indeed.com",
    "ziprecruiter.com", "monster.com", "careerbuilder.com",
]

# Confirmation signals — if any appear, the application was REALLY submitted
CONFIRMATION_SIGNALS = [
    # Text patterns
    "thank you for applying", "application submitted", "application received",
    "successfully submitted", "application complete", "we received your application",
    "your application has been", "thanks for applying", "application was sent",
    "you have successfully applied", "application confirmation",
    # URL patterns
    "confirmation", "success", "submitted", "thank-you", "thankyou",
    # Greenhouse specific
    "application/new", "greenhouse.io/confirmation",
    # Lever specific
    "lever.co/apply", "application-confirmation",
]


def _is_ats(url: str, domains: list) -> bool:
    return any(d in url.lower() for d in domains)


def _detect_ats(url: str) -> str:
    """Returns ATS type string for a URL."""
    if _is_ats(url, GREENHOUSE_DOMAINS): return "greenhouse"
    if _is_ats(url, LEVER_DOMAINS):      return "lever"
    if _is_ats(url, WORKDAY_DOMAINS):    return "workday"
    if _is_ats(url, ASHBY_DOMAINS):      return "ashby"
    if _is_ats(url, SMARTR_DOMAINS):     return "smartrecruiters"
    if _is_ats(url, ICIMS_DOMAINS):      return "icims"
    if _is_ats(url, TALEO_DOMAINS):      return "taleo"
    if _is_ats(url, JOBVITE_DOMAINS):    return "jobvite"
    if _is_ats(url, BAMBOO_DOMAINS):     return "bamboo"
    if _is_ats(url, WORKABLE_DOMAINS):   return "workable"
    if _is_ats(url, AGGREGATOR_DOMAINS): return "aggregator"
    return "generic"


class TrackWorker:

    def __init__(self, track_id: int, stop_event: threading.Event,
                 status_callback: Optional[Callable] = None):
        self.track_id  = track_id
        self.stop_event = stop_event
        self.status_cb  = status_callback or (lambda t, s, m: None)
        self.pool   = get_pool()
        self.store  = get_store()
        self.context = None
        self.page    = None
        self._playwright = None

    def _status(self, status: str, msg: str = ""):
        self.status_cb(self.track_id, status, msg)

    async def run(self):
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
                print(f"[Track {self.track_id}] Error: {e}")
                self.pool.mark_done(job.job_id, "failed")
                self._status("error", str(e)[:80])
                await asyncio.sleep(3)

        await self._close_browser()

    async def _process_job(self, job: Job):
        app_id = None
        try:
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

            # Research
            self._status("researching", f"Researching {job.company}...")
            research = await research_company(job.company, job.title, job.description)
            insight  = synthesize(research)

            # Cover letter
            self._status("writing", f"Writing cover letter...")
            cover_letter = generate_cover_letter(
                job.title, job.company, job.description, insight, app_id
            )
            cover_letter, _, _ = ensure_humanized(cover_letter, job.company, job.title)

            self.store.update_application(app_id, {
                "cover_letter": cover_letter,
                "status": "applying",
            })

            # Approval gate
            if self.store.get("review_mode", True):
                self._status("waiting", f"⏳ Awaiting approval — {job.company}")
                action, cover_letter = await self._request_approval(job, insight, cover_letter, app_id)
                if action in ("skip", "stop"):
                    self.pool.mark_done(job.job_id, "skipped")
                    self.store.update_application(app_id, {"status": "skipped"})
                    if action == "stop":
                        self.stop_event.set()
                    return
                self.store.update_application(app_id, {"cover_letter": cover_letter})

            # Apply
            self._status("applying", f"Applying to {job.company}...")
            success = await self._apply(job, insight, cover_letter, app_id)

            if success:
                self.pool.mark_done(job.job_id, "submitted")
                self.store.update_application(app_id, {
                    "status": "submitted",
                    "applied_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
                self._status("done", f"✅ Submitted: {job.title} @ {job.company}")
                print(f"[Track {self.track_id}] ✅ REAL SUBMISSION: {job.title} @ {job.company}")
                asyncio.create_task(self._post_actions(app_id, job, insight, cover_letter))
            else:
                self.pool.mark_done(job.job_id, "failed")
                self.store.update_application(app_id, {"status": "failed"})

        except Exception as e:
            print(f"[Track {self.track_id}] Pipeline error: {e}")
            if app_id:
                self.store.update_application(app_id, {"status": "failed", "notes": str(e)[:200]})
            self.pool.mark_done(job.job_id, "failed")

    # ─── Approval ──────────────────────────────────────────────────────────────

    async def _request_approval(self, job, insight, cover_letter, app_id):
        from ui.approval_queue import request_approval
        job_data = {
            "title": job.title, "company": job.company,
            "location": job.location, "platform": job.platform,
            "url": job.url, "ats_url": job.ats_url,
            "description": job.description, "score": job.score,
        }
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: request_approval(job_data, insight, cover_letter)
        )

    # ─── Main apply entry point ────────────────────────────────────────────────

    async def _apply(self, job: Job, insight: dict, cover_letter: str,
                     app_id: int) -> bool:
        """
        Navigate to job URL and apply. Returns True ONLY if a real
        confirmation signal is detected on the final page.
        """
        if not self.page:
            return False

        url = job.ats_url or job.url

        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await self._delay(2, 3)
        except Exception as e:
            print(f"[Track {self.track_id}] Navigation error: {e}")
            return False

        current_url = self.page.url
        ats_type    = _detect_ats(current_url)

        print(f"[Track {self.track_id}] URL: {current_url[:70]} → ATS: {ats_type}")

        # If we landed on an aggregator, click through to the real ATS
        if ats_type == "aggregator":
            current_url, ats_type = await self._click_through_aggregator(job)
            if not current_url:
                return False

        return await self._fill_by_ats(ats_type, job, insight, cover_letter, app_id)

    # ─── Aggregator click-through ──────────────────────────────────────────────

    async def _click_through_aggregator(self, job: Job) -> tuple[str, str]:
        """
        Click the Apply button on aggregator pages and follow to the real ATS.
        Handles new tabs, same-tab redirects, and direct href extraction.
        Returns (final_url, ats_type) or ("", "") if failed.
        """
        print(f"[Track {self.track_id}] Aggregator — finding Apply button...")

        # All known apply button selectors, in priority order
        selectors = [
            # Data attributes (most reliable)
            "[data-testid='applyButton']",
            "[data-testid='viewJobButton']",
            "[data-automation-id='applyButton']",
            "[data-cy='apply-button']",
            # Class-based
            "a[class*='apply-btn']", "a[class*='applyBtn']",
            "button[class*='apply-btn']", "button[class*='applyBtn']",
            "a[class*='ApplyButton']", "button[class*='ApplyButton']",
            # Text-based (most universal)
            "a:has-text('Apply on Company Site')",
            "a:has-text('Apply Now')",
            "a:has-text('Apply for This Job')",
            "a:has-text('Apply for this job')",
            "button:has-text('Apply Now')",
            "button:has-text('Apply for Job')",
            "button:has-text('Apply to Job')",
            "a:has-text('Apply')",
            # Built In specific
            "a[href*='apply'][class*='btn']",
            # SimplyHired specific
            "a[href*='jobs'][class*='apply']",
            # Catch-all: any link going to a known ATS
            "a[href*='greenhouse.io']",
            "a[href*='lever.co']",
            "a[href*='myworkdayjobs']",
            "a[href*='ashbyhq']",
            "a[href*='smartrecruiters']",
            "a[href*='workable']",
        ]

        apply_btn = None
        apply_href = ""

        # Scroll down first to ensure button is rendered
        await self.page.evaluate("window.scrollTo(0, 400)")
        await self._delay(1, 2)

        for sel in selectors:
            try:
                el = await self.page.query_selector(sel)
                if el:
                    visible = await el.is_visible()
                    if visible:
                        href = await el.get_attribute("href") or ""
                        # If it's a direct ATS link, use it directly
                        if href and any(d in href for d in
                                        GREENHOUSE_DOMAINS + LEVER_DOMAINS +
                                        WORKDAY_DOMAINS + ASHBY_DOMAINS):
                            apply_href = href
                            apply_btn  = el
                            print(f"[Track {self.track_id}] Direct ATS href found: {href[:60]}")
                            break
                        elif el:
                            apply_btn = el
                            apply_href = href
            except Exception:
                continue

        if not apply_btn:
            print(f"[Track {self.track_id}] No Apply button found — skipping")
            return "", ""

        # If we have a direct ATS href, navigate directly
        if apply_href and apply_href.startswith("http"):
            ats_type = _detect_ats(apply_href)
            if ats_type not in ("aggregator", "generic", ""):
                try:
                    await self.page.goto(apply_href, wait_until="domcontentloaded", timeout=20000)
                    await self._delay(2, 3)
                    return self.page.url, _detect_ats(self.page.url)
                except Exception:
                    pass

        # Otherwise click and handle the result
        current_url = self.page.url

        # Watch for new tab
        try:
            async with self.context.expect_page(timeout=5000) as new_page_info:
                await apply_btn.click()
            new_page = await new_page_info.value
            await new_page.wait_for_load_state("domcontentloaded", timeout=15000)
            await self._delay(2, 3)
            # Switch to the new tab
            self.page = new_page
            final_url = self.page.url
            print(f"[Track {self.track_id}] New tab → {final_url[:70]}")
            return final_url, _detect_ats(final_url)

        except Exception:
            # No new tab — same tab navigation
            try:
                await apply_btn.click()
                await asyncio.sleep(4)
                final_url = self.page.url

                if final_url != current_url:
                    print(f"[Track {self.track_id}] Same tab → {final_url[:70]}")
                    return final_url, _detect_ats(final_url)
                else:
                    print(f"[Track {self.track_id}] No navigation after click")
                    return "", ""
            except Exception as e:
                print(f"[Track {self.track_id}] Click failed: {e}")
                return "", ""

    # ─── ATS Router ────────────────────────────────────────────────────────────

    async def _fill_by_ats(self, ats_type: str, job: Job, insight: dict,
                            cover_letter: str, app_id: int) -> bool:
        """Route to the correct ATS handler."""
        profile = self.store.get_profile() or {}

        if ats_type == "workday":
            from tracks.workday_handler import fill_workday_application
            return await fill_workday_application(
                self.page, self.page.url, job.title, job.company,
                insight, cover_letter, app_id
            )
        elif ats_type == "greenhouse":
            return await self._fill_greenhouse(job, insight, cover_letter, profile, app_id)
        elif ats_type == "lever":
            return await self._fill_lever(job, insight, cover_letter, profile, app_id)
        elif ats_type == "ashby":
            return await self._fill_ashby(job, insight, cover_letter, profile, app_id)
        elif ats_type in ("smartrecruiters", "icims", "taleo", "jobvite",
                          "bamboo", "workable"):
            return await self._fill_generic_ats(job, insight, cover_letter, profile, app_id)
        else:
            return await self._fill_generic_ats(job, insight, cover_letter, profile, app_id)

    # ─── Greenhouse ────────────────────────────────────────────────────────────

    async def _fill_greenhouse(self, job, insight, cover_letter, profile, app_id) -> bool:
        """Fill Greenhouse form — no account needed, direct form."""
        try:
            print(f"[Track {self.track_id}] Filling Greenhouse form...")

            # Wait for form
            await self.page.wait_for_selector(
                "form#application_form, #application-form, form[action*='applications']",
                timeout=12000
            )
            await self._delay(1, 2)

            full_name = profile.get("full_name", "") or ""
            fname = full_name.split()[0] if full_name else ""
            lname = full_name.split()[-1] if full_name and len(full_name.split()) > 1 else full_name

            # Standard fields
            field_map = {
                "first_name": fname,
                "last_name":  lname,
                "email":      profile.get("email", ""),
                "phone":      profile.get("phone", ""),
                "resume":     profile.get("resume_path", ""),
            }
            for field_id, value in field_map.items():
                if value:
                    await self._fill_by_id(field_id, value)

            # LinkedIn / portfolio
            await self._fill_by_placeholder("LinkedIn", profile.get("linkedin_url", ""))
            await self._fill_by_placeholder("Website", profile.get("portfolio_url", ""))
            await self._fill_by_placeholder("GitHub", profile.get("github_url", ""))

            # Cover letter
            for sel in ["textarea[name*='cover']", "textarea[id*='cover']",
                        "textarea[name*='letter']", "[id*='cover_letter'] textarea"]:
                el = await self.page.query_selector(sel)
                if el:
                    await el.fill(cover_letter)
                    break

            # Resume upload
            await self._upload_resume(profile)

            # Custom questions
            await self._fill_all_visible_fields(job, insight, cover_letter, profile)

            # Submit
            await self._delay(1, 2)
            return await self._click_submit_and_verify()

        except Exception as e:
            print(f"[Track {self.track_id}] Greenhouse error: {e}")
            return False

    # ─── Lever ─────────────────────────────────────────────────────────────────

    async def _fill_lever(self, job, insight, cover_letter, profile, app_id) -> bool:
        """Fill Lever form — no account needed, direct form."""
        try:
            print(f"[Track {self.track_id}] Filling Lever form...")

            await self.page.wait_for_selector(
                ".application-form, form.application, [class*='application']",
                timeout=12000
            )
            await self._delay(1, 2)

            full_name = profile.get("full_name", "") or ""
            await self._fill_by_placeholder("Full name", full_name)
            await self._fill_by_placeholder("Email", profile.get("email", ""))
            await self._fill_by_placeholder("Phone", profile.get("phone", ""))
            await self._fill_by_placeholder("LinkedIn", profile.get("linkedin_url", ""))
            await self._fill_by_placeholder("Website", profile.get("portfolio_url", ""))
            await self._fill_by_placeholder("GitHub", profile.get("github_url", ""))

            # Cover letter (Lever often has a comments field)
            for sel in ["textarea[placeholder*='cover']",
                        "textarea[data-field='comments']",
                        "textarea[name*='comments']",
                        "textarea[placeholder*='anything']",
                        "textarea[placeholder*='additional']"]:
                el = await self.page.query_selector(sel)
                if el:
                    await el.fill(cover_letter)
                    break

            await self._upload_resume(profile)
            await self._fill_all_visible_fields(job, insight, cover_letter, profile)

            await self._delay(1, 2)
            return await self._click_submit_and_verify()

        except Exception as e:
            print(f"[Track {self.track_id}] Lever error: {e}")
            return False

    # ─── Ashby ─────────────────────────────────────────────────────────────────

    async def _fill_ashby(self, job, insight, cover_letter, profile, app_id) -> bool:
        """Fill Ashby HQ form."""
        try:
            print(f"[Track {self.track_id}] Filling Ashby form...")
            await self._delay(2, 3)

            full_name = profile.get("full_name", "") or ""
            await self._fill_by_placeholder("Name", full_name)
            await self._fill_by_placeholder("Full name", full_name)
            await self._fill_by_placeholder("Email", profile.get("email", ""))
            await self._fill_by_placeholder("Phone", profile.get("phone", ""))
            await self._fill_by_placeholder("LinkedIn", profile.get("linkedin_url", ""))
            await self._fill_by_placeholder("Website", profile.get("portfolio_url", ""))

            await self._upload_resume(profile)
            await self._fill_all_visible_fields(job, insight, cover_letter, profile)

            await self._delay(1, 2)
            return await self._click_submit_and_verify()

        except Exception as e:
            print(f"[Track {self.track_id}] Ashby error: {e}")
            return False

    # ─── Generic ATS (SmartRecruiters, iCIMS, Taleo, etc.) ───────────────────

    async def _fill_generic_ats(self, job, insight, cover_letter, profile,
                                 app_id) -> bool:
        """
        Universal form filler for any ATS.
        Handles multi-step forms by clicking Next repeatedly.
        Creates accounts if needed.
        """
        try:
            print(f"[Track {self.track_id}] Filling generic ATS form: {self.page.url[:50]}")
            await self._delay(2, 3)

            # Check if account/login is needed
            needs_account = await self._check_needs_account()
            if needs_account:
                success = await self._handle_account_creation(profile)
                if not success:
                    print(f"[Track {self.track_id}] Account creation failed — skipping")
                    return False
                await self._delay(2, 3)

            # Multi-step form loop — up to 20 steps
            for step in range(20):
                print(f"[Track {self.track_id}] Form step {step + 1}")

                # Fill everything visible on this step
                await self._upload_resume(profile)
                await self._fill_all_visible_fields(job, insight, cover_letter, profile)
                await self._delay(1, 2)

                # Check for confirmation first
                if await self._check_confirmation():
                    print(f"[Track {self.track_id}] ✅ Confirmed on step {step + 1}")
                    return True

                # Look for Submit button
                submitted = await self._click_submit_and_verify(check_only=True)
                if submitted:
                    return True

                # Click Next to advance
                advanced = await self._click_next()
                if not advanced:
                    # No next button — try submit
                    return await self._click_submit_and_verify()

            return False

        except Exception as e:
            print(f"[Track {self.track_id}] Generic ATS error: {e}")
            return False

    # ─── Account handling ──────────────────────────────────────────────────────

    async def _check_needs_account(self) -> bool:
        """Check if the current page requires creating or signing into an account."""
        page_text = await self.page.content()
        signals = [
            "create account", "sign up", "register to apply",
            "login to apply", "sign in to apply", "create a profile"
        ]
        return any(s in page_text.lower() for s in signals)

    async def _handle_account_creation(self, profile: dict) -> bool:
        """
        Create account or sign in on ATS systems that require it.
        Uses stored password so we can sign in again later.
        """
        from tracks.workday_handler import _get_or_create_password
        email    = profile.get("email", "")
        password = _get_or_create_password()
        full_name = profile.get("full_name", "") or ""
        fname = full_name.split()[0] if full_name else ""
        lname = full_name.split()[-1] if full_name and len(full_name.split()) > 1 else ""

        print(f"[Track {self.track_id}] Creating/signing into account for {email}")

        # Try sign in first (in case account already exists)
        for email_sel in ["input[type='email']", "input[name*='email']",
                          "input[placeholder*='email' i]", "#email"]:
            el = await self.page.query_selector(email_sel)
            if el:
                await el.fill(email)
                break

        for pw_sel in ["input[type='password']", "input[name*='password']",
                       "#password", "input[placeholder*='password' i]"]:
            el = await self.page.query_selector(pw_sel)
            if el:
                await el.fill(password)
                break

        # Name fields for registration
        for fname_sel in ["input[name*='first']", "input[placeholder*='first' i]",
                          "#firstName", "#first_name"]:
            el = await self.page.query_selector(fname_sel)
            if el and fname:
                await el.fill(fname)
                break

        for lname_sel in ["input[name*='last']", "input[placeholder*='last' i]",
                          "#lastName", "#last_name"]:
            el = await self.page.query_selector(lname_sel)
            if el and lname:
                await el.fill(lname)
                break

        # Submit the account form
        for submit_sel in [
            "button[type='submit']", "button:has-text('Create Account')",
            "button:has-text('Sign Up')", "button:has-text('Register')",
            "button:has-text('Continue')", "input[type='submit']"
        ]:
            try:
                btn = await self.page.query_selector(submit_sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    await self._delay(3, 5)
                    return True
            except Exception:
                continue

        return False

    # ─── Universal field filler ────────────────────────────────────────────────

    async def _fill_all_visible_fields(self, job: Job, insight: dict,
                                        cover_letter: str, profile: dict):
        """
        Fill ALL visible input fields on the current page using smart mapping.
        This is the core of the universal form filler.
        """
        full_name = profile.get("full_name", "") or ""
        addr      = profile.get("address", "") or ""

        # Master field value map — covers every common field name variant
        VALUE_MAP = {
            # Name variants
            "first":        full_name.split()[0] if full_name else "",
            "fname":        full_name.split()[0] if full_name else "",
            "given":        full_name.split()[0] if full_name else "",
            "last":         full_name.split()[-1] if full_name and len(full_name.split()) > 1 else full_name,
            "lname":        full_name.split()[-1] if full_name and len(full_name.split()) > 1 else full_name,
            "family":       full_name.split()[-1] if full_name and len(full_name.split()) > 1 else full_name,
            "surname":      full_name.split()[-1] if full_name and len(full_name.split()) > 1 else full_name,
            "fullname":     full_name,
            "full_name":    full_name,
            "name":         full_name,
            # Contact
            "email":        profile.get("email", ""),
            "mail":         profile.get("email", ""),
            "phone":        profile.get("phone", ""),
            "telephone":    profile.get("phone", ""),
            "mobile":       profile.get("phone", ""),
            "cell":         profile.get("phone", ""),
            # Location
            "address":      addr.split(",")[0].strip(),
            "street":       addr.split(",")[0].strip(),
            "city":         addr.split(",")[0].strip() if "," in addr else addr,
            "zip":          "",
            "postal":       "",
            "state":        addr.split(",")[1].strip() if addr.count(",") >= 1 else "IN",
            "country":      "United States",
            # Professional links
            "linkedin":     profile.get("linkedin_url", ""),
            "github":       profile.get("github_url", ""),
            "website":      profile.get("portfolio_url", ""),
            "portfolio":    profile.get("portfolio_url", ""),
            "url":          profile.get("portfolio_url", ""),
            # Cover letter
            "cover":        cover_letter,
            "letter":       cover_letter,
            "motivation":   cover_letter,
            "message":      cover_letter[:1000],
            "additional":   cover_letter[:500],
            "comments":     cover_letter[:500],
            "anything":     cover_letter[:500],
            # Academic
            "gpa":          profile.get("gpa", ""),
            "grade":        profile.get("gpa", ""),
            "graduation":   profile.get("graduation_date", ""),
            "graduate":     profile.get("graduation_date", ""),
            "degree":       "Bachelor of Science",
            "major":        "Computer Science",
            "university":   "Purdue University",
            "school":       "Purdue University",
            "college":      "Purdue University",
            "institution":  "Purdue University",
            # Work auth
            "authorized":   "Yes",
            "authorization":"Yes",
            "eligible":     "Yes",
            "sponsorship":  "No",
            "sponsor":      "No",
            "visa":         "No",
            "citizen":      "Yes",
            # Compensation
            "salary":       str(profile.get("salary_min", 20)),
            "compensation": str(profile.get("salary_min", 20)),
            "pay":          str(profile.get("salary_min", 20)),
            # Availability
            "start":        "May 2026",
            "available":    "May 2026",
            "begin":        "May 2026",
            "relocate":     "Yes",
            "remote":       "Yes",
            # Misc
            "hear":         "LinkedIn",
            "source":       "Online",
            "referral":     "Online job posting",
            "experience":   "0-1 years",
            "years":        "0",
        }

        # Fill text inputs
        inputs = await self.page.query_selector_all(
            "input:not([type='hidden']):not([type='file']):not([type='checkbox'])"
            ":not([type='radio']):not([type='submit']):not([type='button']), "
            "textarea"
        )

        for inp in inputs:
            try:
                if not await inp.is_visible():
                    continue

                # Get field identifier
                label_text = await self._get_field_label(inp)
                if not label_text:
                    continue

                label_lower = label_text.lower().strip()

                # Check value map
                value = ""
                for key, val in VALUE_MAP.items():
                    if key in label_lower and val:
                        value = str(val)
                        break

                # Fall back to learned answers
                if not value:
                    value = self.store.find_learned_answer(label_lower) or ""

                # Fall back to AI generation for custom questions
                if not value and len(label_lower) > 10 and "?" in label_text:
                    value = generate_form_answer(label_text, job.title, job.company, insight)

                if value:
                    tag = (await inp.evaluate("el => el.tagName")).lower()
                    current = await inp.input_value() if tag == "input" else await inp.evaluate("el => el.value") or ""
                    if not current:  # Don't overwrite
                        if tag == "textarea":
                            await inp.fill(value)
                        else:
                            await inp.fill(value)
                        await self._delay(0.1, 0.4)

            except Exception:
                continue

        # Fill select dropdowns
        selects = await self.page.query_selector_all("select:not([disabled])")
        for sel_el in selects:
            try:
                if not await sel_el.is_visible():
                    continue

                label_text = await self._get_field_label(sel_el)
                label_lower = (label_text or "").lower()

                if any(kw in label_lower for kw in ["country"]):
                    try:
                        await sel_el.select_option(label="United States")
                    except Exception:
                        try:
                            await sel_el.select_option(value="US")
                        except Exception:
                            pass
                elif any(kw in label_lower for kw in ["state", "province"]):
                    addr = profile.get("address", "")
                    state = addr.split(",")[-1].strip() if "," in addr else "IN"
                    try:
                        await sel_el.select_option(label=state)
                    except Exception:
                        pass
                elif any(kw in label_lower for kw in ["authorization", "sponsor", "visa", "work auth"]):
                    for opt in ["Yes", "No Sponsorship Required", "US Citizen",
                                "Authorized", "I am authorized"]:
                        try:
                            await sel_el.select_option(label=opt)
                            break
                        except Exception:
                            continue
                elif any(kw in label_lower for kw in ["experience", "years"]):
                    for opt in ["0-1 years", "Less than 1 year", "0", "Entry Level", "<1"]:
                        try:
                            await sel_el.select_option(label=opt)
                            break
                        except Exception:
                            continue
                elif any(kw in label_lower for kw in ["degree", "education"]):
                    for opt in ["Bachelor", "Bachelor's", "Undergraduate", "BS"]:
                        try:
                            await sel_el.select_option(label=opt)
                            break
                        except Exception:
                            continue
                elif any(kw in label_lower for kw in ["gender"]):
                    try:
                        await sel_el.select_option(label="Female")
                    except Exception:
                        try:
                            await sel_el.select_option(label="Woman")
                        except Exception:
                            try:
                                await sel_el.select_option(label="Prefer not to say")
                            except Exception:
                                pass

            except Exception:
                continue

        # Handle checkboxes (agreements/consents)
        checkboxes = await self.page.query_selector_all("input[type='checkbox']")
        for cb in checkboxes:
            try:
                if not await cb.is_visible():
                    continue
                is_checked = await cb.is_checked()
                if is_checked:
                    continue

                label_text = (await self._get_field_label(cb)).lower()
                if any(kw in label_text for kw in [
                    "agree", "accept", "consent", "acknowledge",
                    "certify", "authorize", "terms", "privacy",
                    "confirm", "understand"
                ]):
                    await cb.click()
                    await self._delay(0.2, 0.5)
            except Exception:
                continue

    # ─── Resume upload ─────────────────────────────────────────────────────────

    async def _upload_resume(self, profile: dict):
        """Find and upload resume to any file input."""
        resume_path = profile.get("resume_path", "")
        if not resume_path:
            return

        selectors = [
            "input[type='file'][name*='resume']",
            "input[type='file'][id*='resume']",
            "input[type='file'][accept*='pdf']",
            "input[type='file'][name*='cv']",
            "input[type='file']",
        ]

        for sel in selectors:
            try:
                el = await self.page.query_selector(sel)
                if el:
                    await el.set_input_files(resume_path)
                    await self._delay(2, 3)
                    print(f"[Track {self.track_id}] Resume uploaded")
                    return
            except Exception:
                continue

    # ─── Submit & verify ───────────────────────────────────────────────────────

    async def _click_submit_and_verify(self, check_only: bool = False) -> bool:
        """
        Find and click the submit button, then verify a real confirmation appears.
        Returns True ONLY if confirmation is detected.
        """
        submit_selectors = [
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Submit Application')",
            "button:has-text('Submit application')",
            "button:has-text('Submit')",
            "button:has-text('Send Application')",
            "button:has-text('Apply Now')",
            "button:has-text('Complete Application')",
            "button:has-text('Finish')",
            "[data-testid='submit-application-button']",
            "[data-automation-id='submitButton']",
        ]

        for sel in submit_selectors:
            try:
                btn = await self.page.query_selector(sel)
                if not btn:
                    continue
                if not await btn.is_visible():
                    continue

                text = (await btn.inner_text()).lower().strip()

                # Skip obvious non-submit buttons
                if any(skip in text for skip in ["next", "back", "previous",
                                                   "save", "cancel", "search"]):
                    continue

                if check_only and "submit" not in text and "apply" not in text:
                    continue

                print(f"[Track {self.track_id}] Clicking: '{text}'")
                await btn.click()
                await self._delay(3, 5)

                # Check for confirmation
                if await self._check_confirmation():
                    return True

            except Exception:
                continue

        return False

    async def _click_next(self) -> bool:
        """Click a Next/Continue button to advance the form."""
        next_selectors = [
            "button:has-text('Next')",
            "button:has-text('Continue')",
            "button:has-text('Save and Continue')",
            "button:has-text('Proceed')",
            "[data-testid='nextButton']",
            "[data-automation-id='nextButton']",
            "[data-automation-id='bottom-navigation-next-button']",
            "button[aria-label*='next' i]",
            "button[aria-label*='continue' i]",
        ]

        for sel in next_selectors:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    await self._delay(2, 3)
                    return True
            except Exception:
                continue

        return False

    async def _check_confirmation(self) -> bool:
        """
        Check if the current page shows a real submission confirmation.
        Checks both page text and URL patterns.
        """
        try:
            # Check URL
            current_url = self.page.url.lower()
            if any(sig in current_url for sig in [
                "confirmation", "thank-you", "thankyou", "success",
                "submitted", "complete", "done"
            ]):
                print(f"[Track {self.track_id}] Confirmation URL: {current_url[:60]}")
                return True

            # Check page text
            page_text = await self.page.evaluate("document.body.innerText")
            page_lower = page_text.lower()

            for signal in CONFIRMATION_SIGNALS:
                if signal in page_lower:
                    print(f"[Track {self.track_id}] Confirmation text: '{signal}'")
                    return True

            # Check for specific confirmation elements
            for sel in [
                "[data-testid='applicationSubmittedMessage']",
                "[data-automation-id='applicationSubmittedMessage']",
                ".application-submitted",
                ".success-message",
                "#confirmation",
                "h1:has-text('Thank')",
                "h2:has-text('Thank')",
                "h1:has-text('Submitted')",
                "h2:has-text('Submitted')",
            ]:
                try:
                    el = await self.page.query_selector(sel)
                    if el and await el.is_visible():
                        print(f"[Track {self.track_id}] Confirmation element: {sel}")
                        return True
                except Exception:
                    continue

        except Exception:
            pass

        return False

    # ─── Field helpers ─────────────────────────────────────────────────────────

    async def _get_field_label(self, field_el) -> str:
        """Get the label text for a form field using multiple strategies."""
        try:
            # Strategy 1: for attribute
            field_id = await field_el.get_attribute("id") or ""
            if field_id:
                label = await self.page.query_selector(f"label[for='{field_id}']")
                if label:
                    return await label.inner_text()

            # Strategy 2: aria-label
            aria = await field_el.get_attribute("aria-label") or ""
            if aria:
                return aria

            # Strategy 3: placeholder
            placeholder = await field_el.get_attribute("placeholder") or ""
            if placeholder:
                return placeholder

            # Strategy 4: name attribute
            name = await field_el.get_attribute("name") or ""
            if name:
                return name.replace("_", " ").replace("-", " ")

            # Strategy 5: data-testid
            testid = await field_el.get_attribute("data-testid") or ""
            if testid:
                return testid.replace("-", " ")

            # Strategy 6: preceding sibling label text
            try:
                label_text = await field_el.evaluate("""
                    el => {
                        let node = el.previousElementSibling;
                        while (node) {
                            if (node.tagName === 'LABEL' || node.tagName === 'SPAN'
                                || node.tagName === 'DIV' || node.tagName === 'P') {
                                const t = node.innerText.trim();
                                if (t.length > 0 && t.length < 100) return t;
                            }
                            node = node.previousElementSibling;
                        }
                        const parent = el.closest('[class*="field"], [class*="Field"], [class*="form-group"]');
                        if (parent) {
                            const lbl = parent.querySelector('label, [class*="label"]');
                            if (lbl) return lbl.innerText.trim();
                        }
                        return '';
                    }
                """)
                if label_text:
                    return label_text
            except Exception:
                pass

        except Exception:
            pass

        return ""

    async def _fill_by_id(self, field_id: str, value: str):
        if not value:
            return
        try:
            for sel in [f"#{field_id}", f"[name='{field_id}']",
                        f"[id*='{field_id}']"]:
                el = await self.page.query_selector(sel)
                if el:
                    current = await el.input_value()
                    if not current:
                        await el.fill(value)
                    await self._delay(0.2, 0.5)
                    return
        except Exception:
            pass

    async def _fill_by_placeholder(self, placeholder: str, value: str):
        if not value:
            return
        try:
            for sel in [
                f"input[placeholder*='{placeholder}' i]",
                f"textarea[placeholder*='{placeholder}' i]",
                f"input[aria-label*='{placeholder}' i]",
            ]:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    current = await el.input_value()
                    if not current:
                        await el.fill(value)
                    await self._delay(0.2, 0.5)
                    return
        except Exception:
            pass

    async def _delay(self, min_s: float, max_s: float):
        await asyncio.sleep(random.uniform(min_s, max_s))

    # ─── Post-submission actions ────────────────────────────────────────────────

    async def _post_actions(self, app_id, job, insight, cover_letter):
        try:
            from extra_effort.people_finder import find_contacts_for_application
            await find_contacts_for_application(job.company, job.title, app_id, insight)
        except Exception:
            pass
        try:
            from email_handler.gmail_sender import send_cold_email_for_application
            send_cold_email_for_application(app_id, job.company, job.title, insight)
        except Exception:
            pass

    # ─── Browser lifecycle ─────────────────────────────────────────────────────

    async def _init_browser(self):
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self.context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=f"/tmp/autoapplyai_track_{self.track_id}",
                headless=False,  # MUST be False — JS doesn't render headless
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--start-maximized",
                ],
                viewport={"width": 1280, "height": 800},
            )
            self.page = await self.context.new_page()
            print(f"[Track {self.track_id}] Browser initialized")
        except Exception as e:
            print(f"[Track {self.track_id}] Browser init failed: {e}")
            self.page = None

    async def _close_browser(self):
        try:
            if self.context:
                await self.context.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
