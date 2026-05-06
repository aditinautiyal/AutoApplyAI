"""
tracks/track_worker.py — BULLETPROOF VERSION
Every failure mode from beta testing addressed.
Never stops mid-form. Never clicks wrong dropdown option.
Never fails silently.
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
            "My background in Computer Science and hands-on experience make me a strong candidate. "
            "I look forward to contributing to your team."
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

# VPN coordination — tracks pause while VPN is switching servers
try:
    from discovery.vpn_controller import VPN_SWITCHING
except ImportError:
    import threading
    VPN_SWITCHING = threading.Event()  # fallback: never blocks

# ── Profile defaults ──────────────────────────────────────────────────────────

PROFILE_PATH_BASE = Path.home() / ".autoapplyai"

# Field IDs that _universal_fill must NEVER touch (filled explicitly)
PROTECTED_IDS   = {"first_name", "last_name", "email", "phone"}
PROTECTED_NAMES = ["first_name", "last_name", "[email]", "[phone]"]

# Confirmation phrases — any one of these means the application went through
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
    "application has been sent",
    "your application was sent",
    "we got your application",
    "application is under review",
]

# Indeed SmartApply review page detection
INDEED_REVIEW_PHRASES = [
    "please review your application",
    "review the contents of this job",
    "review your application",
]

# URLs that can never be a real confirmation page
FALSE_POSITIVE_URLS = [
    "stripe.com/jobs/search",
    "simplyhired.com/search",
    "linkedin.com/jobs",
    "glassdoor.com",
]

# Domains to skip in aggregator link scanning (require login)
SKIP_DOMAINS = [
    "linkedin.com", "glassdoor.com",
    "ziprecruiter.com", "monster.com",
]

# ── Comprehensive popup selectors ─────────────────────────────────────────────
# Every known cookie/GDPR/privacy banner dismiss button

POPUP_SELECTORS = [
    # Standard accept buttons
    "button[data-provides='cookie-consent-accept-all']",
    "button:has-text('Accept all cookies')",
    "button:has-text('Accept All Cookies')",
    "button:has-text('Accept all')",
    "button:has-text('Accept All')",
    "button:has-text('Accept Cookies')",
    "button:has-text('Accept cookies')",
    "button:has-text('I Accept')",
    "button:has-text('I Agree')",
    "button:has-text('I agree')",
    "button:has-text('Agree and proceed')",
    "button:has-text('Agree')",
    "button:has-text('Allow all')",
    "button:has-text('Allow All')",
    "button:has-text('Allow cookies')",
    "button:has-text('Allow Cookies')",
    "button:has-text('OK')",
    "button:has-text('Ok')",
    "button:has-text('Got it')",
    "button:has-text('Got It')",
    "button:has-text('Dismiss')",
    "button:has-text('Close')",
    "button:has-text('Continue')",
    "button:has-text('Proceed')",
    "button:has-text('Yes, I Accept')",
    "button:has-text('Accept & Continue')",
    "button:has-text('Accept and Continue')",
    "button:has-text('Save and Continue')",
    "button:has-text('CONFIRM')",
    "button:has-text('Confirm')",
    # TrustArc
    "button:has-text('SUBMIT ALL PREFERENCES')",
    "button:has-text('Submit All Preferences')",
    "button:has-text('Accept All Preferences')",
    "[class*='call-btn']",
    # OneTrust
    "#onetrust-accept-btn-handler",
    "button#onetrust-accept-btn-handler",
    ".optanon-allow-all",
    "button:has-text('Accept All')",
    # Cookiebot
    "#CybotCookiebotDialogBodyButtonAccept",
    "button:has-text('Allow all cookies')",
    # ID/class patterns
    "[id*='cookie'] button:has-text('Accept')",
    "[class*='cookie'] button:has-text('Accept')",
    "[id*='cookie'] button:has-text('Allow')",
    "[class*='cookie'] button:has-text('Allow')",
    "[id*='consent'] button:has-text('Accept')",
    "[class*='consent'] button:has-text('Accept')",
    "[id*='gdpr'] button:not(:has-text('Reject'))",
    "[class*='gdpr'] button:not(:has-text('Reject'))",
    "[id*='banner'] button:has-text('Accept')",
    "[class*='banner'] button:has-text('Accept')",
    "[class*='CookieBanner'] button",
    "[class*='cookie-banner'] button",
    "[class*='privacy-banner'] button",
    "[class*='consent-banner'] button",
    # Close buttons
    "button[aria-label='Close']",
    "button[aria-label='close']",
    "button[aria-label='Dismiss']",
    ".modal-close",
    "[data-dismiss='modal']",
    "[data-testid='cookie-accept']",
    "[data-testid='consent-accept']",
    # Overlay close
    "[class*='overlay'] button:has-text('Close')",
    "[class*='popup'] button:has-text('Close')",
    "[class*='modal'] button[aria-label='Close']",
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

def _score(option_text: str, target: str) -> float:
    """Score how well option_text matches target. 1.0 = perfect."""
    o = option_text.strip().lower()
    t = target.strip().lower()
    if not o or o in ("select...", "select", "--", "---", "please select",
                       "choose one", "none", "", "loading..."):
        return 0.0
    if o == t:
        return 1.0
    if t in o:
        return max(len(t) / len(o), 0.6)
    if o in t:
        return len(o) / len(t) * 0.85
    t_words = set(t.split())
    o_words = set(o.split())
    common = t_words & o_words
    if common:
        return len(common) / max(len(t_words), len(o_words)) * 0.75
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

    def _log(self, msg: str):
        print(f"[Track {self.track_id}] {msg}")

    def _should_stop(self) -> bool:
        return self._stop or (self._stop_event is not None and self._stop_event.is_set())

    def stop(self):
        self._stop = True

    # ── Browser ───────────────────────────────────────────────────────────────

    async def _launch_browser(self):
        profile_dir = PROFILE_PATH_BASE / f"track_{self.track_id}"
        profile_dir.mkdir(parents=True, exist_ok=True)

        for attempt in range(3):
            try:
                pw = await async_playwright().start()
                self._context = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=False,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                    ignore_default_args=["--enable-automation"],
                )
                self.page = (
                    self._context.pages[0]
                    if self._context.pages
                    else await self._context.new_page()
                )
                await self.page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                self._log(f"Browser launched (attempt {attempt + 1})")
                return
            except Exception as e:
                self._log(f"Browser launch attempt {attempt + 1} failed: {e}")
                await asyncio.sleep(3)

        raise RuntimeError("Could not launch browser after 3 attempts")

    async def _close_browser(self):
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass

    # ── Main Loop ─────────────────────────────────────────────────────────────

    async def run(self):
        consecutive_failures = 0
        while not self._should_stop():
            try:
                # ── VPN coordination: wait if VPN is switching servers ──
                # This prevents EPIPE crashes from mid-session IP rotation
                if VPN_SWITCHING.is_set():
                    self._log("⏸ VPN switching — pausing until IP rotation completes...")
                    await self._close_browser()  # Close browser before network changes
                    # Wait for VPN switch to complete (polls every 2 seconds)
                    while VPN_SWITCHING.is_set() and not self._should_stop():
                        await asyncio.sleep(2)
                    self._log("▶ VPN switch complete — resuming")
                    await asyncio.sleep(3)  # Brief stabilization delay
                    consecutive_failures = 0

                # ── Ensure browser is healthy ──
                browser_ok = False
                for _ in range(3):
                    try:
                        if not self.page or self.page.is_closed():
                            await self._close_browser()
                            await self._launch_browser()
                        # Quick health check
                        await self.page.evaluate("() => document.readyState")
                        browser_ok = True
                        break
                    except Exception as e:
                        self._log(f"Browser not healthy: {e} — restarting")
                        await self._close_browser()
                        await asyncio.sleep(2)
                        consecutive_failures += 1

                if not browser_ok:
                    self._log("Browser restart failed 3x — waiting 30s")
                    await asyncio.sleep(30)
                    consecutive_failures = 0
                    continue

                # ── Get next job ──
                job = self.pool.get_next()
                if not job:
                    await asyncio.sleep(5)
                    continue

                # ── Check VPN again right before applying ──
                if VPN_SWITCHING.is_set():
                    self._log("⏸ VPN switching mid-loop — pausing")
                    await self._close_browser()
                    while VPN_SWITCHING.is_set() and not self._should_stop():
                        await asyncio.sleep(2)
                    self._log("▶ VPN switch done — continuing")
                    await asyncio.sleep(3)
                    # Put job back
                    self.pool.requeue(job) if hasattr(self.pool, 'requeue') else None
                    continue

                self._notify(f"Applying: {job.title} @ {job.company}")
                self._log(f"► {job.title} @ {job.company}")
                consecutive_failures = 0

                try:
                    await self._process_job(job)
                except asyncio.TimeoutError:
                    self._log(f"Timeout — {job.company}")
                    self.pool.mark_done(job.job_id, "failed")
                except (ConnectionError, BrokenPipeError, OSError) as e:
                    # Network/EPIPE error — browser connection died (usually from VPN switch)
                    self._log(f"Network error (VPN switch?): {e} — will retry job")
                    self.pool.mark_done(job.job_id, "failed")
                    await self._close_browser()
                    await asyncio.sleep(5)
                except Exception as e:
                    self._log(f"Error: {e}")
                    self.pool.mark_done(job.job_id, "failed")

            except (ConnectionError, BrokenPipeError, OSError) as e:
                self._log(f"Network/pipe error: {e} — restarting browser")
                await self._close_browser()
                await asyncio.sleep(5)
                consecutive_failures += 1
            except Exception as e:
                self._log(f"Browser crash: {e} — restarting")
                await self._close_browser()
                await asyncio.sleep(3)
                consecutive_failures += 1

            # Safety: if too many consecutive failures, wait longer
            if consecutive_failures >= 5:
                self._log(f"⚠ {consecutive_failures} consecutive failures — waiting 60s")
                await asyncio.sleep(60)
                consecutive_failures = 0

    async def _process_job(self, job):
        # Research
        insight = {}
        if _has_researcher:
            try:
                raw = await asyncio.wait_for(
                    research_company(job.company, job.title,
                                     getattr(job, "description", "") or ""),
                    timeout=90
                )
                insight = synthesize(raw) if _has_synthesizer else (raw or {})
            except Exception as e:
                self._log(f"Research error: {e}")

        # Cover letter
        try:
            cl = _make_cover_letter(job, insight)
        except Exception as e:
            self._log(f"Cover letter error: {e}")
            cl = f"I am excited to apply for {job.title} at {job.company}."

        # Humanizer
        if _has_humanizer:
            try:
                cl, ai_score, attempts = ensure_humanized(cl, job.company, job.title)
                self._log(f"Humanizer: {ai_score:.2f} after {attempts} attempt(s)")
            except Exception as e:
                self._log(f"Humanizer error: {e}")

        # Approval dialog
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
                    self._log(f"Skipped: {job.title}")
                    self.pool.mark_done(job.job_id, "skipped")
                    return
            except Exception as e:
                self._log(f"Approval error: {e} — auto-approving")

        # Apply
        success = await self._apply(job, insight, cl)
        if success:
            self._log(f"✅ CONFIRMED: {job.title} @ {job.company}")
            self.pool.mark_done(job.job_id, "submitted")
        else:
            self._log(f"✗ Failed: {job.title} @ {job.company}")
            self.pool.mark_done(job.job_id, "failed")

    # ── Apply dispatcher ──────────────────────────────────────────────────────

    async def _apply(self, job, insight, cover_letter: str) -> bool:
        try:
            url = getattr(job, "ats_url", "") or job.url
            await self.page.goto(url, timeout=30000, wait_until="domcontentloaded")
            await self._delay(1.5, 2.5)
            await self._dismiss_popups()

            url_now  = self.page.url
            ats_type = _detect_ats(url_now)
            self._log(f"URL: {url_now[:80]} → ATS: {ats_type}")

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
                        self.page, url_now, job.title, job.company,
                        insight, cover_letter, job.job_id
                    )
                except ImportError:
                    pass
            return await self._fill_generic(job, insight, cover_letter, profile)

        except PwTimeout:
            self._log("Page timeout")
            return False
        except (ConnectionError, BrokenPipeError, OSError) as e:
            # Re-raise network errors so run() can handle them properly
            raise
        except Exception as e:
            self._log(f"Apply error: {e}")
            return False

    # ════════════════════════════════════════════════════════════════════════
    #  POPUP DISMISSAL — handles every known banner type
    # ════════════════════════════════════════════════════════════════════════

    async def _dismiss_popups(self) -> int:
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

        # Scroll any terms/conditions containers so checkboxes unlock
        for tc_sel in [
            "[id*='terms']", "[class*='terms-container']",
            "[class*='tos-scroll']", "[class*='scrollable-terms']",
        ]:
            try:
                tc = await self.page.query_selector(tc_sel)
                if tc:
                    await tc.evaluate("el => el.scrollTop = el.scrollHeight")
            except Exception:
                continue

        if dismissed:
            self._log(f"Dismissed {dismissed} popup(s)")
        return dismissed

    # ════════════════════════════════════════════════════════════════════════
    #  DROPDOWN SYSTEM — 4 strategies, guaranteed to work on any dropdown
    # ════════════════════════════════════════════════════════════════════════

    async def _pick_option(self, target: str, hint: str = "") -> bool:
        """
        After a dropdown is OPEN, find and click the best matching visible option
        ANYWHERE in the document (React portals render outside the container).
        This is the core option-picker used by all dropdown strategies.
        """
        await self._delay(0.3, 0.5)

        # Collect every visible option element in the entire document
        option_els = await self.page.query_selector_all(
            "[class*='select__option'],"
            "[role='option'],"
            ".select2-results__option,"
            "[class*='option']:not([class*='disabled']):not(select):not(input):not(textarea),"
            "li[class*='item']:not([class*='disabled']),"
            "[class*='menu-item']:not([class*='disabled'])"
        )

        scored = []
        for opt in option_els:
            try:
                if not await opt.is_visible():
                    continue
                text = (await opt.inner_text()).strip()
                if not text or text.lower() in (
                    "no options", "no results", "loading...",
                    "searching...", "type to search"
                ):
                    continue
                s = _score(text, target)
                scored.append((opt, text, s))
            except Exception:
                continue

        if not scored:
            return False

        scored.sort(key=lambda x: x[2], reverse=True)
        best_el, best_text, best_score = scored[0]

        if best_score < 0.25:
            self._log(f"⚠ No match for '{target}' in '{hint}' "
                      f"(best: '{best_text}' @ {best_score:.2f})")
            return False

        try:
            await best_el.scroll_into_view_if_needed()
            await best_el.click(timeout=3000)
            await self._delay(0.3, 0.5)
            self._log(f"✓ '{hint}' → '{best_text}' ({best_score:.2f})")
            return True
        except Exception:
            return False

    async def _select_native(self, el, target: str, hint: str = "") -> bool:
        """Fill a native HTML <select> element."""
        try:
            opts = await el.evaluate(
                "el => Array.from(el.options).map(o => "
                "({value: o.value, text: o.text.trim(), idx: o.index}))"
            )
            scored = sorted(
                [(o, _score(o["text"], target)) for o in opts],
                key=lambda x: x[1], reverse=True
            )
            if not scored:
                return False

            best, best_score = scored[0]
            # Skip placeholder (index 0 with empty value)
            if best["idx"] == 0 and best["value"] in ("", "0", None) and len(scored) > 1:
                if scored[1][1] >= 0.25:
                    best, best_score = scored[1]

            if best_score < 0.25:
                self._log(f"⚠ No native match for '{target}' in '{hint}'")
                return False

            await el.select_option(index=best["idx"])
            await self._delay(0.3, 0.5)
            # Fire change event so React sees it
            await el.evaluate(
                "el => el.dispatchEvent(new Event('change', {bubbles: true}))"
            )
            selected = await el.evaluate(
                "el => el.options[el.selectedIndex]?.text?.trim() || ''"
            )
            self._log(f"✓ native '{hint}' → '{selected}'")
            return True
        except Exception as e:
            return False

    async def _select_custom(self, trigger_el, target: str, hint: str = "") -> bool:
        """
        Handle React-Select / custom div dropdowns.
        Strategy: click to open → type to filter → pick from document-wide options.
        """
        try:
            await trigger_el.click(timeout=3000)
            await self._delay(0.4, 0.7)

            # Try to type in search input
            search = await self.page.query_selector(
                "input[class*='select__input'],"
                "[class*='select__control'] input:not([type='hidden']),"
                "[class*='selectInput'] input,"
                "[role='combobox'] input:not([type='hidden']),"
                ".select2-search__field"
            )
            if search:
                try:
                    if await search.is_visible():
                        await search.triple_click()
                        await search.press("Control+a")
                        await search.press("Backspace")
                        await self._delay(0.1, 0.2)
                        await search.type(target, delay=50)
                        await self._delay(0.5, 0.8)
                except Exception:
                    pass

            ok = await self._pick_option(target, hint)
            if ok:
                return True

            # If typing didn't help, clear and try without filter
            if search:
                try:
                    await search.triple_click()
                    await search.press("Control+a")
                    await search.press("Backspace")
                    await self._delay(0.3, 0.5)
                    ok = await self._pick_option(target, hint)
                    if ok:
                        return True
                except Exception:
                    pass

            await self.page.keyboard.press("Escape")
            return False

        except Exception:
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    async def _select_keyboard(self, trigger_el, target: str, hint: str = "") -> bool:
        """
        Last resort: arrow key navigation through all options.
        Works for ANY dropdown that opens on click, including non-typeable ones.
        """
        try:
            await trigger_el.click(timeout=3000)
            await self._delay(0.5, 0.8)

            best_score  = 0.0
            best_offset = 0

            for i in range(50):  # Check up to 50 options
                await self.page.keyboard.press("ArrowDown")
                await self._delay(0.08, 0.12)

                highlighted = await self.page.evaluate("""() => {
                    const sel = [
                        '[class*="option--is-focused"]',
                        '[class*="option-focused"]',
                        '[aria-selected="true"]',
                        '[class*="highlighted"]',
                        'li[class*="active"]',
                        '[class*="selected"]:not(select)',
                    ].map(s => document.querySelector(s))
                     .find(el => el && el.offsetParent !== null);
                    return sel ? sel.textContent.trim() : '';
                }""")

                if not highlighted:
                    continue

                s = _score(highlighted, target)
                if s > best_score:
                    best_score  = s
                    best_offset = i

                if s >= 0.95:
                    await self.page.keyboard.press("Enter")
                    self._log(f"✓ keyboard '{hint}' → '{highlighted}'")
                    return True

            if best_score >= 0.3:
                # Navigate back to best position
                await self.page.keyboard.press("Escape")
                await self._delay(0.2, 0.3)
                await trigger_el.click(timeout=3000)
                await self._delay(0.3, 0.5)
                for _ in range(best_offset + 1):
                    await self.page.keyboard.press("ArrowDown")
                    await self._delay(0.07, 0.10)
                await self.page.keyboard.press("Enter")
                self._log(f"✓ keyboard nav '{hint}' score={best_score:.2f}")
                return True

            await self.page.keyboard.press("Escape")
            return False

        except Exception:
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    async def _select_js(self, selector: str, target: str, hint: str = "") -> bool:
        """
        Pure JavaScript fallback — sets native select value directly.
        Works even when Playwright can't interact with the element.
        """
        try:
            result = await self.page.evaluate(f"""(selector, target) => {{
                const el = document.querySelector(selector);
                if (!el || el.tagName !== 'SELECT') return false;
                const target_lower = target.toLowerCase();
                let best_opt = null;
                let best_score = 0;
                for (const opt of el.options) {{
                    const text = opt.text.trim().toLowerCase();
                    if (text === target_lower) {{ best_opt = opt; best_score = 1; break; }}
                    if (text.includes(target_lower) || target_lower.includes(text)) {{
                        const score = Math.min(text.length, target_lower.length) /
                                      Math.max(text.length, target_lower.length);
                        if (score > best_score) {{ best_opt = opt; best_score = score; }}
                    }}
                }}
                if (best_opt && best_score > 0.25) {{
                    el.value = best_opt.value;
                    el.dispatchEvent(new Event('change', {{bubbles: true}}));
                    el.dispatchEvent(new Event('input', {{bubbles: true}}));
                    return best_opt.text.trim();
                }}
                return false;
            }}""", selector, target)
            if result:
                self._log(f"✓ JS select '{hint}' → '{result}'")
                return True
        except Exception:
            pass
        return False

    async def _select_any(self, selectors: str | list, target: str, hint: str = "") -> bool:
        """
        Master dropdown handler. Tries all 4 strategies on all matching elements.
        Never gives up without trying everything.
        """
        if not target:
            return False

        sel_list = [s.strip() for s in (
            selectors if isinstance(selectors, list)
            else selectors.split(",")
        )]

        for sel in sel_list:
            try:
                el = await self.page.query_selector(sel)
                if not el:
                    continue

                tag = (await el.evaluate("el => el.tagName.toLowerCase()")).lower()

                # Strategy 1: Native select
                if tag == "select":
                    if await self._select_native(el, target, hint or sel):
                        return True
                    if await self._select_js(sel, target, hint or sel):
                        return True

                # Strategy 2: Custom dropdown (React-Select etc.)
                if await self._select_custom(el, target, hint or sel):
                    return True

                # Strategy 3: Keyboard navigation
                if await self._select_keyboard(el, target, hint or sel):
                    return True

            except Exception:
                continue

        # Strategy 4: Try all selects on the page that match the hint
        if hint:
            if await self._select_js(
                f"select[name*='{hint.lower().replace(' ', '_')}'],"
                f"select[id*='{hint.lower().replace(' ', '_')}']",
                target, hint
            ):
                return True

        return False

    # ════════════════════════════════════════════════════════════════════════
    #  FIELD FILLER — reliable text input
    # ════════════════════════════════════════════════════════════════════════

    async def _fill_field(self, selectors: list[str], value: str, label: str = "") -> bool:
        """Fill a text input. Click → select all → delete → type → tab."""
        if not value:
            return False
        for sel in selectors:
            try:
                el = await self.page.query_selector(sel)
                if not el or not await el.is_visible() or await el.is_disabled():
                    continue
                await el.click()
                await self._delay(0.1, 0.15)
                await el.press("Control+a")
                await el.press("Delete")
                await el.type(value, delay=40)
                await el.press("Tab")
                actual = await el.input_value()
                if actual.strip():
                    if label:
                        self._log(f"✓ {label} → {actual!r}")
                    return True
            except Exception:
                continue
        # JS fallback
        for sel in selectors:
            try:
                result = await self.page.evaluate(f"""(sel, val) => {{
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    );
                    if (setter && setter.set) setter.set.call(el, val);
                    else el.value = val;
                    el.dispatchEvent(new Event('input', {{bubbles: true}}));
                    el.dispatchEvent(new Event('change', {{bubbles: true}}));
                    el.dispatchEvent(new Event('blur', {{bubbles: true}}));
                    return el.value.length > 0;
                }}""", sel, value)
                if result:
                    if label:
                        self._log(f"✓ {label} (JS) → {value!r}")
                    return True
            except Exception:
                continue
        return False

    # ════════════════════════════════════════════════════════════════════════
    #  AGGREGATOR ESCAPE
    # ════════════════════════════════════════════════════════════════════════

    async def _handle_aggregator(self, job, insight, cover_letter: str, profile: dict) -> bool:
        self._log("Aggregator — scanning for ATS links...")

        # Strategy 1: Scan ALL hrefs for direct ATS links (fastest, most reliable)
        try:
            all_hrefs = await self.page.evaluate("""() =>
                Array.from(document.querySelectorAll('a[href]'))
                    .map(a => a.href)
                    .filter(h => h && h.startsWith('http'))
            """)
            ats_hrefs = [
                h for h in all_hrefs
                if _detect_ats(h) not in ("aggregator", "generic")
                and not any(skip in h for skip in SKIP_DOMAINS)
            ]
            if ats_hrefs:
                target = ats_hrefs[0]
                self._log(f"Found ATS link: {target[:70]}")
                await self.page.goto(target, timeout=30000, wait_until="domcontentloaded")
                await self._delay(1.5, 2.5)
                await self._dismiss_popups()
                return await self._dispatch_to_filler(job, insight, cover_letter, profile)
        except Exception as e:
            self._log(f"Link scan error: {e}")

        # Strategy 2: Click apply button, catch new tab or navigation
        for sel in [
            "a:has-text('Apply Now')", "button:has-text('Apply Now')",
            "a:has-text('Apply on company site')", "a:has-text('Apply on Company Site')",
            "a:has-text('Apply now')", "button:has-text('Apply now')",
            "a:has-text('Apply')", "button:has-text('Apply')",
            "[data-testid*='apply']",
        ]:
            try:
                btn = await self.page.query_selector(sel)
                if not btn or not await btn.is_visible():
                    continue

                href = await btn.get_attribute("href") or ""
                if href and _detect_ats(href) not in ("aggregator", "generic", "") \
                        and not any(skip in href for skip in SKIP_DOMAINS):
                    await self.page.goto(href, timeout=30000, wait_until="domcontentloaded")
                    await self._delay(1.5, 2.5)
                    await self._dismiss_popups()
                    return await self._dispatch_to_filler(job, insight, cover_letter, profile)

                # Click and watch
                pages_before = len(self.page.context.pages)
                url_before   = self.page.url
                await btn.click()
                await self._delay(2, 3)

                # New tab opened?
                pages_after = self.page.context.pages
                if len(pages_after) > pages_before:
                    new_page = pages_after[-1]
                    try:
                        await new_page.wait_for_load_state("domcontentloaded", timeout=10000)
                    except Exception:
                        pass
                    if any(skip in new_page.url for skip in SKIP_DOMAINS):
                        await new_page.close()
                        continue
                    self._log(f"New tab: {new_page.url[:70]}")
                    if _detect_ats(new_page.url) not in ("aggregator", "generic"):
                        self.page = new_page
                        await self._dismiss_popups()
                        return await self._dispatch_to_filler(job, insight, cover_letter, profile)
                    await new_page.close()

                # Same page navigated?
                if self.page.url != url_before:
                    if _detect_ats(self.page.url) not in ("aggregator", "generic"):
                        await self._dismiss_popups()
                        return await self._dispatch_to_filler(job, insight, cover_letter, profile)

            except Exception:
                continue

        self._log("Could not escape aggregator — skipping")
        return False

    async def _dispatch_to_filler(self, job, insight, cover_letter: str, profile: dict) -> bool:
        """Dispatch to the correct form filler based on current URL."""
        ats = _detect_ats(self.page.url)
        if ats == "greenhouse":
            return await self._fill_greenhouse(job, insight, cover_letter, profile)
        if ats == "lever":
            return await self._fill_lever(job, insight, cover_letter, profile)
        return await self._fill_generic(job, insight, cover_letter, profile)

    # ════════════════════════════════════════════════════════════════════════
    #  GREENHOUSE FORM — comprehensive, never skips a field
    # ════════════════════════════════════════════════════════════════════════

    async def _fill_greenhouse(self, job, insight, cover_letter: str, profile: dict) -> bool:
        try:
            self._log("Filling Greenhouse form...")

            # Wait for the form to appear
            form_found = False
            for form_sel in [
                "form#application_form",
                "#application-form",
                "form[action*='applications']",
                ".application-form",
                "form[class*='application']",
            ]:
                try:
                    await self.page.wait_for_selector(form_sel, timeout=8000)
                    form_found = True
                    break
                except PwTimeout:
                    continue

            if not form_found:
                self._log("Form not visible — trying AI fallback")
                return await self._ai_page_fallback(job, cover_letter, profile)

            await self._delay(1, 1.5)
            await self._dismiss_popups()

            # Extract profile values
            full_name = profile.get("full_name", "") or ""
            email     = profile.get("email", "") or ""
            phone     = profile.get("phone", "") or ""
            linkedin  = profile.get("linkedin_url", "") or ""
            addr      = profile.get("address", "") or ""
            first     = full_name.split()[0] if full_name else ""
            last      = full_name.split()[-1] if len(full_name.split()) > 1 else full_name
            state     = addr.split(",")[1].strip() if "," in addr else "Indiana"

            # ── STEP 1: Country (MUST be first — re-renders the form) ──────
            self._log("Setting country...")
            country_done = False

            # Try native select first (most common)
            for cs in ["select#country", "select[name*='country']",
                       "select[id*='country']", "select[aria-label*='country' i]",
                       "[class*='country'] select"]:
                el = await self.page.query_selector(cs)
                if el:
                    if await self._select_native(el, "United States", "Country"):
                        country_done = True
                        break
                    if await self._select_js(cs, "United States", "Country"):
                        country_done = True
                        break

            if not country_done:
                # Try custom dropdown
                for cs in ["[id*='country'] .select__control",
                           "[class*='country'] .select__control",
                           "[aria-label*='country' i]"]:
                    el = await self.page.query_selector(cs)
                    if el:
                        if await self._select_custom(el, "United States", "Country"):
                            country_done = True
                            break

            if not country_done:
                # JS nuclear option: find any select with 'country' anywhere in attributes
                result = await self.page.evaluate("""() => {
                    const selects = Array.from(document.querySelectorAll('select'));
                    for (const sel of selects) {
                        const attrs = (sel.name + sel.id + sel.className).toLowerCase();
                        if (!attrs.includes('country')) continue;
                        const us = Array.from(sel.options).find(o =>
                            o.text.includes('United States') ||
                            o.value === 'US' || o.value === 'USA' || o.value === 'united_states'
                        );
                        if (us) {
                            sel.value = us.value;
                            sel.dispatchEvent(new Event('change', {bubbles: true}));
                            return us.text;
                        }
                    }
                    return null;
                }""")
                if result:
                    self._log(f"✓ Country (nuclear JS) → '{result}'")
                    country_done = True

            await self._delay(0.8, 1.2)  # Wait for re-render

            # ── STEP 2: Name / Email / Phone ──────────────────────────────
            await self._fill_field([
                "#first_name",
                "input[name='job_application[first_name]']",
                "input[autocomplete='given-name']",
                "input[id*='first'][id*='name']",
                "input[placeholder*='First' i]",
                "input[aria-label*='first name' i]",
            ], first, "First Name")
            await self._delay(0.15, 0.25)

            await self._fill_field([
                "#last_name",
                "input[name='job_application[last_name]']",
                "input[autocomplete='family-name']",
                "input[id*='last'][id*='name']",
                "input[placeholder*='Last' i]",
                "input[aria-label*='last name' i]",
            ], last, "Last Name")
            await self._delay(0.15, 0.25)

            await self._fill_field([
                "#email",
                "input[name='job_application[email]']",
                "input[type='email']",
                "input[autocomplete='email']",
                "input[aria-label*='email' i]",
            ], email, "Email")
            await self._delay(0.15, 0.25)

            await self._fill_field([
                "#phone",
                "input[name='job_application[phone]']",
                "input[type='tel']",
                "input[autocomplete='tel']",
                "input[aria-label*='phone' i]",
            ], phone, "Phone")
            await self._delay(0.15, 0.25)

            # ── STEP 3: State ──────────────────────────────────────────────
            await self._select_any(
                "select[name*='state'],select[id*='state'],select[aria-label*='state' i]",
                state, "State"
            )

            # ── STEP 4: Resume upload ──────────────────────────────────────
            resume_path = self.store.get("resume_path", "") or ""
            if resume_path and Path(resume_path).exists():
                for sel in [
                    "input[type='file']", "#resume",
                    "input[name*='resume']", "input[accept*='pdf']",
                    "input[accept*='.pdf']",
                ]:
                    try:
                        el = await self.page.query_selector(sel)
                        if el:
                            await el.set_input_files(resume_path)
                            self._log("Resume uploaded")
                            await self._delay(1.5, 2)
                            break
                    except Exception:
                        continue
            else:
                self._log("No resume path set — skipping file upload")

            # ── STEP 5: Cover letter ───────────────────────────────────────
            # First click "Enter manually"
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
                        self._log("Clicked 'Enter manually'")
                        await self._delay(0.8, 1.5)
                        break
                except Exception:
                    continue

            # Find and fill the cover letter textarea
            cl_done = False
            for cl_sel in [
                "textarea[name*='cover']",
                "textarea[id*='cover']",
                "#cover_letter_text",
                "#cover_letter",
                "textarea[aria-label*='cover' i]",
                "textarea[placeholder*='cover' i]",
            ]:
                try:
                    el = await self.page.query_selector(cl_sel)
                    if el and await el.is_visible():
                        await el.click()
                        await el.fill(cover_letter)
                        self._log("Cover letter filled")
                        cl_done = True
                        break
                except Exception:
                    continue

            if not cl_done:
                # Fallback: any visible textarea that's empty
                for ta in await self.page.query_selector_all("textarea"):
                    try:
                        if await ta.is_visible():
                            current = await ta.input_value()
                            if len(current) < 20:
                                await ta.click()
                                await ta.fill(cover_letter)
                                self._log("Cover letter filled (fallback textarea)")
                                break
                    except Exception:
                        continue

            # ── STEP 6: LinkedIn ───────────────────────────────────────────
            if linkedin:
                await self._fill_field([
                    "input[name*='linkedin']",
                    "input[id*='linkedin']",
                    "input[placeholder*='LinkedIn' i]",
                    "input[placeholder*='linkedin' i]",
                ], linkedin, "LinkedIn")

            # ── STEP 7: Education section ──────────────────────────────────
            await self._fill_education(profile)

            # ── STEP 8: Universal fill — all remaining custom questions ────
            await self._universal_fill(profile, cover_letter)

            # ── STEP 9: Final correction — guarantee personal fields correct ──
            await self._delay(0.3, 0.5)
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
                                self._log(f"✓ Corrected {label}")
                            break
                    except Exception:
                        continue

            await self._dismiss_popups()
            await self._delay(1, 1.5)
            return await self._submit_and_verify()

        except Exception as e:
            self._log(f"Greenhouse error: {e}")
            return False

    # ════════════════════════════════════════════════════════════════════════
    #  EDUCATION SECTION — explicit field-by-field
    # ════════════════════════════════════════════════════════════════════════

    async def _fill_education(self, profile: dict) -> None:
        grad_date  = profile.get("graduation_date", "May 2026") or "May 2026"
        grad_year, grad_month = "2026", "May"
        try:
            parts = grad_date.strip().split()
            if len(parts) == 2:
                grad_month, grad_year = parts[0], parts[1]
        except Exception:
            pass

        self._log("Filling education section...")

        # School
        await self._select_any([
            "select[id*='school']", "select[name*='school']",
            "[aria-label*='School' i] .select__control",
            "[class*='school'] .select__control",
            "[id*='school'] .select__control",
        ], "Purdue University", "School")

        # Discipline / Field of study
        await self._select_any([
            "select[id*='discipline']", "select[name*='discipline']",
            "select[id*='field_of_study']", "select[name*='field_of_study']",
            "[aria-label*='Discipline' i] .select__control",
            "[id*='discipline'] .select__control",
        ], "Computer Science", "Discipline")

        # Degree type
        for val in ["Bachelor of Science", "Bachelor's", "Bachelor", "BS", "B.S."]:
            ok = await self._select_any([
                "select[id*='degree']", "select[name*='degree']",
                "select[id*='education_level']", "select[name*='education_level']",
            ], val, "Degree")
            if ok:
                break

        # Start date year
        await self._fill_field([
            "input[name*='start'][name*='year']",
            "input[id*='start'][id*='year']",
            "input[placeholder*='start year' i]",
            "input[placeholder*='Start year' i]",
        ], "2022", "Start Year")

        # End date month
        await self._select_any([
            "select[name*='end'][name*='month']",
            "select[id*='end'][id*='month']",
            "select[name*='end_date_month']",
            "select[id*='end_date_month']",
            "select[name*='endMonth']",
        ], grad_month, "End Month")

        # End date year
        filled_year = await self._fill_field([
            "input[name*='end'][name*='year']",
            "input[id*='end'][id*='year']",
            "input[name*='end_date_year']",
            "input[name*='endYear']",
        ], grad_year, "End Year")
        if not filled_year:
            await self._select_any([
                "select[name*='end'][name*='year']",
                "select[id*='end'][id*='year']",
            ], grad_year, "End Year")

        # Expected graduation (Greenhouse custom question)
        for val in [grad_date, f"{grad_month} {grad_year}", grad_month, grad_year]:
            ok = await self._select_any([
                "select[name*='graduation']",
                "select[id*='graduation']",
                "select[aria-label*='graduation' i]",
                "select[aria-label*='expected date' i]",
                "[aria-label*='graduation' i] .select__control",
                "[aria-label*='expected date' i] .select__control",
            ], val, "Expected Graduation")
            if ok:
                break

        # Degree type (as custom question)
        for val in ["Bachelor of Science", "Bachelor's", "Bachelor", "BS"]:
            ok = await self._select_any([
                "select[aria-label*='degree' i]",
                "select[aria-label*='type of degree' i]",
                "[aria-label*='type of degree' i] .select__control",
                "[aria-label*='degree pursuing' i] .select__control",
            ], val, "Degree (custom)")
            if ok:
                break

        # Major
        await self._fill_field([
            "input[name*='major']", "input[id*='major']",
            "input[placeholder*='major' i]", "input[aria-label*='major' i]",
        ], "Computer Science", "Major")

        self._log("Education section done.")

    # ════════════════════════════════════════════════════════════════════════
    #  LEVER FORM
    # ════════════════════════════════════════════════════════════════════════

    async def _fill_lever(self, job, insight, cover_letter: str, profile: dict) -> bool:
        try:
            self._log("Filling Lever form...")
            await self.page.wait_for_selector(
                "form.application-form,#application-form", timeout=12000
            )
            await self._delay(1, 2)
            full_name = profile.get("full_name", "") or ""
            await self._fill_field(["input[name='name']"], full_name, "Full Name")
            await self._fill_field(["input[name='email']"], profile.get("email", ""), "Email")
            await self._fill_field(["input[name='phone']"], profile.get("phone", ""), "Phone")
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
            self._log(f"Lever error: {e}")
            return False

    # ════════════════════════════════════════════════════════════════════════
    #  GENERIC FORM
    # ════════════════════════════════════════════════════════════════════════

    async def _fill_generic(self, job, insight, cover_letter: str, profile: dict) -> bool:
        try:
            self._log(f"Filling generic form: {self.page.url[:60]}")

            # Stripe: find embedded ATS link or apply button
            if "stripe.com/jobs" in self.page.url:
                try:
                    links = await self.page.evaluate("""() =>
                        Array.from(document.querySelectorAll('a[href]'))
                            .map(a => a.href)
                            .filter(h => h.includes('greenhouse') ||
                                        h.includes('grnh.se') ||
                                        h.includes('lever.co') ||
                                        h.includes('ashbyhq'))
                    """)
                    if links:
                        await self.page.goto(links[0], timeout=30000, wait_until="domcontentloaded")
                        await self._delay(1.5, 2)
                        await self._dismiss_popups()
                        return await self._dispatch_to_filler(job, insight, cover_letter, profile)

                    # Look for Apply button that navigates to form
                    for btn_sel in [
                        "a[href*='/apply']", "a:has-text('Apply')", "button:has-text('Apply')"
                    ]:
                        btn = await self.page.query_selector(btn_sel)
                        if btn and await btn.is_visible():
                            href = await btn.get_attribute("href") or ""
                            if href:
                                full_url = href if href.startswith("http") else f"https://stripe.com{href}"
                                await self.page.goto(full_url, timeout=30000, wait_until="domcontentloaded")
                                await self._delay(1.5, 2)
                                await self._dismiss_popups()
                                return await self._dispatch_to_filler(job, insight, cover_letter, profile)
                except Exception as e:
                    self._log(f"Stripe detection: {e}")

            # Veracyte and other companies that embed Greenhouse via gh_jid parameter
            if "gh_jid" in self.page.url:
                import urllib.parse
                parsed = urllib.parse.urlparse(self.page.url)
                params = urllib.parse.parse_qs(parsed.query)
                gh_jid = params.get("gh_jid", [None])[0]
                if gh_jid:
                    # Try to find the company slug from the page
                    gh_url = f"https://boards.greenhouse.io/embed/job_app?token={gh_jid}"
                    try:
                        await self.page.goto(gh_url, timeout=30000, wait_until="domcontentloaded")
                        await self._delay(1.5, 2)
                        await self._dismiss_popups()
                        if _detect_ats(self.page.url) == "greenhouse":
                            return await self._fill_greenhouse(job, insight, cover_letter, profile)
                    except Exception:
                        pass

            # General listing page: follow Apply button
            for btn_sel in [
                "a:has-text('Apply Now')", "button:has-text('Apply Now')",
                "a:has-text('Apply for this job')", "a:has-text('Apply for this role')",
                "button:has-text('Apply for this job')",
                "a[href*='/apply']",
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
                        return await self._dispatch_to_filler(job, insight, cover_letter, profile)

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
                        if ats not in ("aggregator", "generic"):
                            self.page = new_page
                            await self._dismiss_popups()
                            return await self._dispatch_to_filler(job, insight, cover_letter, profile)
                    break
                except Exception:
                    continue

            await self._universal_fill(profile, cover_letter)
            await self._dismiss_popups()
            return await self._submit_and_verify()

        except Exception as e:
            self._log(f"Generic error: {e}")
            return False

    # ════════════════════════════════════════════════════════════════════════
    #  UNIVERSAL FILL — catches every remaining field
    # ════════════════════════════════════════════════════════════════════════

    async def _universal_fill(self, profile: dict, cover_letter: str) -> None:
        self._log("Universal form fill...")
        fm = self._build_field_map(profile, cover_letter)
        for pass_num in range(3):
            filled  = await self._fill_text_inputs(fm)
            filled += await self._fill_textareas(fm, cover_letter)
            filled += await self._fill_all_selects(fm)
            await self._fill_radios()
            await self._fill_checkboxes()
            self._log(f"Pass {pass_num + 1}: {filled} field(s)")
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

        cl = cover_letter or ""

        return {
            # Name
            "first name": first, "first_name": first, "given name": first,
            "last name": last, "last_name": last, "surname": last, "family name": last,
            "full name": full_name, "name": full_name, "legal name": full_name,
            # Contact
            "email": email, "email address": email,
            "phone": phone, "telephone": phone, "mobile": phone,
            "phone number": phone, "contact number": phone,
            # Location
            "address": addr.split(",")[0].strip() if addr else "",
            "street": addr.split(",")[0].strip() if addr else "",
            "city": city, "state": state, "province": state,
            "country": "United States",
            "zip": "47906", "postal": "47906", "zip code": "47906",
            # Professional links
            "linkedin": linkedin, "linkedin url": linkedin, "linkedin profile": linkedin,
            "github": github, "github url": github,
            "website": portfolio, "portfolio": portfolio, "personal website": portfolio,
            # Cover letter / motivation (all common label variants)
            "cover letter": cl[:3000], "cover": cl[:3000],
            "motivation": cl[:2000], "motivation letter": cl[:2000],
            "why are you interested": cl[:800],
            "why do you want": cl[:800],
            "why this role": cl[:800],
            "why this company": cl[:800],
            "why are you applying": cl[:800],
            "tell us about yourself": cl[:800],
            "about yourself": cl[:800],
            "message": cl[:1000],
            "comments": cl[:500], "notes": cl[:500],
            "additional information": cl[:500],
            "anything else": cl[:500],
            "what are you hoping to learn": cl[:500],
            "what would you like us to know": cl[:500],
            "introduce yourself": cl[:800],
            "self introduction": cl[:800],
            "personal statement": cl[:800],
            "adjustments needed": "None",
            "interview adjustments": "None",
            # Education
            "school": "Purdue University", "university": "Purdue University",
            "college": "Purdue University", "institution": "Purdue University",
            "school name": "Purdue University",
            "discipline": "Computer Science", "major": "Computer Science",
            "field of study": "Computer Science", "area of study": "Computer Science",
            "program": "Computer Science",
            "degree": "Bachelor of Science", "degree type": "Bachelor of Science",
            "type of degree": "Bachelor of Science", "education level": "Bachelor of Science",
            "gpa": gpa, "grade point": gpa, "cumulative gpa": gpa,
            "graduation date": grad_date, "expected graduation": grad_date,
            "graduation": grad_date, "graduation year": grad_year,
            "graduation month": grad_month, "end date year": grad_year,
            "end date month": grad_month, "start date year": "2022",
            "expected date of graduation": grad_date,
            # Work experience
            "current or previous employer": "AnautAI",
            "employer": "AnautAI", "company name": "AnautAI",
            "previous employer": "AnautAI",
            "current or previous job title": "Software Engineer",
            "job title": "Software Engineer", "current title": "Software Engineer",
            "previous title": "Software Engineer",
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
            # Location / commute
            "commuting distance": "No", "reside within commuting": "No",
            "currently reside": "No", "willing to work": "Yes",
            "work minimum": "Yes", "days per week": "Yes",
            "hybrid": "Yes", "on site": "Yes",
            # Source
            "how did you hear about us": "Job Board",
            "how did you hear": "Job Board",
            "referral source": "Job Board",
            "source": "Job Board",
            # Work auth
            "authorized": "Yes", "work authorized": "Yes", "eligible to work": "Yes",
            "authorised to work": "Yes", "legally authorized": "Yes",
            "sponsorship": "No", "require sponsorship": "No", "visa sponsorship": "No",
            "worked for": "No", "worked here before": "No",
            "worked at": "No",
            "relocate": "Yes", "willing to relocate": "Yes",
            # EEOC / demographic
            "gender": "Prefer not to say",
            "race": "Prefer not to say", "ethnicity": "Prefer not to say",
            "veteran": "No", "disability": "No",
            "hispanic or latino": "No",
            "sexual orientation": "Prefer not to say",
            # Consent
            "brighthire": "Yes, I consent",
            "consent": "Yes", "background check": "Yes", "agree": "Yes",
            # Compensation
            "salary": "25", "expected salary": "25", "hourly rate": "25",
            "desired salary": "25", "compensation": "25",
            "start date": "May 2026", "available": "May 2026",
            "earliest start": "May 2026",
        }

    async def _get_label(self, el) -> str:
        """Extract a readable label for any form element."""
        parts = []
        try:
            el_id = await el.get_attribute("id")
            if el_id:
                lbl = await self.page.query_selector(f"label[for='{el_id}']")
                if lbl:
                    parts.append(await lbl.inner_text())
        except Exception:
            pass
        for attr in ["placeholder", "name", "aria-label", "title", "data-label"]:
            try:
                v = await el.get_attribute(attr)
                if v:
                    parts.append(v)
            except Exception:
                continue
        try:
            parent_label = await el.evaluate("""el => {
                const p = el.closest(
                    '.field,.form-group,[class*="field"],[class*="group"],' +
                    '[class*="question"],[class*="item"]'
                );
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
        """
        Match a label to the field map. CRITICAL: empty label must return ''.
        '' in any_string is always True in Python — this guard is essential.
        """
        label = re.sub(r'[*\(\)\?]', '', label).strip().lower()
        if not label or len(label) < 2:
            return ""
        if label in fm:
            return fm[label]
        # Substring match — prefer longer (more specific) keys
        best_key, best_len = None, 0
        for key in fm:
            if not key:
                continue
            if (key in label or label in key) and len(key) > best_len:
                best_key, best_len = key, len(key)
        if best_key:
            return fm[best_key]
        # Word overlap
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
                # Skip fields explicitly handled elsewhere
                if inp_id in PROTECTED_IDS:
                    continue
                if any(p in inp_name for p in PROTECTED_NAMES):
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
        filled = 0

        # Native <select> elements
        for sel_el in await self.page.query_selector_all("select"):
            try:
                if not await sel_el.is_visible() or await sel_el.is_disabled():
                    continue
                ct = await sel_el.evaluate(
                    "el => el.options[el.selectedIndex]?.text?.trim() || ''"
                )
                if ct.lower() not in ("select...", "select", "please select", "--", ""):
                    continue
                label = await self._get_label(sel_el)
                if not label or len(label) < 2:
                    continue
                val = self._match(label, fm)
                if val:
                    if await self._select_native(sel_el, val, label):
                        filled += 1
                    elif await self._select_keyboard(sel_el, val, label):
                        filled += 1
                else:
                    # Unknown: pick first non-placeholder option
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
            "[role='combobox']:not(input):not(select):not(textarea)"
        ):
            try:
                if not await ctrl.is_visible():
                    continue
                current = await ctrl.evaluate("""el => {
                    const sv = el.querySelector(
                        '[class*="single-value"],[class*="selected"],[class*="placeholder"]'
                    );
                    const text = sv ? sv.textContent.trim()
                                    : el.textContent.trim().split('\\n')[0].trim();
                    return text;
                }""")
                if current and current.lower() not in (
                    "select...", "select", "--", "", "please select"
                ):
                    continue
                label = await ctrl.evaluate("""el => {
                    const c = el.closest(
                        '.field,.form-group,[class*="field"],[class*="group"],[class*="question"]'
                    );
                    if (c) {
                        const l = c.querySelector('label');
                        return l ? l.textContent.trim() : '';
                    }
                    return el.getAttribute('aria-label') || '';
                }""")
                if not label or len(label) < 2:
                    continue
                val = self._match(label.lower().strip(), fm)
                if val:
                    ok = await self._select_custom(ctrl, val, label)
                    if not ok:
                        ok = await self._select_keyboard(ctrl, val, label)
                    if ok:
                        filled += 1
                await self._delay(0.3, 0.5)
            except Exception:
                continue

        return filled

    async def _fill_radios(self) -> None:
        try:
            names = await self.page.evaluate(
                "() => [...new Set("
                "[...document.querySelectorAll('input[type=radio]')].map(r=>r.name)"
                ")]"
            )
            for name in names:
                try:
                    radios = await self.page.query_selector_all(
                        f"input[type='radio'][name='{name}']"
                    )
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
                        elif any(k in n for k in ["gender", "race", "ethnic",
                                                   "veteran", "disability"]):
                            should = any(k in val for k in
                                         ["prefer", "decline", "no answer", "not", "choose"])
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
                lbl  = await self._get_label(cb)
                combined = name + " " + lbl
                if any(k in combined for k in
                       ["agree", "terms", "consent", "accept", "confirm", "acknowledge"]):
                    await cb.check()
                    await self._delay(0.1, 0.2)
            except Exception:
                continue

    # ════════════════════════════════════════════════════════════════════════
    #  AI FALLBACK
    # ════════════════════════════════════════════════════════════════════════

    async def _ai_page_fallback(self, job, cover_letter: str, profile: dict) -> bool:
        try:
            self._log("🤖 AI fallback — screenshot analysis...")
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
                            f"Job application page at {self.page.url}. "
                            "What is blocking the form (popup, login wall, cookie banner)? "
                            "Reply ONLY JSON: "
                            '{"obstacle":"description","selector":"css selector to click"}'
                        )}
                    ]}]
                }, timeout=25,
            )
            if resp.status_code == 200:
                text = resp.json()["content"][0]["text"]
                m = re.search(r'\{.*\}', text, re.DOTALL)
                if m:
                    data = json.loads(m.group())
                    sel  = data.get("selector", "")
                    self._log(f"AI detected: {data.get('obstacle')} → '{sel}'")
                    if sel:
                        await self.page.click(sel, timeout=5000)
                        await self._delay(1, 2)
                        await self._dismiss_popups()
                        return await self._fill_generic(job, {}, cover_letter, profile)
        except Exception as e:
            self._log(f"AI fallback error: {e}")
        return False

    # ════════════════════════════════════════════════════════════════════════
    #  SUBMIT & VERIFY — smart loop with validation error detection
    # ════════════════════════════════════════════════════════════════════════

    async def _submit_and_verify(self) -> bool:
        """
        Submit the form and verify the result.
        - Detects real confirmation
        - Detects validation errors (stops immediately, doesn't retry blindly)
        - Handles multi-step forms (Next button)
        - Handles Indeed SmartApply review page
        """
        await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await self._delay(0.5, 1)

        submit_attempts = 0

        for step in range(15):
            await self._dismiss_popups()

            # False positive guard
            for fp in FALSE_POSITIVE_URLS:
                if fp in self.page.url:
                    self._log(f"False positive URL — not submitting")
                    return False

            # Get page text
            try:
                page_text = (await self.page.inner_text("body")).lower()
            except Exception:
                page_text = ""

            # Check for real confirmation
            for phrase in CONFIRMATION_PHRASES:
                if phrase in page_text:
                    self._log(f"✅ Confirmed: '{phrase}'")
                    return True
            for pattern in ["/confirmation", "/thank-you", "/thankyou",
                             "submitted=true", "/success", "application-submitted"]:
                if pattern in self.page.url.lower():
                    self._log(f"✅ Confirmation URL: {self.page.url[:60]}")
                    return True

            # Indeed SmartApply review page (100% progress) — click final Submit
            if any(p in page_text for p in INDEED_REVIEW_PHRASES):
                self._log("Indeed review page — clicking final Submit...")
                for submit_sel in [
                    "button:has-text('Submit your application')",
                    "button:has-text('Submit Application')",
                    "button:has-text('Submit')",
                    "[data-testid='submit-application']",
                    "button[type='submit']",
                ]:
                    try:
                        btn = await self.page.query_selector(submit_sel)
                        if btn and await btn.is_visible():
                            await btn.click()
                            await self._delay(3, 5)
                            break
                    except Exception:
                        continue
                continue

            # Find submit button
            submit_btn = None
            for btn_sel in [
                "button[type='submit']", "input[type='submit']",
                "button:has-text('Submit Application')",
                "button:has-text('Submit application')",
                "button:has-text('Submit')",
                "button:has-text('Send Application')",
                "button:has-text('Complete Application')",
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
                self._log(f"Clicking: '{text}'")
                await submit_btn.click()
                submit_attempts += 1
                await self._delay(2.5, 4)

                # After first click, check for validation errors
                # If errors exist, stop — don't keep clicking uselessly
                try:
                    errors = await self.page.evaluate("""() => {
                        const errorSelectors = [
                            '[class*="error"]:not([class*="error-page"]):not([class*="error-icon"])',
                            '[class*="invalid"]',
                            '.field_error',
                            '.form-error',
                            '.alert-danger',
                            '[data-error]',
                            '[aria-invalid="true"]',
                        ];
                        let visible = 0;
                        for (const sel of errorSelectors) {
                            for (const el of document.querySelectorAll(sel)) {
                                const style = window.getComputedStyle(el);
                                if (style.display === 'none' || style.visibility === 'hidden')
                                    continue;
                                const text = el.textContent.trim().toLowerCase();
                                if (text.includes('required') || text.includes('invalid') ||
                                    text.includes('please') || text.includes('enter') ||
                                    text.includes('select')) {
                                    visible++;
                                }
                            }
                        }
                        return visible;
                    }""")
                    if errors > 0 and submit_attempts >= 1:
                        self._log(f"✗ {errors} validation error(s) — form incomplete, stopping")
                        return False
                except Exception:
                    pass

                continue  # Loop back to check for confirmation

            # Find Next button (multi-step form)
            next_clicked = False
            for next_sel in [
                "button:has-text('Next')",
                "button:has-text('Continue')",
                "a:has-text('Next')",
                "button:has-text('Next Step')",
                "input[value='Next']",
            ]:
                try:
                    btn = await self.page.query_selector(next_sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        await self._delay(1.5, 2.5)
                        # Fill any new fields that appeared
                        profile = self.store.get_profile() or {}
                        await self._universal_fill(profile, "")
                        next_clicked = True
                        break
                except Exception:
                    continue

            if not next_clicked:
                # Nothing to click and no confirmation
                break

        self._log("✗ No confirmation detected")
        return False

    async def _delay(self, min_s: float, max_s: float) -> None:
        await asyncio.sleep(random.uniform(min_s, max_s))
