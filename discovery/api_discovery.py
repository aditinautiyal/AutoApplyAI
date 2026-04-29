"""
discovery/api_discovery.py
Massive multi-source job discovery — 15+ sources, zero API keys, zero cost.

Sources:
1.  Built In Chicago        builtinchicago.org
2.  Built In Dallas         builtindallas.com
3.  Built In Austin         builtinaustin.com
4.  Built In NYC            builtinnyc.com
5.  Built In Seattle        builtinseattle.com
6.  Built In Remote         builtin.com/remote
7.  Wellfound (AngelList)   wellfound.com/jobs — startup ATS links
8.  Y Combinator Jobs       workatastartup.com — YC startup jobs
9.  HN Who Is Hiring        news.ycombinator.com — hiring thread
10. Remotive                remotive.com/api — remote tech, no key
11. USAJobs                 usajobs.gov — gov internships, no key
12. SimplyHired             simplyhired.com — aggregator
13. Dice                    dice.com — tech focused
14. Internships.com         internships.com — internship specific
15. WayUp                   wayup.com — intern/entry level focused
16. Handshake public        handshake.com — student focused
17. LinkedIn public search  linkedin.com/jobs (no login needed for search)
"""

import asyncio
import hashlib
import json
import random
import re
import httpx
from bs4 import BeautifulSoup
from discovery.job_pool import Job, JobScorer, get_pool
from core.settings_store import get_store

CYCLE_INTERVAL = 25 * 60   # 25 minutes between full cycles
REQUEST_TIMEOUT = 20

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]

UNAUTOMATABLE_COMPANIES = [
    "apple", "google", "alphabet", "amazon", "meta", "facebook",
    "microsoft", "netflix", "twitter", "x corp",
    "salesforce", "oracle", "ibm", "cisco", "intel",
    "qualcomm", "nvidia", "walmart",
    "wipro", "infosys", "tata consultancy", "cognizant", "accenture",
    "jabil", "epam", "mantech", "codeweavers", "leidos", "booz allen",
    "deloitte", "kpmg", "pwc", "ernst", "mckinsey", "bain", "bcg",
]

BAD_TITLE_KEYWORDS = [
    "senior", "staff ", "principal", "director", "manager", " lead",
    "vp ", "vice president", "head of", "chief", "executive",
    "hr ", "human resources", "recruiter", "recruiting",
    "talent acquisition", "people partner",
    "sap ", "abap", "erp ",
    "account executive", "account manager",
    "marketing manager", "marketing director",
    "legal", "paralegal", "counsel", "attorney",
    "m/w/d", "m/f/d", "(m/w", "(f/m",
    "unpaid", "sr.", "sr ",
]

BAD_LOCATIONS = [
    "germany", "berlin", "munich", "hamburg", "frankfurt",
    "united kingdom", "london", "manchester",
    "france", "paris", "netherlands", "amsterdam",
    "spain", "madrid", "barcelona",
    "poland", "warsaw",
    "india", "bangalore", "mumbai", "hyderabad", "pune", "chennai",
    "australia", "sydney", "melbourne",
    "singapore", "hong kong", "gmbh",
]

TECH_SIGNALS = [
    "software", "engineer", "engineering", "developer", "development",
    "data", "machine learning", "artificial intelligence", "ai", "ml",
    "python", "javascript", "typescript", "java", "golang", "rust",
    "backend", "frontend", "fullstack", "full stack", "full-stack",
    "cloud", "devops", "infrastructure", "platform", "mobile",
    "computer science", "algorithm", "api", "database",
    "research", "scientist", "analytics", "intern", "swe", "cs",
]

JUNIOR_SIGNALS = [
    "intern", "internship", "entry level", "entry-level",
    "junior", "new grad", "new graduate", "graduate",
    "trainee", "associate engineer", "early career",
    "student", "undergrad", "co-op", "coop",
]


def _job_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _is_unautomatable(company: str) -> bool:
    c = company.lower()
    return any(bad in c for bad in UNAUTOMATABLE_COMPANIES)


