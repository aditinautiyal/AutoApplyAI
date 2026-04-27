"""
research/company_researcher.py
Deep internet research on a company before every application.
NOT rigid searches. Casts widest net across forums, comments, social, news.
Weak signals aggregated — frequency = weight.
Results stored in company_profiles for reuse.
"""

import asyncio
import random
import re
import time
import httpx
from bs4 import BeautifulSoup
from typing import Optional
from core.settings_store import get_store
from core.api_router import get_router

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
]

MAX_SOURCES = 20      # Max pages to scrape per company
MAX_TEXT_PER_SOURCE = 2000  # Characters per source (token efficiency)


async def _fetch_page(url: str, client: httpx.AsyncClient) -> str:
    """Fetch a URL and return cleaned text."""
    try:
        resp = await client.get(
            url,
            headers={"User-Agent": random.choice(USER_AGENTS)},
            timeout=12,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "lxml")
        # Remove script/style
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        # Collapse whitespace
        text = re.sub(r'\s+', ' ', text)
        return text[:MAX_TEXT_PER_SOURCE]
    except Exception:
        return ""


async def _google_search_raw(query: str, client: httpx.AsyncClient,
                               num: int = 8) -> list[dict]:
    """Raw Google search returning result list."""
    try:
        resp = await client.get(
            "https://www.google.com/search",
            params={"q": query, "num": num, "hl": "en"},
            headers={"User-Agent": random.choice(USER_AGENTS)},
            timeout=12,
        )
        soup = BeautifulSoup(resp.text, "lxml")
        results = []
        for g in soup.select("div.g"):
            a = g.select_one("a")
            snippet = g.select_one("div.VwiC3b, span.st")
            if a and a.get("href", "").startswith("http"):
                results.append({
                    "url": a["href"],
                    "snippet": snippet.get_text(strip=True) if snippet else "",
                })
        return results
    except Exception:
        return []


def _build_research_queries(company: str, job_title: str) -> list[str]:
    """
    Build diverse, broad research queries.
    NOT just interview tips — we want casual mentions, opinions, feelings.
    """
    c = company
    j = job_title

    return [
        # Casual mentions — the gold standard
        f'"{c}" site:reddit.com',
        f'"{c}" "{j}" site:reddit.com',
        f'site:reddit.com "{c}" employees OR working OR culture OR love OR hate OR salary',
        f'"{c}" glassdoor reviews culture values',
        f'"{c}" linkedin employees posts',

        # What the company actually cares about
        f'"{c}" mission values what we look for engineers',
        f'"{c}" engineering blog technical culture',
        f'"{c}" about us team culture why work here',
        f'"{c}" press release 2024 OR 2025',

        # Job-specific research
        f'"{c}" "{j}" interview experience',
        f'"{c}" "{j}" what do you do day to day',
        f'working at "{c}" as {j} OR similar role',

        # Off-hand mentions in broader threads
        f'site:reddit.com "{c}" "I work at" OR "I worked at" OR "my job at"',
        f'site:reddit.com "best part of working at {c}" OR "worst part of working at {c}"',
        f'"{c}" hackernews OR ycombinator',
        f'site:news.ycombinator.com "{c}"',

        # Quora and forums
        f'site:quora.com "{c}"',
        f'"{c}" what do they value interview tips 2024 2025',

        # Company social presence
        f'"{c}" twitter OR linkedin posts 2024 2025',
        f'"{c}" CEO OR founder interview podcast',
        f'"{c}" news layoffs OR growth OR funding OR hiring 2024 2025',

        # Startup-specific (if startup)
        f'"{c}" crunchbase funding team',
        f'"{c}" yc OR sequoia OR a16z OR techcrunch',
    ]


async def research_company(company: str, job_title: str,
                             job_description: str = "") -> dict:
    """
    Full async research pipeline for one company+role.
    Returns structured research dict ready for insight synthesis.
    """
    store = get_store()

    # Check if we already have recent data (within 7 days)
    existing = store.get_company_profile(company)
    if existing and existing.get("last_updated"):
        age_days = (time.time() - time.mktime(
            time.strptime(existing["last_updated"], "%Y-%m-%d %H:%M:%S")
        )) / 86400
        if age_days < 7:
            print(f"[Research] Using cached profile for {company} ({age_days:.1f}d old)")
            return _cached_to_research(existing, job_title)

    print(f"[Research] Starting deep research: {company} / {job_title}")
    queries = _build_research_queries(company, job_title)
    all_snippets = []
    all_text = []
    sources_used = 0

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for query in queries:
            if sources_used >= MAX_SOURCES:
                break

            results = await _google_search_raw(query, client, num=5)

            for result in results:
                if sources_used >= MAX_SOURCES:
                    break

                snippet = result["snippet"]
                if snippet and len(snippet) > 30:
                    all_snippets.append(snippet)

                # Fetch full page for high-value sources
                url = result["url"]
                if any(domain in url for domain in [
                    "reddit.com", "glassdoor.com", "linkedin.com",
                    "news.ycombinator.com", "quora.com", company.lower().replace(" ", "")
                ]):
                    page_text = await _fetch_page(url, client)
                    if page_text:
                        all_text.append(f"[Source: {url[:60]}]\n{page_text}")
                        sources_used += 1

                await asyncio.sleep(random.uniform(0.5, 1.5))

            await asyncio.sleep(random.uniform(1.5, 3.0))

    research_data = {
        "company": company,
        "job_title": job_title,
        "snippets": all_snippets,
        "full_texts": all_text,
        "sources_count": sources_used,
        "job_description": job_description,
    }

    print(f"[Research] Complete: {company} — {sources_used} sources, {len(all_snippets)} snippets")
    return research_data


def _cached_to_research(cached: dict, job_title: str) -> dict:
    """Convert a cached company profile back to research format."""
    return {
        "company": cached["company_name"],
        "job_title": job_title,
        "snippets": [],
        "full_texts": [cached.get("raw_research", "")],
        "sources_count": cached.get("source_count", 0),
        "job_description": "",
        "from_cache": True,
        "personality": cached.get("personality", ""),
        "core_values": cached.get("core_values", ""),
        "culture_signals": cached.get("culture_signals", ""),
        "keywords": cached.get("keywords", ""),
        "tone": cached.get("tone", ""),
    }
