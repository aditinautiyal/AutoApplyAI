"""
discovery/greenhouse_lever_scraper.py
Scrapes Greenhouse and Lever job boards DIRECTLY.

These are the actual ATS systems where applications get submitted.
No aggregator, no login, no Cloudflare — just direct form URLs.

Sources:
- Greenhouse board search: boards.greenhouse.io (public, no auth)
- Lever board search: jobs.lever.co (public, no auth)
- Ashby board search: jobs.ashbyhq.com (public, no auth)
- Known startup Greenhouse boards scraped by company name

This is the cleanest possible source — every job returned has a URL
that goes DIRECTLY to the application form.
"""

import asyncio
import hashlib
import random
import re
import httpx
from bs4 import BeautifulSoup
from discovery.job_pool import Job, JobScorer, get_pool
from core.settings_store import get_store

CYCLE_INTERVAL = 25 * 60
REQUEST_TIMEOUT = 20

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "Chrome/121.0.0.0 Safari/537.36",
]

# Known tech companies using Greenhouse — scraped directly
GREENHOUSE_COMPANIES = [
    # Startups / mid-size tech
    "duolingo", "notion", "figma", "brex", "rippling", "lattice",
    "scale", "cohere", "huggingface", "weights-biases", "anthropic",
    "openai", "mistral", "runway", "together", "anyscale",
    "modal", "modal-labs", "replit", "cursor", "linear",
    "vercel", "planetscale", "supabase", "neon", "turso",
    "retool", "airplane", "airplane-dev", "cortex", "incident-io",
    "ramp", "mercury", "found", "pilot", "gusto",
    "faire", "attentive", "klaviyo", "postscript", "yotpo",
    "airtable", "coda", "craft", "notion", "clickup",
    "hex", "mode", "sigma", "census", "hightouch",
    "dbt-labs", "fivetran", "airbyte", "meltano",
    "deepgram", "assembly-ai", "gladia", "speechmatics",
    "robinhood", "coinbase", "stripe", "plaid", "modern-treasury",
    "greenoaks", "draftbit", "snorkel-ai", "labelbox",
    "argo-ai", "aurora", "zoox", "wayve", "motional",
    "nuro", "gatik", "embark-trucks",
    "instabase", "ironclad", "evisort", "contractpodai",
    "benchling", "insitro", "ginkgo", "recursion",
    "tempus", "flatiron", "veracyte",
    "camunda", "temporal", "inngest",
    "cloudflare", "fastly", "netlify",
    "doppler", "1password", "teleport",
]

# Known companies using Lever
LEVER_COMPANIES = [
    "netflix", "lyft", "reddit", "twitch", "discord",
    "figma", "canva", "miro", "loom", "pitch",
    "amplitude", "mixpanel", "segment", "heap",
    "intercom", "front", "freshdesk", "zendesk",
    "gitlab", "hashicorp", "pulumi", "chef",
    "sendbird", "pubnub", "ably", "pusher",
    "stytch", "auth0", "okta", "duo",
    "paperspace", "lambda-labs", "coreweave",
    "humane", "rabbit", "brilliant-labs",
    "arc", "perplexity", "you", "kagi",
    "mistral", "cohere", "together",
    "grammarly", "wordtune", "jasper",
    "notion", "craft-docs", "coda",
]

# Ashby — used by many YC startups
ASHBY_COMPANIES = [
    "ashby", "linear", "loom", "vercel", "cal",
    "descript", "grain", "fireflies",
    "privy", "postscript", "klaviyo",
    "census-data", "hightouch",
    "motor-ai", "comma-ai",
    "replit", "codesandbox", "stackblitz",
]

INTERN_KEYWORDS = [
    "intern", "internship", "co-op", "coop", "student",
    "entry level", "entry-level", "new grad", "junior",
]

BAD_TITLES = [
    "senior", "staff ", "principal", "director", "manager",
    " lead", "vp ", "head of", "chief", "legal", "counsel",
    "hr ", "recruiter", "sap ", "unpaid",
]

TECH_SIGNALS = [
    "software", "engineer", "developer", "data", "ml", "ai",
    "machine learning", "backend", "frontend", "fullstack",
    "python", "javascript", "cloud", "devops", "research",
    "scientist", "analytics", "product", "design",
]


def _job_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _is_intern_role(title: str, description: str = "") -> bool:
    combined = (title + " " + description).lower()
    return any(kw in combined for kw in INTERN_KEYWORDS)


def _passes(title: str) -> bool:
    tl = title.lower()
    for bad in BAD_TITLES:
        if bad in tl:
            return False
    return True