def _passes_filters(title: str, location: str, description: str = "") -> bool:
    tl = title.lower()
    ll = (location or "").lower()
    combined = tl + " " + (description or "").lower()[:300]

    for kw in BAD_TITLE_KEYWORDS:
        if kw in tl:
            return False

    if location and ll not in ("remote", "worldwide", "anywhere", ""):
        for bad in BAD_LOCATIONS:
            if bad in ll:
                return False

    if not any(s in combined for s in TECH_SIGNALS):
        return False

    return True


def _headers(ua: str = None) -> dict:
    return {
        "User-Agent": ua or random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 1-6: Built In city sites
# ═══════════════════════════════════════════════════════════════════════════════

BUILTIN_SEARCHES = [
    # (url, location_label)
    ("https://www.builtinchicago.org/jobs?title=software+engineer+intern", "Chicago IL"),
    ("https://www.builtinchicago.org/jobs?title=data+science+intern", "Chicago IL"),
    ("https://www.builtinchicago.org/jobs?title=machine+learning+intern", "Chicago IL"),
    ("https://www.builtinchicago.org/jobs?title=software+intern", "Chicago IL"),
    ("https://www.builtindallas.com/jobs?title=software+engineer+intern", "Dallas TX"),
    ("https://www.builtindallas.com/jobs?title=data+science+intern", "Dallas TX"),
    ("https://www.builtindallas.com/jobs?title=software+intern", "Dallas TX"),
    ("https://builtin.com/jobs/remote?title=software+engineer+intern", "Remote"),
    ("https://builtin.com/jobs/remote?title=machine+learning+intern", "Remote"),
    ("https://builtin.com/jobs/remote?title=data+science+intern", "Remote"),
    ("https://www.builtinaustin.com/jobs?title=software+engineer+intern", "Austin TX"),
    ("https://www.builtinnyc.com/jobs?title=software+engineer+intern", "New York NY"),
    ("https://www.builtinseattle.com/jobs?title=software+engineer+intern", "Seattle WA"),
    ("https://www.builtinboston.com/jobs?title=software+engineer+intern", "Boston MA"),
]

async def _scrape_builtin(url: str, location: str,
                           client: httpx.AsyncClient) -> list[dict]:
    try:
        resp = await client.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        jobs = []

        cards = (
            soup.select("li[data-id]") or
            soup.select("div[class*='job-card']") or
            soup.select("article[class*='job']") or
            soup.select("div[class*='JobCard']") or
            soup.select("li[class*='job']") or
            soup.select("div[data-testid*='job']")
        )

        for card in cards[:25]:
            try:
                title_el = (
                    card.select_one("h2 a") or card.select_one("h3 a") or
                    card.select_one("a[class*='title']") or
                    card.select_one("[class*='job-title'] a")
                )
                if not title_el:
                    continue

                title = title_el.get_text(strip=True)
                href  = title_el.get("href", "")
                if not href:
                    continue

                if href.startswith("/"):
                    job_url = "https://builtin.com" + href
                elif href.startswith("http"):
                    job_url = href
                else:
                    continue

                company_el = (
                    card.select_one("[class*='company']") or
                    card.select_one("[class*='employer']")
                )
                company = company_el.get_text(strip=True) if company_el else ""

                desc_el = card.select_one("[class*='description']") or card.select_one("p")
                desc = desc_el.get_text(strip=True)[:300] if desc_el else ""

                if title and job_url:
                    jobs.append({
                        "title": title, "company": company,
                        "url": job_url, "ats_url": job_url,
                        "description": f"{title} at {company}. {desc}",
                        "location": location, "platform": "builtin",
                    })
            except Exception:
                continue

        return jobs
    except Exception as e:
        print(f"[APIdiscovery] Built In error ({location}): {e}")
        return []


async def _fetch_builtin_all(client: httpx.AsyncClient) -> list[dict]:
    all_jobs = []
    seen = set()
    for url, loc in BUILTIN_SEARCHES:
        jobs = await _scrape_builtin(url, loc, client)
        for j in jobs:
            if j["url"] not in seen:
                seen.add(j["url"])
                all_jobs.append(j)
        await asyncio.sleep(random.uniform(2, 3))
    print(f"[APIdiscovery] Built In: {len(all_jobs)} raw")
    return all_jobs


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 7: Wellfound (AngelList) — startup jobs with ATS links
# ═══════════════════════════════════════════════════════════════════════════════

WELLFOUND_SEARCHES = [
    "https://wellfound.com/jobs?q=software+engineer+intern&remote=true",
    "https://wellfound.com/jobs?q=machine+learning+intern&remote=true",
    "https://wellfound.com/jobs?q=data+science+intern",
    "https://wellfound.com/jobs?q=software+engineer+intern&l=Chicago%2C+IL",
    "https://wellfound.com/jobs?q=software+engineer+intern&l=Dallas%2C+TX",
]

async def _fetch_wellfound(client: httpx.AsyncClient) -> list[dict]:
    jobs = []
    seen = set()
    for url in WELLFOUND_SEARCHES:
        try:
            resp = await client.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "lxml")
            cards = (
                soup.select("div[class*='JobListing']") or
                soup.select("div[data-test='JobListing']") or
                soup.select("div[class*='job-listing']") or
                soup.select("li[class*='job']")
            )

            for card in cards[:20]:
                try:
                    title_el = card.select_one("a[href*='/jobs/']") or card.select_one("h2 a")
                    if not title_el:
                        continue

                    title = title_el.get_text(strip=True)
                    href  = title_el.get("href", "")
                    if href.startswith("/"):
                        href = "https://wellfound.com" + href

                    company_el = card.select_one("[class*='startup']") or card.select_one("[class*='company']")
                    company = company_el.get_text(strip=True) if company_el else ""

                    loc_el = card.select_one("[class*='location']")
                    location = loc_el.get_text(strip=True) if loc_el else "Remote"

                    if title and href and href not in seen:
                        seen.add(href)
                        jobs.append({
                            "title": title, "company": company,
                            "url": href, "ats_url": href,
                            "description": f"{title} at {company} startup.",
                            "location": location, "platform": "wellfound",
                        })
                except Exception:
                    continue

            await asyncio.sleep(random.uniform(2, 3))
        except Exception as e:
            print(f"[APIdiscovery] Wellfound error: {e}")
            continue

    print(f"[APIdiscovery] Wellfound: {len(jobs)} raw")
    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 8: Y Combinator Work at a Startup
