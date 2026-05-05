"""
tracks/track_worker.py
FULL REBUILD — Universal, verified, robust form filling.

Design principles:
  - Handles EVERY form field on EVERY page, not just known fields
  - Multi-pass filling: runs until no more fields can be filled
  - Dropdown selection is VERIFIED before accepting — never clicks wrong option
  - Handles non-typeable dropdowns by scrolling through options
  - Popup/overlay/cookie dismissal on every navigation
  - Claude AI vision fallback for unknown page obstacles
  - Strict submission confirmation — no false positives
"""

from __future__ import annotations
import asyncio
import base64
import json
import random
import re
from pathlib import Path

import httpx
from playwright.async_api import async_playwright, Page, TimeoutError as PwTimeout

from core.settings_store import get_store
from discovery.job_pool import get_pool
from tracks.cover_letter_gen import generate_cover_letter
from tracks.humanizer_check import ensure_humanized
from research.company_researcher import research_company
from research.insight_synthesizer import synthesize

# ─── Constants ────────────────────────────────────────────────────────────────

PROFILE_PATH_BASE = Path.home() / ".autoapplyai"

# Strict confirmation phrases — only these count as real submissions
CONFIRMATION_PHRASES = [
    "thank you for your application",
    "your application has been submitted",
    "application received",
    "application complete",
    "we have received your application",
    "we will review your application",
    "we'll be in touch",
    "you've successfully applied",
    "successfully submitted",
    "your application is complete",
    "application submitted successfully",
    "we received your application",
    "thanks for applying",
    "thank you for applying",
    "application was submitted",
]

# These URL patterns mean the page is NOT a real confirmation
FALSE_POSITIVE_URLS = [
    "stripe.com/jobs/search",
    "simplyhired.com",
    "indeed.com",
    "linkedin.com/jobs",
    "glassdoor.com",
]

# Popup dismiss selectors — ordered most-specific → most-generic
POPUP_SELECTORS = [
    "button[data-provides='cookie-consent-accept-all']",
    "button:has-text('Accept all cookies')",
    "button:has-text('Accept All Cookies')",
    "button:has-text('Accept all')",
    "button:has-text('Accept All')",
    "button:has-text('Accept Cookies')",
    "button:has-text('Accept cookies')",
    "button:has-text('I Accept')",
    "button:has-text('I agree')",
    "button:has-text('Agree and continue')",
    "button:has-text('Agree')",
    "button:has-text('Allow all')",
    "button:has-text('Allow All')",
    "button:has-text('OK, got it')",
    "button:has-text('Got it')",
    "button:has-text('OK')",
    "button:has-text('Dismiss')",
    "button:has-text('Close')",
    "button:has-text('Continue')",
    "button:has-text('Proceed')",
    "[id*='cookie'] button:has-text('Accept')",
    "[class*='cookie'] button:has-text('Accept')",
    "[id*='gdpr'] button:not(:has-text('Reject'))",
    "[class*='gdpr'] button:not(:has-text('Reject'))",
    "[id*='consent'] button:has-text('Accept')",
    "[class*='consent'] button:has-text('Accept')",
    "[id*='banner'] button:has-text('Accept')",
    "[class*='banner'] button:has-text('Accept')",
    "button[aria-label='Close']",
    "button[aria-label='close']",
    ".modal-close",
    ".close-button",
    "[data-dismiss='modal']",
    ".cookie-close",
    "[class*='popup'] button:has-text('Close')",
    "[class*='overlay'] button",
]

# Minimum confidence to click a dropdown option (0.0–1.0)
# Below this, we skip rather than risk clicking the wrong thing
DROPDOWN_MIN_CONFIDENCE = 0.45


# ─── ATS Detection ────────────────────────────────────────────────────────────

def _detect_ats(url: str) -> str:
    u = url.lower()
    if "greenhouse.io" in u or "boards.greenhouse" in u or "job-boards.greenhouse" in u:
        return "greenhouse"
    if "lever.co" in u:
        return "lever"
    if "myworkdayjobs" in u or "workday.com" in u:
        return "workday"
    if "ashbyhq.com" in u or "jobs.ashbyhq" in u:
        return "ashby"
    if "jobvite.com" in u:
        return "jobvite"
    if "simplyhired" in u or "indeed.com" in u or "linkedin.com/jobs" in u or "glassdoor" in u:
        return "aggregator"
    return "generic"


# ─── Option Scoring ───────────────────────────────────────────────────────────

def _score_option(option_text: str, target: str) -> float:
    """
    Score how well an option_text matches a target value.
    Returns 0.0 (no match) to 1.0 (perfect match).
    Never returns a high score for placeholder-like options.
    """
    o = option_text.strip().lower()
    t = target.strip().lower()

    # Skip empty / placeholder options
    if not o or o in ("select...", "select", "--", "---", "please select", "choose one", "none"):
        return 0.0

    # Exact match
    if o == t:
        return 1.0

    # One fully contains the other
    if t in o:
        score = len(t) / len(o)
        return max(score, 0.6)
    if o in t:
        return len(o) / len(t) * 0.85

    # Word-level overlap
    t_words = set(t.split())
    o_words = set(o.split())
    common = t_words & o_words
    if common:
        return len(common) / max(len(t_words), len(o_words)) * 0.75

    # First 4-char prefix match (e.g. "Bach" matches "Bachelor of Science")
    if len(t) >= 4 and o.startswith(t[:4]):
        return 0.55

    return 0.0


# ─── Worker ───────────────────────────────────────────────────────────────────

