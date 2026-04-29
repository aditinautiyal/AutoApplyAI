"""
discovery/playwright_scraper.py
Headless Playwright browser for JS-rendered job sites.

Sites like Built In, Wellfound, and YC are React apps — httpx gets
empty HTML. A real browser renders the JavaScript and sees actual jobs.

Uses headless=True (fast, invisible) — only for READING job listings.
Form filling still uses headless=False in track_worker.py.

Sources:
- Built In Chicago / Dallas / Remote / NYC / Austin / Seattle
- Wellfound (AngelList) — startup ATS links
- Y Combinator Work at a Startup
- Internships.com
"""

import asyncio
import hashlib
import pathlib
import random
import re
from discovery.job_pool import Job, JobScorer, get_pool
from core.settings_store import get_store

SCRAPER_CYCLE = 30 * 60  # 30 minutes between Playwright cycles

UNAUTOMATABLE = [
    "apple", "google", "alphabet", "amazon", "meta", "facebook",
    "microsoft", "netflix", "twitter", "x corp",
    "salesforce", "oracle", "ibm", "cisco", "intel",
    "qualcomm", "nvidia", "walmart",
    "wipro", "infosys", "tata consultancy", "cognizant", "accenture",
    "jabil", "epam", "mantech", "codeweavers", "leidos",
]

BAD_TITLES = [
    "senior", "staff ", "principal", "director", "manager", " lead",
    "vp ", "vice president", "head of", "chief",
    "hr ", "human resources", "recruiter",
    "sap ", "abap", "legal", "counsel", "unpaid", "sr.", "m/w/d",
]

BAD_LOCS = [
    "germany", "berlin", "london", "united kingdom", "france", "paris",
    "india", "bangalore", "mumbai", "australia", "singapore",
]

TECH = [
    "software", "engineer", "developer", "data", "machine learning",
    "ai", "ml", "python", "backend", "frontend", "fullstack",
    "cloud", "devops", "mobile", "computer science", "intern",
    "research", "scientist", "analytics", "algorithm",
]


def _job_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _passes(title: str, location: str, desc: str = "") -> bool:
    tl  = title.lower()
    ll  = (location or "").lower()
    all_text = tl + " " + (desc or "").lower()[:200]

    for kw in BAD_TITLES:
        if kw in tl:
            return False

    if location and ll not in ("remote", "worldwide", "anywhere", ""):
        for bad in BAD_LOCS:
            if bad in ll:
                return False

    return any(s in all_text for s in TECH)


def _is_bad_company(company: str) -> bool:
    cl = company.lower()
    return any(bad in cl for bad in UNAUTOMATABLE)


# ═══════════════════════════════════════════════════════════════════════════════
# Built In cities
# ═══════════════════════════════════════════════════════════════════════════════

BUILTIN_URLS = [
    ("https://www.builtinchicago.org/jobs?title=software+engineer+intern", "Chicago IL"),
    ("https://www.builtinchicago.org/jobs?title=machine+learning+intern", "Chicago IL"),
    ("https://www.builtinchicago.org/jobs?title=data+science+intern", "Chicago IL"),
    ("https://www.builtindallas.com/jobs?title=software+engineer+intern", "Dallas TX"),
    ("https://www.builtindallas.com/jobs?title=machine+learning+intern", "Dallas TX"),
    ("https://builtin.com/jobs/remote?title=software+engineer+intern", "Remote"),
    ("https://builtin.com/jobs/remote?title=machine+learning+intern", "Remote"),
    ("https://builtin.com/jobs/remote?title=data+science+intern", "Remote"),
    ("https://www.builtinnyc.com/jobs?title=software+engineer+intern", "New York NY"),
    ("https://www.builtinaustin.com/jobs?title=software+engineer+intern", "Austin TX"),
    ("https://www.builtinseattle.com/jobs?title=software+engineer+intern", "Seattle WA"),
]