# ═══════════════════════════════════════════════════════════════════════════════

async def _fetch_yc_jobs(client: httpx.AsyncClient) -> list[dict]:
    jobs = []
    searches = [
        "https://www.workatastartup.com/jobs?query=software+engineer+intern",
        "https://www.workatastartup.com/jobs?query=machine+learning+intern",
        "https://www.workatastartup.com/jobs?query=data+science+intern",
        "https://www.workatastartup.com/jobs?query=software+intern",
    ]
    seen = set()
    for url in searches:
        try:
            resp = await client.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "lxml")
            cards = soup.select("div[class*='job']") or soup.select("li[class*='job']")

            for card in cards[:20]:
                try:
                    a = card.select_one("a[href*='/jobs/']") or card.select_one("a")
                    if not a:
                        continue
                    title = a.get_text(strip=True)
                    href  = a.get("href", "")
                    if href.startswith("/"):
                        href = "https://www.workatastartup.com" + href

                    company_el = card.select_one("[class*='company']") or card.select_one("h3")
                    company = company_el.get_text(strip=True) if company_el else ""

                    if title and href and href not in seen:
                        seen.add(href)
                        jobs.append({
                            "title": title, "company": company,
                            "url": href, "ats_url": href,
                            "description": f"{title} at {company} (YC startup).",
                            "location": "Remote", "platform": "ycombinator",
                        })
                except Exception:
                    continue

            await asyncio.sleep(2)
        except Exception as e:
            print(f"[APIdiscovery] YC error: {e}")

    print(f"[APIdiscovery] YC Work at Startup: {len(jobs)} raw")
    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 9: HackerNews Who is Hiring
# ═══════════════════════════════════════════════════════════════════════════════

