"""
discovery/reddit_scanner.py
Scans Reddit for job postings, hiring threads, and referral opportunities.
No auth needed — reads public posts via JSON API.
Targets: r/forhire, r/cscareerquestions, r/MachineLearning, r/startups, etc.
"""

import asyncio
import hashlib
import random
import re
import time
import httpx
from discovery.job_pool import Job, JobScorer, get_pool
from core.settings_store import get_store

# Subreddits to scan for job posts
JOB_SUBREDDITS = [
    "forhire",
    "cscareerquestions",
    "MachineLearning",
    "artificial",
    "datascience",
    "learnmachinelearning",
    "startups",
    "entrepreneur",
    "remotework",
    "WFH",
    "jobsearchhacks",
    "internships",
    "ITCareerQuestions",
    "Python",
    "softwaregore",  # sometimes has hiring posts
    "programming",
]

# Hiring signal keywords
HIRING_KEYWORDS = [
    "hiring", "looking for", "we're hiring", "job opening", "internship available",
    "apply here", "apply at", "application link", "join our team", "open position",
    "software engineer", "ML engineer", "data scientist", "intern", "entry level",
    "new grad", "junior developer", "remote position", "fulltime", "full-time",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
]


def _job_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _extract_apply_url(text: str) -> str:
    """Extract application URL from post text."""
    # Look for greenhouse, lever, workday URLs first
    ats_pattern = r'https?://[^\s<>"]+(?:greenhouse\.io|lever\.co|myworkdayjobs|ashbyhq|workable)[^\s<>"]*'
    match = re.search(ats_pattern, text, re.IGNORECASE)
    if match:
        return match.group(0)

    # Generic URL in post
    url_pattern = r'https?://[^\s<>"\)]+(?:apply|jobs|careers|hiring)[^\s<>"\)]*'
    match = re.search(url_pattern, text, re.IGNORECASE)
    if match:
        return match.group(0)

    return ""


def _is_hiring_post(title: str, text: str) -> bool:
    """Check if a post is about hiring / job opportunity."""
    combined = (title + " " + text).lower()
    return any(kw in combined for kw in HIRING_KEYWORDS)


def _extract_job_details(title: str, text: str, url: str) -> dict:
    """Extract job title, company, location from post."""
    # Common patterns: "[Hiring] Company - Role | Location"
    company = ""
    job_title = title

    # [Hiring] pattern
    if "[hiring]" in title.lower():
        rest = re.sub(r'\[hiring\]', '', title, flags=re.IGNORECASE).strip()
        if " - " in rest:
            parts = rest.split(" - ", 1)
            company = parts[0].strip()
            job_title = parts[1].strip()
        elif " | " in rest:
            parts = rest.split(" | ", 1)
            job_title = parts[0].strip()

    # Location
    location = ""
    loc_patterns = [
        r'\b(Remote|Chicago|Dallas|New York|San Francisco|Austin|Seattle|Boston|US)\b',
        r'\|([^|]+)\|',
        r'\(([^)]+, [A-Z]{2})\)',
    ]
    combined = title + " " + text[:500]
    for pattern in loc_patterns:
        m = re.search(pattern, combined, re.IGNORECASE)
        if m:
            location = m.group(1).strip()
            break

    apply_url = _extract_apply_url(text)

    return {
        "title": job_title[:120],
        "company": company,
        "url": apply_url or url,
        "ats_url": apply_url,
        "description": text[:400],
        "location": location,
    }


async def _fetch_subreddit_new(subreddit: str, client: httpx.AsyncClient,
                                limit: int = 25) -> list[dict]:
    """Fetch new posts from a subreddit via Reddit JSON API."""
    try:
        resp = await client.get(
            f"https://www.reddit.com/r/{subreddit}/new.json",
            params={"limit": limit},
            headers={
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "application/json",
            },
            timeout=12,
        )
        if resp.status_code != 200:
            return []

        data = resp.json()
        posts = []
        for child in data.get("data", {}).get("children", []):
            post = child.get("data", {})
            posts.append({
                "title": post.get("title", ""),
                "text": post.get("selftext", ""),
                "url": f"https://reddit.com{post.get('permalink', '')}",
                "external_url": post.get("url", ""),
                "score": post.get("score", 0),
                "created": post.get("created_utc", 0),
            })
        return posts

    except Exception as e:
        print(f"[Reddit] Error fetching r/{subreddit}: {e}")
        return []


