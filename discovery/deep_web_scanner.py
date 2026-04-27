"""
discovery/deep_web_scanner.py
Deep web and startup board discovery.
Rate-limited. Runs as a background coroutine alongside other sources.
"""

import asyncio
import hashlib
import random
import re
import httpx
from bs4 import BeautifulSoup
from discovery.job_pool import Job, JobScorer, get_pool
from core.settings_store import get_store

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
]

# YC jobs and HackerNews — don't require Google
DIRECT_SOURCES = [
    "https://www.ycombinator.com/jobs",
    "https://hn.algolia.com/api/v1/search?query=Ask+HN+Who+is+hiring&tags=story&hitsPerPage=1",
]

DEEP_QUERIES = [
    'software engineer intern 2025 site:jobs.ashbyhq.com',
    'machine learning intern 2025 site:jobs.ashbyhq.com',
    '"summer 2025" intern software AI site:wellfound.com',
    'CS intern 2025 startup apply site:jobs.ashbyhq.com OR site:greenhouse.io',
]

CYCLE_INTERVAL = 45 * 60  # 45 minutes between full cycles


def _job_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


async def _fetch_yc_jobs(client: httpx.AsyncClient) -> list[dict]:
    """Fetch YC job board."""
    try:
        resp = await client.get(
            "https://www.ycombinator.com/jobs",
            headers={"User-Agent": random.choice(USER_AGENTS)},
            timeout=12,
        )
        soup = BeautifulSoup(resp.text, "lxml")
        jobs = []
        for job_card in soup.select("a[class*='job']")[:20]:
            title_el = job_card.select_one("h3, h2, [class*='title']")
            company_el = job_card.select_one("[class*='company']")
            location_el = job_card.select_one("[class*='location']")
            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            location = location_el.get_text(strip=True) if location_el else ""
            url = job_card.get("href", "")
            if url and not url.startswith("http"):
                url = f"https://www.ycombinator.com{url}"
            if title:
                jobs.append({
                    "title": title,
                    "company": company,
                    "url": url,
                    "ats_url": url,
                    "description": f"{title} at {company}",
                    "location": location,
                    "platform": "ycombinator",
                })
        return jobs
    except Exception as e:
        print(f"[DeepScan] YC jobs error: {e}")
        return []


async def _fetch_hn_hiring(client: httpx.AsyncClient) -> list[dict]:
    """Fetch HackerNews Who is Hiring thread."""
    try:
        resp = await client.get(
            "https://hn.algolia.com/api/v1/search",
            params={"query": "Ask HN: Who is hiring?", "tags": "story", "hitsPerPage": 1},
            headers={"User-Agent": random.choice(USER_AGENTS)},
            timeout=12,
        )
        data = resp.json()
        hits = data.get("hits", [])
        if not hits:
            return []

        thread_id = hits[0].get("objectID")
        if not thread_id:
            return []

        comments_resp = await client.get(
            f"https://hn.algolia.com/api/v1/items/{thread_id}",
            headers={"User-Agent": random.choice(USER_AGENTS)},
            timeout=15,
        )
        thread_data = comments_resp.json()
        children = thread_data.get("children", [])

        jobs = []
        for comment in children[:60]:
            text = comment.get("text", "") or ""
            if not text:
                continue
            clean_text = re.sub(r'<[^>]+>', ' ', text)
            clean_text = re.sub(r'\s+', ' ', clean_text).strip()

            # Look for intern mentions
            if not any(kw in clean_text.lower() for kw in ["intern", "entry level", "new grad"]):
                continue

            lines = clean_text.split('|')
            title = lines[0].strip() if lines else clean_text[:80]

            url_match = re.search(
                r'https?://[^\s<>"]+(?:apply|jobs|careers|greenhouse|lever)[^\s<>"]*',
                clean_text, re.IGNORECASE
            )
            apply_url = url_match.group(0) if url_match else ""

            loc_match = re.search(
                r'\b(Remote|Chicago|Dallas|NYC|New York|SF|San Francisco|Austin|Boston|US Only)\b',
                clean_text, re.IGNORECASE
            )
            location = loc_match.group(1) if loc_match else ""

            company_match = re.match(r'^([A-Z][a-zA-Z\s&\.]+?)[\s\|]', clean_text)
            company = company_match.group(1).strip() if company_match else ""

            if title and len(clean_text) > 20:
                jobs.append({
                    "title": title[:100],
                    "company": company,
                    "url": apply_url or f"https://news.ycombinator.com/item?id={thread_id}",
                    "ats_url": apply_url,
                    "description": clean_text[:400],
                    "location": location,
                    "platform": "hackernews",
                })

        return jobs
    except Exception as e:
        print(f"[DeepScan] HN fetch error: {e}")
        return []