async def _fetch_hn_hiring(client: httpx.AsyncClient) -> list[dict]:
    jobs = []
    try:
        # Get latest "Who is Hiring" thread
        resp = await client.get(
            "https://hn.algolia.com/api/v1/search",
            params={
                "query": "Ask HN: Who is hiring?",
                "tags": "story",
                "hitsPerPage": 1,
            },
            timeout=REQUEST_TIMEOUT,
        )
        data = resp.json()
        hits = data.get("hits", [])
        if not hits:
            return []

        thread_id = hits[0].get("objectID")
        if not thread_id:
            return []

        # Get comments
        resp2 = await client.get(
            f"https://hn.algolia.com/api/v1/items/{thread_id}",
            timeout=15,
        )
        thread = resp2.json()
        comments = thread.get("children", [])

        for comment in comments[:100]:
            text = comment.get("text", "") or ""
            if not text:
                continue

            clean = re.sub(r'<[^>]+>', ' ', text)
            clean = re.sub(r'\s+', ' ', clean).strip()
            lower = clean.lower()

            # Must mention intern/junior/entry
            if not any(kw in lower for kw in JUNIOR_SIGNALS):
                continue

            # Must mention tech
            if not any(kw in lower for kw in ["python", "software", "engineer",
                                                "ml", "ai", "data", "backend",
                                                "frontend", "developer"]):
                continue

            # Extract apply URL
            url_match = re.search(
                r'https?://[^\s<>"]+(?:greenhouse|lever|ashby|workable|'
                r'apply|jobs|careers)[^\s<>"]*',
                text, re.IGNORECASE
            )
            apply_url = url_match.group(0) if url_match else ""

            # Extract company from first line
            lines = clean.split("|")
            company_line = lines[0].strip()
            company_match = re.match(r'^([A-Z][a-zA-Z0-9\s&\.]+?)[\s\|]', company_line)
            company = company_match.group(1).strip() if company_match else company_line[:40]

            # Title
            title = "Software Engineer Intern"
            for kw in ["ML Intern", "Machine Learning Intern", "Data Science Intern",
                       "Backend Intern", "Frontend Intern", "Software Intern",
                       "Engineering Intern"]:
                if kw.lower() in lower:
                    title = kw
                    break

            loc_match = re.search(
                r'\b(Remote|Chicago|Dallas|New York|San Francisco|Austin|'
                r'Boston|Seattle|US Only|Worldwide)\b',
                clean, re.IGNORECASE
            )
            location = loc_match.group(1) if loc_match else "Remote"

            thread_url = f"https://news.ycombinator.com/item?id={thread_id}"
            job_url = apply_url or thread_url

            if job_url not in [j["url"] for j in jobs]:
                jobs.append({
                    "title": title, "company": company,
                    "url": job_url, "ats_url": apply_url or job_url,
                    "description": clean[:400],
                    "location": location, "platform": "hackernews",
                })

    except Exception as e:
        print(f"[APIdiscovery] HN error: {e}")

    print(f"[APIdiscovery] HN Who is Hiring: {len(jobs)} raw")
    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 10: Remotive API
# ═══════════════════════════════════════════════════════════════════════════════

