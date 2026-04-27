"""
discovery/google_search.py
Discovers direct ATS job links via Google.
Rate-limited to run once every 30 minutes to avoid IP blocks and control costs.
"""

import asyncio
import hashlib
import random
import re
import httpx
from bs4 import BeautifulSoup
from discovery.job_pool import Job, JobScorer, get_pool
from core.settings_store import get_store

ATS_DOMAINS = [
    "greenhouse.io/jobs",
    "lever.co/",
    "myworkdayjobs.com",
    "jobs.ashbyhq.com",
    "apply.workable.com",
    "boards.greenhouse.io",
    "jobs.lever.co",
    "smartrecruiters.com/jobs",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

# Only run Google search every 30 minutes to avoid rate limiting and control costs
GOOGLE_CYCLE_INTERVAL = 30 * 60  # 30 minutes between full cycles

# Focused, high-value queries only — fewer queries = less rate limiting
FOCUSED_QUERIES = [
    'software engineer intern 2025 site:greenhouse.io OR site:lever.co',
    'machine learning intern 2025 site:greenhouse.io OR site:lever.co',
    'AI intern summer 2025 site:jobs.ashbyhq.com OR site:greenhouse.io',
    'data science intern 2025 Chicago OR Dallas OR Remote site:greenhouse.io',
    'software engineer intern remote 2025 site:lever.co OR site:greenhouse.io',
    'computer science intern 2025 startup site:jobs.ashbyhq.com',
    'python developer intern 2025 site:greenhouse.io',
    'ML engineer intern 2025 site:lever.co',
]


def _job_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


async def _google_search(query: str, num_results: int = 8) -> list[dict]:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    params = {"q": query, "num": num_results, "hl": "en", "gl": "us"}

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


def _parse_job_from_result(result: dict) -> dict:
    title = result["title"]
    url = result["url"]
    snippet = result["snippet"]

    company = ""
    if " - " in title:
        parts = title.rsplit(" - ", 1)
        company = parts[-1].strip()
        title = parts[0].strip()
    elif " at " in title.lower():
        parts = title.lower().split(" at ", 1)
        company = title[len(parts[0]) + 4:].strip()
        title = title[:len(parts[0])].strip()

    platform = "web"
    for domain in ATS_DOMAINS:
        if domain.split("/")[0] in url:
            platform = domain.split(".")[0]
            break

    location = ""
    loc_match = re.search(
        r'(Remote|Chicago|Dallas|New York|San Francisco|Austin|Seattle|Boston)',
        snippet, re.IGNORECASE
    )
    if loc_match:
        location = loc_match.group(1)

    return {
        "title": title or result["title"],
        "company": company,
        "url": url,
        "ats_url": url,
        "platform": platform,
        "description": snippet,
        "location": location,
    }


async def run_google_discovery(continuous: bool = True, stop_event=None):
    store = get_store()
    pool = get_pool()
    scorer = JobScorer()

    print("[GoogleSearch] Starting (rate-limited to 1 cycle per 30 min)...")
    iteration = 0

    while True:
        if stop_event and stop_event.is_set():
            break

        added = 0
        # Shuffle queries and only run half per cycle to stay under rate limits
        queries = FOCUSED_QUERIES.copy()
        random.shuffle(queries)
        queries_this_cycle = queries[:4]  # Only 4 queries per cycle

        for query in queries_this_cycle:
            if stop_event and stop_event.is_set():
                break

            results = await _google_search(query, num_results=8)

            for result in results:
                job_data = _parse_job_from_result(result)
                if not job_data["title"] or not job_data["url"]:
                    continue

                job = Job(
                    score=0.0,
                    job_id=_job_id(job_data["url"]),
                    title=job_data["title"],
                    company=job_data["company"],
                    url=job_data["url"],
                    ats_url=job_data["ats_url"],
                    platform=job_data["platform"],
                    description=job_data["description"],
                    location=job_data["location"],
                )
                job.score = scorer.score(job)

                if scorer.passes_threshold(job.score):
                    if pool.add(job):
                        added += 1
                        print(f"[GoogleSearch] Added: {job.title} @ {job.company} (score: {job.score:.1f})")

            # Polite delay between queries
            await asyncio.sleep(random.uniform(8, 15))

        iteration += 1
        print(f"[GoogleSearch] Cycle {iteration}: +{added} jobs. Pool: {pool.size()}")

        if not continuous:
            break

        # Wait 30 minutes before next cycle — this is what keeps costs low
        print(f"[GoogleSearch] Sleeping 30 min before next cycle...")
        for _ in range(GOOGLE_CYCLE_INTERVAL // 10):
            if stop_event and stop_event.is_set():
                break
            await asyncio.sleep(10)