async def _scrape_builtin(page, url: str, location: str) -> list[dict]:
    jobs = []
    try:
        await page.goto(url, wait_until="networkidle", timeout=25000)
        await asyncio.sleep(3)

        # Scroll to load lazy content
        for _ in range(3):
            await page.evaluate("window.scrollBy(0, 800)")
            await asyncio.sleep(1)

        # Extract job cards using Playwright's full DOM
        cards = await page.query_selector_all(
            "li[data-id], div[class*='job-card'], article[class*='job'], "
            "div[class*='JobCard'], li[class*='job'], div[data-testid*='job']"
        )

        if not cards:
            # Fallback: get all job links from page
            links = await page.query_selector_all("a[href*='/job/'], a[href*='/jobs/']")
            for link in links[:30]:
                try:
                    title = (await link.inner_text()).strip()
                    href  = await link.get_attribute("href") or ""
                    if href.startswith("/"):
                        href = "https://builtin.com" + href
                    if title and href and len(title) > 5:
                        jobs.append({
                            "title": title, "company": "",
                            "url": href, "ats_url": href,
                            "description": f"{title}. {location}.",
                            "location": location, "platform": "builtin",
                        })
                except Exception:
                    continue
            return jobs

        for card in cards[:30]:
            try:
                # Title + link
                title_el = (
                    await card.query_selector("h2 a") or
                    await card.query_selector("h3 a") or
                    await card.query_selector("a[class*='title']") or
                    await card.query_selector("a[class*='job']")
                )
                if not title_el:
                    continue

                title = (await title_el.inner_text()).strip()
                href  = await title_el.get_attribute("href") or ""
                if href.startswith("/"):
                    href = "https://builtin.com" + href

                # Company
                company_el = (
                    await card.query_selector("[class*='company']") or
                    await card.query_selector("[class*='employer']") or
                    await card.query_selector("span[class*='name']")
                )
                company = (await company_el.inner_text()).strip() if company_el else ""

                if title and href:
                    jobs.append({
                        "title": title, "company": company,
                        "url": href, "ats_url": href,
                        "description": f"{title} at {company}. {location}.",
                        "location": location, "platform": "builtin",
                    })
            except Exception:
                continue

    except Exception as e:
        print(f"[PWscraper] Built In error ({location}): {e}")

    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# Wellfound
# ═══════════════════════════════════════════════════════════════════════════════

WELLFOUND_URLS = [
    "https://wellfound.com/jobs?q=software+engineer+intern&remote=true",
    "https://wellfound.com/jobs?q=machine+learning+intern",
    "https://wellfound.com/jobs?q=data+science+intern",
    "https://wellfound.com/jobs?q=software+engineer+intern&l=Chicago%2C+IL",
    "https://wellfound.com/jobs?q=AI+engineer+intern",
]


async def _scrape_wellfound(page, url: str) -> list[dict]:
    jobs = []
    try:
        await page.goto(url, wait_until="networkidle", timeout=25000)
        await asyncio.sleep(4)

        for _ in range(3):
            await page.evaluate("window.scrollBy(0, 800)")
            await asyncio.sleep(1.5)

        cards = await page.query_selector_all(
            "div[class*='JobListing'], div[data-test*='JobListing'], "
            "li[class*='job'], div[class*='job-listing'], "
            "a[href*='/jobs/']"
        )

        for card in cards[:25]:
            try:
                link_el = await card.query_selector("a[href*='/jobs/']") or card
                if not hasattr(link_el, 'inner_text'):
                    continue

                title_el = (
                    await card.query_selector("h2") or
                    await card.query_selector("h3") or
                    await card.query_selector("[class*='title']") or
                    link_el
                )
                title = (await title_el.inner_text()).strip()
                if not title or len(title) < 3:
                    continue

                href = await link_el.get_attribute("href") or ""
                if href.startswith("/"):
                    href = "https://wellfound.com" + href

                company_el = await card.query_selector("[class*='company'], [class*='startup']")
                company = (await company_el.inner_text()).strip() if company_el else ""

                loc_el = await card.query_selector("[class*='location']")
                location = (await loc_el.inner_text()).strip() if loc_el else "Remote"

                if title and href:
                    jobs.append({
                        "title": title, "company": company,
                        "url": href, "ats_url": href,
                        "description": f"{title} at {company} startup.",
                        "location": location, "platform": "wellfound",
                    })
            except Exception:
                continue

    except Exception as e:
        print(f"[PWscraper] Wellfound error: {e}")

    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# Y Combinator Work at a Startup