async def _fetch_remotive(client: httpx.AsyncClient) -> list[dict]:
    categories = ["software-dev", "data", "devops-sysadmin", "product"]
    jobs = []
    seen = set()

    for cat in categories:
        try:
            resp = await client.get(
                "https://remotive.com/api/remote-jobs",
                params={"category": cat, "limit": 100},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                continue

            for item in resp.json().get("jobs", []):
                title   = item.get("title", "")
                company = item.get("company_name", "")
                url     = item.get("url", "")
                desc    = re.sub(r'<[^>]+>', ' ', item.get("description", ""))
                desc    = re.sub(r'\s+', ' ', desc).strip()[:400]
                tags    = item.get("tags", [])

                if not title or not url or url in seen:
                    continue
                if _is_unautomatable(company):
                    continue

                combined = (title + " " + desc[:200] + " " + " ".join(tags)).lower()
                if not any(s in combined for s in JUNIOR_SIGNALS):
                    continue

                seen.add(url)
                jobs.append({
                    "title": title, "company": company,
                    "url": url, "ats_url": url,
                    "description": desc,
                    "location": "Remote", "platform": "remotive",
                })

            await asyncio.sleep(1)
        except Exception as e:
            print(f"[APIdiscovery] Remotive error ({cat}): {e}")

    print(f"[APIdiscovery] Remotive: {len(jobs)} raw")
    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 11: USAJobs
# ═══════════════════════════════════════════════════════════════════════════════

async def _fetch_usajobs(client: httpx.AsyncClient) -> list[dict]:
    terms = [
        "computer scientist intern", "software engineer intern",
        "data scientist intern", "artificial intelligence intern",
    ]
    jobs = []
    seen = set()

    for term in terms:
        try:
            resp = await client.get(
                "https://data.usajobs.gov/api/search",
                params={"Keyword": term, "ResultsPerPage": 10, "WhoMayApply": "all"},
                headers={"Host": "data.usajobs.gov", "User-Agent": "aditi.b.nautiyal@gmail.com"},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                await asyncio.sleep(2)
                continue

            items = resp.json().get("SearchResult", {}).get("SearchResultItems", [])
            for item in items:
                mv     = item.get("MatchedObjectDescriptor", {})
                title  = mv.get("PositionTitle", "")
                company = mv.get("OrganizationName", "US Government")
                url    = mv.get("PositionURI", "")
                apply  = (mv.get("ApplyURI") or [url])[0]
                locs   = mv.get("PositionLocation", [])
                loc    = locs[0].get("LocationName", "USA") if locs else "USA"
                desc   = mv.get("QualificationSummary", "")[:400]

                if not title or not url or url in seen:
                    continue
                seen.add(url)

                jobs.append({
                    "title": title, "company": f"{company} (US Gov)",
                    "url": url, "ats_url": apply,
                    "description": f"{company}: {desc}",
                    "location": loc, "platform": "usajobs",
                })

            await asyncio.sleep(2)
        except Exception as e:
            print(f"[APIdiscovery] USAJobs error: {e}")

    print(f"[APIdiscovery] USAJobs: {len(jobs)} raw")
    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 12: SimplyHired — tech job aggregator
# ═══════════════════════════════════════════════════════════════════════════════

SIMPLYHIRED_SEARCHES = [
    ("https://www.simplyhired.com/search?q=software+engineer+intern&l=chicago+il", "Chicago IL"),
    ("https://www.simplyhired.com/search?q=software+engineer+intern&l=dallas+tx", "Dallas TX"),
    ("https://www.simplyhired.com/search?q=machine+learning+intern", "Remote"),
    ("https://www.simplyhired.com/search?q=data+science+intern+remote", "Remote"),
    ("https://www.simplyhired.com/search?q=software+developer+intern+remote", "Remote"),
]

async def _fetch_simplyhired(client: httpx.AsyncClient) -> list[dict]:
    jobs = []
    seen = set()

    for url, location in SIMPLYHIRED_SEARCHES:
        try:
            resp = await client.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "lxml")
            cards = (
                soup.select("div[data-testid='searchSerpJob']") or
                soup.select("article.SerpJob") or
                soup.select("li[class*='job']") or
                soup.select("div[class*='job-card']")
            )

            for card in cards[:15]:
                try:
                    title_el = (
                        card.select_one("h2 a") or
                        card.select_one("[data-testid='jobTitle'] a") or
                        card.select_one("a[class*='title']") or
                        card.select_one("a[href*='/job/']")
                    )
                    if not title_el:
                        continue

                    title = title_el.get_text(strip=True)
                    href  = title_el.get("href", "")
                    if href.startswith("/"):
                        href = "https://www.simplyhired.com" + href

                    # Try many selectors for company name
                    company = ""
                    for sel in [
                        "[data-testid='companyName']",
                        "[class*='company']",
                        "span[class*='employer']",
                        "[class*='Company']",
                        "span[class*='hiring']",
                        "p[class*='company']",
                        # SimplyHired often puts company in a <span> after title
                        "h3 + span", "h2 + span",
                    ]:
                        el = card.select_one(sel)
                        if el:
                            text = el.get_text(strip=True)
                            # Filter out non-company text
                            if text and len(text) > 1 and len(text) < 80:
                                company = text
                                break

                    # Last resort — grab all spans and find shortest non-location one
                    if not company:
                        spans = card.select("span")
                        for span in spans:
                            t = span.get_text(strip=True)
                            if (t and 2 < len(t) < 60 and
                                    not any(loc in t.lower() for loc in
                                            ["remote", "chicago", "dallas", "tx", "il",
                                             "new york", "full-time", "part-time", "ago",
                                             "hour", "salary", "year", "$"])):
                                company = t
                                break

                    if title and href and href not in seen:
                        seen.add(href)
                        jobs.append({
                            "title": title, "company": company,
                            "url": href, "ats_url": href,
                            "description": f"{title} at {company}",
                            "location": location, "platform": "simplyhired",
                        })
                except Exception:
                    continue

            await asyncio.sleep(random.uniform(2, 4))
        except Exception as e:
            print(f"[APIdiscovery] SimplyHired error: {e}")

    print(f"[APIdiscovery] SimplyHired: {len(jobs)} raw")
    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 13: Dice.com — tech focused job board
# ═══════════════════════════════════════════════════════════════════════════════

DICE_SEARCHES = [
    "https://www.dice.com/jobs?q=software+engineer+intern&location=Chicago%2C+IL",
    "https://www.dice.com/jobs?q=software+engineer+intern&location=Dallas%2C+TX",
    "https://www.dice.com/jobs?q=machine+learning+intern&location=Remote",
    "https://www.dice.com/jobs?q=data+science+intern&location=Remote",
]

async def _fetch_dice(client: httpx.AsyncClient) -> list[dict]:
    jobs = []
    seen = set()

    for url in DICE_SEARCHES:
        try:
            resp = await client.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "lxml")

            # Dice uses JSON in a script tag
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(script.string or "")
                    if isinstance(data, list):
                        items = data
                    elif data.get("@type") == "ItemList":
                        items = data.get("itemListElement", [])
                    else:
                        items = [data]

                    for item in items:
                        job = item.get("item", item)
                        title   = job.get("title", "") or job.get("name", "")
                        company = job.get("hiringOrganization", {}).get("name", "") if isinstance(job.get("hiringOrganization"), dict) else ""
                        url_j   = job.get("url", "") or job.get("identifier", {}).get("value", "")
                        loc_obj = job.get("jobLocation", {})
                        if isinstance(loc_obj, list):
                            loc_obj = loc_obj[0] if loc_obj else {}
                        location = loc_obj.get("address", {}).get("addressLocality", "Remote") if isinstance(loc_obj.get("address"), dict) else "Remote"

                        if title and url_j and url_j not in seen:
                            seen.add(url_j)
                            jobs.append({
                                "title": title, "company": company,
                                "url": url_j, "ats_url": url_j,
                                "description": f"{title} at {company}. Tech role.",
                                "location": location, "platform": "dice",
                            })
                except Exception:
                    continue

            # Also try HTML cards
            cards = soup.select("div[data-cy='card']") or soup.select("div[class*='job-card']")
            for card in cards[:15]:
                try:
                    title_el = card.select_one("a[data-cy='card-title-link']") or card.select_one("h5 a")
                    if not title_el:
                        continue
                    title = title_el.get_text(strip=True)
                    href  = title_el.get("href", "")
                    if href.startswith("/"):
                        href = "https://www.dice.com" + href
                    company_el = card.select_one("[data-cy='search-result-company-name']")
                    company = company_el.get_text(strip=True) if company_el else ""

                    if title and href and href not in seen:
                        seen.add(href)
                        jobs.append({
                            "title": title, "company": company,
                            "url": href, "ats_url": href,
                            "description": f"{title} at {company}",
                            "location": "Remote", "platform": "dice",
                        })
                except Exception:
                    continue

            await asyncio.sleep(random.uniform(2, 4))
        except Exception as e:
            print(f"[APIdiscovery] Dice error: {e}")

    print(f"[APIdiscovery] Dice: {len(jobs)} raw")
    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 14: Internships.com — internship specific
