"""
discovery/api_discovery.py
Free job API discovery — no scraping, no rate limits, no IP blocks.

KEY FIX: The Muse returns landing page URLs, not direct application forms.
_resolve_ats_url() fetches each Muse job page, finds the real ATS link
(Greenhouse/Lever/Workday/Apple Jobs), and stores THAT as ats_url so the
track worker lands directly on the actual application form.
"""

import asyncio
import hashlib
import re
import httpx
from bs4 import BeautifulSoup
from discovery.job_pool import Job, JobScorer, get_pool
from core.settings_store import get_store

CYCLE_INTERVAL = 20 * 60
REQUEST_TIMEOUT = 20

# Known ATS domains — if a URL contains one of these it's a direct form link
ATS_DOMAINS = [
    "greenhouse.io", "lever.co", "myworkdayjobs.com", "jobs.ashbyhq.com",
    "apply.workable.com", "smartrecruiters.com", "icims.com", "taleo.net",
    "jobs.apple.com", "careers.google.com", "amazon.jobs", "microsoft.com/careers",
    "meta.com/careers", "linkedin.com/jobs", "jobs.lever.co", "boards.greenhouse.io",
    "job-boards.greenhouse.io", "jobs.jobvite.com", "bamboohr.com",
]

BAD_TITLE_KEYWORDS = [
    "senior", "staff ", "principal", "director", "manager", " lead",
    "vp ", "vice president", "head of", "chief", "executive",
    "hr ", "human resources", "recruiter", "recruiting", "talent acquisition",
    "people partner", "people operations",
    "sap ", "abap", "erp ", "oracle ",
    "account executive", "account manager",
    "marketing manager", "marketing director",
    "legal", "paralegal", "counsel", "attorney",
    "m/w/d", "m/f/d", "(m/w", "(f/m",
    "unpaid", "sr.", "sr ",
]

BAD_LOCATIONS = [
    "germany", "berlin", "munich", "hamburg", "frankfurt", "cologne",
    "united kingdom", "london", "manchester",
    "france", "paris",
    "netherlands", "amsterdam",
    "spain", "madrid", "barcelona",
    "poland", "warsaw",
    "india", "bangalore", "mumbai", "hyderabad", "pune", "chennai",
    "australia", "sydney", "melbourne",
    "canada", "toronto", "vancouver",
    "singapore", "hong kong",
    "gmbh",
]

TECH_SIGNALS = [
    "software", "engineer", "engineering", "developer", "development",
    "data", "machine learning", "artificial intelligence", "ai ", " ml ",
    "python", "javascript", "typescript", "java ", "golang", "rust ",
    "backend", "frontend", "fullstack", "full stack", "full-stack",
    "cloud", "devops", "infrastructure", "platform", "mobile",
    "computer science", "cs ", "algorithm", "api ", "database",
    "research", "scientist", "analytics", "intern",
]

JUNIOR_SIGNALS = [
    "intern", "internship", "entry level", "entry-level",
    "junior", "new grad", "new graduate", "graduate",
    "trainee", "associate engineer", "apprentice",
    "early career", "recent graduate",
]

MUSE_LEVELS = ["Internship", "Entry Level", "Mid Level"]
MUSE_PAGES  = [1, 2]


def _job_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _is_ats_url(url: str) -> bool:
    """Returns True if this URL is a direct ATS application form."""
    url_lower = url.lower()
    return any(domain in url_lower for domain in ATS_DOMAINS)


def _passes_hard_filters(title: str, location: str, description: str = "") -> bool:
    title_lower = title.lower()
    location_lower = (location or "").lower()
    combined = title_lower + " " + (description or "").lower()[:300]

    for kw in BAD_TITLE_KEYWORDS:
        if kw in title_lower:
            return False

    if location and location.lower() not in ("remote", "worldwide", "anywhere", ""):
        for bad_loc in BAD_LOCATIONS:
            if bad_loc in location_lower:
                return False

    if not any(sig in combined for sig in TECH_SIGNALS):
        return False

    return True


# ─── ATS URL Resolver ─────────────────────────────────────────────────────────

async def _resolve_ats_url(muse_url: str, client: httpx.AsyncClient) -> str:
    """
    Fetches a Muse job landing page and extracts the real ATS application URL.
    The Muse shows an "Apply on Company Site" button that links to the actual form.

    Returns the real ATS URL, or the original Muse URL if resolution fails.
    """
    try:
        resp = await client.get(
            muse_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=15,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            return muse_url

        soup = BeautifulSoup(resp.text, "lxml")

        # Strategy 1: Find direct ATS links in the page
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if _is_ats_url(href):
                return href

        # Strategy 2: Look for apply button with data attributes
        for el in soup.find_all(attrs={"data-apply-url": True}):
            url = el.get("data-apply-url", "")
            if url and url.startswith("http"):
                return url

        # Strategy 3: Look for apply button by text
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True).lower()
            if any(phrase in text for phrase in [
                "apply on company site", "apply now", "apply here",
                "apply on", "apply at", "apply for this job"
            ]):
                href = a.get("href", "")
                if href and href.startswith("http") and "themuse.com" not in href:
                    return href

        # Strategy 4: Scan all script tags for ATS URLs
        for script in soup.find_all("script"):
            content = script.string or ""
            for domain in ATS_DOMAINS:
                match = re.search(
                    rf'https?://[^\s\'"]+{re.escape(domain)}[^\s\'"]*',
                    content
                )
                if match:
                    return match.group(0)

        # Couldn't resolve — return original
        return muse_url

    except Exception as e:
        print(f"[APIdiscovery] URL resolve error for {muse_url[:60]}: {e}")
        return muse_url


