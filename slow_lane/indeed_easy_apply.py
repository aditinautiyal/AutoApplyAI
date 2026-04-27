"""
slow_lane/indeed_easy_apply.py
Human-paced Indeed Easy Apply automation.
Logged in via persistent browser session. Long delays between applications.
"""

import asyncio
import random
import re
import time
import threading
from core.settings_store import get_store
from tracks.cover_letter_gen import generate_cover_letter, generate_form_answer
from tracks.humanizer_check import ensure_humanized
from research.company_researcher import research_company
from research.insight_synthesizer import synthesize

MIN_DELAY = 10 * 60   # 10 minutes between apps
MAX_DELAY = 25 * 60   # 25 minutes between apps

ROLE_SEARCHES = [
    "software engineer intern",
    "machine learning intern",
    "data science intern",
    "AI engineer intern",
    "computer science intern",
]


class IndeedSlowLane:
    def __init__(self, stop_event: threading.Event, status_callback=None):
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
        print("[SlowLane:Indeed] Starting...")
        await self._init_browser()

        logged_in = await self._login()
        if not logged_in:
            self._status("error", "Indeed login failed. Check credentials in Settings.")
            return

        self._status("running", "Indeed slow lane active")

        while not self.stop_event.is_set():
            try:
                applied = await self._apply_one()
                if applied:
                    self._applied_today += 1
                    self._status("applied", f"Applied via Indeed Easy Apply ({self._applied_today} today)")

                delay = random.randint(MIN_DELAY, MAX_DELAY)
                print(f"[SlowLane:Indeed] Waiting {delay // 60}min before next...")
                for _ in range(delay):
                    if self.stop_event.is_set():
                        break
                    await asyncio.sleep(1)

            except Exception as e:
                print(f"[SlowLane:Indeed] Error: {e}")
                await asyncio.sleep(60)

        await self._close_browser()

    async def _apply_one(self) -> bool:
        profile = self.store.get_profile() or {}
        role = random.choice(ROLE_SEARCHES)
        location = self._get_location(profile)

        search_url = (
            f"https://www.indeed.com/jobs?q={role.replace(' ', '+')}"
            f"&l={location.replace(' ', '+')}"
            f"&fromage=1"   # Posted in last 1 day
            f"&sort=date"
        )

        await self.page.goto(search_url, wait_until="networkidle", timeout=30000)
        await self._delay(3, 5)
        await self._scroll(self.page)

        # Find Easy Apply jobs
        job_cards = await self.page.query_selector_all(".job_seen_beacon")
        if not job_cards:
            return False

        # Pick from first 6 cards randomly
        card = random.choice(job_cards[:min(6, len(job_cards))])
        await card.click()
        await self._delay(2, 4)

        # Check for Easy Apply button
        apply_btn = await self.page.query_selector(
            "button[id*='indeedApplyButton'], .ia-IndeedApplyButton, button:has-text('Easily apply')"
        )
        if not apply_btn:
            return False

        # Get job details
        title_el = await self.page.query_selector(".jobsearch-JobInfoHeader-title")
        company_el = await self.page.query_selector("[data-company-name]")
        desc_el = await self.page.query_selector("#jobDescriptionText")

        title = await title_el.inner_text() if title_el else role
        company = await company_el.inner_text() if company_el else "Unknown Company"
        description = await desc_el.inner_text() if desc_el else ""

        print(f"[SlowLane:Indeed] Applying: {title} @ {company}")

        # Research and generate content
        research = await research_company(company, title, description)
        insight = synthesize(research)
        cover_letter = generate_cover_letter(title, company, description, insight)
        cover_letter, _, _ = ensure_humanized(cover_letter, company, title)

        await apply_btn.click()
        await self._delay(2, 3)

        success = await self._fill_indeed_form(title, company, insight, cover_letter, profile)

        if success:
            self.store.log_application({
                "job_title": title.strip(),
                "company_name": company.strip(),
                "job_url": self.page.url,
                "platform": "indeed_easy_apply",
                "lane_type": "slow",
                "status": "submitted",
                "applied_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "cover_letter": cover_letter,
            })
            return True

        return False

    async def _fill_indeed_form(self, title: str, company: str, insight: dict,
                                 cover_letter: str, profile: dict) -> bool:
        """Fill Indeed's multi-step application form."""
        max_steps = 10

        for step in range(max_steps):
            if self.stop_event.is_set():
                return False

            await self._delay(1, 2)

            # Fill all visible fields
            fields = await self.page.query_selector_all(
                "input:not([type='hidden']):not([type='submit']), textarea, select"
            )

            for field in fields:
                try:
                    field_type = await field.get_attribute("type") or "text"
                    if field_type in ("hidden", "submit", "file", "checkbox", "radio"):
                        continue

                    label_text = await self._get_field_label(field)
                    tag = (await field.evaluate("el => el.tagName")).lower()

                    value = self._map_value(
                        label_text.lower(), profile, cover_letter, insight
                    )

                    if value:
                        if tag == "select":
                            try:
                                await field.select_option(label=value)
                            except Exception:
                                await field.select_option(index=0)
                        else:
                            await field.fill("")
                            await self._type_human(field, value)
                        await self._delay(0.4, 1.2)

                except Exception:
                    continue

            # Resume upload
            file_input = await self.page.query_selector("input[type='file']")
            if file_input and profile.get("resume_path"):
                try:
                    await file_input.set_input_files(profile["resume_path"])
                    await self._delay(1, 2)
                except Exception:
                    pass

            # Find next/submit
            next_btn = await self.page.query_selector(
                "button[data-testid='IndeedApplyButton-button'],"
                "button:has-text('Continue'),"
                "button:has-text('Submit'),"
                "button:has-text('Apply now')"
            )

            if not next_btn:
                break

            btn_text = (await next_btn.inner_text()).lower()
            await self._delay(1, 2)
            await next_btn.click()

            if any(w in btn_text for w in ["submit", "apply"]):
                await self._delay(2, 4)
                return True

        # Check for confirmation
        confirm = await self.page.query_selector(
            "[data-testid='postApply'], .ia-PostApply, "
            "h1:has-text('application was sent'), h1:has-text('Applied')"
        )
        return bool(confirm)

    async def _get_field_label(self, field) -> str:
        try:
            field_id = await field.get_attribute("id")
            if field_id:
                label = await self.page.query_selector(f"label[for='{field_id}']")
                if label:
                    return await label.inner_text()
            return await field.get_attribute("placeholder") or await field.get_attribute("name") or ""
        except Exception:
            return ""

    def _map_value(self, field_name: str, profile: dict,
                   cover_letter: str, insight: dict) -> str:
        mapping = {
            "first": profile.get("full_name", "").split()[0] if profile.get("full_name") else "",
            "last": profile.get("full_name", "").split()[-1] if profile.get("full_name") else "",
            "name": profile.get("full_name", ""),
            "email": profile.get("email", ""),
            "phone": profile.get("phone", ""),
            "city": (profile.get("address") or "").split(",")[0].strip(),
            "linkedin": profile.get("linkedin_url", ""),
            "github": profile.get("github_url", ""),
            "website": profile.get("portfolio_url", ""),
            "cover": cover_letter,
            "letter": cover_letter,
            "gpa": profile.get("gpa", ""),
            "graduation": profile.get("graduation_date", ""),
            "authorized": "Yes",
            "sponsor": "No",
            "salary": str(profile.get("salary_min", 20)),
            "experience": "0-1 years",
        }
        for key, value in mapping.items():
            if key in field_name and value:
                return str(value)

        stored = self.store.find_learned_answer(field_name)
        return stored or ""

    def _get_location(self, profile: dict) -> str:
        locs = profile.get("locations", "")
        if locs:
            loc_list = [l.strip() for l in locs.split(",")]
            return random.choice(loc_list)
        return "Remote"

    async def _type_human(self, element, text: str):
        await element.click()
        for char in text:
            await element.type(char, delay=random.randint(45, 145))
            if random.random() < 0.04:
                await asyncio.sleep(random.uniform(0.2, 0.5))

    async def _scroll(self, page):
        await page.evaluate("window.scrollBy(0, Math.random() * 400 + 100)")
        await asyncio.sleep(random.uniform(0.5, 1.5))

    async def _delay(self, min_s: float, max_s: float):
        await asyncio.sleep(random.uniform(min_s, max_s))

    async def _login(self) -> bool:
        try:
            await self.page.goto("https://www.indeed.com/", timeout=20000)
            await asyncio.sleep(2)
            # Check if signed in — look for user menu
            user_menu = await self.page.query_selector(
                "[data-testid='UserAccountMenu'], .gnav-AccountDropdown"
            )
            if user_menu:
                return True
            # Not signed in — open sign-in page for user to manually login
            await self.page.goto("https://secure.indeed.com/account/login", timeout=20000)
            print("[SlowLane:Indeed] Please sign in to Indeed in the opened browser window.")
            # Wait up to 2 minutes for manual login
            for _ in range(120):
                if self.stop_event.is_set():
                    return False
                user_menu = await self.page.query_selector(
                    "[data-testid='UserAccountMenu'], .gnav-AccountDropdown"
                )
                if user_menu:
                    print("[SlowLane:Indeed] Login detected — continuing.")
                    return True
                await asyncio.sleep(1)
            return False
        except Exception as e:
            print(f"[SlowLane:Indeed] Login error: {e}")
            return False

    async def _init_browser(self):
        from playwright.async_api import async_playwright
        import pathlib
        self._playwright = await async_playwright().start()
        user_data_dir = str(pathlib.Path.home() / ".autoapplyai" / "indeed_profile")
        self.context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
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