# ═══════════════════════════════════════════════════════════════════════════════

YC_URLS = [
    "https://www.workatastartup.com/jobs?query=software+engineer+intern",
    "https://www.workatastartup.com/jobs?query=machine+learning+intern",
    "https://www.workatastartup.com/jobs?query=data+science+intern",
    "https://www.workatastartup.com/jobs?query=AI+intern",
]


async def _scrape_yc(page, url: str) -> list[dict]:
    jobs = []
    try:
        await page.goto(url, wait_until="networkidle", timeout=25000)
        await asyncio.sleep(4)

        for _ in range(3):
            await page.evaluate("window.scrollBy(0, 800)")
            await asyncio.sleep(1)

        cards = await page.query_selector_all(
            "div[class*='job'], li[class*='job'], "
            "a[href*='/jobs/']"
        )

        for card in cards[:25]:
            try:
                a = (
                    await card.query_selector("a[href*='/jobs/']") or
                    await card.query_selector("a")
                )
                if not a:
                    continue

                title_el = (
                    await card.query_selector("h2") or
                    await card.query_selector("h3") or
                    await card.query_selector("[class*='title']") or
                    a
                )
                title = (await title_el.inner_text()).strip()
                if not title or len(title) < 3:
                    continue

                href = await a.get_attribute("href") or ""
                if href.startswith("/"):
                    href = "https://www.workatastartup.com" + href

                company_el = await card.query_selector("[class*='company'], h3, span")
                company = ""
                if company_el:
                    t = (await company_el.inner_text()).strip()
                    if t != title and len(t) < 60:
                        company = t

                if title and href:
                    jobs.append({
                        "title": title, "company": company,
                        "url": href, "ats_url": href,
                        "description": f"{title} at {company} (YC startup).",
                        "location": "Remote", "platform": "ycombinator",
                    })
            except Exception:
                continue

    except Exception as e:
        print(f"[PWscraper] YC error: {e}")

    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# Internships.com
# ═══════════════════════════════════════════════════════════════════════════════

INTERNSHIPS_URLS = [
    ("https://www.internships.com/search?q=software+engineer&location=Chicago+IL", "Chicago IL"),
    ("https://www.internships.com/search?q=software+engineer&location=Dallas+TX", "Dallas TX"),
    ("https://www.internships.com/search?q=machine+learning&location=Remote", "Remote"),
    ("https://www.internships.com/search?q=data+science&location=Remote", "Remote"),
    ("https://www.internships.com/search?q=AI+engineer&location=Remote", "Remote"),
]


async def _scrape_internships(page, url: str, location: str) -> list[dict]:
    jobs = []
    try:
        await page.goto(url, wait_until="networkidle", timeout=25000)
        await asyncio.sleep(3)

        for _ in range(2):
            await page.evaluate("window.scrollBy(0, 800)")
            await asyncio.sleep(1)

        cards = await page.query_selector_all(
            "div[class*='job'], li[class*='job'], article, "
            "div[class*='listing'], div[class*='card']"
        )

        for card in cards[:20]:
            try:
                a = (
                    await card.query_selector("a[href*='/internship']") or
                    await card.query_selector("h2 a") or
                    await card.query_selector("a")
                )
                if not a:
                    continue

                title = (await a.inner_text()).strip()
                if not title or len(title) < 3:
                    continue

                href = await a.get_attribute("href") or ""
                if href.startswith("/"):
                    href = "https://www.internships.com" + href

                company_el = await card.query_selector("[class*='company'], [class*='employer']")
                company = (await company_el.inner_text()).strip() if company_el else ""

                if title and href:
                    jobs.append({
                        "title": title, "company": company,
                        "url": href, "ats_url": href,
                        "description": f"{title} internship at {company}.",
                        "location": location, "platform": "internships.com",
                    })
            except Exception:
                continue

    except Exception as e:
        print(f"[PWscraper] Internships.com error ({location}): {e}")

    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# Main runner
# ═══════════════════════════════════════════════════════════════════════════════

