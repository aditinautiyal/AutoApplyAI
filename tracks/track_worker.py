"""
tracks/track_worker.py  —  FINAL VERSION
All function signatures verified against actual codebase.
Includes: approval dialog, aggregator escape, universal form fill,
          verified dropdown selection, strict submission confirmation.
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

# ── Optional imports — verified signatures ────────────────────────────────────

try:
    from tracks.cover_letter_gen import generate_cover_letter as _gcl
    # Signature: generate_cover_letter(job_title, company, job_description, insight) -> str
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
            "My background in Computer Science and hands-on project experience make me "
            "a strong candidate. I look forward to contributing to your team."
        )

try:
    from tracks.humanizer_check import ensure_humanized
    # Signature: ensure_humanized(text, company, title) -> (str, float, int)
    _has_humanizer = True
except ImportError:
    _has_humanizer = False

try:
    from research.company_researcher import research_company
    # Signature: async research_company(company, title, description) -> dict
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
    # Signature: request_approval(job_data, insight, cover_letter) -> (action, cover_letter)
    _has_approval = True
except ImportError:
    _has_approval = False

# ─── Constants ────────────────────────────────────────────────────────────────

PROFILE_PATH_BASE = Path.home() / ".autoapplyai"

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
]

FALSE_POSITIVE_URLS = [
    "stripe.com/jobs/search",
    "simplyhired.com",
    "indeed.com",
    "linkedin.com/jobs",
    "glassdoor.com",
]

POPUP_SELECTORS = [
    "button[data-provides='cookie-consent-accept-all']",
    "button:has-text('Accept all cookies')",
    "button:has-text('Accept All Cookies')",
    "button:has-text('Accept all')",
    "button:has-text('Accept All')",
    "button:has-text('Accept Cookies')",
    "button:has-text('I Accept')",
    "button:has-text('I agree')",
    "button:has-text('Agree')",
    "button:has-text('Allow all')",
    "button:has-text('OK')",
    "button:has-text('Got it')",
    "button:has-text('Dismiss')",
    "button:has-text('Close')",
    "[id*='cookie'] button:has-text('Accept')",
    "[class*='cookie'] button:has-text('Accept')",
    "[id*='consent'] button:has-text('Accept')",
    "[id*='gdpr'] button:not(:has-text('Reject'))",
    "button[aria-label='Close']",
    "button[aria-label='close']",
    ".modal-close",
    "[data-dismiss='modal']",
]

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
    if "simplyhired" in u or "indeed.com" in u or "linkedin.com/jobs" in u or "glassdoor" in u:
        return "aggregator"
    return "generic"


# ─── Option Scoring ───────────────────────────────────────────────────────────

def _score_option(option_text: str, target: str) -> float:
    o = option_text.strip().lower()
    t = target.strip().lower()
    if not o or o in ("select...", "select", "--", "---", "please select", "choose one", "none"):
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


# ─── TrackWorker ──────────────────────────────────────────────────────────────

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

        # Wrap callback — never crashes regardless of signature
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
                    research_company(
                        job.company,
                        job.title,
                        getattr(job, "description", "") or ""
                    ),
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

        # 4. Approval dialog (blocks until user approves/skips)
        if _has_approval:
            try:
                job_data = {
                    "title":       job.title,
                    "company":     job.company,
                    "location":    getattr(job, "location", ""),
                    "platform":    getattr(job, "platform", ""),
                    "url":         job.url,
                    "ats_url":     getattr(job, "ats_url", ""),
                    "description": getattr(job, "description", ""),
                    "score":       getattr(job, "score", 0),
                }
                loop = asyncio.get_event_loop()
                action, cl = await loop.run_in_executor(
                    None, lambda: request_approval(job_data, insight, cl)
                )
                if action == "skip":
                    print(f"[Track {self.track_id}] Skipped by user: {job.title}")
                    self.pool.mark_done(job.job_id, "skipped")
                    return
            except Exception as e:
                print(f"[Track {self.track_id}] Approval error: {e} — auto-approving")

        # 5. Apply
        success = await self._apply(job, insight, cl)
        if success:
            print(f"[Track {self.track_id}] ✅ CONFIRMED SUBMISSION: {job.title} @ {job.company}")
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
                    return await self._fill_generic(job, insight, cover_letter, profile)
            return await self._fill_generic(job, insight, cover_letter, profile)

        except PwTimeout:
            print(f"[Track {self.track_id}] Page timeout")
            return False
        except Exception as e:
            print(f"[Track {self.track_id}] Apply error: {e}")
            return False

    # ── Aggregator Escape ─────────────────────────────────────────────────────

    async def _handle_aggregator(self, job, insight, cover_letter: str, profile: dict) -> bool:
        print(f"[Track {self.track_id}] Aggregator — finding Apply button...")
        profile = self.store.get_profile() or {}

        # ── Strategy 1: Scan ALL links on page for direct ATS URLs (no clicking needed) ──
        try:
            all_links = await self.page.evaluate("""() => {
                return Array.from(document.querySelectorAll('a[href]'))
                    .map(a => a.href)
                    .filter(h => h && h.startsWith('http'));
            }""")
            ats_links = [h for h in all_links if _detect_ats(h) not in ("aggregator", "generic")]
            if ats_links:
                best = ats_links[0]
                ats_type = _detect_ats(best)
                print(f"[Track {self.track_id}] Found ATS link in page: {best[:70]} → {ats_type}")
                await self.page.goto(best, timeout=30000, wait_until="domcontentloaded")
                await self._delay(1.5, 2.5)
                await self._dismiss_popups()
                if ats_type == "greenhouse":
                    return await self._fill_greenhouse(job, insight, cover_letter, profile)
                elif ats_type == "lever":
                    return await self._fill_lever(job, insight, cover_letter, profile)
                else:
                    return await self._fill_generic(job, insight, cover_letter, profile)
        except Exception as e:
            print(f"[Track {self.track_id}] Link scan error: {e}")

        # ── Strategy 2: Click Apply button and catch new tab OR same-page navigation ──
        apply_selectors = [
            "a:has-text('Apply Now')",
            "button:has-text('Apply Now')",
            "a:has-text('Apply on company site')",
            "a:has-text('Apply on Company Site')",
            "a:has-text('Apply now')",
            "button:has-text('Apply now')",
            "[data-testid*='apply']",
            "a:has-text('Apply')",
            "button:has-text('Apply')",
        ]

        for sel in apply_selectors:
            try:
                btn = await self.page.query_selector(sel)
                if not btn or not await btn.is_visible():
                    continue

                # Check href first — navigate directly without clicking if it's an ATS link
                href = await btn.get_attribute("href") or ""
                if href and _detect_ats(href) not in ("aggregator", "generic", ""):
                    print(f"[Track {self.track_id}] Direct ATS href: {href[:70]}")
                    await self.page.goto(href, timeout=30000, wait_until="domcontentloaded")
                    await self._delay(1.5, 2.5)
                    await self._dismiss_popups()
                    new_ats = _detect_ats(self.page.url)
                    if new_ats == "greenhouse":
                        return await self._fill_greenhouse(job, insight, cover_letter, profile)
                    elif new_ats == "lever":
                        return await self._fill_lever(job, insight, cover_letter, profile)
                    elif new_ats not in ("aggregator",):
                        return await self._fill_generic(job, insight, cover_letter, profile)
                    continue

                # Click and watch for new tab (with short timeout)
                url_before = self.page.url
                pages_before = len(self.page.context.pages)
                await btn.click()
                await self._delay(2, 3)

                # Check if a new tab opened
                pages_after = self.page.context.pages
                if len(pages_after) > pages_before:
                    new_page = pages_after[-1]
                    try:
                        await new_page.wait_for_load_state("domcontentloaded", timeout=10000)
                    except Exception:
                        pass
                    new_url = new_page.url
                    new_ats = _detect_ats(new_url)
                    print(f"[Track {self.track_id}] New tab: {new_url[:70]} → {new_ats}")
                    if new_ats not in ("aggregator", "generic"):
                        self.page = new_page
                        await self._dismiss_popups()
                        if new_ats == "greenhouse":
                            return await self._fill_greenhouse(job, insight, cover_letter, profile)
                        elif new_ats == "lever":
                            return await self._fill_lever(job, insight, cover_letter, profile)
                        else:
                            return await self._fill_generic(job, insight, cover_letter, profile)
                    await new_page.close()

                # Check if same page navigated
                if self.page.url != url_before:
                    new_ats = _detect_ats(self.page.url)
                    if new_ats not in ("aggregator", "generic"):
                        await self._dismiss_popups()
                        if new_ats == "greenhouse":
                            return await self._fill_greenhouse(job, insight, cover_letter, profile)
                        elif new_ats == "lever":
                            return await self._fill_lever(job, insight, cover_letter, profile)
                        else:
                            return await self._fill_generic(job, insight, cover_letter, profile)

            except Exception:
                continue

        print(f"[Track {self.track_id}] Could not escape aggregator — skipping")
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
                    await self._delay(0.3, 0.5)
            except Exception:
                continue
        for tc_sel in ["[id*='terms']", "[class*='terms']", "div:has-text('Terms of Service')"]:
            try:
                tc = await self.page.query_selector(tc_sel)
                if tc:
                    await tc.evaluate("el => el.scrollTop = el.scrollHeight")
            except Exception:
                continue
        if dismissed:
            print(f"[Track {self.track_id}] Dismissed {dismissed} popup(s)")

    # ── Dropdown Handling ─────────────────────────────────────────────────────

    async def _smart_select(self, field_hint: str, target_value: str) -> bool:
        if not target_value:
            return False
        el = None
        for sel in field_hint.split(","):
            try:
                el = await self.page.query_selector(sel.strip())
                if el:
                    break
            except Exception:
                continue
        if not el:
            return False
        tag = await el.evaluate("el => el.tagName.toLowerCase()")
        return await self._native_select(el, target_value, field_hint) if tag == "select" \
               else await self._custom_dropdown(el, target_value, field_hint)

    async def _native_select(self, el, target: str, hint: str = "") -> bool:
        try:
            opts = await el.evaluate(
                "el => Array.from(el.options).map(o => ({value:o.value,text:o.text.trim(),idx:o.index}))"
            )
            scored = sorted([(o, _score_option(o["text"], target)) for o in opts],
                            key=lambda x: x[1], reverse=True)
            if not scored or scored[0][1] < DROPDOWN_MIN_CONFIDENCE:
                return False
            best = scored[0][0]
            if best["idx"] == 0 and best["value"] in ("", "0", None) and len(scored) > 1:
                best = scored[1][0]
            await el.select_option(index=best["idx"])
            selected = await el.evaluate("el => el.options[el.selectedIndex]?.text?.trim() || ''")
            if _score_option(selected, target) >= DROPDOWN_MIN_CONFIDENCE:
                print(f"[Track {self.track_id}] ✓ '{hint}' → '{selected}'")
                return True
            await el.select_option(index=0)
            return False
        except Exception:
            return False

    async def _custom_dropdown(self, trigger_el, target: str, hint: str = "") -> bool:
        try:
            await trigger_el.click(timeout=3000)
            await self._delay(0.4, 0.7)
            search_input = await self.page.query_selector(
                "input[class*='select__input'],[class*='select__control'] input,"
                "[role='combobox'] input,.select2-search__field"
            )
            if search_input:
                try:
                    await search_input.fill("")
                    await search_input.type(target, delay=60)
                    await self._delay(0.5, 0.9)
                except Exception:
                    pass
            option_els = await self.page.query_selector_all(
                "[class*='select__option']:not([class*='disabled']),"
                "[role='option']:not([aria-disabled='true']),"
                ".select2-results__option"
            )
            if not option_els:
                await trigger_el.evaluate("el => el.click()")
                await self._delay(0.5, 0.8)
                option_els = await self.page.query_selector_all(
                    "[class*='select__option'],[role='option']"
                )
            if not option_els:
                await self.page.keyboard.press("Escape")
                return False
            scored_els = []
            for opt_el in option_els:
                try:
                    if not await opt_el.is_visible():
                        continue
                    text = (await opt_el.inner_text()).strip()
                    scored_els.append((opt_el, text, _score_option(text, target)))
                except Exception:
                    continue
            if not scored_els:
                await self.page.keyboard.press("Escape")
                return False
            scored_els.sort(key=lambda x: x[2], reverse=True)
            best_el, best_text, best_score = scored_els[0]
            if best_score < DROPDOWN_MIN_CONFIDENCE:
                await self.page.keyboard.press("Escape")
                return False
            await best_el.scroll_into_view_if_needed()
            await best_el.click(timeout=3000)
            await self._delay(0.3, 0.5)
            print(f"[Track {self.track_id}] ✓ '{hint}' → '{best_text}' ({best_score:.2f})")
            return True
        except Exception:
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    # ── Universal Form Filler ─────────────────────────────────────────────────

    async def _universal_fill(self, profile: dict, cover_letter: str) -> None:
        print(f"[Track {self.track_id}] Universal form fill...")
        fm = self._build_field_map(profile, cover_letter)
        for pass_num in range(3):
            filled  = await self._fill_text_inputs(fm)
            filled += await self._fill_textareas(fm, cover_letter)
            filled += await self._fill_native_selects(fm)
            filled += await self._fill_custom_dropdowns(fm)
            await self._fill_radios()
            await self._fill_checkboxes()
            print(f"[Track {self.track_id}] Pass {pass_num+1}: {filled} field(s) filled")
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
        last  = full_name.split()[-1] if full_name else ""
        city  = "West Lafayette"
        state = "Indiana"
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
            "authorized": "Yes", "work authorized": "Yes", "eligible to work": "Yes",
            "sponsorship": "No", "require sponsorship": "No", "visa sponsorship": "No",
            "relocate": "Yes", "willing to relocate": "Yes",
            "gender": "Prefer not to say", "race": "Prefer not to say",
            "ethnicity": "Prefer not to say", "veteran": "No", "disability": "No",
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
                const p = el.closest('.field,.form-group,[class*="field"],[class*="group"]');
                if (p) { const l = p.querySelector('label,[class*="label"]');
                         return l ? l.textContent.trim() : ''; }
                return '';
            }""")
            if parent_label:
                parts.append(parent_label)
        except Exception:
            pass
        return " ".join(parts).lower().strip()

    def _match(self, label: str, fm: dict) -> str:
        label = re.sub(r'[*\(\)]', '', label).strip().lower()
        if label in fm:
            return fm[label]
        best_key, best_len = None, 0
        for key in fm:
            if (key in label or label in key) and len(key) > best_len:
                best_key, best_len = key, len(key)
        if best_key:
            return fm[best_key]
        lw = set(label.split())
        best_overlap, best_key = 0, None
        for key in fm:
            ov = len(lw & set(key.split()))
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
                current = await inp.input_value()
                if len(current) > 1:
                    continue
                label = await self._get_label(inp)
                val   = self._match(label, fm)
                if not val:
                    continue
                # Safety: never put non-email text into an email field
                inp_type = (await inp.get_attribute("type") or "").lower()
                if inp_type == "email" and "@" not in val:
                    continue
                # Safety: never put cover letter text into a short field
                inp_id = (await inp.get_attribute("id") or "").lower()
                if any(k in inp_id for k in ["first", "last", "name", "phone", "email"]) and len(val) > 100:
                    continue
                await inp.triple_click()
                await inp.fill(val)
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
                val = self._match(await self._get_label(ta), fm) or cover_letter[:800]
                await ta.click()
                await ta.fill(val)
                filled += 1
                await self._delay(0.2, 0.4)
            except Exception:
                continue
        return filled

    async def _fill_native_selects(self, fm: dict) -> int:
        filled = 0
        for sel_el in await self.page.query_selector_all("select"):
            try:
                if not await sel_el.is_visible() or await sel_el.is_disabled():
                    continue
                ct = await sel_el.evaluate("el => el.options[el.selectedIndex]?.text?.trim() || ''")
                if ct.lower() not in ("select...", "select", "please select", "--", ""):
                    continue
                label = await self._get_label(sel_el)
                val   = self._match(label, fm)
                if val:
                    if await self._native_select(sel_el, val, label):
                        filled += 1
                else:
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
        return filled

    async def _fill_custom_dropdowns(self, fm: dict) -> int:
        filled = 0
        for ctrl in await self.page.query_selector_all(
            "[class*='select__control']:not([class*='disabled']),[role='combobox']:not(input)"
        ):
            try:
                if not await ctrl.is_visible():
                    continue
                current = await ctrl.evaluate("""el => {
                    const sv = el.querySelector('[class*="single-value"],[class*="selected"]');
                    return sv ? sv.textContent.trim() : '';
                }""")
                if current and current.lower() not in ("select...", "select", "--", ""):
                    continue
                label = await ctrl.evaluate("""el => {
                    const c = el.closest('.field,.form-group,[class*="field"],[class*="group"]');
                    if (c) { const l = c.querySelector('label'); return l ? l.textContent.trim() : ''; }
                    return el.getAttribute('aria-label') || '';
                }""")
                val = self._match(label.lower().strip(), fm)
                if val and await self._custom_dropdown(ctrl, val, label):
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
                        if any(k in n for k in ["authorized", "eligible"]):
                            should = val in ("yes", "true", "1")
                        elif any(k in n for k in ["sponsor", "visa"]):
                            should = val in ("no", "false", "0")
                        elif any(k in n for k in ["gender", "race", "ethnic", "veteran", "disability"]):
                            should = any(k in val for k in ["prefer", "decline", "no answer"])
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

    # ── Greenhouse ────────────────────────────────────────────────────────────

    async def _fill_greenhouse(self, job, insight, cover_letter: str, profile: dict) -> bool:
        try:
            print(f"[Track {self.track_id}] Filling Greenhouse form...")
            try:
                await self.page.wait_for_selector(
                    "form#application_form,#application-form,form[action*='applications']",
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

            print(f"[Track {self.track_id}] Profile: name={full_name!r} email={email!r} phone={phone!r}")

            # Step 1: Country FIRST — selecting it re-renders the form and clears other fields
            await self._smart_select(
                "select#country,select[name*='country'],select[id*='country']",
                "United States"
            )
            await self._delay(0.8, 1.2)  # Wait for re-render

            # Step 2: State
            state = addr.split(",")[1].strip() if "," in addr else "Indiana"
            await self._smart_select("select[name*='state'],select[id*='state']", state)
            await self._delay(0.3, 0.5)

            # Step 3: Resume upload
            resume_path = self.store.get("resume_path", "") or ""
            if resume_path and Path(resume_path).exists():
                for sel in ["input[type='file']", "#resume", "input[name*='resume']"]:
                    try:
                        el = await self.page.query_selector(sel)
                        if el:
                            await el.set_input_files(resume_path)
                            print(f"[Track {self.track_id}] Resume uploaded")
                            await self._delay(1.5, 2)
                            break
                    except Exception:
                        continue

            # Step 4: Cover letter — click Enter manually first
            for em_sel in [
                "a:has-text('Enter manually')",
                "button:has-text('Enter manually')",
                "label:has-text('Enter manually')",
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

            for cl_sel in ["textarea[name*='cover']", "textarea[id*='cover']", "#cover_letter_text"]:
                try:
                    el = await self.page.query_selector(cl_sel)
                    if el and await el.is_visible():
                        await el.fill(cover_letter)
                        break
                except Exception:
                    continue

            # Step 5: Universal fill for all other fields (education, custom questions etc.)
            await self._universal_fill(profile, cover_letter)

            # Step 6: Force-fill personal info LAST so they always have correct values
            # (country re-render or universal_fill may have put wrong values)
            named_fields = [
                ("#first_name", "job_application[first_name]", first,  "First Name"),
                ("#last_name",  "job_application[last_name]",  last,   "Last Name"),
                ("#email",      "job_application[email]",      email,  "Email"),
                ("#phone",      "job_application[phone]",      phone,  "Phone"),
            ]
            for id_sel, name_attr, val, label in named_fields:
                if not val:
                    continue
                filled = False
                # Try by ID first
                try:
                    el = await self.page.query_selector(id_sel)
                    if el and await el.is_visible():
                        await el.triple_click()
                        await el.fill(val)
                        print(f"[Track {self.track_id}] ✓ {label} → {val!r}")
                        filled = True
                except Exception:
                    pass
                # Fallback: by name attribute
                if not filled:
                    try:
                        el = await self.page.query_selector(f"input[name='{name_attr}']")
                        if el and await el.is_visible():
                            await el.triple_click()
                            await el.fill(val)
                            print(f"[Track {self.track_id}] ✓ {label} (by name) → {val!r}")
                    except Exception:
                        pass
                await self._delay(0.15, 0.3)

            # LinkedIn
            if linkedin:
                await self._fill_input_selector(
                    "input[name*='linkedin'],input[id*='linkedin'],input[placeholder*='LinkedIn' i]",
                    linkedin
                )

            await self._dismiss_popups()
            await self._delay(1, 2)
            return await self._click_submit_and_verify()

        except Exception as e:
            print(f"[Track {self.track_id}] Greenhouse error: {e}")
            return False

    async def _fill_lever(self, job, insight, cover_letter: str, profile: dict) -> bool:
        try:
            print(f"[Track {self.track_id}] Filling Lever form...")
            await self.page.wait_for_selector(
                "form.application-form,#application-form", timeout=12000
            )
            await self._delay(1, 2)
            full_name = profile.get("full_name", "") or ""
            for sel, val in [
                ("input[name='name']",  full_name),
                ("input[name='email']", profile.get("email", "")),
                ("input[name='phone']", profile.get("phone", "")),
            ]:
                await self._fill_input_selector(sel, val)
            try:
                ta = await self.page.query_selector("textarea[name*='comments'],textarea")
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

    async def _fill_generic(self, job, insight, cover_letter: str, profile: dict) -> bool:
        try:
            print(f"[Track {self.track_id}] Filling generic form: {self.page.url[:60]}")

            # Check if this is a job listing page (not an application form)
            # Look for an Apply button that leads to the real form
            for btn_sel in [
                "a:has-text('Apply Now')", "button:has-text('Apply Now')",
                "a:has-text('Apply for this job')", "a:has-text('Apply for this role')",
                "a:has-text('Apply for this position')",
                "button:has-text('Apply for this job')",
                "a[href*='apply']", "button:has-text('Apply')",
            ]:
                try:
                    btn = await self.page.query_selector(btn_sel)
                    if not btn or not await btn.is_visible():
                        continue
                    # Check if href leads to a known ATS
                    href = await btn.get_attribute("href") or ""
                    if href and _detect_ats(href) not in ("generic", "aggregator", ""):
                        await self.page.goto(href, timeout=30000, wait_until="domcontentloaded")
                        await self._delay(1.5, 2.5)
                        await self._dismiss_popups()
                        new_ats = _detect_ats(self.page.url)
                        if new_ats == "greenhouse":
                            return await self._fill_greenhouse(job, insight, cover_letter, profile)
                        elif new_ats == "lever":
                            return await self._fill_lever(job, insight, cover_letter, profile)
                        break
                    # Otherwise click and check for navigation or new tab
                    pages_before = len(self.page.context.pages)
                    url_before = self.page.url
                    await btn.click()
                    await self._delay(2, 3)
                    await self._dismiss_popups()
                    # New tab?
                    pages_after = self.page.context.pages
                    if len(pages_after) > pages_before:
                        new_page = pages_after[-1]
                        try:
                            await new_page.wait_for_load_state("domcontentloaded", timeout=10000)
                        except Exception:
                            pass
                        new_ats = _detect_ats(new_page.url)
                        if new_ats == "greenhouse":
                            self.page = new_page
                            return await self._fill_greenhouse(job, insight, cover_letter, profile)
                        elif new_ats == "lever":
                            self.page = new_page
                            return await self._fill_lever(job, insight, cover_letter, profile)
                    break
                except Exception:
                    continue

            await self._universal_fill(profile, cover_letter)
            await self._dismiss_popups()
            return await self._click_submit_and_verify()
        except Exception as e:
            print(f"[Track {self.track_id}] Generic error: {e}")
            return False

    async def _fill_input_selector(self, selector: str, value: str) -> bool:
        if not value:
            return False
        for sel in selector.split(","):
            try:
                el = await self.page.query_selector(sel.strip())
                if el and await el.is_visible():
                    await el.triple_click()
                    await el.type(value, delay=30)
                    return True
            except Exception:
                continue
        return False

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
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
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

    async def _click_submit_and_verify(self) -> bool:
        await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await self._delay(0.5, 1)

        for _step in range(10):
            await self._dismiss_popups()

            for fp in FALSE_POSITIVE_URLS:
                if fp in self.page.url:
                    print(f"[Track {self.track_id}] False positive URL — not submitting")
                    return False

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

            submit_btn = None
            for btn_sel in [
                "button[type='submit']", "input[type='submit']",
                "button:has-text('Submit Application')",
                "button:has-text('Submit')",
                "button:has-text('Send Application')",
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
                print(f"[Track {self.track_id}] Clicking: '{text}'")
                await submit_btn.click()
                await self._delay(2.5, 4)
                continue

            next_clicked = False
            for next_sel in [
                "button:has-text('Next')", "button:has-text('Continue')", "a:has-text('Next')"
            ]:
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