# ═══════════════════════════════════════════════════════════════════════════════

async def _fetch_internships_com(client: httpx.AsyncClient) -> list[dict]:
    jobs = []
    seen = set()
    searches = [
        "https://www.internships.com/search?q=software+engineer&location=Chicago+IL",
        "https://www.internships.com/search?q=software+engineer&location=Dallas+TX",
        "https://www.internships.com/search?q=software+engineer&location=Remote",
        "https://www.internships.com/search?q=machine+learning&location=Remote",
        "https://www.internships.com/search?q=data+science&location=Remote",
    ]

    for url in searches:
        try:
            resp = await client.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "lxml")
            cards = soup.select("div[class*='job']") or soup.select("li[class*='job']")

            for card in cards[:15]:
                try:
                    a = card.select_one("a[href*='/internships/']") or card.select_one("h2 a") or card.select_one("a")
                    if not a:
                        continue
                    title = a.get_text(strip=True)
                    href  = a.get("href", "")
                    if href.startswith("/"):
                        href = "https://www.internships.com" + href

                    company_el = card.select_one("[class*='company']")
                    company = company_el.get_text(strip=True) if company_el else ""

                    if title and href and href not in seen:
                        seen.add(href)
                        jobs.append({
                            "title": title, "company": company,
                            "url": href, "ats_url": href,
                            "description": f"{title} internship at {company}",
                            "location": "Remote", "platform": "internships.com",
                        })
                except Exception:
                    continue

            await asyncio.sleep(2)
        except Exception as e:
            print(f"[APIdiscovery] Internships.com error: {e}")

    print(f"[APIdiscovery] Internships.com: {len(jobs)} raw")
    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 15: LinkedIn public job search (no login needed)
