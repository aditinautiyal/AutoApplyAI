"""
research/advice_scraper.py
Scrapes the internet for job application advice, cover letter templates,
cold email tips, recruiter approach strategies.
Tracks mention frequency and success rates.
Organizes into advice_db for use in all applications.
"""

import asyncio
import json
import random
import re
import hashlib
import time
import httpx
from bs4 import BeautifulSoup
from core.settings_store import get_store
from core.api_router import get_router

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
]

ADVICE_QUERIES = [
    # Cover letter advice
    "best cover letter template software engineer intern that got hired",
    "site:reddit.com cover letter that worked intern tech company",
    "cold email recruiter template that got response site:reddit.com",
    "linkedin message recruiter template got interview",
    "how to cold email startup founder internship that worked",
    "cover letter tips that actually work reddit 2024 2025",

    # Application tips
    "site:reddit.com how I landed Google intern offer",
    "site:reddit.com how I got interview big tech no experience",
    "site:linkedin.com resume format that got me 10 interviews",
    "what recruiters look for cover letter reddit 2024",
    "how to stand out job application no experience site:reddit.com",
    "cold emailing companies internship success story reddit",

    # Recruiter approach
    "how to message recruiter linkedin what to say got interview",
    "site:reddit.com reached out to employee got referral how",
    "networking got job reddit tips what worked",
    "site:reddit.com r/cscareerquestions tips got internship offer",

    # Startup specific
    "how to approach startup for job cold email what works",
    "emailing startup CEO internship worked reddit",
    "site:reddit.com small startup hiring intern how to apply stand out",

    # Keywords and formatting
    "ATS resume keywords software engineer that works 2024 2025",
    "resume words that get past ATS filter tech jobs",
    "action verbs resume tech internship that got hired reddit",
]


async def _fetch_page(url: str, client: httpx.AsyncClient) -> str:
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
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        return re.sub(r'\s+', ' ', text)[:3000]
    except Exception:
        return ""


async def _google_search(query: str, client: httpx.AsyncClient) -> list[dict]:
    try:
        resp = await client.get(
            "https://www.google.com/search",
            params={"q": query, "num": 6, "hl": "en"},
            headers={"User-Agent": random.choice(USER_AGENTS)},
            timeout=12,
        )
        soup = BeautifulSoup(resp.text, "lxml")
        results = []
        for g in soup.select("div.g"):
            a = g.select_one("a")
            snippet = g.select_one("div.VwiC3b")
            if a and a.get("href", "").startswith("http"):
                results.append({
                    "url": a["href"],
                    "snippet": snippet.get_text(strip=True) if snippet else "",
                })
        return results
    except Exception:
        return []


def _extract_advice_insights(raw_text: str, query: str) -> list[dict]:
    """
    Use Claude Haiku to extract structured advice from raw scraped text.
    Returns list of advice items with type, content, and keywords.
    """
    router = get_router()
    if not raw_text.strip():
        return []

    prompt = f"""Extract specific, actionable job application advice from this text.
Search context: {query}

Return a JSON array of advice items. Each item:
{{
  "type": "cover_letter|cold_email|resume|networking|recruiter_message|general",
  "content": "The specific advice or tip (1-2 sentences, concrete)",
  "keywords": ["keyword1", "keyword2"],
  "strength": "strong|medium|weak"
}}

Only include advice that is:
- Specific and actionable (not generic like "be yourself")
- From real experience or data
- About job applications, cold emails, cover letters, or networking

Return [] if no good advice found.
Return ONLY the JSON array.

Text:
{raw_text[:2500]}"""

    try:
        resp = router.complete(prompt, max_tokens=800)
        resp = resp.strip()
        if resp.startswith("```"):
            resp = re.sub(r"```[a-z]*\n?", "", resp).strip().rstrip("```")
        items = json.loads(resp)
        return items if isinstance(items, list) else []
    except Exception:
        return []