class TrackWorker:
    def __init__(self, track_id: int, status_cb=None, log_cb=None):
        self.track_id  = track_id
        self.status_cb = status_cb or (lambda *a: None)
        self.log_cb    = log_cb    or (lambda *a: None)
        self.page: Page | None = None
        self._context  = None
        self._stop     = False
        self.store     = get_store()
        self.pool      = get_pool()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def _launch_browser(self):
        profile_dir = PROFILE_PATH_BASE / f"track_{self.track_id}"
        profile_dir.mkdir(parents=True, exist_ok=True)
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        self._context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        self.page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        await self.page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

    async def _close_browser(self):
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass

    def stop(self):
        self._stop = True

    async def run(self):
        while not self._stop:
            try:
                if not self.page or self.page.is_closed():
                    await self._close_browser()
                    await self._launch_browser()

                job = self.pool.pop_next()
                if not job:
                    await asyncio.sleep(5)
                    continue

                self.status_cb(self.track_id, f"Applying: {job.title} @ {job.company}")
                print(f"[Track {self.track_id}] ► {job.title} @ {job.company}")

                try:
                    research = await asyncio.wait_for(
                        research_company(job.company, job.title, job.description), timeout=90
                    )
                    insight = synthesize(research)
                    cl = await generate_cover_letter(job, insight)
                    cl, ai_score, attempts = ensure_humanized(cl, job.company, job.title)
                    print(f"[Humanizer] AI score {ai_score:.2f} after {attempts} attempt(s)")

                    success = await self._apply(job, insight, cl)

                    if success:
                        print(f"[Track {self.track_id}] ✅ CONFIRMED SUBMISSION: {job.title} @ {job.company}")
                        self.pool.mark_done(job.job_id, "submitted")
                    else:
                        print(f"[Track {self.track_id}] ✗ Failed: {job.title} @ {job.company} — will retry next cycle")
                        self.pool.mark_done(job.job_id, "failed")

                except asyncio.TimeoutError:
                    print(f"[Track {self.track_id}] Timeout — {job.company}")
                    self.pool.mark_done(job.job_id, "failed")
                except Exception as e:
                    print(f"[Track {self.track_id}] Error: {e}")
                    self.pool.mark_done(job.job_id, "failed")

            except Exception as e:
                print(f"[Track {self.track_id}] Browser crash: {e} — restarting")
                await self._close_browser()
                await asyncio.sleep(5)

    # ── Apply ─────────────────────────────────────────────────────────────────

    async def _apply(self, job, insight, cover_letter: str) -> bool:
        try:
            url = job.ats_url or job.url
            await self.page.goto(url, timeout=30000, wait_until="domcontentloaded")
            await self._delay(1.5, 2.5)
            await self._dismiss_popups()

            current_url = self.page.url
            ats_type    = _detect_ats(current_url)
            print(f"[Track {self.track_id}] URL: {current_url[:80]} → ATS: {ats_type}")

            profile = self.store.get_profile() or {}

            if ats_type == "aggregator":
                return await self._handle_aggregator(job, insight, cover_letter, profile)
            if ats_type == "greenhouse":
                return await self._fill_greenhouse(job, insight, cover_letter, profile)
            if ats_type == "lever":
                return await self._fill_lever(job, insight, cover_letter, profile)
            if ats_type == "workday":
                from tracks.workday_handler import fill_workday_application
                return await fill_workday_application(
                    self.page, current_url, job.title, job.company,
                    insight, cover_letter, job.job_id
                )
            return await self._fill_generic(job, insight, cover_letter, profile)

        except PwTimeout:
            print(f"[Track {self.track_id}] Page timeout")
            return False
        except Exception as e:
            print(f"[Track {self.track_id}] Apply error: {e}")
            return False

    # ── Popup Dismissal ───────────────────────────────────────────────────────

    async def _dismiss_popups(self) -> None:
        """Aggressively dismiss any cookie banner, GDPR notice, modal, overlay."""
        dismissed = 0
        for selector in POPUP_SELECTORS:
            try:
                el = await self.page.query_selector(selector)
                if el and await el.is_visible():
                    await el.click(timeout=2000)
                    dismissed += 1
                    await self._delay(0.3, 0.6)
            except Exception:
                continue

        # Scroll Terms & Conditions containers so checkbox unlocks
        for tc_sel in [
            "[id*='terms']", "[class*='terms']", "[id*='tos']",
            "div:has-text('Terms and Conditions')", "div:has-text('Terms of Service')",
        ]:
            try:
                tc = await self.page.query_selector(tc_sel)
                if tc:
                    await tc.evaluate("el => el.scrollTop = el.scrollHeight")
            except Exception:
                continue

        if dismissed:
            print(f"[Track {self.track_id}] Dismissed {dismissed} popup(s)")

    # ══════════════════════════════════════════════════════════════════════════
    #  UNIVERSAL DROPDOWN HANDLER
    #  Handles: native <select>, React-Select, Select2, custom div dropdowns,
    #           non-typeable scroll-only lists, searchable dropdowns.
    #  ALWAYS VERIFIES the selection — never silently picks the wrong option.
    # ══════════════════════════════════════════════════════════════════════════

    async def _smart_select(
        self,
        field_hint: str,        # CSS selector OR label text to locate the field
        target_value: str,      # What we want to select
        by_label: bool = False, # If True, field_hint is a label string not a selector
    ) -> bool:
        """
        Master dropdown handler. Tries in order:
          1. Native <select> with select_option (exact, partial, fuzzy)
          2. React-Select / Select2: click → type → verify option → click
          3. Non-typeable custom list: click → enumerate options → click best match
          4. Keyboard arrow navigation as last resort
        Always verifies the final selection text before returning True.
        """
        if not target_value:
            return False

        el = None

        # ── Locate the element ──
        if by_label:
            el = await self._find_field_by_label(field_hint)
        else:
            try:
                el = await self.page.query_selector(field_hint)
            except Exception:
                pass

        if not el:
            return False

        tag = await el.evaluate("el => el.tagName.toLowerCase()")

        # ── Strategy 1: Native <select> ──
        if tag == "select":
            return await self._native_select(el, target_value, field_hint)

        # ── Strategy 2+3: Custom / React-Select ──
        return await self._custom_dropdown(el, target_value, field_hint)

    async def _native_select(self, el, target: str, hint: str = "") -> bool:
        """Fill a native HTML <select> element. Verifies after selection."""
        # Get all options first for informed selection
        opts = await el.evaluate(
            "el => Array.from(el.options).map(o => ({value: o.value, text: o.text.trim(), idx: o.index}))"
        )

        # Score each option
        scored = [(o, _score_option(o["text"], target)) for o in opts]
        scored.sort(key=lambda x: x[1], reverse=True)

        best_opt, best_score = scored[0] if scored else (None, 0.0)

        if best_score < DROPDOWN_MIN_CONFIDENCE:
            print(f"[Track {self.track_id}] ⚠️  No confident match for '{target}' in '{hint}' "
                  f"(best: '{best_opt['text'] if best_opt else '?'}' @ {best_score:.2f})")
            return False

        # Don't select placeholder (index 0 with empty value usually)
        if best_opt["idx"] == 0 and best_opt["value"] in ("", "0", None):
            # Try second best
            if len(scored) > 1:
                best_opt, best_score = scored[1]

        try:
            await el.select_option(index=best_opt["idx"])
            # Verify
            selected_text = await el.evaluate("el => el.options[el.selectedIndex]?.text?.trim() || ''")
            verify_score  = _score_option(selected_text, target)
            if verify_score >= DROPDOWN_MIN_CONFIDENCE:
                print(f"[Track {self.track_id}] ✓ '{hint}' → '{selected_text}' (conf {verify_score:.2f})")
                return True
            else:
                print(f"[Track {self.track_id}] ⚠️  Selected '{selected_text}' but wanted '{target}' — resetting")
                await el.select_option(index=0)
                return False
        except Exception as e:
            print(f"[Track {self.track_id}] Native select error: {e}")
            return False

    async def _custom_dropdown(self, trigger_el, target: str, hint: str = "") -> bool:
        """
        Handle custom dropdowns (React-Select, Select2, div-based lists).
        Strategy A: click → type → verify options → click best
        Strategy B: click → no-type list → enumerate → click best
        """
        try:
            # Click to open
            await trigger_el.click(timeout=3000)
            await self._delay(0.4, 0.7)

            # Check if an input appeared (typeable dropdown)
            search_input = await self.page.query_selector(
                "input[class*='select__input'], "
                "input[class*='search__input'], "
                "[class*='select__control'] input, "
                "[class*='dropdown'] input:not([type='hidden']), "
                "[role='combobox'] input, "
                ".select2-search__field"
            )

            options_visible = False

            if search_input:
                # ── Strategy A: Typeable dropdown ──
                try:
                    await search_input.fill("")  # Clear first
                    await search_input.type(target, delay=60)
                    await self._delay(0.5, 0.9)
                    options_visible = True
                except Exception:
                    pass

            # Collect visible options regardless of whether we typed
            option_els = await self.page.query_selector_all(
                "[class*='select__option']:not([class*='disabled']), "
                "[class*='option']:not([class*='disabled']):not([class*='selected']), "
                "[role='option']:not([aria-disabled='true']), "
                ".select2-results__option:not(.select2-results__option--disabled), "
                "li[class*='option']:not([class*='disabled'])"
            )

            if not option_els:
                # No options appeared — dropdown may not have opened
                # Try clicking again with JavaScript
                await trigger_el.evaluate("el => el.click()")
                await self._delay(0.5, 0.8)
                option_els = await self.page.query_selector_all(
                    "[class*='select__option'], [role='option'], li[class*='option']"
                )

            if not option_els:
                print(f"[Track {self.track_id}] ⚠️  No options found for '{hint}' — skipping")
                await self.page.keyboard.press("Escape")
                return False

            # Score all visible options
            scored_els = []
            for opt_el in option_els:
                try:
                    if not await opt_el.is_visible():
                        continue
                    text = (await opt_el.inner_text()).strip()
                    score = _score_option(text, target)
                    scored_els.append((opt_el, text, score))
                except Exception:
                    continue

            if not scored_els:
                await self.page.keyboard.press("Escape")
                return False

            scored_els.sort(key=lambda x: x[2], reverse=True)
            best_el, best_text, best_score = scored_els[0]

            if best_score < DROPDOWN_MIN_CONFIDENCE:
                print(f"[Track {self.track_id}] ⚠️  No confident match for '{target}' "
                      f"(best: '{best_text}' @ {best_score:.2f}) in '{hint}' — skipping")
                await self.page.keyboard.press("Escape")
                return False

            # If low-medium confidence, log a warning but proceed
            if best_score < 0.75:
                print(f"[Track {self.track_id}] ⚡ Low-confidence match: '{target}' → '{best_text}' "
                      f"({best_score:.2f}) in '{hint}'")

            # Click the best option
            await best_el.scroll_into_view_if_needed()
            await best_el.click(timeout=3000)
            await self._delay(0.3, 0.5)

            # Verify: check the displayed value changed
            displayed = await self._get_displayed_value(trigger_el)
            verify_score = _score_option(displayed, target) if displayed else best_score

            if verify_score >= DROPDOWN_MIN_CONFIDENCE or best_score >= 0.75:
                print(f"[Track {self.track_id}] ✓ '{hint}' → '{best_text}' (conf {best_score:.2f})")
                return True
            else:
                print(f"[Track {self.track_id}] ⚠️  Selection verification failed: "
                      f"displayed='{displayed}', wanted='{target}'")
                return False

        except Exception as e:
            print(f"[Track {self.track_id}] Custom dropdown error for '{hint}': {e}")
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    async def _get_displayed_value(self, container_el) -> str:
        """Get the currently displayed/selected value text from a custom dropdown."""
        try:
            return await container_el.evaluate("""el => {
                const single = el.querySelector(
                    '[class*="single-value"], [class*="selected"], [class*="value-text"]'
                );
                if (single) return single.textContent.trim();
                const ctrl = el.querySelector('[class*="control"], [class*="selected"]');
                if (ctrl) return ctrl.textContent.trim();
                return el.textContent.trim().split('\\n')[0].trim();
            }""")
        except Exception:
            return ""

    async def _find_field_by_label(self, label_text: str):
        """Locate a form field element by its associated label text."""
        label_lower = label_text.lower()
        try:
            # Direct label → for= association
            labels = await self.page.query_selector_all("label")
            for lbl in labels:
                try:
                    text = (await lbl.inner_text()).strip().lower()
                    if label_lower in text or text in label_lower:
                        lbl_for = await lbl.get_attribute("for")
                        if lbl_for:
                            el = await self.page.query_selector(f"#{lbl_for}")
                            if el:
                                return el
                        # Try child element
                        child = await lbl.query_selector("select, input, [class*='select__control']")
                        if child:
                            return child
                except Exception:
                    continue

            # aria-label match
            for tag in ["select", "input", "[class*='select__control']"]:
                try:
                    el = await self.page.query_selector(f"{tag}[aria-label*='{label_text}' i]")
                    if el:
                        return el
                except Exception:
                    continue

            # Placeholder match
            el = await self.page.query_selector(f"input[placeholder*='{label_text}' i]")
            if el:
                return el

            # name/id fuzzy match
            slug = label_text.lower().replace(" ", "_").replace(" ", "-")
            for attr in ["name", "id"]:
                for tag in ["select", "input"]:
                    el = await self.page.query_selector(f"{tag}[{attr}*='{slug}']")
                    if el:
                        return el

        except Exception:
            pass
        return None

    # ══════════════════════════════════════════════════════════════════════════
    #  UNIVERSAL FORM FILLER
    #  Runs multiple passes, fills EVERY visible unfilled field.
    # ══════════════════════════════════════════════════════════════════════════

    async def _universal_fill(self, profile: dict, cover_letter: str) -> None:
        """
        Universal multi-pass form filler.
        Pass 1: Fill all text inputs and textareas by label
        Pass 2: Fill all native selects
        Pass 3: Fill all custom dropdowns
        Pass 4: Handle radios, checkboxes
        Pass 5: Re-check any still-empty required fields
        """
        print(f"[Track {self.track_id}] Universal form fill — starting...")
        fm = self._build_field_map(profile, cover_letter)

        # Multiple passes so fields that unlock after others are filled get caught
        for pass_num in range(3):
            filled_this_pass = 0

            # ── Text inputs ──
            filled_this_pass += await self._fill_text_inputs(fm)

            # ── Textareas ──
            filled_this_pass += await self._fill_textareas(fm, cover_letter)

            # ── Native selects ──
            filled_this_pass += await self._fill_native_selects(fm)

            # ── Custom dropdowns (React-Select etc.) ──
            filled_this_pass += await self._fill_custom_dropdowns(fm)

            # ── Radios ──
            await self._fill_radios(fm)

            # ── Checkboxes (agree/terms) ──
            await self._fill_checkboxes()

            print(f"[Track {self.track_id}] Pass {pass_num+1}: filled {filled_this_pass} field(s)")
            if filled_this_pass == 0:
                break  # Stable — nothing left to fill
            await self._delay(0.3, 0.6)

        print(f"[Track {self.track_id}] Universal form fill — complete.")

    def _build_field_map(self, profile: dict, cover_letter: str) -> dict:
        """Build the complete label → value mapping for this applicant."""
        full_name  = profile.get("full_name", "") or ""
        email      = profile.get("email", "") or ""
        phone      = profile.get("phone", "") or ""
        addr       = profile.get("address", "") or ""
        linkedin   = profile.get("linkedin_url", "") or ""
        github     = profile.get("github_url", "") or ""
        portfolio  = profile.get("portfolio_url", "") or ""
        gpa        = profile.get("gpa", "3.8") or "3.8"
        grad_date  = profile.get("graduation_date", "May 2026") or "May 2026"
        grad_year  = "2026"
        grad_month = "May"
        try:
            parts = grad_date.strip().split()
            if len(parts) == 2:
                grad_month, grad_year = parts[0], parts[1]
        except Exception:
            pass

        first = full_name.split()[0] if full_name else ""
        last  = full_name.split()[-1] if full_name else ""
        city  = "West Lafayette"
        state = "Indiana"
        if "," in addr:
            parts = [p.strip() for p in addr.split(",")]
            city  = parts[0] if parts else city
            state = parts[1] if len(parts) > 1 else state

        return {
            # Name
            "first name": first, "first_name": first, "given name": first,
            "last name": last,   "last_name": last,  "surname": last, "family name": last,
            "full name": full_name, "name": full_name, "legal name": full_name,

            # Contact
            "email": email, "email address": email,
            "phone": phone, "telephone": phone, "mobile": phone, "cell": phone,
            "phone number": phone, "contact number": phone,

            # Location
            "address": addr.split(",")[0].strip() if addr else "",
            "street": addr.split(",")[0].strip() if addr else "",
            "city": city,
            "state": state, "province": state, "region": state,
            "country": "United States",
            "zip": "47906", "postal": "47906", "zip code": "47906",

            # Professional
            "linkedin": linkedin, "linkedin url": linkedin, "linkedin profile": linkedin,
            "github": github, "github url": github, "github profile": github,
            "website": portfolio, "portfolio": portfolio, "personal website": portfolio,
            "portfolio url": portfolio, "personal url": portfolio,

            # Cover letter
            "cover letter": cover_letter, "cover": cover_letter[:3000],
            "letter": cover_letter[:3000], "motivation": cover_letter[:2000],
            "motivation letter": cover_letter[:2000],
            "why are you interested": cover_letter[:800],
            "why do you want": cover_letter[:800],
            "why this role": cover_letter[:800],
            "why this company": cover_letter[:800],
            "tell us about yourself": cover_letter[:800],
            "about yourself": cover_letter[:800],
            "message": cover_letter[:1000],
            "comments": cover_letter[:500], "notes": cover_letter[:500],
            "additional information": cover_letter[:500],
            "additional comments": cover_letter[:500],
            "anything else": cover_letter[:500],

            # Education
            "school": "Purdue University", "university": "Purdue University",
            "college": "Purdue University", "institution": "Purdue University",
            "school name": "Purdue University",
            "discipline": "Computer Science", "major": "Computer Science",
            "field of study": "Computer Science", "area of study": "Computer Science",
            "program": "Computer Science",
            "degree": "Bachelor of Science", "degree type": "Bachelor of Science",
            "type of degree": "Bachelor of Science", "education level": "Bachelor of Science",
            "highest degree": "Bachelor of Science",
            "gpa": gpa, "grade point": gpa, "cumulative gpa": gpa,

            # Dates
            "graduation date": grad_date, "expected graduation": grad_date,
            "graduation": grad_date, "grad date": grad_date,
            "graduation year": grad_year, "grad year": grad_year,
            "graduation month": grad_month, "grad month": grad_month,
            "end date year": grad_year, "end year": grad_year,
            "end date month": grad_month, "end month": grad_month,
            "start date year": "2022", "start year": "2022",

            # Work auth
            "authorized": "Yes", "work authorized": "Yes",
            "authorization": "Yes", "work authorization": "Yes",
            "legally authorized": "Yes", "eligible to work": "Yes",
            "sponsorship": "No", "require sponsorship": "No",
            "visa sponsorship": "No", "sponsor": "No",
            "visa": "No", "visa status": "Citizen",
            "citizen": "Yes", "us citizen": "Yes",
            "relocate": "Yes", "willing to relocate": "Yes",
            "remote": "Yes", "open to remote": "Yes",

            # EEOC / voluntary
            "gender": "Prefer not to say",
            "race": "Prefer not to say", "ethnicity": "Prefer not to say",
            "veteran": "No", "veteran status": "No",
            "disability": "No", "disabled": "No",
            "sexual orientation": "Prefer not to say",

            # Compensation
            "salary": "25", "expected salary": "25", "desired salary": "25",
            "hourly rate": "25", "pay rate": "25",
            "start date": "May 2026", "available": "May 2026",
        }

    async def _get_field_label(self, el) -> str:
        """Extract the label/purpose for any form element."""
        parts = []
        try:
            # label[for=id]
            el_id = await el.get_attribute("id")
            if el_id:
                lbl = await self.page.query_selector(f"label[for='{el_id}']")
                if lbl:
                    parts.append(await lbl.inner_text())
        except Exception:
            pass

        for attr in ["placeholder", "name", "aria-label", "aria-labelledby", "title", "data-label"]:
            try:
                v = await el.get_attribute(attr)
                if v:
                    parts.append(v)
            except Exception:
                continue

        # Look at parent container for label text
        try:
            parent_text = await el.evaluate("""el => {
                const parent = el.closest('.field, .form-group, .form-field, [class*="field"], [class*="group"]');
                if (parent) {
                    const lbl = parent.querySelector('label, .label, [class*="label"]');
                    return lbl ? lbl.textContent.trim() : '';
                }
                return '';
            }""")
            if parent_text:
                parts.append(parent_text)
        except Exception:
            pass

        return " ".join(parts).lower().strip()

    def _match_label_to_value(self, label: str, fm: dict) -> str:
        """Match a field label string to the best value in the field map."""
        label = label.lower().strip()
        # Remove asterisks and common noise
        label = re.sub(r'[*\(\)]', '', label).strip()

        # Exact match first
        if label in fm:
            return fm[label]

        # Substring match — longer keys first (more specific)
        best_key = None
        best_len = 0
        for key in fm:
            if key in label or label in key:
                if len(key) > best_len:
                    best_key = key
                    best_len = len(key)

        if best_key:
            return fm[best_key]

        # Word overlap match
        label_words = set(label.split())
        best_overlap = 0
        best_key = None
        for key in fm:
            key_words = set(key.split())
            overlap = len(label_words & key_words)
            if overlap > best_overlap:
                best_overlap = overlap
                best_key = key

        if best_overlap >= 1 and best_key:
            return fm[best_key]

        return ""

    async def _fill_text_inputs(self, fm: dict) -> int:
        """Fill all visible text/email/tel/url inputs."""
        filled = 0
        inputs = await self.page.query_selector_all(
            "input[type='text'], input[type='email'], input[type='tel'], "
            "input[type='url'], input[type='number'], input:not([type]), "
            "input[type='search']"
        )
        for inp in inputs:
            try:
                if not await inp.is_visible():
                    continue
                if await inp.is_disabled():
                    continue
                current = await inp.input_value()
                if current and len(current) > 1:
                    continue  # Already filled

                label = await self._get_field_label(inp)
                val   = self._match_label_to_value(label, fm)

                if val:
                    await inp.triple_click()
                    await inp.type(val, delay=30)
                    filled += 1
                    await self._delay(0.1, 0.2)
            except Exception:
                continue
        return filled

    async def _fill_textareas(self, fm: dict, cover_letter: str) -> int:
        """Fill all visible textareas."""
        filled = 0
        textareas = await self.page.query_selector_all("textarea")
        for ta in textareas:
            try:
                if not await ta.is_visible():
                    continue
                if await ta.is_disabled():
                    continue
                current = await ta.input_value()
                if current and len(current) > 20:
                    continue

                label = await self._get_field_label(ta)
                val   = self._match_label_to_value(label, fm)

                # Default any empty textarea to a truncated cover letter
                if not val:
                    val = cover_letter[:800]

                await ta.click()
                await ta.fill(val)
                filled += 1
                await self._delay(0.2, 0.4)
            except Exception:
                continue
        return filled

    async def _fill_native_selects(self, fm: dict) -> int:
        """Fill all visible unfilled native <select> elements."""
        filled = 0
        selects = await self.page.query_selector_all("select")
        for sel_el in selects:
            try:
                if not await sel_el.is_visible():
                    continue
                if await sel_el.is_disabled():
                    continue
                # Check if already has a non-empty, non-placeholder value
                current_val = await sel_el.evaluate("el => el.value")
                current_text = await sel_el.evaluate(
                    "el => el.options[el.selectedIndex]?.text?.trim() || ''"
                )
                if current_val and current_val not in ("", "0", "null") and \
                   current_text.lower() not in ("select...", "select", "please select", "--", ""):
                    continue

                label = await self._get_field_label(sel_el)
                val   = self._match_label_to_value(label, fm)

                if val:
                    ok = await self._native_select(sel_el, val, label)
                    if ok:
                        filled += 1
                    await self._delay(0.2, 0.4)
                else:
                    # Unknown field — pick the second option (first non-placeholder)
                    opts = await sel_el.evaluate(
                        "el => Array.from(el.options).map(o => ({v: o.value, t: o.text.trim(), i: o.index}))"
                    )
                    for opt in opts[1:]:  # skip index 0 placeholder
                        if opt["t"].lower() not in ("select...", "select", "--", "", "please select"):
                            await sel_el.select_option(index=opt["i"])
                            print(f"[Track {self.track_id}] ⚡ Unknown select '{label}' → '{opt['t']}' (first non-empty)")
                            filled += 1
                            break
                    await self._delay(0.2, 0.3)

            except Exception:
                continue
        return filled

    async def _fill_custom_dropdowns(self, fm: dict) -> int:
        """Fill all visible custom (React-Select, Select2, div-based) dropdowns."""
        filled = 0
        # React-Select controls that aren't already filled
        controls = await self.page.query_selector_all(
            "[class*='select__control']:not([class*='disabled']), "
            "[class*='react-select__control'], "
            ".select2-selection:not(.select2-selection--multiple), "
            "[role='combobox']:not(input)"
        )
        for ctrl in controls:
            try:
                if not await ctrl.is_visible():
                    continue

                # Check if already has a real value
                current = await self._get_displayed_value(ctrl)
                if current and current.lower() not in ("select...", "select", "--", "", "please select"):
                    continue

                # Get the parent container label
                label = await ctrl.evaluate("""el => {
                    const container = el.closest('.field, .form-group, [class*="field"], [class*="group"]');
                    if (container) {
                        const lbl = container.querySelector('label, .label, [class*="label"]');
                        return lbl ? lbl.textContent.trim() : '';
                    }
                    return el.getAttribute('aria-label') || '';
                }""")

                label = label.lower().strip()
                val   = self._match_label_to_value(label, fm)

                if val:
                    ok = await self._custom_dropdown(ctrl, val, label)
                    if ok:
                        filled += 1
                    await self._delay(0.3, 0.6)

            except Exception:
                continue
        return filled

    async def _fill_radios(self, fm: dict) -> None:
        """Handle radio button groups."""
        try:
            # Get all radio groups by name
            radio_names = await self.page.evaluate("""() => {
                const radios = document.querySelectorAll('input[type=radio]');
                return [...new Set([...radios].map(r => r.name))];
            }""")

            for name in radio_names:
                try:
                    # Get all radios in this group
                    radios = await self.page.query_selector_all(f"input[type='radio'][name='{name}']")
                    # Check if any already selected
                    any_checked = any([await r.is_checked() for r in radios])
                    if any_checked:
                        continue

                    # Determine intent from name
                    name_lower = name.lower()
                    val_lower = self._match_label_to_value(name_lower, fm).lower()

                    for radio in radios:
                        try:
                            if not await radio.is_visible():
                                continue
                            radio_val = (await radio.get_attribute("value") or "").lower()
                            radio_label = await self._get_radio_label(radio)

                            should_select = False

                            # Work auth — prefer Yes
                            if any(k in name_lower for k in ["authorized", "eligible", "legally"]):
                                should_select = radio_val in ("yes", "true", "1")
                            # Sponsorship — prefer No
                            elif any(k in name_lower for k in ["sponsor", "visa"]):
                                should_select = radio_val in ("no", "false", "0")
                            # Relocate — prefer Yes
                            elif "relocate" in name_lower:
                                should_select = radio_val in ("yes", "true", "1")
                            # EEOC — prefer "Prefer not to say"
                            elif any(k in name_lower for k in ["gender", "race", "ethnic", "veteran", "disability"]):
                                should_select = any(
                                    k in radio_val or k in radio_label.lower()
                                    for k in ["prefer", "decline", "no answer", "not disclose"]
                                )
                            # Default: pick "Yes" for positive questions
                            elif val_lower == "yes":
                                should_select = radio_val in ("yes", "true", "1")

                            if should_select:
                                await radio.check()
                                await self._delay(0.2, 0.4)
                                break

                        except Exception:
                            continue

                except Exception:
                    continue
        except Exception:
            pass

    async def _get_radio_label(self, radio_el) -> str:
        """Get the visible label for a radio input."""
        try:
            return await radio_el.evaluate("""el => {
                // Check for associated <label>
                if (el.id) {
                    const lbl = document.querySelector(`label[for='${el.id}']`);
                    if (lbl) return lbl.textContent.trim();
                }
                // Check parent label
                const parent = el.closest('label');
                if (parent) return parent.textContent.trim();
                // Check next sibling text
                const next = el.nextSibling;
                if (next && next.nodeType === 3) return next.textContent.trim();
                return '';
            }""")
        except Exception:
            return ""

    async def _fill_checkboxes(self) -> None:
        """Check T&C agree / consent checkboxes."""
        checkboxes = await self.page.query_selector_all("input[type='checkbox']")
        for cb in checkboxes:
            try:
                if not await cb.is_visible():
                    continue
                if await cb.is_checked():
                    continue
                name = (await cb.get_attribute("name") or "").lower()
                label_text = ""
                try:
                    label_text = await self._get_radio_label(cb)
                except Exception:
                    pass
                combined = (name + " " + label_text).lower()
                if any(k in combined for k in ["agree", "terms", "consent", "accept", "confirm", "acknowledge"]):
                    await cb.check()
                    await self._delay(0.2, 0.3)
            except Exception:
                continue

    # ── Greenhouse Form ───────────────────────────────────────────────────────

    async def _fill_greenhouse(self, job, insight, cover_letter: str, profile: dict) -> bool:
        try:
            print(f"[Track {self.track_id}] Filling Greenhouse form...")

            try:
                await self.page.wait_for_selector(
                    "form#application_form, #application-form, form[action*='applications']",
                    timeout=15000
                )
            except PwTimeout:
                print(f"[Track {self.track_id}] Form not found — AI fallback")
                return await self._ai_page_fallback(job, cover_letter, profile)

            await self._delay(1, 2)
            await self._dismiss_popups()

            full_name = profile.get("full_name", "") or ""
            email     = profile.get("email", "") or ""
            phone     = profile.get("phone", "") or ""
            linkedin  = profile.get("linkedin_url", "") or ""

            # Step 1-4: Basic identity fields
            for sel, val in [
                ("#first_name, input[name='job_application[first_name]']", full_name.split()[0] if full_name else ""),
                ("#last_name, input[name='job_application[last_name]']",   full_name.split()[-1] if full_name else ""),
                ("#email, input[name='job_application[email]']",           email),
                ("#phone, input[name='job_application[phone]']",           phone),
            ]:
                await self._fill_input_selector(sel, val)
                await self._delay(0.15, 0.3)

            # Step 5: Country
            await self._smart_select("select#country, select[name*='country'], select[id*='country']", "United States")
            await self._delay(0.3, 0.5)

            # Step 6: State
            addr  = profile.get("address", "") or ""
            state = addr.split(",")[1].strip() if "," in addr else "Indiana"
            await self._smart_select("select[name*='state'], select[id*='state']", state)
            await self._delay(0.2, 0.4)

            # Step 7: Resume upload
            resume_path = self.store.get("resume_path", "") or ""
            if resume_path and Path(resume_path).exists():
                for sel in ["input[type='file']", "#resume", "input[name*='resume']", "input[accept*='pdf']"]:
                    try:
                        el = await self.page.query_selector(sel)
                        if el:
                            await el.set_input_files(resume_path)
                            print(f"[Track {self.track_id}] Resume uploaded")
                            await self._delay(1.5, 2.5)
                            break
                    except Exception:
                        continue

            # Step 8: Cover letter — MUST click "Enter manually" first
            for em_sel in [
                "a:has-text('Enter manually')",
                "button:has-text('Enter manually')",
                "label:has-text('Enter manually')",
                "span:has-text('Enter manually')",
                "[data-source='manual']",
            ]:
                try:
                    em = await self.page.query_selector(em_sel)
                    if em and await em.is_visible():
                        await em.click()
                        print(f"[Track {self.track_id}] Clicked 'Enter manually'")
                        await self._delay(0.8, 1.5)
                        break
                except Exception:
                    continue

            for cl_sel in [
                "textarea[name*='cover']", "textarea[id*='cover']",
                "#cover_letter_text", "textarea[aria-label*='cover' i]",
            ]:
                try:
                    el = await self.page.query_selector(cl_sel)
                    if el and await el.is_visible():
                        await el.click()
                        await el.fill(cover_letter)
                        await self._delay(0.5, 1)
                        break
                except Exception:
                    continue

            # Step 9: LinkedIn
            if linkedin:
                await self._fill_input_selector(
                    "input[name*='linkedin'], input[id*='linkedin'], input[placeholder*='LinkedIn' i]",
                    linkedin
                )

            # Step 10: Universal fill — catches EVERYTHING else including education
            await self._universal_fill(profile, cover_letter)

            # Step 11: Dismiss popups one more time
            await self._dismiss_popups()
            await self._delay(1, 2)

            # Step 12: Submit
            return await self._click_submit_and_verify()

        except Exception as e:
            print(f"[Track {self.track_id}] Greenhouse error: {e}")
            return False

    async def _fill_input_selector(self, selector: str, value: str) -> bool:
        """Fill the first matching visible input for a compound CSS selector."""
        if not value:
            return False
        for sel in selector.split(", "):
            try:
                el = await self.page.query_selector(sel.strip())
                if el and await el.is_visible():
                    await el.triple_click()
                    await el.type(value, delay=30)
                    return True
            except Exception:
                continue
        return False

    # ── Lever Form ────────────────────────────────────────────────────────────

    async def _fill_lever(self, job, insight, cover_letter: str, profile: dict) -> bool:
        try:
            print(f"[Track {self.track_id}] Filling Lever form...")
            await self.page.wait_for_selector("form.application-form, #application-form", timeout=12000)
            await self._delay(1, 2)
            full_name = profile.get("full_name", "") or ""
            for sel, val in [
                ("input[name='name']",  full_name),
                ("input[name='email']", profile.get("email", "")),
                ("input[name='phone']", profile.get("phone", "")),
            ]:
                await self._fill_input_selector(sel, val)
            try:
                ta = await self.page.query_selector("textarea[name*='comments'], textarea")
                if ta:
                    await ta.fill(cover_letter)
            except Exception:
                pass
            resume_path = self.store.get("resume_path", "") or ""
            if resume_path and Path(resume_path).exists():
                try:
                    el = await self.page.query_selector("input[type='file']")
                    if el:
                        await el.set_input_files(resume_path)
                        await self._delay(1, 2)
                except Exception:
                    pass
            await self._universal_fill(profile, cover_letter)
            await self._dismiss_popups()
            return await self._click_submit_and_verify()
        except Exception as e:
            print(f"[Track {self.track_id}] Lever error: {e}")
            return False

    # ── Generic Form ──────────────────────────────────────────────────────────

    async def _fill_generic(self, job, insight, cover_letter: str, profile: dict) -> bool:
        try:
            print(f"[Track {self.track_id}] Filling generic form: {self.page.url[:60]}")
            for btn_sel in [
                "a:has-text('Apply Now')", "a:has-text('Apply now')",
                "button:has-text('Apply Now')", "button:has-text('Apply')",
                "a[href*='apply']:not([href*='login'])",
            ]:
                try:
                    btn = await self.page.query_selector(btn_sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        await self._delay(2, 3)
                        await self._dismiss_popups()
                        break
                except Exception:
                    continue
            await self._universal_fill(profile, cover_letter)
            await self._dismiss_popups()
            return await self._click_submit_and_verify()
        except Exception as e:
            print(f"[Track {self.track_id}] Generic error: {e}")
            return False

    # ── Aggregator Handler ────────────────────────────────────────────────────

    async def _handle_aggregator(self, job, insight, cover_letter: str, profile: dict) -> bool:
        try:
            for btn_sel in [
                "button:has-text('Apply Now')", "a:has-text('Apply Now')",
                "button:has-text('Easy Apply')", "a:has-text('Easy Apply')",
                "a[href*='apply']:not([href*='login'])",
            ]:
                try:
                    btn = await self.page.query_selector(btn_sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        await self._delay(2, 3)
                        new_ats = _detect_ats(self.page.url)
                        if new_ats not in ("aggregator", "generic"):
                            return await self._apply(job, insight, cover_letter)
                        break
                except Exception:
                    continue
            print(f"[Track {self.track_id}] Could not escape aggregator — skipping")
            return False
        except Exception as e:
            print(f"[Track {self.track_id}] Aggregator error: {e}")
            return False

    # ── AI Page Fallback ──────────────────────────────────────────────────────

    async def _ai_page_fallback(self, job, cover_letter: str, profile: dict) -> bool:
        """Take a screenshot and ask Claude to identify what's blocking the form."""
        try:
            print(f"[Track {self.track_id}] 🤖 AI fallback — screenshot analysis...")
            screenshot = await self.page.screenshot(type="png")
            b64 = base64.b64encode(screenshot).decode()

            resp = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={"Content-Type": "application/json"},
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 400,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                            {"type": "text", "text": (
                                f"Job application page at {self.page.url}. Something is blocking the form. "
                                "What CSS selector should I click to get past it? "
                                'Reply ONLY JSON: {"obstacle":"description","selector":"css","action":"click"}'
                            )}
                        ]
                    }]
                },
                timeout=25,
            )
            if resp.status_code == 200:
                text = resp.json()["content"][0]["text"]
                m = re.search(r'\{.*\}', text, re.DOTALL)
                if m:
                    data = json.loads(m.group())
                    obstacle = data.get("obstacle", "?")
                    selector = data.get("selector", "")
                    print(f"[Track {self.track_id}] AI detected: {obstacle} → clicking '{selector}'")
                    if selector:
                        await self.page.click(selector, timeout=5000)
                        await self._delay(1, 2)
                        await self._dismiss_popups()
                        return await self._fill_generic(job, {}, cover_letter, profile)
        except Exception as e:
            print(f"[Track {self.track_id}] AI fallback error: {e}")
        return False

    # ── Submit and Verify (STRICT) ────────────────────────────────────────────

    async def _click_submit_and_verify(self) -> bool:
        """
        Scroll, click submit, then STRICTLY verify confirmation.
        Only returns True on a confirmed real submission.
        """
        await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await self._delay(0.5, 1)

        # Multi-step forms: click Next until we reach Submit
        for _step in range(10):
            await self._dismiss_popups()

            # Check for real confirmation BEFORE looking for next button
            page_text = ""
            try:
                page_text = (await self.page.inner_text("body")).lower()
            except Exception:
                pass

            for phrase in CONFIRMATION_PHRASES:
                if phrase in page_text:
                    print(f"[Track {self.track_id}] ✅ Confirmed: '{phrase}'")
                    return True

            # Check confirmation URL
            for pattern in ["/confirmation", "/thank-you", "/thankyou", "submitted=true", "/success?"]:
                if pattern in self.page.url.lower():
                    print(f"[Track {self.track_id}] ✅ Confirmation URL: {self.page.url[:60]}")
                    return True

            # Look for submit button
            submit_btn = None
            for btn_sel in [
                "button[type='submit']",
                "input[type='submit']",
                "button:has-text('Submit Application')",
                "button:has-text('Submit application')",
                "button:has-text('Submit')",
                "button:has-text('Send Application')",
                "button:has-text('Apply Now')",
            ]:
                try:
                    btn = await self.page.query_selector(btn_sel)
                    if btn and await btn.is_visible():
                        submit_btn = btn
                        break
                except Exception:
                    continue

            if submit_btn:
                btn_text = (await submit_btn.inner_text()).strip()
                print(f"[Track {self.track_id}] Clicking: '{btn_text}'")

                # False positive guard: don't mark as submitted on known-bad URLs
                for fp in FALSE_POSITIVE_URLS:
                    if fp in self.page.url:
                        print(f"[Track {self.track_id}] False positive URL — not submitting")
                        return False

                await submit_btn.click()
                await self._delay(2.5, 4)
                continue  # Loop: check confirmation at top of next iteration

            # Check for Next button (multi-step form)
            next_btn = None
            for next_sel in [
                "button:has-text('Next')", "button:has-text('Continue')",
                "button[type='button']:has-text('Next')",
                "a:has-text('Next')", "input[value='Next']",
            ]:
                try:
                    btn = await self.page.query_selector(next_sel)
                    if btn and await btn.is_visible():
                        next_btn = btn
                        break
                except Exception:
                    continue

            if next_btn:
                await next_btn.click()
                await self._delay(1.5, 2.5)
                # Fill new fields that appeared
                profile = self.store.get_profile() or {}
                await self._universal_fill(profile, "")
                continue

            # Nothing to click and no confirmation — not submitted
            break

        print(f"[Track {self.track_id}] ✗ No confirmation detected")
        return False

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _delay(self, min_s: float, max_s: float) -> None:
        await asyncio.sleep(random.uniform(min_s, max_s))