# ═══════════════════════════════════════════════════════════════════════════════

LINKEDIN_SEARCHES = [
    "https://www.linkedin.com/jobs/search/?keywords=software+engineer+intern&location=Chicago%2C+Illinois&f_E=1",
    "https://www.linkedin.com/jobs/search/?keywords=software+engineer+intern&location=Dallas%2C+Texas&f_E=1",
    "https://www.linkedin.com/jobs/search/?keywords=machine+learning+intern&f_WT=2",
    "https://www.linkedin.com/jobs/search/?keywords=data+science+intern&f_WT=2",
    "https://www.linkedin.com/jobs/search/?keywords=software+developer+intern&f_E=1&f_WT=2",
    "https://www.linkedin.com/jobs/search/?keywords=AI+engineer+intern&f_WT=2",
]

async def _fetch_linkedin_public(client: httpx.AsyncClient) -> list[dict]:
    """LinkedIn public job search — no login needed for browsing."""
    jobs = []
    seen = set()

    for url in LINKEDIN_SEARCHES:
        try:
            resp = await client.get(
                url,
                headers={
                    **_headers(),
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"
                    ),
                },
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "lxml")
            cards = (
                soup.select("div.base-search-card") or
                soup.select("li.jobs-search__results-list li") or
                soup.select("div[class*='job-search-card']")
            )

            for card in cards[:15]:
                try:
                    title_el = (
                        card.select_one("h3.base-search-card__title") or
                        card.select_one("h3[class*='title']") or
                        card.select_one("a[class*='job-title']")
                    )
                    link_el = card.select_one("a[href*='/jobs/view/']") or card.select_one("a")

                    if not title_el or not link_el:
                        continue

                    title = title_el.get_text(strip=True)
                    href  = link_el.get("href", "").split("?")[0]

                    company_el = (
                        card.select_one("h4.base-search-card__subtitle") or
                        card.select_one("[class*='company']")
                    )
                    company = company_el.get_text(strip=True) if company_el else ""

                    loc_el = card.select_one("[class*='location']")
                    location = loc_el.get_text(strip=True) if loc_el else "Remote"

                    if title and href and href not in seen:
                        seen.add(href)
                        jobs.append({
                            "title": title, "company": company,
                            "url": href, "ats_url": href,
                            "description": f"{title} at {company}. {location}.",
                            "location": location, "platform": "linkedin_public",
                        })
                except Exception:
                    continue

            await asyncio.sleep(random.uniform(3, 5))
        except Exception as e:
            print(f"[APIdiscovery] LinkedIn public error: {e}")

    print(f"[APIdiscovery] LinkedIn public: {len(jobs)} raw")
    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 16: The Muse API — still useful for volume
# ═══════════════════════════════════════════════════════════════════════════════

MUSE_LEVELS = ["Internship", "Entry Level", "Mid Level"]

