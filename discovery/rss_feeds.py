"""
discovery/rss_feeds.py
Parses job RSS feeds — 100% ToS-safe, no scraping, no auth needed.
Indeed, Handshake (public), USAJobs all provide RSS.
"""

import asyncio
import hashlib
import re
import feedparser
import httpx
from discovery.job_pool import Job, JobScorer, get_pool
from core.settings_store import get_store


def _job_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _build_feed_urls(profile: dict) -> list[tuple[str, str]]:
    roles = profile.get("target_roles", "software engineer intern AI ML")
    locations = profile.get("locations", "Chicago,Dallas,Remote")
    loc_list = [l.strip().replace(" ", "+") for l in locations.split(",")][:3]

    role_terms = [
        "software+engineer+intern",
        "machine+learning+intern",
        "AI+intern",
        "data+science+intern",
        "computer+science+intern",
        "software+developer+intern",
        "python+developer+intern",
    ]

    feeds = []

    # Indeed RSS
    for role in role_terms[:4]:
        for loc in loc_list + ["remote"]:
            feeds.append((
                f"https://www.indeed.com/rss?q={role}&l={loc}&sort=date",
                "indeed"
            ))

    # USAJobs RSS
    for role in ["computer+scientist", "data+scientist", "artificial+intelligence", "software+engineer"]:
        feeds.append((
            f"https://www.usajobs.gov/Search/Results?k={role}&format=rss",
            "usajobs"
        ))

    return feeds


async def _parse_feed(feed_url: str, platform: str,
                       client: httpx.AsyncClient) -> list[dict]:
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

            loc_match = re.search(
                r'(Remote|Chicago|Dallas|New York|San Francisco|Austin|Boston|\w+,\s*[A-Z]{2})',
                description, re.IGNORECASE
            )
            if loc_match:
                location = loc_match.group(1)

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

                    # Use scorer's threshold instead of hardcoded value
                    if scorer.passes_threshold(job.score):
                        was_added = pool.add(job)
                        if was_added:
                            added_total += 1
                            print(f"[RSS] Added: {job.title} @ {job.company} (score: {job.score:.1f})")

                await asyncio.sleep(2)

        iteration += 1
        print(f"[RSS] Cycle {iteration}: added {added_total} jobs. Pool size: {pool.size()}")

        if not continuous:
            break

        await asyncio.sleep(300)