async def _fetch_subreddit_search(subreddit: str, query: str,
                                   client: httpx.AsyncClient) -> list[dict]:
    """Search within a subreddit."""
    try:
        resp = await client.get(
            f"https://www.reddit.com/r/{subreddit}/search.json",
            params={"q": query, "restrict_sr": 1, "sort": "new", "limit": 15},
            headers={
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "application/json",
            },
            timeout=12,
        )
        if resp.status_code != 200:
            return []

        data = resp.json()
        posts = []
        for child in data.get("data", {}).get("children", []):
            post = child.get("data", {})
            posts.append({
                "title": post.get("title", ""),
                "text": post.get("selftext", ""),
                "url": f"https://reddit.com{post.get('permalink', '')}",
                "external_url": post.get("url", ""),
                "score": post.get("score", 0),
            })
        return posts
    except Exception:
        return []


async def run_reddit_discovery(continuous: bool = True, stop_event=None):
    """
    Continuously scan Reddit for job postings.
    Runs as background coroutine alongside other discovery sources.
    """
    store = get_store()
    pool = get_pool()
    scorer = JobScorer()
    profile = store.get_profile() or {}

    target_roles = profile.get("target_roles", "software engineer AI ML intern")
    search_terms = [
        "hiring intern", "software engineer intern", "AI ML hiring",
        "machine learning intern", "data science intern", "remote intern"
    ]

    print("[Reddit] Discovery starting...")
    iteration = 0

    while True:
        if stop_event and stop_event.is_set():
            break

        added_total = 0

        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            # Scan key subreddits for new posts
            for subreddit in JOB_SUBREDDITS:
                if stop_event and stop_event.is_set():
                    break

                posts = await _fetch_subreddit_new(subreddit, client)

                for post in posts:
                    title = post["title"]
                    text = post["text"]

                    if not _is_hiring_post(title, text):
                        continue

                    details = _extract_job_details(title, text, post["url"])
                    if not details["title"]:
                        continue

                    apply_url = details["ats_url"] or details["url"]
                    job = Job(
                        score=0.0,
                        job_id=_job_id(apply_url),
                        title=details["title"],
                        company=details["company"],
                        url=post["url"],
                        ats_url=details["ats_url"],
                        platform=f"reddit/r/{subreddit}",
                        description=details["description"],
                        location=details["location"],
                    )
                    job.score = scorer.score(job)

                    if job.score >= 4.0:
                        was_added = pool.add(job)
                        if was_added:
                            added_total += 1
                            print(f"[Reddit] Added: {job.title} @ {job.company or 'Unknown'} (r/{subreddit})")

                await asyncio.sleep(random.uniform(2, 4))

            # Also search specific terms in r/forhire and r/cscareerquestions
            for term in search_terms[:3]:
                for subreddit in ["forhire", "cscareerquestions"]:
                    if stop_event and stop_event.is_set():
                        break
                    posts = await _fetch_subreddit_search(subreddit, term, client)
                    for post in posts:
                        if not _is_hiring_post(post["title"], post["text"]):
                            continue
                        details = _extract_job_details(
                            post["title"], post["text"], post["url"]
                        )
                        apply_url = details["ats_url"] or details["url"]
                        job = Job(
                            score=0.0,
                            job_id=_job_id(apply_url),
                            title=details["title"],
                            company=details["company"],
                            url=post["url"],
                            ats_url=details["ats_url"],
                            platform=f"reddit/r/{subreddit}",
                            description=details["description"],
                            location=details["location"],
                        )
                        job.score = scorer.score(job)
                        if job.score >= 4.0:
                            if pool.add(job):
                                added_total += 1
                    await asyncio.sleep(random.uniform(1.5, 3))

        iteration += 1
        print(f"[Reddit] Cycle {iteration}: +{added_total} jobs. Pool: {pool.size()}")

        if not continuous:
            break

        await asyncio.sleep(120)  # Re-scan every 2 minutes