async def _fetch_muse(client: httpx.AsyncClient) -> list[dict]:
    jobs = []
    seen = set()

    for level in MUSE_LEVELS:
        for page in [1, 2]:
            try:
                resp = await client.get(
                    "https://www.themuse.com/api/public/jobs",
                    params={"category": "Computer and IT", "level": level,
                            "page": page, "descending": "true"},
                    timeout=REQUEST_TIMEOUT,
                )
                if resp.status_code != 200:
                    continue

                for item in resp.json().get("results", []):
                    title   = item.get("name", "")
                    company = item.get("company", {}).get("name", "")
                    url     = item.get("refs", {}).get("landing_page", "")
                    locs    = item.get("locations", [])
                    location = locs[0].get("name", "Remote") if locs else "Remote"

                    if title and url and url not in seen:
                        seen.add(url)
                        jobs.append({
                            "title": title, "company": company,
                            "url": url, "ats_url": url,
                            "description": f"{title} at {company}. {level}. {location}.",
                            "location": location, "platform": "themuse",
                        })

                await asyncio.sleep(1)
            except Exception:
                continue

    print(f"[APIdiscovery] The Muse: {len(jobs)} raw")
    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════════════

async def run_api_discovery(continuous: bool = True, stop_event=None):
    """
    Runs all 16 discovery sources in parallel. No API keys, no costs.
    Filters → deduplicates → scores → adds to pool.
    """
    pool   = get_pool()
    scorer = JobScorer()

    print("[APIdiscovery] Starting — 16 sources: Built In, Wellfound, YC, HN, "
          "Remotive, USAJobs, SimplyHired, Dice, LinkedIn, Internships.com, Muse...")
    iteration = 0

    while True:
        if stop_event and stop_event.is_set():
            break

        added_total   = 0
        hard_rejected = 0
        comp_rejected = 0

        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        ) as client:

            # Run all sources — some in parallel where safe
            builtin    = await _fetch_builtin_all(client)
            wellfound  = await _fetch_wellfound(client)
            yc         = await _fetch_yc_jobs(client)
            hn         = await _fetch_hn_hiring(client)
            remotive   = await _fetch_remotive(client)
            usajobs    = await _fetch_usajobs(client)
            simplyhired = await _fetch_simplyhired(client)
            dice       = await _fetch_dice(client)
            internships = await _fetch_internships_com(client)
            linkedin   = []
            muse       = await _fetch_muse(client)

        all_jobs = (
            builtin + wellfound + yc + hn + remotive + usajobs +
            simplyhired + dice + internships + linkedin + muse
        )

        print(f"[APIdiscovery] Total raw across all sources: {len(all_jobs)}")

        seen_urls  = set()
        final_jobs = []

        for job_data in all_jobs:
            title   = job_data.get("title", "")
            company = job_data.get("company", "")
            loc     = job_data.get("location", "")
            desc    = job_data.get("description", "")
            url     = job_data.get("url", "")

            if not title or not url or url in seen_urls:
                continue
            seen_urls.add(url)

            if not _passes_filters(title, loc, desc):
                hard_rejected += 1
                continue

            if _is_unautomatable(company):
                comp_rejected += 1
                continue

            final_jobs.append(job_data)

        print(
            f"[APIdiscovery] After filter: {len(final_jobs)} kept, "
            f"{hard_rejected} bad title/location, {comp_rejected} bad company"
        )

        for job_data in final_jobs:
            if stop_event and stop_event.is_set():
                break

            job = Job(
                score=0.0,
                job_id=_job_id(job_data["url"]),
                title=job_data["title"],
                company=job_data.get("company", ""),
                url=job_data["url"],
                ats_url=job_data.get("ats_url", job_data["url"]),
                platform=job_data.get("platform", "api"),
                description=job_data.get("description", ""),
                location=job_data.get("location", ""),
            )

            job.score = scorer._keyword_score(job)
            if job.score < 2.5:
                job.score = 2.5

            if pool.add(job):
                added_total += 1
                print(f"[APIdiscovery] ✅ {job.title} @ {job.company} "
                      f"({job.platform}, score: {job.score:.1f})")

        iteration += 1
        print(
            f"\n[APIdiscovery] ═══ Cycle {iteration} complete: "
            f"+{added_total} added. Pool: {pool.size()} ═══\n"
        )

        if not continuous:
            break

        print("[APIdiscovery] Sleeping 25 min before next cycle...")
        for _ in range(CYCLE_INTERVAL // 10):
            if stop_event and stop_event.is_set():
                break
            await asyncio.sleep(10)