async def run_playwright_scraping(continuous: bool = True, stop_event=None):
    """
    Runs headless Playwright browser to scrape JS-rendered job sites.
    Runs every 30 minutes alongside the httpx-based discovery.
    """
    pool   = get_pool()
    scorer = JobScorer()

    print("[PWscraper] Starting headless browser scraper...")

    while True:
        if stop_event and stop_event.is_set():
            break

        added = 0
        all_jobs = []

        try:
            from playwright.async_api import async_playwright

            # Use a dedicated persistent profile for the scraper
            scraper_profile = str(
                pathlib.Path.home() / ".autoapplyai" / "scraper_profile"
            )

            async with async_playwright() as pw:
                context = await pw.chromium.launch_persistent_context(
                    user_data_dir=scraper_profile,
                    headless=True,  # Invisible — just reading, not filling forms
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                    ],
                    viewport={"width": 1280, "height": 800},
                )

                page = await context.new_page()

                # Set realistic user agent
                await page.set_extra_http_headers({
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    )
                })

                # ── Built In cities ──────────────────────────────────────────
                print("[PWscraper] Scraping Built In cities...")
                for url, location in BUILTIN_URLS:
                    if stop_event and stop_event.is_set():
                        break
                    jobs = await _scrape_builtin(page, url, location)
                    all_jobs.extend(jobs)
                    print(f"[PWscraper] Built In {location}: {len(jobs)}")
                    await asyncio.sleep(random.uniform(2, 4))

                # ── Wellfound ────────────────────────────────────────────────
                print("[PWscraper] Scraping Wellfound...")
                for url in WELLFOUND_URLS:
                    if stop_event and stop_event.is_set():
                        break
                    jobs = await _scrape_wellfound(page, url)
                    all_jobs.extend(jobs)
                    print(f"[PWscraper] Wellfound: {len(jobs)}")
                    await asyncio.sleep(random.uniform(2, 4))

                # ── YC Work at a Startup ─────────────────────────────────────
                print("[PWscraper] Scraping YC Work at a Startup...")
                for url in YC_URLS:
                    if stop_event and stop_event.is_set():
                        break
                    jobs = await _scrape_yc(page, url)
                    all_jobs.extend(jobs)
                    print(f"[PWscraper] YC: {len(jobs)}")
                    await asyncio.sleep(random.uniform(2, 4))

                # ── Internships.com ──────────────────────────────────────────
                print("[PWscraper] Scraping Internships.com...")
                for url, location in INTERNSHIPS_URLS:
                    if stop_event and stop_event.is_set():
                        break
                    jobs = await _scrape_internships(page, url, location)
                    all_jobs.extend(jobs)
                    print(f"[PWscraper] Internships.com {location}: {len(jobs)}")
                    await asyncio.sleep(random.uniform(2, 3))

                await context.close()

        except Exception as e:
            print(f"[PWscraper] Browser error: {e}")

        # Filter and add to pool
        seen = set()
        for job_data in all_jobs:
            title   = job_data.get("title", "")
            company = job_data.get("company", "")
            loc     = job_data.get("location", "")
            desc    = job_data.get("description", "")
            url     = job_data.get("url", "")

            if not title or not url or url in seen:
                continue
            seen.add(url)

            if not _passes(title, loc, desc):
                continue

            if _is_bad_company(company):
                continue

            job = Job(
                score=0.0,
                job_id=_job_id(url),
                title=title,
                company=company,
                url=url,
                ats_url=job_data.get("ats_url", url),
                platform=job_data.get("platform", "playwright"),
                description=desc,
                location=loc,
            )

            job.score = scorer._keyword_score(job)
            if job.score < 2.5:
                job.score = 2.5

            if pool.add(job):
                added += 1
                print(f"[PWscraper] ✅ {job.title} @ {job.company} "
                      f"({job.platform}, score: {job.score:.1f})")

        print(f"[PWscraper] Cycle complete: +{added} jobs. Pool: {pool.size()}")

        if not continuous:
            break

        print(f"[PWscraper] Sleeping 30 min before next scrape...")
        for _ in range(SCRAPER_CYCLE // 10):
            if stop_event and stop_event.is_set():
                break
            await asyncio.sleep(10)
