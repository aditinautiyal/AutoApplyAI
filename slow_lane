"""
slow_lane/linkedin_easy_apply.py
Human-paced LinkedIn Easy Apply automation.
Logged in via stored session. Long random delays between applications.
Completely separate from fast lane — runs in its own async loop.
"""

import asyncio
import random
import time
import threading
from core.settings_store import get_store
from tracks.cover_letter_gen import generate_cover_letter, generate_form_answer
from tracks.humanizer_check import ensure_humanized
from research.company_researcher import research_company
from research.insight_synthesizer import synthesize

# Delays between Easy Apply submissions — human-paced
MIN_DELAY_BETWEEN_APPS = 8 * 60    # 8 minutes
MAX_DELAY_BETWEEN_APPS = 20 * 60   # 20 minutes

LINKEDIN_JOB_SEARCHES = [
    "software engineer intern AI",
    "machine learning intern",
    "data science intern",
    "AI research intern",
    "computer science intern",
    "backend engineer intern",
]


class LinkedInSlowLane:
    """
    Slow-lane LinkedIn Easy Apply.
    Logs in once, applies gradually and naturally.
    """

    def __init__(self, stop_event: threading.Event,
                 status_callback=None):
        self.stop_event = stop_event
        self.status_cb = status_callback or (lambda s, m: None)
        self.store = get_store()
        self.browser = None
        self.context = None
        self.page = None
        self._applied_today = 0

    def _status(self, status: str, msg: str = ""):
        self.status_cb(status, msg)

    async def run(self):
        """Main slow lane loop."""
        print("[SlowLane:LinkedIn] Starting...")
        await self._init_browser()

        logged_in = await self._login()
        if not logged_in:
            self._status("error", "LinkedIn login failed. Check credentials in Settings.")
            return

        self._status("running", "LinkedIn slow lane active")

        while not self.stop_event.is_set():
            try:
                applied = await self._apply_one_batch()
                if applied:
                    self._applied_today += applied
                    self._status("applied", f"Applied via LinkedIn Easy Apply ({self._applied_today} today)")

                # Long human-like delay before next application
                delay = random.randint(MIN_DELAY_BETWEEN_APPS, MAX_DELAY_BETWEEN_APPS)
                print(f"[SlowLane:LinkedIn] Waiting {delay//60}min before next application...")
                for _ in range(delay):
                    if self.stop_event.is_set():
                        break
                    await asyncio.sleep(1)

            except Exception as e:
                print(f"[SlowLane:LinkedIn] Error: {e}")
                await asyncio.sleep(60)

        await self._close_browser()

    async def _apply_one_batch(self) -> int:
        """Find and apply to 1 Easy Apply job."""
        search_term = random.choice(LINKEDIN_JOB_SEARCHES)
        profile = self.store.get_profile() or {}
        location = self._get_location_for_search(profile)

        # Navigate to jobs search
        search_url = (
            f"https://www.linkedin.com/jobs/search/"
            f"?keywords={search_term.replace(' ', '%20')}"
            f"&location={location.replace(' ', '%20')}"
            f"&f_AL=true"  # Easy Apply filter
            f"&sortBy=DD"   # Date posted
        )

        await self.page.goto(search_url, wait_until="networkidle", timeout=30000)
        await self._human_delay(3, 6)
        await self._human_scroll(self.page)

        # Find job cards
        job_cards = await self.page.query_selector_all(".job-card-container")
        if not job_cards:
            return 0

        # Pick random card from first 5 (not always the first one)
        card = random.choice(job_cards[:min(5, len(job_cards))])
        await card.click()
        await self._human_delay(2, 4)

        # Get job details
        title_el = await self.page.query_selector(".job-details-jobs-unified-top-card__job-title")
        company_el = await self.page.query_selector(".job-details-jobs-unified-top-card__company-name")
        desc_el = await self.page.query_selector(".jobs-description__content")

        title = await title_el.inner_text() if title_el else "Unknown Role"
        company = await company_el.inner_text() if company_el else "Unknown Company"
        description = await desc_el.inner_text() if desc_el else ""

        # Check if already applied
        already_applied = await self.page.query_selector(".artdeco-inline-feedback--success")
        if already_applied:
            return 0

        # Click Easy Apply button
        easy_apply_btn = await self.page.query_selector("button.jobs-apply-button")
        if not easy_apply_btn:
            return 0

        btn_text = await easy_apply_btn.inner_text()
        if "Easy Apply" not in btn_text:
            return 0

        print(f"[SlowLane:LinkedIn] Applying: {title} @ {company}")

        # Research company
        research = await research_company(company, title, description)
        insight = synthesize(research)

        cover_letter = generate_cover_letter(title, company, description, insight)
        cover_letter, _, _ = ensure_humanized(cover_letter, company, title)

        await easy_apply_btn.click()
        await self._human_delay(2, 3)

        # Fill the Easy Apply modal
        submitted = await self._fill_easy_apply_modal(title, company, insight, cover_letter)

        if submitted:
            self.store.log_application({
                "job_title": title.strip(),
                "company_name": company.strip(),
                "job_url": self.page.url,
                "platform": "linkedin_easy_apply",
                "lane_type": "slow",
                "status": "submitted",
                "applied_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "cover_letter": cover_letter,
            })
            return 1

        return 0

    async def _fill_easy_apply_modal(self, title: str, company: str,
                                      insight: dict, cover_letter: str) -> bool:
        """Fill LinkedIn Easy Apply multi-step modal."""
        profile = self.store.get_profile() or {}
        max_steps = 8

        for step in range(max_steps):
            if self.stop_event.is_set():
                return False

            await self._human_delay(1, 2)

            # Fill visible fields
            fields = await self.page.query_selector_all(
                ".jobs-easy-apply-modal input, .jobs-easy-apply-modal textarea, .jobs-easy-apply-modal select"
            )

            for field in fields:
                try:
                    field_type = await field.get_attribute("type") or "text"
                    field_id = await field.get_attribute("id") or ""
                    field_label = await self._get_field_label(field) or field_id
                    tag = (await field.evaluate("el => el.tagName")).lower()

                    if field_type in ("hidden", "submit", "file"):
                        continue

                    value = self._get_value_for_field(
                        field_label.lower(), profile, cover_letter, insight, title, company
                    )

                    if value:
                        if tag == "select":
                            try:
                                await field.select_option(label=value)
                            except Exception:
                                await field.select_option(index=1)
                        else:
                            await field.fill("")
                            await self._type_human(field, value)

                    await self._human_delay(0.5, 1.5)

                except Exception:
                    continue

            # Handle resume upload if present
            resume_input = await self.page.query_selector(
                ".jobs-easy-apply-modal input[type='file']"
            )
            if resume_input and profile.get("resume_path"):
                try:
                    await resume_input.set_input_files(profile["resume_path"])
                    await self._human_delay(1, 2)
                except Exception:
                    pass

            # Look for Next or Submit button
            next_btn = await self.page.query_selector(
                "button[aria-label='Continue to next step'], "
                "button[aria-label='Submit application'], "
                "button:has-text('Next'), button:has-text('Submit'), "
                "button:has-text('Review')"
            )

            if not next_btn:
                break

            btn_text = await next_btn.inner_text()
            await self._human_delay(1, 2)
            await next_btn.click()

            if any(word in btn_text.lower() for word in ["submit", "done"]):
                await self._human_delay(2, 4)
                return True

        # Check for confirmation
        confirm = await self.page.query_selector(
            "[data-test-id='application-submitted'], .artdeco-inline-feedback--success"
        )
        return bool(confirm)

    async def _get_field_label(self, field) -> str:
        """Try to find the label for a form field."""
        try:
            field_id = await field.get_attribute("id")
            if field_id:
                label = await self.page.query_selector(f"label[for='{field_id}']")
                if label:
                    return await label.inner_text()
            placeholder = await field.get_attribute("placeholder")
            return placeholder or ""
        except Exception:
            return ""

    def _get_value_for_field(self, field_name: str, profile: dict,
                              cover_letter: str, insight: dict,
                              job_title: str, company: str) -> str:
        """Map field name to profile value."""
        mapping = {
            "first": profile.get("full_name", "").split()[0] if profile.get("full_name") else "",
            "last": profile.get("full_name", "").split()[-1] if profile.get("full_name") else "",
            "name": profile.get("full_name", ""),
            "email": profile.get("email", ""),
            "phone": profile.get("phone", ""),
            "linkedin": profile.get("linkedin_url", ""),
            "website": profile.get("portfolio_url", ""),
            "cover": cover_letter,
            "letter": cover_letter,
            "gpa": profile.get("gpa", ""),
            "graduation": profile.get("graduation_date", ""),
            "authorized": "Yes",
            "sponsor": "No",
            "years": "0-1",
            "salary": f"{profile.get('salary_min', 20)}",
        }
        for key, value in mapping.items():
            if key in field_name and value:
                return str(value)

        # Check learned answers
        stored = self.store.find_learned_answer(field_name)
        if stored:
            return stored

        return ""

    def _get_location_for_search(self, profile: dict) -> str:
        locs = profile.get("locations", "")
        if locs:
            loc_list = [l.strip() for l in locs.split(",")]
            return random.choice(loc_list)
        return "United States"

    async def _login(self) -> bool:
        """Navigate to LinkedIn and verify logged in."""
        try:
            await self.page.goto("https://www.linkedin.com/feed/", timeout=20000)
            await asyncio.sleep(3)
            # Check if we're logged in (feed loads without redirect to login)
            current_url = self.page.url
            if "login" in current_url or "checkpoint" in current_url:
                print("[SlowLane:LinkedIn] Not logged in — need LinkedIn session")
                return False
            return True
        except Exception as e:
            print(f"[SlowLane:LinkedIn] Login check error: {e}")
            return False

    async def _type_human(self, element, text: str):
        """Type with human-like speed."""
        await element.click()
        for char in text:
            await element.type(char, delay=random.randint(50, 150))
            if random.random() < 0.05:
                await asyncio.sleep(random.uniform(0.2, 0.6))

    async def _human_scroll(self, page):
        await page.evaluate("window.scrollBy(0, Math.random() * 300 + 100)")
        await asyncio.sleep(random.uniform(0.5, 1.5))

    async def _human_delay(self, min_s: float, max_s: float):
        await asyncio.sleep(random.uniform(min_s, max_s))

    async def _init_browser(self):
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()
        # Use persistent context so LinkedIn session is saved
        user_data_dir = str(
            __import__("pathlib").Path.home() / ".autoapplyai" / "linkedin_profile"
        )
        self.context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,  # LinkedIn detects headless more aggressively
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.page = await self.context.new_page()

    async def _close_browser(self):
        try:
            if self.context:
                await self.context.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
