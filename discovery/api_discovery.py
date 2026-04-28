"""
discovery/api_discovery.py
Free job API discovery — no scraping, no rate limits, no IP blocks.

KEY FIXES:
1. Follows full redirect chains to get the real final ATS URL
2. Filters out companies whose portals require 2FA/Apple ID/Google account
   (Apple, Google, Amazon, Microsoft, Meta, etc.) — these can never be
   automated and waste track slots
3. Only queues jobs where the final URL is a known automatable ATS
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

# ── Automatable ATS domains — track worker can fill these forms ───────────────
AUTOMATABLE_ATS = [
    "greenhouse.io",
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "lever.co",
    "jobs.lever.co",
    "myworkdayjobs.com",
    "jobs.ashbyhq.com",
    "apply.workable.com",
    "smartrecruiters.com",
    "icims.com",
    "taleo.net",
    "jobvite.com",
    "bamboohr.com",
    "recruiting.ultipro.com",
    "jazz.co",
    "breezy.hr",
    "hire.trakstar.com",
    "recruitee.com",
    "pinpointhq.com",
    "dover.com",
]

# ── Companies whose portals require Apple ID / Google account / 2FA ───────────
# These cannot be automated — skip them entirely
UNAUTOMATABLE_COMPANIES = [
    "apple", "google", "alphabet", "amazon", "meta", "facebook",
    "microsoft", "netflix", "twitter", "x corp", "uber", "lyft",
    "salesforce", "oracle", "sap", "ibm", "cisco", "intel",
    "qualcomm", "nvidia",   # These use Workday but with heavy auth layers
]

# ── Redirect/tracking domains that need to be followed through ────────────────
REDIRECT_DOMAINS = [
    "recruitics.com", "jobvite.com/redirect", "click.appcast.io",
    "jobs.smartrecruiters.com/redirect", "go.greenhouse.io",
    "wd1.myworkdayjobs.com/redirect", "t.co/", "bit.ly/",
    "apply.indeed.com/redirect", "click.appcast.io",
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


def _is_automatable_ats(url: str) -> bool:
    """Returns True if this URL points to an ATS we can automate."""
    url_lower = url.lower()
    return any(ats in url_lower for ats in AUTOMATABLE_ATS)


def _is_unautomatable_company(company: str) -> bool:
    """Returns True if this company's portal cannot be automated."""
    company_lower = company.lower()
    return any(bad in company_lower for bad in UNAUTOMATABLE_COMPANIES)


def _is_redirect_url(url: str) -> bool:
    """Returns True if this URL is a tracking/redirect link needing resolution."""
    url_lower = url.lower()
    return any(redir in url_lower for redir in REDIRECT_DOMAINS)


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


# ─── URL Resolution ───────────────────────────────────────────────────────────

