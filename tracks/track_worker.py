"""
tracks/track_worker.py — DEFINITIVE VERSION
Key changes from previous:
  - Dropdown: search whole document for options after opening, not just inside container
  - Dropdown: keyboard arrow navigation fallback for stubborn selects
  - Submit loop: detects validation errors immediately and stops — no more 10x clicking
  - Education section: explicit targeted pass for every Greenhouse education field
  - Country/Phone: reliable click+type with verification loop
  - Cover letter: falls back to "Enter manually" textarea regardless of resume path
  - Aggregator: skips Indeed/LinkedIn SmartApply login-required flows
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

# ── Optional imports ──────────────────────────────────────────────────────────

try:
    from tracks.cover_letter_gen import generate_cover_letter as _gcl
    def _make_cover_letter(job, insight: dict) -> str:
        return _gcl(
            job_title=job.title,
            company=job.company,
            job_description=getattr(job, "description", "") or "",
            insight=insight,
        )
except ImportError:
    def _make_cover_letter(job, insight: dict) -> str:
        return (
            f"I am excited to apply for the {job.title} position at {job.company}. "
            "My background in Computer Science and hands-on experience make me a strong candidate."
        )

try:
    from tracks.humanizer_check import ensure_humanized
    _has_humanizer = True
except ImportError:
    _has_humanizer = False

try:
    from research.company_researcher import research_company
    _has_researcher = True
except ImportError:
    _has_researcher = False

try:
    from research.insight_synthesizer import synthesize
    _has_synthesizer = True
except ImportError:
    _has_synthesizer = False

try:
    from ui.approval_queue import request_approval
    _has_approval = True
except ImportError:
    _has_approval = False

# ── Constants ─────────────────────────────────────────────────────────────────

PROFILE_PATH_BASE = Path.home() / ".autoapplyai"

# Field IDs that _universal_fill must never overwrite
PROTECTED_FIELD_IDS = {"first_name", "last_name", "email", "phone"}
PROTECTED_FIELD_NAMES = ["first_name", "last_name", "[email]", "[phone]"]

CONFIRMATION_PHRASES = [
    "thank you for your application",
    "your application has been submitted",
    "application received", "application complete",
    "we have received your application",
    "we will review your application",
    "we'll be in touch", "you've successfully applied",
    "successfully submitted", "your application is complete",
    "application submitted successfully", "we received your application",
    "thanks for applying", "thank you for applying",
]

# These URLs must never be treated as submitted
FALSE_POSITIVE_URLS = [
    "stripe.com/jobs/search", "simplyhired.com",
    "smartapply.indeed.com", "indeed.com/apply",
    "linkedin.com/jobs", "glassdoor.com",
]

# Aggregator link scan — skip these (require login)
SKIP_LINK_DOMAINS = [
    "smartapply.indeed.com", "indeed.com/apply", "linkedin.com",
    "glassdoor.com", "ziprecruiter.com", "monster.com",
]

POPUP_SELECTORS = [
    "button[data-provides='cookie-consent-accept-all']",
    "button:has-text('Accept all cookies')", "button:has-text('Accept All Cookies')",
    "button:has-text('Accept all')", "button:has-text('Accept All')",
    "button:has-text('Accept Cookies')", "button:has-text('Accept cookies')",
    "button:has-text('I Accept')", "button:has-text('I Agree')", "button:has-text('I agree')",
    "button:has-text('Agree and proceed')", "button:has-text('Agree')",
    "button:has-text('Allow all')", "button:has-text('Allow All')",
    "button:has-text('Allow cookies')", "button:has-text('Allow Cookies')",
    "button:has-text('OK')", "button:has-text('Ok')",
    "button:has-text('Got it')", "button:has-text('Got It')",
    "button:has-text('Dismiss')", "button:has-text('Close')",
    "button:has-text('Continue')", "button:has-text('Proceed')",
    "[id*='cookie'] button:has-text('Accept')", "[class*='cookie'] button:has-text('Accept')",
    "[id*='cookie'] button:has-text('Allow')", "[class*='cookie'] button:has-text('Allow')",
    "[id*='consent'] button:has-text('Accept')", "[class*='consent'] button:has-text('Accept')",
    "[id*='gdpr'] button:not(:has-text('Reject'))",
    "[class*='gdpr'] button:not(:has-text('Reject'))",
    "[id*='banner'] button:has-text('Accept')", "[class*='banner'] button:has-text('Accept')",
    "[class*='CookieBanner'] button", "[class*='cookie-banner'] button",
    "button[aria-label='Close']", "button[aria-label='close']", "button[aria-label='Dismiss']",
    ".modal-close", "[data-dismiss='modal']",
    "[data-testid='cookie-accept']", "[data-testid='consent-accept']",
]


# ── ATS Detection ─────────────────────────────────────────────────────────────

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
    if "simplyhired" in u or "indeed.com" in u or "linkedin.com/jobs" in u or "glassdoor" in u:
        return "aggregator"
    return "generic"


# ── Option Scoring ────────────────────────────────────────────────────────────

def _score_option(option_text: str, target: str) -> float:
    o = option_text.strip().lower()
    t = target.strip().lower()
    if not o or o in ("select...", "select", "--", "---", "please select", "choose one", "none", ""):
        return 0.0
    if o == t:
        return 1.0
    if t in o:
        return max(len(t) / len(o), 0.6)
    if o in t:
        return len(o) / len(t) * 0.85
    common = set(t.split()) & set(o.split())
    if common:
        return len(common) / max(len(t.split()), len(o.split())) * 0.75
    if len(t) >= 4 and o.startswith(t[:4]):
        return 0.55
    return 0.0


# ── TrackWorker ───────────────────────────────────────────────────────────────

class TrackWorker:
    def __init__(self, track_id: int, stop_event=None,
                 status_callback=None, status_cb=None, log_cb=None):
        self.track_id    = track_id
        self._stop_event = stop_event
        self.log_cb      = log_cb or (lambda *a: None)
        self.page: Page | None = None
        self._context    = None
        self._stop       = False
        self.store       = get_store()
        self.pool        = get_pool()
        _cb = status_callback or status_cb
        if _cb:
            import inspect
            try:
                n = len(inspect.signature(_cb).parameters)
                self._cb = _cb if n >= 2 else (lambda tid, msg: _cb(msg))
            except Exception:
                self._cb = lambda tid, msg: None
        else:
            self._cb = lambda tid, msg: None

    def _notify(self, msg: str):
        try:
            self._cb(self.track_id, msg)
        except Exception:
            pass

    def _should_stop(self) -> bool:
        return self._stop or (self._stop_event is not None and self._stop_event.is_set())

    def stop(self):
        self._stop = True

    # ── Browser ───────────────────────────────────────────────────────────────

    async def _launch_browser(self):
        profile_dir = PROFILE_PATH_BASE / f"track_{self.track_id}"
        profile_dir.mkdir(parents=True, exist_ok=True)
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

    # ── Main Loop ─────────────────────────────────────────────────────────────

    async def run(self):
        while not self._should_stop():
            try:
                if not self.page or self.page.is_closed():
                    await self._close_browser()
                    await self._launch_browser()

                job = self.pool.get_next()
                if not job:
                    await asyncio.sleep(5)
                    continue

                self._notify(f"Applying: {job.title} @ {job.company}")
                print(f"[Track {self.track_id}] ► {job.title} @ {job.company}")

                try:
                    await self._process_job(job)
                except asyncio.TimeoutError:
                    print(f"[Track {self.track_id}] Timeout — {job.company}")
                    self.pool.mark_done(job.job_id, "failed")
                except Exception as e:
                    print(f"[Track {self.track_id}] Error: {e}")
                    self.pool.mark_done(job.job_id, "failed")

            except Exception as e:
                print(f"[Track {self.track_id}] Browser crash: {e} — restarting")
                await self._close_browser()
                await asyncio.sleep(3)

    async def _process_job(self, job):
        # 1. Research
        insight = {}
        if _has_researcher:
            try:
                raw = await asyncio.wait_for(
                    research_company(job.company, job.title, getattr(job, "description", "") or ""),
                    timeout=90
                )
                insight = synthesize(raw) if _has_synthesizer else (raw or {})
            except Exception as e:
                print(f"[Track {self.track_id}] Research error: {e}")

        # 2. Cover letter
        try:
            cl = _make_cover_letter(job, insight)
        except Exception as e:
            print(f"[Track {self.track_id}] Cover letter error: {e}")
            cl = f"I am excited to apply for {job.title} at {job.company}."

        # 3. Humanizer
        if _has_humanizer:
            try:
                cl, ai_score, attempts = ensure_humanized(cl, job.company, job.title)
                print(f"[Humanizer] AI score {ai_score:.2f} after {attempts} attempt(s)")
            except Exception as e:
                print(f"[Track {self.track_id}] Humanizer error: {e}")

        # 4. Approval dialog
        if _has_approval:
            try:
                job_data = {
                    "title": job.title, "company": job.company,
                    "location": getattr(job, "location", ""),
                    "platform": getattr(job, "platform", ""),
                    "url": job.url, "ats_url": getattr(job, "ats_url", ""),
                    "description": getattr(job, "description", ""),
                    "score": getattr(job, "score", 0),
                }
                loop = asyncio.get_event_loop()
                action, cl = await loop.run_in_executor(
                    None, lambda: request_approval(job_data, insight, cl)
                )
                if action == "skip":
                    print(f"[Track {self.track_id}] Skipped: {job.title}")
                    self.pool.mark_done(job.job_id, "skipped")
                    return
            except Exception as e:
                print(f"[Track {self.track_id}] Approval error: {e} — auto-approving")

        # 5. Apply
        success = await self._apply(job, insight, cl)
        if success:
            print(f"[Track {self.track_id}] ✅ CONFIRMED: {job.title} @ {job.company}")
            self.pool.mark_done(job.job_id, "submitted")
        else:
            print(f"[Track {self.track_id}] ✗ Failed: {job.title} @ {job.company}")
            self.pool.mark_done(job.job_id, "failed")

    # ── Apply ─────────────────────────────────────────────────────────────────

    async def _apply(self, job, insight, cover_letter: str) -> bool:
        try:
            url = getattr(job, "ats_url", "") or job.url
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
                try:
                    from tracks.workday_handler import fill_workday_application
                    return await fill_workday_application(
                        self.page, current_url, job.title, job.company,
                        insight, cover_letter, job.job_id
                    )
                except ImportError:
                    pass
            return await self._fill_generic(job, insight, cover_letter, profile)

        except PwTimeout:
            print(f"[Track {self.track_id}] Page timeout")
            return False
        except Exception as e:
            print(f"[Track {self.track_id}] Apply error: {e}")
            return False

    # ── Popup Dismissal ───────────────────────────────────────────────────────

    async def _dismiss_popups(self) -> None:
        dismissed = 0
        for selector in POPUP_SELECTORS:
            try:
                el = await self.page.query_selector(selector)
                if el and await el.is_visible():
                    await el.click(timeout=2000)
                    dismissed += 1
                    await self._delay(0.2, 0.4)
            except Exception:
                continue
        for tc_sel in ["[id*='terms']", "[class*='terms']"]:
            try:
                tc = await self.page.query_selector(tc_sel)
                if tc:
                    await tc.evaluate("el => el.scrollTop = el.scrollHeight")
            except Exception:
                continue
        if dismissed:
            print(f"[Track {self.track_id}] Dismissed {dismissed} popup(s)")

    # ══════════════════════════════════════════════════════════════════════════
    #  DROPDOWN SYSTEM — Three strategies, always tries all three
    # ══════════════════════════════════════════════════════════════════════════

    async def _select_any(self, field_hint: str, target: str) -> bool:
        """
        Master select handler. Tries:
          1. Native <select> element (direct option selection)
          2. React-Select / custom div dropdown (click → search → pick option from document)
          3. Keyboard arrow navigation (for any dropdown type as last resort)
        """
        if not target:
            return False

        # Find the field element
        el = None
        for sel in [s.strip() for s in field_hint.split(",")]:
            try:
                found = await self.page.query_selector(sel)
                if found:
                    el = found
                    break
            except Exception:
                continue
        if not el:
            return False

        tag = (await el.evaluate("el => el.tagName.toLowerCase()")).lower()

        # Strategy 1: Native select
        if tag == "select":
            ok = await self._native_select(el, target, field_hint)
            if ok:
                return True

        # Strategy 2: Custom dropdown (React-Select, Select2, etc.)
        ok = await self._custom_dropdown_v2(el, target, field_hint)
        if ok:
            return True

        # Strategy 3: Keyboard navigation
        return await self._keyboard_select(el, target, field_hint)

    async def _native_select(self, el, target: str, hint: str = "") -> bool:
        """Select from a native <select>. Tries exact, fuzzy, then index."""
        try:
            opts = await el.evaluate(
                "el => Array.from(el.options).map(o => ({value:o.value,text:o.text.trim(),idx:o.index}))"
            )
            scored = sorted(
                [(o, _score_option(o["text"], target)) for o in opts],
                key=lambda x: x[1], reverse=True
            )
            if not scored:
                return False

            best, best_score = scored[0]

            # Skip placeholder option
            if best["idx"] == 0 and best["value"] in ("", "0", None) and len(scored) > 1:
                if scored[1][1] > 0.3:
                    best, best_score = scored[1]

            if best_score < 0.3:
                print(f"[Track {self.track_id}] ⚠ No match for '{target}' in '{hint}' "
                      f"(best: '{best['text']}' @ {best_score:.2f})")
                return False

            await el.select_option(index=best["idx"])
            await self._delay(0.3, 0.5)
            selected = await el.evaluate("el => el.options[el.selectedIndex]?.text?.trim() || ''")
            print(f"[Track {self.track_id}] ✓ native '{hint}' → '{selected}'")
            return True
        except Exception as e:
            return False

    async def _custom_dropdown_v2(self, trigger_el, target: str, hint: str = "") -> bool:
        """
        Click to open dropdown, wait for options to appear ANYWHERE in document,
        score them, click the best one. Options render in a portal outside the form.
        """
        try:
            # Click to open
            await trigger_el.click(timeout=3000)
            await self._delay(0.5, 0.8)

            # Try to type in search box if present
            search = await self.page.query_selector(
                "input[class*='select__input'],"
                "[class*='select__control'] input:not([type='hidden']),"
                "[class*='selectInput'] input,"
                "[role='combobox'] input,"
                ".select2-search__field"
            )
            if search and await search.is_visible():
                try:
                    await search.triple_click()
                    await search.press("Control+a")
                    await search.press("Backspace")
                    await self._delay(0.1, 0.2)
                    await search.type(target, delay=50)
                    await self._delay(0.6, 1.0)
                except Exception:
                    pass

            # Collect ALL visible options from the ENTIRE document
            await self._delay(0.3, 0.5)
            option_els = await self.page.query_selector_all(
                "[class*='select__option'],"
                "[role='option'],"
                ".select2-results__option,"
                "[class*='option']:not([class*='disabled']):not(select):not(input)"
            )

            # Filter to only visible ones and score
            scored = []
            for opt in option_els:
                try:
                    if not await opt.is_visible():
                        continue
                    text = (await opt.inner_text()).strip()
                    if not text or text.lower() in ("no options", "no results", "loading...", "searching..."):
                        continue
                    score = _score_option(text, target)
                    scored.append((opt, text, score))
                except Exception:
                    continue

            if not scored:
                await self.page.keyboard.press("Escape")
                return False

            scored.sort(key=lambda x: x[2], reverse=True)
            best_el, best_text, best_score = scored[0]

            if best_score < 0.3:
                print(f"[Track {self.track_id}] ⚠ Custom dropdown: no match for '{target}' "
                      f"(best: '{best_text}' @ {best_score:.2f})")
                await self.page.keyboard.press("Escape")
                return False

            await best_el.scroll_into_view_if_needed()
            await best_el.click(timeout=3000)
            await self._delay(0.3, 0.5)
            print(f"[Track {self.track_id}] ✓ custom '{hint}' → '{best_text}' ({best_score:.2f})")
            return True

        except Exception as e:
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    async def _keyboard_select(self, trigger_el, target: str, hint: str = "") -> bool:
        """
        Last resort: open dropdown, use arrow keys to navigate through options,
        read each one, stop at best match, press Enter.
        Works for ANY dropdown type including non-typeable ones.
        """
        try:
            await trigger_el.click(timeout=3000)
            await self._delay(0.5, 0.8)

            best_score = 0.0
            best_offset = 0  # how many arrow-downs from current position

            # Navigate up to 30 options
            for i in range(30):
                await self.page.keyboard.press("ArrowDown")
                await self._delay(0.1, 0.2)

                # Read the currently highlighted option text
                highlighted = await self.page.evaluate("""() => {
                    const focused = document.querySelector(
                        '[class*="option--is-focused"],[class*="option-focused"],' +
                        '[aria-selected="true"],[class*="highlighted"],' +
                        'option:checked'
                    );
                    return focused ? focused.textContent.trim() : '';
                }""")

                if not highlighted:
                    continue

                score = _score_option(highlighted, target)
                if score > best_score:
                    best_score = score
                    best_offset = i

                # Perfect match — select immediately
                if score >= 0.95:
                    await self.page.keyboard.press("Enter")
                    print(f"[Track {self.track_id}] ✓ keyboard '{hint}' → '{highlighted}'")
                    return True

            if best_score >= 0.3:
                # Go back to best position and select
                await self.page.keyboard.press("Escape")
                await self._delay(0.3, 0.5)
                await trigger_el.click(timeout=3000)
                await self._delay(0.4, 0.6)
                for _ in range(best_offset + 1):
                    await self.page.keyboard.press("ArrowDown")
                    await self._delay(0.08, 0.12)
                await self.page.keyboard.press("Enter")
                print(f"[Track {self.track_id}] ✓ keyboard nav '{hint}' → score {best_score:.2f}")
                return True

            await self.page.keyboard.press("Escape")
            return False

        except Exception:
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    # ── Reliable Field Filler ─────────────────────────────────────────────────

    async def _fill_field(self, selectors: list[str], value: str, label: str = "") -> bool:
        """Fill a text input reliably using click → select-all → type → tab."""
        if not value:
            return False
        for sel in selectors:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible() and not await el.is_disabled():
                    await el.click()
                    await self._delay(0.1, 0.15)
                    await el.press("Control+a")
                    await el.press("Delete")
                    await el.type(value, delay=40)
                    await el.press("Tab")
                    actual = await el.input_value()
                    if actual.strip():
                        if label:
                            print(f"[Track {self.track_id}] ✓ {label} → {actual!r}")
                        return True
            except Exception:
                continue
        return False

    # ── Aggregator Escape ─────────────────────────────────────────────────────

    async def _handle_aggregator(self, job, insight, cover_letter: str, profile: dict) -> bool:
        print(f"[Track {self.track_id}] Aggregator — scanning for ATS links...")

        # Strategy 1: Scan all hrefs for direct ATS links
        try:
            all_hrefs = await self.page.evaluate("""() =>
                Array.from(document.querySelectorAll('a[href]'))
                    .map(a => a.href)
                    .filter(h => h && h.startsWith('http'))
            """)
            ats_hrefs = [
                h for h in all_hrefs
                if _detect_ats(h) not in ("aggregator", "generic")
                and not any(skip in h for skip in SKIP_LINK_DOMAINS)
            ]
            if ats_hrefs:
                target = ats_hrefs[0]
                ats_type = _detect_ats(target)
                print(f"[Track {self.track_id}] Found ATS link: {target[:70]}")
                await self.page.goto(target, timeout=30000, wait_until="domcontentloaded")
                await self._delay(1.5, 2.5)
                await self._dismiss_popups()
                ats_type = _detect_ats(self.page.url)
                if ats_type == "greenhouse":
                    return await self._fill_greenhouse(job, insight, cover_letter, profile)
                if ats_type == "lever":
                    return await self._fill_lever(job, insight, cover_letter, profile)
                return await self._fill_generic(job, insight, cover_letter, profile)
        except Exception as e:
            print(f"[Track {self.track_id}] Link scan error: {e}")

        # Strategy 2: Click apply button and catch new tab or navigation
        for sel in [
            "a:has-text('Apply Now')", "button:has-text('Apply Now')",
            "a:has-text('Apply on company site')", "a:has-text('Apply on Company Site')",
            "a:has-text('Apply now')", "button:has-text('Apply now')",
            "[data-testid*='apply']", "a:has-text('Apply')", "button:has-text('Apply')",
        ]:
            try:
                btn = await self.page.query_selector(sel)
                if not btn or not await btn.is_visible():
                    continue
                href = await btn.get_attribute("href") or ""
                if href and _detect_ats(href) not in ("aggregator", "generic", "") \
                        and not any(skip in href for skip in SKIP_LINK_DOMAINS):
                    await self.page.goto(href, timeout=30000, wait_until="domcontentloaded")
                    await self._delay(1.5, 2.5)
                    await self._dismiss_popups()
                    ats = _detect_ats(self.page.url)
                    if ats == "greenhouse":
                        return await self._fill_greenhouse(job, insight, cover_letter, profile)
                    if ats == "lever":
                        return await self._fill_lever(job, insight, cover_letter, profile)
                    if ats not in ("aggregator",):
                        return await self._fill_generic(job, insight, cover_letter, profile)
                    continue

                pages_before = len(self.page.context.pages)
                url_before   = self.page.url
                await btn.click()
                await self._delay(2, 3)

                pages_after = self.page.context.pages
                if len(pages_after) > pages_before:
                    new_page = pages_after[-1]
                    try:
                        await new_page.wait_for_load_state("domcontentloaded", timeout=10000)
                    except Exception:
                        pass
                    new_url = new_page.url
                    # Skip login-required flows
                    if any(skip in new_url for skip in SKIP_LINK_DOMAINS):
                        await new_page.close()
                        continue
                    ats = _detect_ats(new_url)
                    print(f"[Track {self.track_id}] New tab: {new_url[:70]} → {ats}")
                    if ats not in ("aggregator", "generic"):
                        self.page = new_page
                        await self._dismiss_popups()
                        if ats == "greenhouse":
                            return await self._fill_greenhouse(job, insight, cover_letter, profile)
                        if ats == "lever":
                            return await self._fill_lever(job, insight, cover_letter, profile)
                        return await self._fill_generic(job, insight, cover_letter, profile)
                    await new_page.close()

                if self.page.url != url_before:
                    ats = _detect_ats(self.page.url)
                    if ats not in ("aggregator", "generic"):
                        await self._dismiss_popups()
                        if ats == "greenhouse":
                            return await self._fill_greenhouse(job, insight, cover_letter, profile)
                        if ats == "lever":
                            return await self._fill_lever(job, insight, cover_letter, profile)
                        return await self._fill_generic(job, insight, cover_letter, profile)
            except Exception:
                continue

        print(f"[Track {self.track_id}] Could not escape aggregator — skipping")
        return False

    # ── Greenhouse Form ───────────────────────────────────────────────────────

    async def _fill_greenhouse(self, job, insight, cover_letter: str, profile: dict) -> bool:
        try:
            print(f"[Track {self.track_id}] Filling Greenhouse form...")

            try:
                await self.page.wait_for_selector(
                    "form#application_form,#application-form,form[action*='applications'],"
                    ".application-form,.apply-form",
                    timeout=15000
                )
            except PwTimeout:
                return await self._ai_page_fallback(job, cover_letter, profile)

            await self._delay(1, 2)
            await self._dismiss_popups()

            full_name = profile.get("full_name", "") or ""
            email     = profile.get("email", "") or ""
            phone     = profile.get("phone", "") or ""
            linkedin  = profile.get("linkedin_url", "") or ""
            addr      = profile.get("address", "") or ""
            first     = full_name.split()[0] if full_name else ""
            last      = full_name.split()[-1] if len(full_name.split()) > 1 else full_name

            # ── Country FIRST (re-renders form) ──
            await self._select_any(
                "select#country,select[name*='country'],select[id*='country']",
                "United States"
            )
            await self._delay(0.8, 1.2)

            # ── Name / Email / Phone ──
            for selectors, value, label in [
                (["#first_name", "input[name='job_application[first_name]']",
                  "input[autocomplete='given-name']", "input[placeholder*='First' i]"],
                 first, "First Name"),
                (["#last_name", "input[name='job_application[last_name]']",
                  "input[autocomplete='family-name']", "input[placeholder*='Last' i]"],
                 last, "Last Name"),
                (["#email", "input[name='job_application[email]']",
                  "input[type='email']", "input[autocomplete='email']"],
                 email, "Email"),
                (["#phone", "input[name='job_application[phone]']",
                  "input[type='tel']", "input[autocomplete='tel']"],
                 phone, "Phone"),
            ]:
                await self._fill_field(selectors, value, label)
                await self._delay(0.2, 0.3)

            # ── State ──
            state = addr.split(",")[1].strip() if "," in addr else "Indiana"
            await self._select_any("select[name*='state'],select[id*='state']", state)

            # ── Resume upload ──
            resume_path = self.store.get("resume_path", "") or ""
            if resume_path and Path(resume_path).exists():
                for sel in ["input[type='file']", "#resume", "input[name*='resume']", "input[accept*='pdf']"]:
                    try:
                        el = await self.page.query_selector(sel)
                        if el:
                            await el.set_input_files(resume_path)
                            print(f"[Track {self.track_id}] Resume uploaded")
                            await self._delay(1.5, 2)
                            break
                    except Exception:
                        continue

            # ── Cover letter — ALWAYS click Enter manually then fill textarea ──
            for em_sel in [
                "a:has-text('Enter manually')", "button:has-text('Enter manually')",
                "label:has-text('Enter manually')", "span:has-text('Enter manually')",
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

            # Find and fill the cover letter textarea (try multiple approaches)
            cl_filled = False
            for cl_sel in [
                "textarea[name*='cover']", "textarea[id*='cover']",
                "#cover_letter_text", "textarea[aria-label*='cover' i]",
                "#cover_letter",
            ]:
                try:
                    el = await self.page.query_selector(cl_sel)
                    if el and await el.is_visible():
                        await el.click()
                        await el.fill(cover_letter)
                        cl_filled = True
                        print(f"[Track {self.track_id}] Cover letter filled")
                        break
                except Exception:
                    continue

            if not cl_filled:
                # Find any textarea that became visible after clicking Enter manually
                textareas = await self.page.query_selector_all("textarea")
                for ta in textareas:
                    try:
                        if await ta.is_visible():
                            current = await ta.input_value()
                            if not current or len(current) < 20:
                                await ta.click()
                                await ta.fill(cover_letter)
                                print(f"[Track {self.track_id}] Cover letter filled (fallback)")
                                break
                    except Exception:
                        continue

            # ── LinkedIn ──
            if linkedin:
                await self._fill_field(
                    ["input[name*='linkedin']", "input[id*='linkedin']",
                     "input[placeholder*='LinkedIn' i]", "input[placeholder*='linkedin' i]"],
                    linkedin, "LinkedIn"
                )

            # ── Education section — explicit targeted pass ──
            await self._fill_education(profile)

            # ── Universal fill for all remaining custom questions ──
            await self._universal_fill(profile, cover_letter)

            # ── Final correction — ensure name/email/phone are correct ──
            await self._delay(0.5, 1)
            for selectors, value, label in [
                (["#first_name", "input[name='job_application[first_name]']",
                  "input[autocomplete='given-name']"], first, "First Name"),
                (["#last_name", "input[name='job_application[last_name]']",
                  "input[autocomplete='family-name']"], last, "Last Name"),
                (["#email", "input[name='job_application[email]']",
                  "input[type='email']"], email, "Email"),
                (["#phone", "input[name='job_application[phone]']",
                  "input[type='tel']"], phone, "Phone"),
            ]:
                if not value:
                    continue
                for sel in selectors:
                    try:
                        el = await self.page.query_selector(sel)
                        if el and await el.is_visible():
                            current = await el.input_value()
                            if current.strip() != value.strip():
                                await el.click()
                                await el.press("Control+a")
                                await el.press("Delete")
                                await el.type(value, delay=40)
                                await el.press("Tab")
                                print(f"[Track {self.track_id}] ✓ Corrected {label}")
                            break
                    except Exception:
                        continue

            await self._dismiss_popups()
            await self._delay(1, 1.5)
            return await self._submit_and_verify()

        except Exception as e:
            print(f"[Track {self.track_id}] Greenhouse error: {e}")
            return False

    # ── Education Section ─────────────────────────────────────────────────────

    async def _fill_education(self, profile: dict) -> None:
        """
        Explicitly fill every education field in Greenhouse forms.
        These are almost always native <select> or React-Select dropdowns.
        """
        grad_date  = profile.get("graduation_date", "May 2026") or "May 2026"
        grad_year  = "2026"
        grad_month = "May"
        try:
            parts = grad_date.strip().split()
            if len(parts) == 2:
                grad_month, grad_year = parts[0], parts[1]
        except Exception:
            pass

        print(f"[Track {self.track_id}] Filling education section...")

        # School
        for sel in ["select[id*='school']", "select[name*='school']",
                    "[aria-label*='school' i] .select__control",
                    "[class*='school'] .select__control"]:
            if await self._select_any(sel, "Purdue University"):
                break

        # Discipline / Field of study
        for sel in ["select[id*='discipline']", "select[name*='discipline']",
                    "select[id*='field_of_study']", "select[name*='field_of_study']",
                    "[aria-label*='discipline' i] .select__control"]:
            if await self._select_any(sel, "Computer Science"):
                break

        # Degree type
        for sel in ["select[id*='degree']", "select[name*='degree']",
                    "select[id*='education_level']"]:
            for val in ["Bachelor of Science", "Bachelor's", "Bachelor", "BS"]:
                if await self._select_any(sel, val):
                    break

        # Start date year (text input)
        await self._fill_field(
            ["input[name*='start'][name*='year']", "input[id*='start'][id*='year']",
             "input[placeholder*='start year' i]"],
            "2022"
        )

        # End date month
        for sel in ["select[name*='end'][name*='month']", "select[id*='end'][id*='month']",
                    "select[name*='end_date_month']", "select[id*='end_date_month']"]:
            if await self._select_any(sel, grad_month):
                break

        # End date year (text input)
        await self._fill_field(
            ["input[name*='end'][name*='year']", "input[id*='end'][id*='year']",
             "input[name*='end_date_year']", "input[placeholder*='end year' i]"],
            grad_year
        )
        # Also try as select
        for sel in ["select[name*='end'][name*='year']", "select[id*='end'][id*='year']"]:
            await self._select_any(sel, grad_year)

        # Expected graduation date (custom Greenhouse question)
        for hint in ["expected date of graduation", "expected graduation", "graduation date"]:
            for sel in [
                f"select[aria-label*='{hint}' i]",
                "select[name*='graduation']",
                f"[aria-label*='{hint}' i] .select__control",
            ]:
                if await self._select_any(sel, grad_date):
                    break
                if await self._select_any(sel, grad_month):
                    break

        # Degree type custom question
        for hint in ["type of degree", "degree pursuing", "degree currently pursuing"]:
            for sel in [f"select[aria-label*='{hint}' i]",
                        f"[aria-label*='{hint}' i] .select__control"]:
                for val in ["Bachelor of Science", "Bachelor's", "Bachelor", "BS"]:
                    if await self._select_any(sel, val):
                        break

        # Major (text input)
        await self._fill_field(
            ["input[name*='major']", "input[id*='major']",
             "input[placeholder*='major' i]", "input[aria-label*='major' i]"],
            "Computer Science"
        )

        print(f"[Track {self.track_id}] Education section done.")

    # ── Lever Form ────────────────────────────────────────────────────────────

    async def _fill_lever(self, job, insight, cover_letter: str, profile: dict) -> bool:
        try:
            print(f"[Track {self.track_id}] Filling Lever form...")
            await self.page.wait_for_selector(
                "form.application-form,#application-form", timeout=12000
            )
            await self._delay(1, 2)
            full_name = profile.get("full_name", "") or ""
            await self._fill_field(["input[name='name']"], full_name, "Full Name")
            await self._fill_field(["input[name='email']"], profile.get("email", "") or "", "Email")
            await self._fill_field(["input[name='phone']"], profile.get("phone", "") or "", "Phone")
            try:
                ta = await self.page.query_selector("textarea[name*='comments'],textarea")
                if ta and await ta.is_visible():
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
            return await self._submit_and_verify()
        except Exception as e:
            print(f"[Track {self.track_id}] Lever error: {e}")
            return False

    # ── Generic Form ──────────────────────────────────────────────────────────

    async def _fill_generic(self, job, insight, cover_letter: str, profile: dict) -> bool:
        try:
            print(f"[Track {self.track_id}] Filling generic form: {self.page.url[:60]}")

            # Stripe: look for embedded Greenhouse link
            if "stripe.com/jobs" in self.page.url:
                try:
                    apply_links = await self.page.evaluate("""() =>
                        Array.from(document.querySelectorAll('a[href]'))
                            .map(a => a.href)
                            .filter(h => h.includes('greenhouse') || h.includes('grnh.se')
                                      || h.includes('lever.co') || h.includes('ashbyhq'))
                    """)
                    if apply_links:
                        await self.page.goto(apply_links[0], timeout=30000, wait_until="domcontentloaded")
                        await self._delay(1.5, 2)
                        await self._dismiss_popups()
                        ats = _detect_ats(self.page.url)
                        if ats == "greenhouse":
                            return await self._fill_greenhouse(job, insight, cover_letter, profile)
                        if ats == "lever":
                            return await self._fill_lever(job, insight, cover_letter, profile)
                    # Look for Apply button on the Stripe listing page
                    for btn_sel in ["a:has-text('Apply Now')", "a:has-text('Apply')", "button:has-text('Apply')"]:
                        btn = await self.page.query_selector(btn_sel)
                        if btn and await btn.is_visible():
                            href = await btn.get_attribute("href") or ""
                            if href:
                                await self.page.goto(href if href.startswith("http") else f"https://stripe.com{href}",
                                                     timeout=30000, wait_until="domcontentloaded")
                                await self._delay(1.5, 2)
                                await self._dismiss_popups()
                                ats = _detect_ats(self.page.url)
                                if ats == "greenhouse":
                                    return await self._fill_greenhouse(job, insight, cover_letter, profile)
                                break
                except Exception as e:
                    print(f"[Track {self.track_id}] Stripe detection: {e}")

            # Follow Apply button for other listing pages
            for btn_sel in [
                "a:has-text('Apply Now')", "button:has-text('Apply Now')",
                "a:has-text('Apply for this job')", "a:has-text('Apply for this role')",
                "button:has-text('Apply for this job')", "a[href*='apply']",
            ]:
                try:
                    btn = await self.page.query_selector(btn_sel)
                    if not btn or not await btn.is_visible():
                        continue
                    href = await btn.get_attribute("href") or ""
                    if href and _detect_ats(href) not in ("generic", "aggregator", ""):
                        await self.page.goto(href, timeout=30000, wait_until="domcontentloaded")
                        await self._delay(1.5, 2.5)
                        await self._dismiss_popups()
                        ats = _detect_ats(self.page.url)
                        if ats == "greenhouse":
                            return await self._fill_greenhouse(job, insight, cover_letter, profile)
                        if ats == "lever":
                            return await self._fill_lever(job, insight, cover_letter, profile)
                        break
                    pages_before = len(self.page.context.pages)
                    url_before   = self.page.url
                    await btn.click()
                    await self._delay(2, 3)
                    await self._dismiss_popups()
                    if len(self.page.context.pages) > pages_before:
                        new_page = self.page.context.pages[-1]
                        try:
                            await new_page.wait_for_load_state("domcontentloaded", timeout=10000)
                        except Exception:
                            pass
                        ats = _detect_ats(new_page.url)
                        if ats == "greenhouse":
                            self.page = new_page
                            return await self._fill_greenhouse(job, insight, cover_letter, profile)
                        if ats == "lever":
                            self.page = new_page
                            return await self._fill_lever(job, insight, cover_letter, profile)
                    break
                except Exception:
                    continue

            await self._universal_fill(profile, cover_letter)
            await self._dismiss_popups()
            return await self._submit_and_verify()

        except Exception as e:
            print(f"[Track {self.track_id}] Generic error: {e}")
            return False

    # ── Universal Form Filler ─────────────────────────────────────────────────

    async def _universal_fill(self, profile: dict, cover_letter: str) -> None:
        print(f"[Track {self.track_id}] Universal form fill...")
        fm = self._build_field_map(profile, cover_letter)
        for pass_num in range(3):
            filled  = await self._fill_text_inputs(fm)
            filled += await self._fill_textareas(fm, cover_letter)
            filled += await self._fill_all_selects(fm)
            await self._fill_radios()
            await self._fill_checkboxes()
            print(f"[Track {self.track_id}] Pass {pass_num+1}: {filled} field(s)")
            if filled == 0:
                break
            await self._delay(0.3, 0.5)

    def _build_field_map(self, profile: dict, cover_letter: str) -> dict:
        full_name  = profile.get("full_name", "") or ""
        email      = profile.get("email", "") or ""
        phone      = profile.get("phone", "") or ""
        addr       = profile.get("address", "") or ""
        linkedin   = profile.get("linkedin_url", "") or ""
        github     = profile.get("github_url", "") or ""
        portfolio  = profile.get("portfolio_url", "") or ""
        gpa        = profile.get("gpa", "3.8") or "3.8"
        grad_date  = profile.get("graduation_date", "May 2026") or "May 2026"
        grad_year, grad_month = "2026", "May"
        try:
            parts = grad_date.strip().split()
            if len(parts) == 2:
                grad_month, grad_year = parts[0], parts[1]
        except Exception:
            pass
        first = full_name.split()[0] if full_name else ""
        last  = full_name.split()[-1] if len(full_name.split()) > 1 else full_name
        city, state = "West Lafayette", "Indiana"
        if "," in addr:
            p = [x.strip() for x in addr.split(",")]
            city  = p[0] if p else city
            state = p[1] if len(p) > 1 else state

        return {
            "first name": first, "first_name": first, "given name": first,
            "last name": last, "last_name": last, "surname": last, "family name": last,
            "full name": full_name, "name": full_name, "legal name": full_name,
            "email": email, "email address": email,
            "phone": phone, "telephone": phone, "mobile": phone, "phone number": phone,
            "address": addr.split(",")[0].strip() if addr else "",
            "street": addr.split(",")[0].strip() if addr else "",
            "city": city, "state": state, "province": state,
            "country": "United States",
            "zip": "47906", "postal": "47906", "zip code": "47906",
            "linkedin": linkedin, "linkedin url": linkedin, "linkedin profile": linkedin,
            "github": github, "github url": github,
            "website": portfolio, "portfolio": portfolio, "personal website": portfolio,
            "cover letter": cover_letter[:3000], "cover": cover_letter[:3000],
            "motivation": cover_letter[:2000], "motivation letter": cover_letter[:2000],
            "why are you interested": cover_letter[:800],
            "why do you want": cover_letter[:800],
            "why this role": cover_letter[:800],
            "why this company": cover_letter[:800],
            "tell us about yourself": cover_letter[:800],
            "about yourself": cover_letter[:800],
            "message": cover_letter[:1000],
            "comments": cover_letter[:500], "notes": cover_letter[:500],
            "additional information": cover_letter[:500],
            "anything else": cover_letter[:500],
            "what are you hoping to learn": cover_letter[:500],
            "what would you like us to know": cover_letter[:500],
            "adjustments needed": "None",
            "school": "Purdue University", "university": "Purdue University",
            "college": "Purdue University", "institution": "Purdue University",
            "discipline": "Computer Science", "major": "Computer Science",
            "field of study": "Computer Science", "area of study": "Computer Science",
            "degree": "Bachelor of Science", "degree type": "Bachelor of Science",
            "type of degree": "Bachelor of Science",
            "gpa": gpa, "grade point": gpa,
            "graduation date": grad_date, "expected graduation": grad_date,
            "graduation": grad_date, "graduation year": grad_year,
            "graduation month": grad_month, "end date year": grad_year,
            "end date month": grad_month, "start date year": "2022",
            # Work experience
            "current or previous employer": "AnautAI",
            "employer": "AnautAI", "company name": "AnautAI",
            "current or previous job title": "Software Engineer",
            "job title": "Software Engineer", "current title": "Software Engineer",
            # Years of experience
            "years of experience": "0-1", "years experience": "0-1",
            "how many years": "0-1",
            "experience with typescript": "0-1",
            "experience with javascript": "1-2",
            "experience with python": "1-2",
            "experience with java": "0-1",
            "experience with c#": "0-1",
            "experience with c++": "0-1",
            "experience with sql": "0-1",
            "experience with react": "0-1",
            "typescript": "0-1", "javascript": "1-2", "python": "1-2",
            # Location
            "commuting distance": "No", "reside within commuting": "No",
            "currently reside": "No", "willing to work": "Yes",
            "work minimum": "Yes", "days per week": "Yes",
            "hybrid": "Yes", "on site": "Yes",
            # Source
            "how did you hear about us": "Job Board",
            "how did you hear": "Job Board",
            "referral source": "Job Board",
            # Work auth
            "authorized": "Yes", "work authorized": "Yes", "eligible to work": "Yes",
            "authorised to work": "Yes",
            "sponsorship": "No", "require sponsorship": "No", "visa sponsorship": "No",
            "worked for": "No", "worked here before": "No",
            "relocate": "Yes", "willing to relocate": "Yes",
            # EEOC
            "gender": "Prefer not to say", "race": "Prefer not to say",
            "ethnicity": "Prefer not to say", "veteran": "No", "disability": "No",
            # Consent
            "brighthire": "Yes, I consent", "consent": "Yes",
            "background check": "Yes", "agree": "Yes",
            # Salary
            "salary": "25", "expected salary": "25", "hourly rate": "25",
            "start date": "May 2026", "available": "May 2026",
        }

    async def _get_label(self, el) -> str:
        parts = []
        try:
            el_id = await el.get_attribute("id")
            if el_id:
                lbl = await self.page.query_selector(f"label[for='{el_id}']")
                if lbl:
                    parts.append(await lbl.inner_text())
        except Exception:
            pass
        for attr in ["placeholder", "name", "aria-label", "title"]:
            try:
                v = await el.get_attribute(attr)
                if v:
                    parts.append(v)
            except Exception:
                continue
        try:
            parent_label = await el.evaluate("""el => {
                const p = el.closest('.field,.form-group,[class*="field"],[class*="group"],[class*="question"]');
                if (p) {
                    const l = p.querySelector('label,[class*="label"]');
                    return l ? l.textContent.trim() : '';
                }
                return '';
            }""")
            if parent_label:
                parts.append(parent_label)
        except Exception:
            pass
        return " ".join(parts).lower().strip()

    def _match(self, label: str, fm: dict) -> str:
        """Match label to field map value. Empty label always returns ''."""
        label = re.sub(r'[*\(\)\?]', '', label).strip().lower()
        if not label or len(label) < 2:
            return ""
        if label in fm:
            return fm[label]
        best_key, best_len = None, 0
        for key in fm:
            if not key:
                continue
            if (key in label or label in key) and len(key) > best_len:
                best_key, best_len = key, len(key)
        if best_key:
            return fm[best_key]
        lw = set(w for w in label.split() if len(w) > 2)
        if not lw:
            return ""
        best_overlap, best_key = 0, None
        for key in fm:
            kw = set(w for w in key.split() if len(w) > 2)
            ov = len(lw & kw)
            if ov > best_overlap:
                best_overlap, best_key = ov, key
        return fm[best_key] if best_overlap >= 1 and best_key else ""

    async def _fill_text_inputs(self, fm: dict) -> int:
        filled = 0
        for inp in await self.page.query_selector_all(
            "input[type='text'],input[type='email'],input[type='tel'],"
            "input[type='url'],input[type='number'],input:not([type])"
        ):
            try:
                if not await inp.is_visible() or await inp.is_disabled():
                    continue
                inp_id   = (await inp.get_attribute("id") or "").replace("-", "_").lower()
                inp_name = (await inp.get_attribute("name") or "").lower()
                if inp_id in PROTECTED_FIELD_IDS:
                    continue
                if any(p in inp_name for p in PROTECTED_FIELD_NAMES):
                    continue
                if len(await inp.input_value()) > 1:
                    continue
                label = await self._get_label(inp)
                if not label or len(label) < 2:
                    continue
                val = self._match(label, fm)
                if val:
                    await inp.click()
                    await inp.press("Control+a")
                    await inp.press("Delete")
                    await inp.type(val, delay=30)
                    await inp.press("Tab")
                    filled += 1
                    await self._delay(0.1, 0.2)
            except Exception:
                continue
        return filled

    async def _fill_textareas(self, fm: dict, cover_letter: str) -> int:
        filled = 0
        for ta in await self.page.query_selector_all("textarea"):
            try:
                if not await ta.is_visible() or await ta.is_disabled():
                    continue
                if len(await ta.input_value()) > 20:
                    continue
                label = await self._get_label(ta)
                if not label or len(label) < 2:
                    continue
                val = self._match(label, fm) or cover_letter[:800]
                await ta.click()
                await ta.fill(val)
                filled += 1
                await self._delay(0.2, 0.4)
            except Exception:
                continue
        return filled

    async def _fill_all_selects(self, fm: dict) -> int:
        """Fill both native selects and custom dropdowns using _select_any."""
        filled = 0

        # Native selects
        for sel_el in await self.page.query_selector_all("select"):
            try:
                if not await sel_el.is_visible() or await sel_el.is_disabled():
                    continue
                ct = await sel_el.evaluate("el => el.options[el.selectedIndex]?.text?.trim() || ''")
                if ct.lower() not in ("select...", "select", "please select", "--", ""):
                    continue
                label = await self._get_label(sel_el)
                if not label or len(label) < 2:
                    continue
                val = self._match(label, fm)
                if val:
                    if await self._native_select(sel_el, val, label):
                        filled += 1
                else:
                    # Unknown select: pick first non-placeholder option
                    opts = await sel_el.evaluate(
                        "el => Array.from(el.options).map(o=>({v:o.value,t:o.text.trim(),i:o.index}))"
                    )
                    for opt in opts[1:]:
                        if opt["t"].lower() not in ("select...", "select", "--", ""):
                            await sel_el.select_option(index=opt["i"])
                            filled += 1
                            break
                await self._delay(0.2, 0.4)
            except Exception:
                continue

        # Custom dropdowns (React-Select etc.)
        for ctrl in await self.page.query_selector_all(
            "[class*='select__control']:not([class*='disabled']),"
            "[role='combobox']:not(input):not(select)"
        ):
            try:
                if not await ctrl.is_visible():
                    continue
                current = await ctrl.evaluate("""el => {
                    const sv = el.querySelector('[class*="single-value"],[class*="selected"],[class*="placeholder"]');
                    return sv ? sv.textContent.trim() : el.textContent.trim().split('\\n')[0].trim();
                }""")
                if current and current.lower() not in ("select...", "select", "--", ""):
                    continue
                label = await ctrl.evaluate("""el => {
                    const c = el.closest('.field,.form-group,[class*="field"],[class*="group"],[class*="question"]');
                    if (c) { const l = c.querySelector('label'); return l ? l.textContent.trim() : ''; }
                    return el.getAttribute('aria-label') || '';
                }""")
                if not label or len(label) < 2:
                    continue
                val = self._match(label.lower().strip(), fm)
                if val and await self._custom_dropdown_v2(ctrl, val, label):
                    filled += 1
                await self._delay(0.3, 0.5)
            except Exception:
                continue

        return filled

    async def _fill_radios(self) -> None:
        try:
            names = await self.page.evaluate(
                "() => [...new Set([...document.querySelectorAll('input[type=radio]')].map(r=>r.name))]"
            )
            for name in names:
                try:
                    radios = await self.page.query_selector_all(f"input[type='radio'][name='{name}']")
                    if any([await r.is_checked() for r in radios]):
                        continue
                    n = name.lower()
                    for radio in radios:
                        if not await radio.is_visible():
                            continue
                        val = (await radio.get_attribute("value") or "").lower()
                        should = False
                        if any(k in n for k in ["authorized", "eligible", "authorised"]):
                            should = val in ("yes", "true", "1")
                        elif any(k in n for k in ["sponsor", "visa"]):
                            should = val in ("no", "false", "0")
                        elif any(k in n for k in ["gender", "race", "ethnic", "veteran", "disability"]):
                            should = any(k in val for k in ["prefer", "decline", "no answer", "not"])
                        if should:
                            await radio.check()
                            await self._delay(0.2, 0.3)
                            break
                except Exception:
                    continue
        except Exception:
            pass

    async def _fill_checkboxes(self) -> None:
        for cb in await self.page.query_selector_all("input[type='checkbox']"):
            try:
                if not await cb.is_visible() or await cb.is_checked():
                    continue
                name = (await cb.get_attribute("name") or "").lower()
                if any(k in name for k in ["agree", "terms", "consent", "accept", "confirm"]):
                    await cb.check()
            except Exception:
                continue

    # ── AI Fallback ───────────────────────────────────────────────────────────

    async def _ai_page_fallback(self, job, cover_letter: str, profile: dict) -> bool:
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
                    "messages": [{"role": "user", "content": [
                        {"type": "image", "source": {
                            "type": "base64", "media_type": "image/png", "data": b64
                        }},
                        {"type": "text", "text": (
                            f"Job application page at {self.page.url}. What is blocking the form? "
                            'Reply ONLY JSON: {"obstacle":"description","selector":"css","action":"click"}'
                        )}
                    ]}]
                }, timeout=25,
            )
            if resp.status_code == 200:
                text = resp.json()["content"][0]["text"]
                m = re.search(r'\{.*\}', text, re.DOTALL)
                if m:
                    data = json.loads(m.group())
                    selector = data.get("selector", "")
                    print(f"[Track {self.track_id}] AI: {data.get('obstacle')} → '{selector}'")
                    if selector:
                        await self.page.click(selector, timeout=5000)
                        await self._delay(1, 2)
                        await self._dismiss_popups()
                        return await self._fill_generic(job, {}, cover_letter, profile)
        except Exception as e:
            print(f"[Track {self.track_id}] AI fallback error: {e}")
        return False

    # ── Submit & Verify ───────────────────────────────────────────────────────

    async def _submit_and_verify(self) -> bool:
        """
        Submit the form and verify confirmation.
        KEY FIX: After first submit click, check for validation errors immediately.
        If errors found, stop — don't click submit 10 more times blindly.
        """
        await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await self._delay(0.5, 1)

        for step in range(10):
            await self._dismiss_popups()

            # False positive guard
            for fp in FALSE_POSITIVE_URLS:
                if fp in self.page.url:
                    print(f"[Track {self.track_id}] False positive URL — not submitting")
                    return False

            # Check for real confirmation
            try:
                page_text = (await self.page.inner_text("body")).lower()
            except Exception:
                page_text = ""

            for phrase in CONFIRMATION_PHRASES:
                if phrase in page_text:
                    print(f"[Track {self.track_id}] ✅ Confirmed: '{phrase}'")
                    return True
            for pattern in ["/confirmation", "/thank-you", "/thankyou", "submitted=true"]:
                if pattern in self.page.url.lower():
                    print(f"[Track {self.track_id}] ✅ Confirmation URL")
                    return True

            # Find submit button
            submit_btn = None
            for btn_sel in [
                "button[type='submit']", "input[type='submit']",
                "button:has-text('Submit Application')", "button:has-text('Submit application')",
                "button:has-text('Submit')", "button:has-text('Send Application')",
            ]:
                try:
                    btn = await self.page.query_selector(btn_sel)
                    if btn and await btn.is_visible():
                        submit_btn = btn
                        break
                except Exception:
                    continue

            if submit_btn:
                text = (await submit_btn.inner_text()).strip()
                print(f"[Track {self.track_id}] Clicking submit: '{text}'")
                await submit_btn.click()
                await self._delay(2.5, 4)

                # ── CRITICAL: Check for validation errors after clicking submit ──
                # If the form has errors, stop immediately — don't retry blindly
                try:
                    error_count = await self.page.evaluate("""() => {
                        const errors = document.querySelectorAll(
                            '[class*="error"]:not([class*="error-page"]),' +
                            '[class*="invalid"],.field_error,.form-error,' +
                            '[data-error]'
                        );
                        let visible = 0;
                        for (const e of errors) {
                            const style = window.getComputedStyle(e);
                            if (style.display !== 'none' && style.visibility !== 'hidden') {
                                const text = e.textContent.trim();
                                if (text.includes('required') || text.includes('invalid') ||
                                    text.includes('please') || text.includes('enter')) {
                                    visible++;
                                }
                            }
                        }
                        return visible;
                    }""")
                    if error_count > 0:
                        print(f"[Track {self.track_id}] ✗ {error_count} validation error(s) — form incomplete, stopping")
                        return False
                except Exception:
                    pass
                continue

            # Multi-step: find Next button
            next_clicked = False
            for next_sel in ["button:has-text('Next')", "button:has-text('Continue')", "a:has-text('Next')"]:
                try:
                    btn = await self.page.query_selector(next_sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        await self._delay(1.5, 2.5)
                        profile = self.store.get_profile() or {}
                        await self._universal_fill(profile, "")
                        next_clicked = True
                        break
                except Exception:
                    continue
            if not next_clicked:
                break

        print(f"[Track {self.track_id}] ✗ No confirmation detected")
        return False

    async def _delay(self, min_s: float, max_s: float) -> None:
        await asyncio.sleep(random.uniform(min_s, max_s))