async def _google_search(query: str, client: httpx.AsyncClient) -> list[dict]:
    try:
        resp = await client.get(
            "https://www.google.com/search",
            params={"q": query, "num": 6, "hl": "en", "gl": "us"},
            headers={"User-Agent": random.choice(USER_AGENTS)},
            timeout=12,
        )
        soup = BeautifulSoup(resp.text, "lxml")
        results = []
        for g in soup.select("div.g"):
            a = g.select_one("a")
            title_el = g.select_one("h3")
            snippet_el = g.select_one("div.VwiC3b")
            if a and title_el and a.get("href", "").startswith("http"):
                results.append({
                    "url": a["href"],
                    "title": title_el.get_text(strip=True),
                    "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
                })
        return results
    except Exception:
        return []


async def run_deep_web_discovery(continuous: bool = True, stop_event=None):
    """
    Deep web discovery — YC jobs, HackerNews hiring, niche ATS boards.
    This is the correct function name expected by discovery_manager.py
    """
    pool = get_pool()
    scorer = JobScorer()

    print("[DeepScan] Deep web discovery starting...")
    iteration = 0

    while True:
        if stop_event and stop_event.is_set():
            break

        added_total = 0

        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            # 1. YC Jobs
            yc_jobs = await _fetch_yc_jobs(client)
            for job_data in yc_jobs:
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
                if scorer.passes_threshold(job.score) and pool.add(job):
                    added_total += 1
            await asyncio.sleep(3)

            # 2. HackerNews
            hn_jobs = await _fetch_hn_hiring(client)
            for job_data in hn_jobs:
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
                if scorer.passes_threshold(job.score) and pool.add(job):
                    added_total += 1
            await asyncio.sleep(3)

            # 3. Targeted Google queries (few, polite)
            for query in DEEP_QUERIES[:2]:
                if stop_event and stop_event.is_set():
                    break
                results = await _google_search(query, client)
                for result in results:
                    title = result.get("title", "")
                    url = result.get("url", "")
                    if not title or not url:
                        continue

                    company = ""
                    if " - " in title:
                        parts = title.rsplit(" - ", 1)
                        company = parts[-1].strip()
                        title = parts[0].strip()

                    loc_match = re.search(
                        r'(Remote|Chicago|Dallas|New York|San Francisco|Austin)',
                        result.get("snippet", ""), re.IGNORECASE
                    )
                    location = loc_match.group(1) if loc_match else ""

                    job = Job(
                        score=0.0,
                        job_id=_job_id(url),
                        title=title,
                        company=company,
                        url=url,
                        ats_url=url,
                        platform="deep_web",
                        description=result.get("snippet", "")[:400],
                        location=location,
                    )
                    job.score = scorer.score(job)
                    if scorer.passes_threshold(job.score) and pool.add(job):
                        added_total += 1

                await asyncio.sleep(random.uniform(10, 20))

        iteration += 1
        print(f"[DeepScan] Cycle {iteration}: +{added_total} jobs. Pool: {pool.size()}")

        if not continuous:
            break

        # Long sleep between cycles
        print(f"[DeepScan] Sleeping 45 min before next cycle...")
        for _ in range(CYCLE_INTERVAL // 10):
            if stop_event and stop_event.is_set():
                break
            await asyncio.sleep(10)
