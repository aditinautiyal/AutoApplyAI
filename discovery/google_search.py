"""
discovery/google_search.py
Discovers direct ATS job links via Google.
Targets Greenhouse, Lever, Workday, and other ATS platforms directly.
No login needed. Bypasses LinkedIn/Indeed entirely for bulk applying.
"""

import asyncio
import hashlib
import time
import random
import re
import httpx
from bs4 import BeautifulSoup
from typing import Generator
from discovery.job_pool import Job, JobScorer, get_pool
from core.settings_store import get_store


# ATS domains to look for — applying here is untraceable to LinkedIn/Indeed
ATS_DOMAINS = [
    "greenhouse.io/jobs",
    "lever.co/",
    "myworkdayjobs.com",
    "jobs.ashbyhq.com",
    "apply.workable.com",
    "boards.greenhouse.io",
    "jobs.lever.co",
    "smartrecruiters.com/jobs",
    "careers.icims.com",
    "jobvite.com/careers",
    "taleo.net",
    "brassring.com",
    "successfactors.com",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


def _job_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _build_queries(profile: dict) -> list[str]:
    """Build diverse Google search queries for ATS job links."""
    roles = profile.get("target_roles", "software engineering intern AI ML")
    locations = profile.get("locations", "Chicago Dallas Remote")
    loc_list = [l.strip() for l in locations.split(",")][:3]

    ats_site_query = " OR ".join(
        f'site:{d.split("/")[0]}' for d in ATS_DOMAINS[:6]
    )

    queries = []

    # Direct ATS queries
    for role in ["software engineer intern", "AI ML intern", "machine learning intern",
                  "data science intern", "computer science intern"]:
        for loc in loc_list + ["remote"]:
            queries.append(
                f'{role} {loc} ({ats_site_query})'
            )

    # Fellowship queries
    queries.extend([
        f'AI research fellowship 2025 apply ({ats_site_query})',
        f'machine learning fellowship internship 2025 ({ats_site_query})',
    ])

    # Startup queries
    queries.extend([
        f'AI startup internship 2025 apply now Chicago OR Dallas OR remote',
        f'early stage startup software intern 2025 apply',
        f'YC startup hiring intern 2025 software AI',
    ])

    # Deep variety — forums, posts
    queries.extend([
        f'site:reddit.com r/cscareerquestions internship 2025 apply link',
        f'site:reddit.com internship AI ML 2025 "apply here" OR "application link"',
        f'site:news.ycombinator.com "who is hiring" 2025 intern',
    ])

    random.shuffle(queries)
    return queries


async def _google_search(query: str, num_results: int = 10) -> list[dict]:
    """
    Fetch Google search results for a query.
    Returns list of {title, url, snippet} dicts.
    """
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    params = {
        "q": query,
        "num": num_results,
        "hl": "en",
        "gl": "us",
    }

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        try:
            resp = await client.get(
                "https://www.google.com/search",
                params=params,
                headers=headers
            )
            if resp.status_code != 200:
                return []

            soup = BeautifulSoup(resp.text, "lxml")
            results = []

            for g in soup.select("div.g"):
                link = g.select_one("a")
                title_el = g.select_one("h3")
                snippet_el = g.select_one("div.VwiC3b, span.st")

                if not link or not title_el:
                    continue

                url = link.get("href", "")
                if not url.startswith("http"):
                    continue

                results.append({
                    "title": title_el.get_text(strip=True),
                    "url": url,
                    "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
                })

            return results
        except Exception as e:
            print(f"[GoogleSearch] Error: {e}")
            return []


def _extract_ats_url(url: str, snippet: str, title: str) -> tuple[str, str]:
    """
    Try to identify a direct ATS application URL.
    Returns (ats_url, platform_name).
    """
    for domain in ATS_DOMAINS:
        platform_name = domain.split(".")[0].split("/")[0]
        if domain.split("/")[0] in url:
            return url, platform_name
    # If not direct ATS, return original (still useful)
    return url, "web"


def _parse_job_from_result(result: dict) -> dict:
    """Extract job info from a Google search result."""
    title = result["title"]
    url = result["url"]
    snippet = result["snippet"]

    # Try to extract company name (usually in title like "Software Engineer - Acme Corp")
    company = ""
    if " - " in title:
        parts = title.rsplit(" - ", 1)
        company = parts[-1].strip()
        title = parts[0].strip()
    elif " at " in title.lower():
        parts = title.lower().split(" at ", 1)
        company = title[len(parts[0]) + 4:].strip()
        title = title[:len(parts[0])].strip()

    ats_url, platform = _extract_ats_url(url, snippet, title)

    # Extract location from snippet
    location = ""
    loc_patterns = [
        r'(Remote|Chicago|Dallas|New York|San Francisco|Austin|Seattle|Boston)',
        r'(\w+,\s*[A-Z]{2})\b',
    ]
    for pattern in loc_patterns:
        m = re.search(pattern, snippet, re.IGNORECASE)
        if m:
            location = m.group(1)
            break

    return {
        "title": title or result["title"],
        "company": company,
        "url": url,
        "ats_url": ats_url,
        "platform": platform,
        "description": snippet,
        "location": location,
    }


async def run_google_discovery(continuous: bool = True, stop_event=None):
    """
    Main discovery loop. Continuously searches Google and adds to pool.
    Runs as background coroutine.
    """
    store = get_store()
    pool = get_pool()
    scorer = JobScorer()
    profile = store.get_profile() or {}

    print("[Discovery] Google ATS search starting...")
    iteration = 0

    while True:
        if stop_event and stop_event.is_set():
            break

        queries = _build_queries(profile)

        for query in queries:
            if stop_event and stop_event.is_set():
                break

            results = await _google_search(query, num_results=10)
            added = 0

            for result in results:
                job_data = _parse_job_from_result(result)
                if not job_data["title"] or not job_data["url"]:
                    continue

                job = Job(
                    score=0.0,  # will be scored below
                    job_id=_job_id(job_data["url"]),
                    title=job_data["title"],
                    company=job_data["company"],
                    url=job_data["url"],
                    ats_url=job_data["ats_url"],
                    platform=job_data["platform"],
                    description=job_data["description"],
                    location=job_data["location"],
                )

                # Score it (uses fast Haiku call)
                job.score = scorer.score(job)

                # Only add if score >= 4.0 (not terrible fit)
                if job.score >= 4.0:
                    was_added = pool.add(job)
                    if was_added:
                        added += 1
                        print(f"[Discovery] Added: {job.title} @ {job.company} (score: {job.score:.1f})")

            # Delay between queries — be respectful to Google
            await asyncio.sleep(random.uniform(3, 8))

        iteration += 1
        print(f"[Discovery] Cycle {iteration} complete. Pool size: {pool.size()}")

        if not continuous:
            break

        # Wait before next full cycle
        await asyncio.sleep(60)