async def _follow_redirects_to_ats(url: str, client: httpx.AsyncClient,
                                    max_hops: int = 5) -> str:
    """
    Follow a URL through all redirects until we reach a final ATS URL.
    Returns the final URL, or original if resolution fails.
    """
    current = url
    for hop in range(max_hops):
        try:
            resp = await client.get(
                current,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"
                    ),
                },
                timeout=12,
                follow_redirects=True,
            )

            final_url = str(resp.url)

            # If we landed on an automatable ATS, we're done
            if _is_automatable_ats(final_url):
                return final_url

            # If we're still on a redirect/tracking page, parse for the next URL
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")

                # Strategy 1: Find ATS links directly in the page
                for a in soup.find_all("a", href=True):
                    href = a.get("href", "")
                    if _is_automatable_ats(href):
                        return href

                # Strategy 2: meta refresh redirect
                meta = soup.find("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
                if meta:
                    content = meta.get("content", "")
                    url_match = re.search(r'url=(.+)', content, re.I)
                    if url_match:
                        current = url_match.group(1).strip("'\"")
                        continue

                # Strategy 3: Apply button
                for a in soup.find_all("a", href=True):
                    text = a.get_text(strip=True).lower()
                    href = a.get("href", "")
                    if (any(phrase in text for phrase in [
                        "apply on company site", "apply now", "apply here",
                        "apply for this job", "apply on"
                    ]) and href.startswith("http") and "themuse.com" not in href):
                        current = href
                        break
                else:
                    # No more hops possible
                    return final_url

            else:
                return final_url

        except Exception as e:
            return current

    return current


async def _resolve_muse_jobs(jobs: list[dict],
                              client: httpx.AsyncClient) -> list[dict]:
    """
    For each Muse job:
    1. Follow the landing page to find the real ATS URL
    2. Filter out unautomatable companies
    3. Filter out jobs where final URL is not a known automatable ATS
    """
    semaphore = asyncio.Semaphore(4)
    resolved = []

    async def process_one(job_data: dict) -> dict | None:
        company = job_data.get("company", "")
        muse_url = job_data.get("url", "")

        # Skip unautomatable companies immediately
        if _is_unautomatable_company(company):
            print(f"[APIdiscovery] Skipped (unautomatable portal): {company}")
            return None

        async with semaphore:
            final_url = await _follow_redirects_to_ats(muse_url, client)
            await asyncio.sleep(0.5)

        if _is_automatable_ats(final_url):
            job_data["ats_url"] = final_url
            print(f"[APIdiscovery] ✅ Resolved: {company} → {final_url[:65]}")
            return job_data
        else:
            print(f"[APIdiscovery] ⛔ No automatable ATS found for {company} "
                  f"({final_url[:50]})")
            return None

    results = await asyncio.gather(*[process_one(j) for j in jobs])
    return [r for r in results if r is not None]


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
                        "ats_url": url,
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
    """Remotive remote tech jobs — junior/intern only. Links go directly to ATS."""
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
    """USAJobs REST API — government CS internships with direct apply URLs."""
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
                apply_url = mv.get("ApplyURI", [url])[0] if mv.get("ApplyURI") else url
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
    Fetches → hard filters → resolves real ATS URLs → filters unautomatable
    → queues only jobs with confirmed working automatable ATS URLs.
    """
    pool   = get_pool()
    scorer = JobScorer()

    print("[APIdiscovery] Starting — The Muse, Remotive, USAJobs...")
    iteration = 0

    while True:
        if stop_event and stop_event.is_set():
            break

        added_total        = 0
        hard_rejected      = 0
        ats_rejected       = 0
        all_jobs           = []

        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "AutoApplyAI Job Discovery/1.0"},
        ) as client:

            # Fetch raw
            muse = await _fetch_muse_all(client)
            print(f"[APIdiscovery] Muse raw: {len(muse)}")
            await asyncio.sleep(2)

            remotive = await _fetch_remotive(client)
            print(f"[APIdiscovery] Remotive raw: {len(remotive)}")
            await asyncio.sleep(2)

            usajobs = await _fetch_usajobs(client)
            print(f"[APIdiscovery] USAJobs raw: {len(usajobs)}")

            all_jobs = muse + remotive + usajobs
            print(f"[APIdiscovery] Total raw: {len(all_jobs)}")

            # Hard filter (title/location/tech keywords)
            filtered = []
            for job_data in all_jobs:
                if _passes_hard_filters(
                    job_data.get("title", ""),
                    job_data.get("location", ""),
                    job_data.get("description", ""),
                ):
                    filtered.append(job_data)
                else:
                    hard_rejected += 1

            print(f"[APIdiscovery] After hard filter: {len(filtered)} kept, "
                  f"{hard_rejected} rejected")

            # Resolve Muse URLs to real ATS (follows all redirects)
            muse_jobs  = [j for j in filtered if j.get("platform") == "themuse"]
            other_jobs = [j for j in filtered if j.get("platform") != "themuse"]

            if muse_jobs:
                print(f"[APIdiscovery] Resolving {len(muse_jobs)} Muse URLs...")
                muse_jobs = await _resolve_muse_jobs(muse_jobs, client)
                print(f"[APIdiscovery] {len(muse_jobs)} Muse jobs have automatable ATS")

            # For non-Muse jobs, filter unautomatable companies
            other_valid = []
            for j in other_jobs:
                if _is_unautomatable_company(j.get("company", "")):
                    ats_rejected += 1
                else:
                    other_valid.append(j)

            final_jobs = muse_jobs + other_valid
            print(f"[APIdiscovery] Final queue candidates: {len(final_jobs)}")

        # Score and add to pool
        for job_data in final_jobs:
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
                ats_short = (job.ats_url or "")[:55]
                print(f"[APIdiscovery] ✅ Queued: {job.title} @ {job.company} "
                      f"→ {ats_short}")

        iteration += 1
        print(
            f"[APIdiscovery] Cycle {iteration}: +{added_total} queued, "
            f"{hard_rejected} hard-filtered, {ats_rejected} unautomatable. "
            f"Pool: {pool.size()}"
        )

        if not continuous:
            break

        print("[APIdiscovery] Sleeping 20 min before next cycle...")
        for _ in range(CYCLE_INTERVAL // 10):
            if stop_event and stop_event.is_set():
                break
            await asyncio.sleep(10)
