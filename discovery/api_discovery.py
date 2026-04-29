"""
discovery/api_discovery.py
Free job API discovery — no scraping, no rate limits, no IP blocks.

Sources:
- The Muse API  — US tech jobs, all levels
- Remotive API  — remote tech jobs worldwide
- USAJobs API   — government CS internships

URL resolution strategy:
- Muse jobs keep their themuse.com URL — track_worker clicks
  "Apply on Company Site" and follows through to the real ATS.
  Trying to resolve at discovery time fails because Muse pages
  are JavaScript-rendered (httpx sees empty HTML, no apply link).
- Remotive/USAJobs go directly to their ATS URLs already.
- Unautomatable companies (Apple, Google, Amazon, etc.) are
  blocked before they ever reach the queue.
"""

import asyncio
import hashlib
import re
import httpx
from discovery.job_pool import Job, JobScorer, get_pool
from core.settings_store import get_store

CYCLE_INTERVAL = 20 * 60
REQUEST_TIMEOUT = 20

# Companies whose portals require Apple ID / Google account / 2FA
# These cannot ever be automated — skip them completely
UNAUTOMATABLE_COMPANIES = [
    "apple", "google", "alphabet", "amazon", "meta", "facebook",
    "microsoft", "netflix", "twitter", "x corp", "uber", "lyft",
    "salesforce", "oracle", "sap", "ibm", "cisco", "intel",
    "qualcomm", "nvidia", "walmart", "jpmorgan", "goldman sachs",
    "morgan stanley", "deloitte", "mckinsey", "accenture",
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


def _is_unautomatable_company(company: str) -> bool:
    company_lower = company.lower()
    return any(bad in company_lower for bad in UNAUTOMATABLE_COMPANIES)


def _passes_hard_filters(title: str, location: str, description: str = "") -> bool:
    title_lower    = title.lower()
    location_lower = (location or "").lower()
    combined       = title_lower + " " + (description or "").lower()[:300]

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


# ─── The Muse API ─────────────────────────────────────────────────────────────

async def _fetch_muse_all(client: httpx.AsyncClient) -> list[dict]:
    """
    Fetch Computer & IT jobs from The Muse across multiple levels and pages.
    Returns themuse.com landing page URLs — track_worker clicks through to ATS.
    """
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
                        "ats_url": url,  # track_worker will click Apply → real ATS
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
    """
    Remotive remote tech jobs. Junior/intern filtered.
    Remotive links go directly to company ATS — no click-through needed.
    """
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

                if _is_unautomatable_company(company):
                    continue

                combined = (title + " " + desc[:200] + " " + " ".join(tags)).lower()
                if not any(sig in combined for sig in JUNIOR_SIGNALS):
                    continue

                seen_urls.add(url)
                jobs.append({
                    "title": title,
                    "company": company,
                    "url": url,
                    "ats_url": url,
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
    """USAJobs REST API — government CS internships. Direct apply URLs."""
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
                params={
                    "Keyword": term,
                    "ResultsPerPage": 10,
                    "WhoMayApply": "all",
                },
                headers={
                    "Host": "data.usajobs.gov",
                    "User-Agent": "aditi.b.nautiyal@gmail.com",
                },
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                await asyncio.sleep(2)
                continue

            data  = resp.json()
            items = data.get("SearchResult", {}).get("SearchResultItems", [])
            for item in items:
                mv        = item.get("MatchedObjectDescriptor", {})
                title     = mv.get("PositionTitle", "")
                company   = mv.get("OrganizationName", "US Government")
                url       = mv.get("PositionURI", "")
                apply_url = (mv.get("ApplyURI") or [url])[0]
                loc_list  = mv.get("PositionLocation", [])
                location  = loc_list[0].get("LocationName", "USA") if loc_list else "USA"
                desc      = mv.get("QualificationSummary", "")[:400]

                if not title or not url or url in seen_urls:
                    continue
                seen_urls.add(url)

                jobs.append({
                    "title": title,
                    "company": f"{company} (US Gov)",
                    "url": url,
                    "ats_url": apply_url,
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

    Pipeline:
    1. Fetch raw jobs from Muse + Remotive + USAJobs
    2. Hard filter (title/location/tech keywords)
    3. Block unautomatable companies
    4. Queue remaining jobs — track_worker handles ATS navigation
    """
    pool   = get_pool()
    scorer = JobScorer()

    print("[APIdiscovery] Starting — The Muse, Remotive, USAJobs...")
    iteration = 0

    while True:
        if stop_event and stop_event.is_set():
            break

        added_total    = 0
        hard_rejected  = 0
        comp_rejected  = 0

        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "AutoApplyAI Job Discovery/1.0"},
        ) as client:

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

        final_jobs = []
        for job_data in all_jobs:
            title   = job_data.get("title", "")
            company = job_data.get("company", "")
            loc     = job_data.get("location", "")
            desc    = job_data.get("description", "")
            url     = job_data.get("url", "")

            if not title or not url:
                continue

            # Hard filter — title/location/tech keywords
            if not _passes_hard_filters(title, loc, desc):
                hard_rejected += 1
                continue

            # Block unautomatable companies
            if _is_unautomatable_company(company):
                comp_rejected += 1
                print(f"[APIdiscovery] ⛔ {company} (unautomatable portal)")
                continue

            final_jobs.append(job_data)

        print(
            f"[APIdiscovery] Kept: {len(final_jobs)}, "
            f"hard-filtered: {hard_rejected}, "
            f"company-blocked: {comp_rejected}"
        )

        for job_data in final_jobs:
            if stop_event and stop_event.is_set():
                break

            job = Job(
                score=0.0,
                job_id=_job_id(job_data["url"]),
                title=job_data["title"],
                company=job_data.get("company", ""),
                url=job_data["url"],
                ats_url=job_data.get("ats_url", job_data["url"]),
                platform=job_data.get("platform", "api"),
                description=job_data.get("description", ""),
                location=job_data.get("location", ""),
            )

            job.score = scorer._keyword_score(job)
            if job.score < 2.5:
                job.score = 2.5

            if pool.add(job):
                added_total += 1
                print(f"[APIdiscovery] ✅ {job.title} @ {job.company} "
                      f"({job.platform}, score: {job.score:.1f})")

        iteration += 1
        print(
            f"[APIdiscovery] Cycle {iteration}: +{added_total} added. "
            f"Pool: {pool.size()}"
        )

        if not continuous:
            break

        print("[APIdiscovery] Sleeping 20 min before next cycle...")
        for _ in range(CYCLE_INTERVAL // 10):
            if stop_event and stop_event.is_set():
                break
            await asyncio.sleep(10)
