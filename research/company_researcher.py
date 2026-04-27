"""
research/company_researcher.py
Deep internet research on a company before every application.
Capped at 3 minutes max — never hangs the track pipeline.
Falls back to minimal insight if research times out or fails.
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

# Hard limits — research must never block the pipeline
MAX_SOURCES = 5           # Reduced from 20 — fewer sources, faster
MAX_TEXT_PER_SOURCE = 1500
RESEARCH_TIMEOUT = 90     # 90 seconds max for entire research phase
FETCH_TIMEOUT = 8         # 8 seconds per individual URL fetch


async def _fetch_page(url: str, client: httpx.AsyncClient) -> str:
    try:
        resp = await client.get(
            url,
            headers={"User-Agent": random.choice(USER_AGENTS)},
            timeout=FETCH_TIMEOUT,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        text = re.sub(r'\s+', ' ', text)
        return text[:MAX_TEXT_PER_SOURCE]
    except Exception:
        return ""


async def _google_search_raw(query: str, client: httpx.AsyncClient,
                               num: int = 5) -> list[dict]:
    try:
        resp = await client.get(
            "https://www.google.com/search",
            params={"q": query, "num": num, "hl": "en"},
            headers={"User-Agent": random.choice(USER_AGENTS)},
            timeout=FETCH_TIMEOUT,
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
        return results[:5]
    except Exception:
        return []


def _build_research_queries(company: str, job_title: str) -> list[str]:
    """Focused queries only — fewer is better to avoid rate limiting."""
    c = company
    return [
        f'"{c}" site:reddit.com culture employees',
        f'"{c}" glassdoor reviews',
        f'"{c}" mission values engineers',
        f'"{c}" engineering blog',
        f'site:reddit.com "{c}" "I work at" OR "I worked at"',
    ]


async def _do_research(company: str, job_title: str,
                        job_description: str) -> dict:
    """
    Inner research coroutine — wrapped in timeout by research_company().
    """
    queries = _build_research_queries(company, job_title)
    all_snippets = []
    all_text = []
    sources_used = 0

    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
        for query in queries:
            if sources_used >= MAX_SOURCES:
                break

            results = await _google_search_raw(query, client, num=4)

            for result in results:
                if sources_used >= MAX_SOURCES:
                    break

                snippet = result["snippet"]
                if snippet and len(snippet) > 30:
                    all_snippets.append(snippet)

                url = result["url"]
                if any(domain in url for domain in [
                    "reddit.com", "glassdoor.com",
                    "news.ycombinator.com",
                ]):
                    page_text = await _fetch_page(url, client)
                    if page_text:
                        all_text.append(f"[Source: {url[:60]}]\n{page_text}")
                        sources_used += 1

                await asyncio.sleep(random.uniform(0.5, 1.0))

            await asyncio.sleep(random.uniform(1.0, 2.0))

    return {
        "company": company,
        "job_title": job_title,
        "snippets": all_snippets,
        "full_texts": all_text,
        "sources_count": sources_used,
        "job_description": job_description,
    }


async def research_company(company: str, job_title: str,
                             job_description: str = "") -> dict:
    """
    Full async research pipeline for one company+role.
    Hard timeout of 90 seconds — falls back to minimal insight if exceeded.
    Never blocks the track pipeline.
    """
    store = get_store()

    # Check cache first (within 7 days)
    existing = store.get_company_profile(company)
    if existing and existing.get("last_updated"):
        try:
            age_days = (time.time() - time.mktime(
                time.strptime(existing["last_updated"], "%Y-%m-%d %H:%M:%S")
            )) / 86400
            if age_days < 7:
                print(f"[Research] Using cached profile for {company}")
                return _cached_to_research(existing, job_title)
        except Exception:
            pass

    print(f"[Research] Researching {company} (90s timeout)...")

    try:
        # Hard timeout — research MUST complete within 90 seconds
        research_data = await asyncio.wait_for(
            _do_research(company, job_title, job_description),
            timeout=RESEARCH_TIMEOUT
        )
        print(f"[Research] Done: {company} — {research_data['sources_count']} sources")
        return research_data

    except asyncio.TimeoutError:
        print(f"[Research] Timeout for {company} — using minimal insight")
        return {
            "company": company,
            "job_title": job_title,
            "snippets": [],
            "full_texts": [],
            "sources_count": 0,
            "job_description": job_description,
            "timed_out": True,
        }
    except Exception as e:
        print(f"[Research] Error for {company}: {e} — using minimal insight")
        return {
            "company": company,
            "job_title": job_title,
            "snippets": [],
            "full_texts": [],
            "sources_count": 0,
            "job_description": job_description,
        }


def _cached_to_research(cached: dict, job_title: str) -> dict:
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