def _headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/html",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Greenhouse direct scraper
# ═══════════════════════════════════════════════════════════════════════════════

async def _scrape_greenhouse_company(company: str,
                                      client: httpx.AsyncClient) -> list[dict]:
    """Scrape a specific company's Greenhouse board for intern roles."""
    jobs = []
    try:
        # Greenhouse has a JSON API — much more reliable than HTML
        resp = await client.get(
            f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs",
            params={"content": "true"},
            headers=_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return []

        data = resp.json()
        for item in data.get("jobs", []):
            title    = item.get("title", "")
            job_id   = item.get("id", "")
            location = item.get("location", {}).get("name", "Remote")

            if not title or not job_id:
                continue
            if not _is_intern_role(title):
                continue
            if not _passes(title):
                continue

            url = f"https://boards.greenhouse.io/{company}/jobs/{job_id}"

            jobs.append({
                "title":       title,
                "company":     data.get("company", {}).get("name", company.title()),
                "url":         url,
                "ats_url":     url,
                "description": f"{title} internship at {company.title()}. {location}.",
                "location":    location,
                "platform":    "greenhouse_direct",
            })

    except Exception as e:
        if "404" not in str(e) and "not found" not in str(e).lower():
            print(f"[ATSscraper] Greenhouse {company} error: {e}")

    return jobs


async def _fetch_greenhouse_all(client: httpx.AsyncClient) -> list[dict]:
    """Scrape all known Greenhouse company boards."""
    all_jobs = []
    seen = set()

    # Run in batches of 10 to avoid overwhelming
    batch_size = 10
    companies  = GREENHOUSE_COMPANIES.copy()
    random.shuffle(companies)  # Vary order each cycle

    for i in range(0, len(companies), batch_size):
        batch = companies[i:i + batch_size]
        results = await asyncio.gather(
            *[_scrape_greenhouse_company(c, client) for c in batch],
            return_exceptions=True
        )
        for result in results:
            if isinstance(result, list):
                for job in result:
                    if job["url"] not in seen:
                        seen.add(job["url"])
                        all_jobs.append(job)

        await asyncio.sleep(2)

    return all_jobs


# ═══════════════════════════════════════════════════════════════════════════════
# Lever direct scraper
# ═══════════════════════════════════════════════════════════════════════════════

async def _scrape_lever_company(company: str,
                                 client: httpx.AsyncClient) -> list[dict]:
    """Scrape a company's Lever board for intern roles."""
    jobs = []
    try:
        # Lever also has a JSON API
        resp = await client.get(
            f"https://api.lever.co/v0/postings/{company}",
            params={"mode": "json"},
            headers=_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return []

        postings = resp.json()
        for item in postings:
            title    = item.get("text", "")
            url      = item.get("hostedUrl", "")
            location = item.get("categories", {}).get("location", "Remote")
            team     = item.get("categories", {}).get("team", "")
            desc     = item.get("descriptionPlain", "")[:200]

            if not title or not url:
                continue
            if not _is_intern_role(title, desc):
                continue
            if not _passes(title):
                continue

            jobs.append({
                "title":       title,
                "company":     company.title().replace("-", " "),
                "url":         url,
                "ats_url":     url,
                "description": f"{title} at {company.title()}. {team}. {location}.",
                "location":    location,
                "platform":    "lever_direct",
            })

    except Exception as e:
        if "404" not in str(e) and "not found" not in str(e).lower():
            print(f"[ATSscraper] Lever {company} error: {e}")

    return jobs


async def _fetch_lever_all(client: httpx.AsyncClient) -> list[dict]:
    """Scrape all known Lever company boards."""
    all_jobs = []
    seen = set()

    companies = LEVER_COMPANIES.copy()
    random.shuffle(companies)

    batch_size = 10
    for i in range(0, len(companies), batch_size):
        batch = companies[i:i + batch_size]
        results = await asyncio.gather(
            *[_scrape_lever_company(c, client) for c in batch],
            return_exceptions=True
        )
        for result in results:
            if isinstance(result, list):
                for job in result:
                    if job["url"] not in seen:
                        seen.add(job["url"])
                        all_jobs.append(job)

        await asyncio.sleep(2)

    return all_jobs


# ═══════════════════════════════════════════════════════════════════════════════
# Ashby direct scraper
# ═══════════════════════════════════════════════════════════════════════════════

async def _scrape_ashby_company(company: str,
                                 client: httpx.AsyncClient) -> list[dict]:
    """Scrape a company's Ashby board."""
    jobs = []
    try:
        resp = await client.get(
            f"https://jobs.ashbyhq.com/api/non-user-graphql",
            method="POST",
            json={
                "operationName": "ApiJobBoardWithTeams",
                "variables": {"organizationHostedJobsPageName": company},
                "query": """
                    query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
                        jobBoard: jobBoardWithTeams(
                            organizationHostedJobsPageName: $organizationHostedJobsPageName
                        ) {
                            teams { name parentTeamName }
                            jobPostings {
                                id title location { name }
                                isRemote employmentType
                            }
                        }
                    }
                """
            },
            headers={**_headers(), "Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return []

        data = resp.json()
        postings = (data.get("data", {})
                        .get("jobBoard", {})
                        .get("jobPostings", []))

        for item in postings:
            title    = item.get("title", "")
            job_id   = item.get("id", "")
            loc      = item.get("location", {}) or {}
            location = loc.get("name", "Remote") if loc else "Remote"
            is_remote = item.get("isRemote", False)
            if is_remote:
                location = "Remote"

            if not title or not job_id:
                continue
            if not _is_intern_role(title):
                continue
            if not _passes(title):
                continue

            url = f"https://jobs.ashbyhq.com/{company}/{job_id}"
            jobs.append({
                "title":       title,
                "company":     company.title().replace("-", " "),
                "url":         url,
                "ats_url":     url,
                "description": f"{title} at {company.title()}. {location}.",
                "location":    location,
                "platform":    "ashby_direct",
            })

    except Exception as e:
        if "404" not in str(e):
            pass  # Ashby errors are usually just missing companies

    return jobs


async def _fetch_ashby_all(client: httpx.AsyncClient) -> list[dict]:
    """Scrape Ashby boards."""
    all_jobs = []
    seen = set()

    for company in ASHBY_COMPANIES:
        try:
            jobs = await _scrape_ashby_company(company, client)
            for job in jobs:
                if job["url"] not in seen:
                    seen.add(job["url"])
                    all_jobs.append(job)
            await asyncio.sleep(1)
        except Exception:
            continue

    return all_jobs


# ═══════════════════════════════════════════════════════════════════════════════
# Main loop
# ═══════════════════════════════════════════════════════════════════════════════

async def run_ats_scraping(continuous: bool = True, stop_event=None):
    """
    Directly scrapes Greenhouse, Lever, and Ashby job boards.
    Returns direct application form URLs — no aggregator, no login needed.
    """
    pool   = get_pool()
    scorer = JobScorer()

    print("[ATSscraper] Starting direct Greenhouse/Lever/Ashby scraper...")
    iteration = 0

    while True:
        if stop_event and stop_event.is_set():
            break

        added = 0

        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        ) as client:

            # Greenhouse
            gh_jobs = await _fetch_greenhouse_all(client)
            print(f"[ATSscraper] Greenhouse direct: {len(gh_jobs)} intern roles")

            # Lever
            lv_jobs = await _fetch_lever_all(client)
            print(f"[ATSscraper] Lever direct: {len(lv_jobs)} intern roles")

            # Ashby
            ab_jobs = await _fetch_ashby_all(client)
            print(f"[ATSscraper] Ashby direct: {len(ab_jobs)} intern roles")

        all_jobs = gh_jobs + lv_jobs + ab_jobs
        print(f"[ATSscraper] Total direct ATS jobs: {len(all_jobs)}")

        seen = set()
        for job_data in all_jobs:
            if stop_event and stop_event.is_set():
                break

            url = job_data.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)

            job = Job(
                score=0.0,
                job_id=_job_id(url),
                title=job_data["title"],
                company=job_data.get("company", ""),
                url=url,
                ats_url=url,
                platform=job_data.get("platform", "ats_direct"),
                description=job_data.get("description", ""),
                location=job_data.get("location", ""),
            )

            job.score = scorer._keyword_score(job)
            if job.score < 2.5:
                job.score = 3.0  # Direct ATS intern roles — boost minimum

            if pool.add(job):
                added += 1
                print(f"[ATSscraper] ✅ {job.title} @ {job.company} "
                      f"({job.platform})")

        iteration += 1
        print(f"[ATSscraper] Cycle {iteration}: +{added} direct ATS jobs. "
              f"Pool: {pool.size()}")

        if not continuous:
            break

        print(f"[ATSscraper] Sleeping 25 min...")
        for _ in range(CYCLE_INTERVAL // 10):
            if stop_event and stop_event.is_set():
                break
            await asyncio.sleep(10)