async def _resolve_batch(jobs: list[dict], client: httpx.AsyncClient) -> list[dict]:
    """
    Resolves ATS URLs for a batch of Muse jobs concurrently.
    Replaces ats_url with the real application form URL.
    """
    async def resolve_one(job_data: dict) -> dict:
        muse_url = job_data.get("url", "")
        if not muse_url or "themuse.com" not in muse_url:
            return job_data

        real_url = await _resolve_ats_url(muse_url, client)
        if real_url and real_url != muse_url:
            print(f"[APIdiscovery] Resolved: {job_data.get('company', '')} → {real_url[:60]}")
            job_data["ats_url"] = real_url
        else:
            # Could not resolve — mark so track_worker knows to click Apply button
            job_data["ats_url"] = muse_url
            job_data["needs_click_apply"] = True

        return job_data

    # Run resolutions concurrently, max 5 at a time to be polite
    semaphore = asyncio.Semaphore(5)

    async def safe_resolve(job_data):
        async with semaphore:
            result = await resolve_one(job_data)
            await asyncio.sleep(0.5)
            return result

    return await asyncio.gather(*[safe_resolve(j) for j in jobs])


# ─── The Muse API ─────────────────────────────────────────────────────────────

async def _fetch_muse_all(client: httpx.AsyncClient) -> list[dict]:
    """Fetch Computer & IT jobs from The Muse, multiple levels and pages."""
    jobs = []
    seen_urls = set()

    for level in MUSE_LEVELS:
        for page in MUSE_PAGES:
            try:
                resp = await client.get(
                    "https://www.themuse.com/api/public/jobs",
                    params={
                        "category": "Computer and IT",
                        "level": level,
                        "page": page,
                        "descending": "true",
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                if resp.status_code != 200:
                    continue

                data = resp.json()
                for item in data.get("results", []):
                    title    = item.get("name", "")
                    company  = item.get("company", {}).get("name", "")
                    url      = item.get("refs", {}).get("landing_page", "")
                    locs     = item.get("locations", [])
                    location = locs[0].get("name", "Remote") if locs else "Remote"
                    cats     = item.get("categories", [])
                    category = cats[0].get("name", "") if cats else "Technology"

                    if not title or not url or url in seen_urls:
                        continue
                    seen_urls.add(url)

                    jobs.append({
                        "title": title,
                        "company": company,
                        "url": url,
                        "ats_url": url,  # Will be resolved below
                        "description": (
                            f"{title} at {company}. {level} {category}. "
                            f"Location: {location}."
                        ),
                        "location": location,
                        "platform": "themuse",
                    })

                await asyncio.sleep(1)

            except Exception as e:
                print(f"[APIdiscovery] Muse error ({level} p{page}): {e}")
                continue

    return jobs


# ─── Remotive API ─────────────────────────────────────────────────────────────

async def _fetch_remotive(client: httpx.AsyncClient) -> list[dict]:
    """Remotive remote tech jobs — junior/intern level only. Direct ATS links."""
    categories = ["software-dev", "data", "devops-sysadmin", "product"]
    jobs = []
    seen_urls = set()

    for category in categories:
        try:
            resp = await client.get(
                "https://remotive.com/api/remote-jobs",
                params={"category": category, "limit": 100},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                continue

            data = resp.json()
            for item in data.get("jobs", []):
                title   = item.get("title", "")
                company = item.get("company_name", "")
                url     = item.get("url", "")
                desc    = re.sub(r'<[^>]+>', ' ', item.get("description", ""))
                desc    = re.sub(r'\s+', ' ', desc).strip()[:500]
                tags    = item.get("tags", [])

                if not title or not url or url in seen_urls:
                    continue

                combined = (title + " " + desc[:200] + " " + " ".join(tags)).lower()
                if not any(sig in combined for sig in JUNIOR_SIGNALS):
                    continue

                seen_urls.add(url)
                jobs.append({
                    "title": title,
                    "company": company,
                    "url": url,
                    "ats_url": url,  # Remotive links go directly to ATS
                    "description": desc[:400],
                    "location": "Remote",
                    "platform": "remotive",
                })

            await asyncio.sleep(1)

        except Exception as e:
            print(f"[APIdiscovery] Remotive error ({category}): {e}")
            continue

    return jobs


# ─── USAJobs API ──────────────────────────────────────────────────────────────

async def _fetch_usajobs(client: httpx.AsyncClient) -> list[dict]:
    """USAJobs REST API — government CS internships. Direct application URLs."""
    terms = [
        "computer scientist intern",
        "data scientist intern",
        "software engineer intern",
        "artificial intelligence intern",
    ]
    jobs = []
    seen_urls = set()

    for term in terms:
        try:
            resp = await client.get(
                "https://data.usajobs.gov/api/search",
                params={"Keyword": term, "ResultsPerPage": 10, "WhoMayApply": "all"},
                headers={"Host": "data.usajobs.gov", "User-Agent": "aditi.b.nautiyal@gmail.com"},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                await asyncio.sleep(2)
                continue

            data  = resp.json()
            items = data.get("SearchResult", {}).get("SearchResultItems", [])
            for item in items:
                mv       = item.get("MatchedObjectDescriptor", {})
                title    = mv.get("PositionTitle", "")
                company  = mv.get("OrganizationName", "US Government")
                url      = mv.get("PositionURI", "")
                apply_url = mv.get("ApplyURI", [url])[0] if mv.get("ApplyURI") else url
                loc_list = mv.get("PositionLocation", [])
                location = loc_list[0].get("LocationName", "USA") if loc_list else "USA"
                desc     = mv.get("QualificationSummary", "")[:400]

                if not title or not url or url in seen_urls:
                    continue
                seen_urls.add(url)

                jobs.append({
                    "title": title,
                    "company": f"{company} (US Gov)",
                    "url": url,
                    "ats_url": apply_url,  # USAJobs has direct apply URLs
                    "description": f"{company}: {desc}",
                    "location": location,
                    "platform": "usajobs",
                })

            await asyncio.sleep(2)

        except Exception as e:
            print(f"[APIdiscovery] USAJobs error ({term}): {e}")
            continue

    return jobs


# ─── Main loop ────────────────────────────────────────────────────────────────

async def run_api_discovery(continuous: bool = True, stop_event=None):
    """
    Main API discovery loop.
    Fetches broadly → hard filters → resolves real ATS URLs → queues.
    """
    pool   = get_pool()
    scorer = JobScorer()

    print("[APIdiscovery] Starting — The Muse, Remotive, USAJobs...")
    iteration = 0

    while True:
        if stop_event and stop_event.is_set():
            break

        added_total    = 0
        rejected_total = 0
        all_jobs       = []

        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "AutoApplyAI Job Discovery/1.0"},
        ) as client:

            # Fetch raw jobs
            muse = await _fetch_muse_all(client)
            print(f"[APIdiscovery] Muse raw: {len(muse)}")
            await asyncio.sleep(2)

            remotive = await _fetch_remotive(client)
            print(f"[APIdiscovery] Remotive raw: {len(remotive)}")
            await asyncio.sleep(2)

            usajobs = await _fetch_usajobs(client)
            print(f"[APIdiscovery] USAJobs raw: {len(usajobs)}")

            all_jobs = muse + remotive + usajobs
            print(f"[APIdiscovery] Total raw: {len(all_jobs)} — filtering...")

            # Hard filter first (cheap — no network calls)
            filtered = []
            for job_data in all_jobs:
                if _passes_hard_filters(
                    job_data.get("title", ""),
                    job_data.get("location", ""),
                    job_data.get("description", ""),
                ):
                    filtered.append(job_data)
                else:
                    rejected_total += 1

            print(f"[APIdiscovery] After filter: {len(filtered)} kept, "
                  f"{rejected_total} rejected")

            # Resolve real ATS URLs for Muse jobs (network calls)
            muse_jobs  = [j for j in filtered if j.get("platform") == "themuse"]
            other_jobs = [j for j in filtered if j.get("platform") != "themuse"]

            if muse_jobs:
                print(f"[APIdiscovery] Resolving ATS URLs for {len(muse_jobs)} Muse jobs...")
                muse_jobs = await _resolve_batch(muse_jobs, client)

            filtered = muse_jobs + other_jobs

        # Score and add to pool
        for job_data in filtered:
            if stop_event and stop_event.is_set():
                break

            title = job_data.get("title", "")
            url   = job_data.get("url", "")
            if not title or not url:
                continue

            job = Job(
                score=0.0,
                job_id=_job_id(url),
                title=title,
                company=job_data.get("company", ""),
                url=url,
                ats_url=job_data.get("ats_url", url),
                platform=job_data.get("platform", "api"),
                description=job_data.get("description", ""),
                location=job_data.get("location", ""),
            )

            job.score = scorer._keyword_score(job)
            if job.score < 2.5:
                job.score = 2.5

            if pool.add(job):
                added_total += 1
                ats = job.ats_url[:50] if job.ats_url else "?"
                print(f"[APIdiscovery] ✅ {job.title} @ {job.company} → {ats}")

        iteration += 1
        print(
            f"[APIdiscovery] Cycle {iteration}: "
            f"+{added_total} added, {rejected_total} filtered. "
            f"Pool: {pool.size()}"
        )

        if not continuous:
            break

        print("[APIdiscovery] Sleeping 20 min before next cycle...")
        for _ in range(CYCLE_INTERVAL // 10):
            if stop_event and stop_event.is_set():
                break
            await asyncio.sleep(10)
