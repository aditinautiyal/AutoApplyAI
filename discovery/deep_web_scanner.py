"""
discovery/deep_web_scanner.py
Broad internet job discovery. Goes beyond standard platforms.
Finds jobs on company blogs, HackerNews "Who is Hiring", startup sites,
academic job boards, niche forums, and anywhere else they appear.
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

# Direct sources to scrape
DIRECT_SOURCES = [
    # HackerNews monthly "Who is Hiring" thread
    ("https://news.ycombinator.com/ask", "hn_wih", "who is hiring"),
    # Y Combinator job board
    ("https://www.ycombinator.com/jobs", "yc_jobs", None),
    # Wellfound (AngelList) — startups
    ("https://wellfound.com/jobs?role=Software+Engineer&remote=true", "wellfound", None),
]

# Google queries targeting deep/niche sources
DEEP_QUERIES = [
    # HackerNews hiring threads
    'site:news.ycombinator.com "Ask HN: Who is hiring" 2024 OR 2025',
    'site:news.ycombinator.com "who wants to be hired" 2025',

    # Company career pages not indexed by job boards
    '"internship" "apply" "software" OR "AI" OR "ML" "2025" -site:linkedin.com -site:indeed.com',

    # Niche communities
    'site:dev.to hiring intern software engineer 2025',
    'site:hashnode.com "we are hiring" intern developer',
    'site:medium.com startup hiring intern engineer 2025',

    # Academic and research
    '"research intern" "apply" "machine learning" OR "AI" 2025 site:edu',
    '"summer research" "undergraduate" "computer science" "2025" apply',
    'NSF REU computer science 2025 apply',
    'Argonne OR Sandia OR NIST internship 2025 computer science apply',

    # Startups via blogs
    '"join us" OR "we\'re hiring" "software engineer" "intern" startup 2025',
    '"open roles" startup AI ML engineer intern site:notion.so OR site:jobs.ashbyhq.com',

    # Chicago and Dallas specific deep search
    '"Chicago" startup hiring software intern 2025 apply',
    '"Dallas" tech company intern software engineer 2025',
    'Chicago AI startup "software engineer intern" 2025',

    # Fellowship and special programs
    '"fellowship" "computer science" "2025" "apply" "paid" OR "stipend"',
    '"fellowship" "AI" OR "machine learning" "2025" undergraduate apply',
    'Google STEP intern 2025 apply',
    'Microsoft Explore intern 2025 apply',
    'Meta University intern 2025 apply',
    'Amazon Propel intern 2025 apply',

    # Smaller/niche job boards
    'site:simplyhired.com software engineer intern AI 2025',
    'site:ziprecruiter.com machine learning intern 2025',
    'site:dice.com software engineer intern AI ML 2025 entry level',
    'site:builtinchicago.org software engineer intern 2025',
    'site:builtindallas.com software engineer intern 2025',
]


def _job_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


async def _google_search(query: str, client: httpx.AsyncClient,
                          num: int = 8) -> list[dict]:
    """Standard Google search returning results list."""
    try:
        resp = await client.get(
            "https://www.google.com/search",
            params={"q": query, "num": num, "hl": "en", "gl": "us"},
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


async def _fetch_hn_hiring(client: httpx.AsyncClient) -> list[dict]:
    """
    Fetch HackerNews 'Who is Hiring' thread and extract job mentions.
    Returns list of job dicts.
    """
    try:
        # Search for the latest monthly thread
        resp = await client.get(
            "https://hn.algolia.com/api/v1/search",
            params={
                "query": "Ask HN: Who is hiring?",
                "tags": "story",
                "hitsPerPage": 3,
            },
            headers={"User-Agent": random.choice(USER_AGENTS)},
            timeout=12,
        )
        data = resp.json()
        hits = data.get("hits", [])
        if not hits:
            return []

        # Get the most recent thread
        thread = hits[0]
        thread_id = thread.get("objectID")
        if not thread_id:
            return []

        # Fetch comments
        comments_resp = await client.get(
            f"https://hn.algolia.com/api/v1/items/{thread_id}",
            headers={"User-Agent": random.choice(USER_AGENTS)},
            timeout=15,
        )
        thread_data = comments_resp.json()
        children = thread_data.get("children", [])

        jobs = []
        for comment in children[:100]:
            text = comment.get("text", "") or ""
            if not text:
                continue
            # Clean HTML
            clean_text = re.sub(r'<[^>]+>', ' ', text)
            clean_text = re.sub(r'\s+', ' ', clean_text).strip()

            # Extract key info
            lines = clean_text.split('|')
            title = lines[0].strip() if lines else clean_text[:80]

            # Find apply URL in text
            url_match = re.search(
                r'https?://[^\s<>"]+(?:apply|jobs|careers|greenhouse|lever|workday)[^\s<>"]*',
                clean_text, re.IGNORECASE
            )
            apply_url = url_match.group(0) if url_match else ""

            # Location
            loc_match = re.search(
                r'\b(Remote|Chicago|Dallas|NYC|New York|SF|San Francisco|Austin|Boston|US Only)\b',
                clean_text, re.IGNORECASE
            )
            location = loc_match.group(1) if loc_match else ""

            # Company (usually first line)
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


async def _fetch_yc_jobs(client: httpx.AsyncClient) -> list[dict]:
    """Scrape YC job board for intern/entry-level roles."""
    try:
        resp = await client.get(
            "https://www.ycombinator.com/jobs",
            headers={"User-Agent": random.choice(USER_AGENTS)},
            timeout=15,
        )
        soup = BeautifulSoup(resp.text, "lxml")
        jobs = []

        for job_card in soup.select("a[class*='job']")[:30]:
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


def _parse_result_to_job(result: dict, platform: str = "deep_web") -> dict:
    """Convert a Google search result to job dict."""
    title = result["title"]
    url = result["url"]
    snippet = result["snippet"]

    company = ""
    if " - " in title:
        parts = title.rsplit(" - ", 1)
        company = parts[-1].strip()
        title = parts[0].strip()
    elif " at " in title.lower():
        idx = title.lower().index(" at ")
        company = title[idx + 4:].strip()
        title = title[:idx].strip()

    loc_match = re.search(
        r'(Remote|Chicago|Dallas|New York|San Francisco|Austin|Boston|\w+,\s*[A-Z]{2})',
        snippet, re.IGNORECASE
    )
    location = loc_match.group(1) if loc_match else ""

    return {
        "title": title or result["title"],
        "company": company,
        "url": url,
        "ats_url": url,
        "description": snippet[:400],
        "location": location,
        "platform": platform,
    }


async def run_deep_web_discovery(continuous: bool = True, stop_event=None):
    """
    Broad internet discovery. Finds jobs on niche sites, HN, YC, fellowships,
    company blogs, and anywhere Google can find them.
    """
    pool = get_pool()
    scorer = JobScorer()
    store = get_store()
    profile = store.get_profile() or {}

    print("[DeepScan] Deep web discovery starting...")
    iteration = 0

    while True:
        if stop_event and stop_event.is_set():
            break

        added_total = 0

        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            # 1. HackerNews "Who is Hiring"
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
                if job.score >= 3.5 and pool.add(job):
                    added_total += 1
            await asyncio.sleep(3)

            # 2. YC Jobs
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
                if job.score >= 3.5 and pool.add(job):
                    added_total += 1
            await asyncio.sleep(3)

            # 3. Deep Google queries
            random.shuffle(DEEP_QUERIES)
            for query in DEEP_QUERIES[:15]:  # Rotate through queries each cycle
                if stop_event and stop_event.is_set():
                    break

                results = await _google_search(query, client, num=8)
                for result in results:
                    job_data = _parse_result_to_job(result, "deep_web")
                    if not job_data["title"]:
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
                    if job.score >= 3.5 and pool.add(job):
                        added_total += 1

                await asyncio.sleep(random.uniform(4, 9))

        iteration += 1
        print(f"[DeepScan] Cycle {iteration}: +{added_total} jobs. Pool: {pool.size()}")

        if not continuous:
            break

        await asyncio.sleep(300)  # 5 minute rest between full cycles