def _save_advice(items: list[dict], source_url: str, source_platform: str):
    """Save advice items to the advice_db, incrementing mention count for duplicates."""
    store = get_store()

    for item in items:
        content = item.get("content", "").strip()
        if not content or len(content) < 20:
            continue

        advice_type = item.get("type", "general")
        keywords = json.dumps(item.get("keywords", []))

        # Check for similar existing advice (simple keyword overlap check)
        existing = store.conn.execute("""
            SELECT id, mention_count FROM advice_db
            WHERE advice_type=? AND content LIKE ?
        """, (advice_type, f"%{content[:40]}%")).fetchone()

        if existing:
            store.conn.execute("""
                UPDATE advice_db SET mention_count=mention_count+1
                WHERE id=?
            """, (existing[0],))
        else:
            store.conn.execute("""
                INSERT INTO advice_db (advice_type, content, source_url, source_platform, tags)
                VALUES (?, ?, ?, ?, ?)
            """, (advice_type, content, source_url, source_platform, keywords))

    store.conn.commit()


def get_best_advice(advice_type: str = None, limit: int = 10) -> list[dict]:
    """
    Get highest-ranked advice items for use in generation.
    Ranked by mention_count * (1 + success_score).
    """
    store = get_store()
    query = """
        SELECT advice_type, content, mention_count, success_score, tags
        FROM advice_db
    """
    params = []
    if advice_type:
        query += " WHERE advice_type=?"
        params.append(advice_type)
    query += " ORDER BY (mention_count * (1.0 + success_score)) DESC LIMIT ?"
    params.append(limit)

    cursor = store.conn.execute(query, params)
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def update_advice_success(advice_content_fragment: str):
    """
    Increment success score for advice that correlated with a positive response.
    Called when an application gets interview/offer.
    """
    store = get_store()
    store.conn.execute("""
        UPDATE advice_db SET success_score=success_score+1.0
        WHERE content LIKE ?
    """, (f"%{advice_content_fragment[:30]}%",))
    store.conn.commit()


async def run_advice_scraping(stop_event=None):
    """
    One-time (and periodic) advice scraping run.
    Collects tips from across the internet into advice_db.
    """
    store = get_store()

    # Check how many advice items we already have
    existing_count = store.conn.execute("SELECT COUNT(*) FROM advice_db").fetchone()[0]
    if existing_count > 500:
        print(f"[Advice] Already have {existing_count} items — skipping scrape")
        return

    print(f"[Advice] Starting advice scraping ({len(ADVICE_QUERIES)} queries)...")
    total_added = 0

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for query in ADVICE_QUERIES:
            if stop_event and stop_event.is_set():
                break

            results = await _google_search(query, client)

            for result in results[:3]:
                url = result["url"]
                snippet = result["snippet"]

                # Save snippet-level advice immediately
                snippet_advice = _extract_advice_insights(snippet, query)
                if snippet_advice:
                    platform = "reddit" if "reddit.com" in url else "web"
                    _save_advice(snippet_advice, url, platform)
                    total_added += len(snippet_advice)

                # Fetch full page for high-value sources
                if any(domain in url for domain in ["reddit.com", "linkedin.com"]):
                    page_text = await _fetch_page(url, client)
                    if page_text:
                        page_advice = _extract_advice_insights(page_text, query)
                        if page_advice:
                            platform = "reddit" if "reddit.com" in url else "linkedin"
                            _save_advice(page_advice, url, platform)
                            total_added += len(page_advice)
                    await asyncio.sleep(random.uniform(1.5, 3.0))

            await asyncio.sleep(random.uniform(3, 7))

    print(f"[Advice] Scraping complete — added {total_added} advice items")


def get_advice_context_for_generation(advice_type: str = "cover_letter") -> str:
    """
    Get a formatted string of top advice to inject into cover letter/email prompts.
    """
    items = get_best_advice(advice_type=advice_type, limit=8)
    if not items:
        return ""

    lines = [f"Proven advice for {advice_type.replace('_', ' ')}:"]
    for item in items:
        count = item.get("mention_count", 1)
        score = item.get("success_score", 0)
        lines.append(f"- {item['content']} (mentioned {count}x, success score: {score:.1f})")

    return "\n".join(lines)
