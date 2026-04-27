"""
discovery/rss_feeds.py
Parses job RSS feeds — 100% ToS-safe, no scraping, no auth needed.
Indeed, Handshake (public), USAJobs all provide RSS.
"""

import asyncio
import hashlib
import time
import feedparser
import httpx
from discovery.job_pool import Job, JobScorer, get_pool
from core.settings_store import get_store


def _job_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _build_feed_urls(profile: dict) -> list[tuple[str, str]]:
    """Build list of (feed_url, platform_name) from user preferences."""
    roles = profile.get("target_roles", "software engineer intern AI ML")
    locations = profile.get("locations", "Chicago,Dallas,Remote")

    # Parse location list
    loc_list = [l.strip().replace(" ", "+") for l in locations.split(",")][:3]

    role_terms = [
        "software+engineer+intern",
        "machine+learning+intern",
        "AI+intern",
        "data+science+intern",
        "computer+science+intern",
    ]

    feeds = []

    # Indeed RSS (public, no auth)
    for role in role_terms[:3]:
        for loc in loc_list:
            feeds.append((
                f"https://www.indeed.com/rss?q={role}&l={loc}&sort=date",
                "indeed"
            ))

    # USAJobs RSS (federal/research positions)
    for role in ["computer+scientist", "data+scientist", "artificial+intelligence"]:
        feeds.append((
            f"https://www.usajobs.gov/Search/Results?k={role}&format=rss",
            "usajobs"
        ))

    # LinkedIn public job feeds (no auth needed for public listings)
    for role in role_terms[:3]:
        feeds.append((
            f"https://www.linkedin.com/jobs/search/?keywords={role}&location=United+States&f_E=1&f_JT=I&format=rss",
            "linkedin_public"
        ))

    return feeds


async def _parse_feed(feed_url: str, platform: str,
                       client: httpx.AsyncClient) -> list[dict]:
    """Fetch and parse one RSS feed."""
    try:
        resp = await client.get(
            feed_url,
            headers={"User-Agent": "AutoApplyAI Job Discovery/1.0"},
            timeout=15,
        )
        if resp.status_code != 200:
            return []

        feed = feedparser.parse(resp.text)
        jobs = []

        for entry in feed.entries[:20]:
            title = getattr(entry, "title", "")
            url = getattr(entry, "link", "")
            description = getattr(entry, "summary", "")
            location = ""

            # Try to extract location from description
            import re
            loc_match = re.search(
                r'(Remote|Chicago|Dallas|New York|San Francisco|Austin|Boston|\w+,\s*[A-Z]{2})',
                description, re.IGNORECASE
            )
            if loc_match:
                location = loc_match.group(1)

            # Try to extract company
            company = ""
            if " - " in title:
                parts = title.rsplit(" - ", 1)
                company = parts[-1].strip()
                title = parts[0].strip()
            elif " at " in title.lower():
                idx = title.lower().index(" at ")
                company = title[idx + 4:].strip()
                title = title[:idx].strip()

            if title and url:
                jobs.append({
                    "title": title,
                    "company": company,
                    "url": url,
                    "description": description[:400],
                    "location": location,
                    "platform": platform,
                })

        return jobs

    except Exception as e:
        print(f"[RSS] Error parsing {platform} feed: {e}")
        return []


async def run_rss_discovery(continuous: bool = True, stop_event=None):
    """
    Continuously parse RSS feeds and add jobs to pool.
    Runs as background coroutine alongside Google discovery.
    """
    store = get_store()
    pool = get_pool()
    scorer = JobScorer()
    profile = store.get_profile() or {}

    print("[RSS] Feed discovery starting...")
    iteration = 0

    while True:
        if stop_event and stop_event.is_set():
            break

        feed_urls = _build_feed_urls(profile)
        added_total = 0

        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            for feed_url, platform in feed_urls:
                if stop_event and stop_event.is_set():
                    break

                jobs = await _parse_feed(feed_url, platform, client)

                for job_data in jobs:
                    job = Job(
                        score=0.0,
                        job_id=_job_id(job_data["url"]),
                        title=job_data["title"],
                        company=job_data["company"],
                        url=job_data["url"],
                        ats_url=job_data["url"],
                        platform=job_data["platform"],
                        description=job_data["description"],
                        location=job_data["location"],
                    )
                    job.score = scorer.score(job)

                    if job.score >= 4.0:
                        was_added = pool.add(job)
                        if was_added:
                            added_total += 1

                await asyncio.sleep(2)

        iteration += 1
        print(f"[RSS] Cycle {iteration}: added {added_total} jobs. Pool size: {pool.size()}")

        if not continuous:
            break

        await asyncio.sleep(300)  # Re-check feeds every 5 minutes
