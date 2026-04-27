"""
extra_effort/people_finder.py
Finds people at target companies who could help boost an application.
Searches LinkedIn, Reddit, GitHub for active, relevant contacts.
Flags: mutual connection, same school, same employer, active recruiter, high engagement.
Auto-sends on LinkedIn/Reddit if OAuth connected. Draft only otherwise.
"""

import asyncio
import json
import random
import re
import httpx
from bs4 import BeautifulSoup
from core.settings_store import get_store
from core.api_router import get_router

# Flag types with labels
FLAGS = {
    "mutual":       ("🔴", "Mutual Connection"),
    "same_school":  ("🟠", "Same School/University"),
    "same_employer":("🟡", "Shared Past Employer"),
    "distant":      ("🟢", "Distant Connection"),
    "recruiter":    ("🔵", "Active Recruiter"),
    "high_impact":  ("⭐", "High Impact (multiple flags)"),
    "high_engage":  ("💬", "Highly Active/Engaged"),
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
]


async def find_contacts_for_application(
    company: str,
    job_title: str,
    application_id: int,
    insight: dict,
) -> list[dict]:
    """
    Main entry point. Finds and scores relevant contacts for one application.
    Returns list of contact dicts, saved to DB automatically.
    """
    store = get_store()
    profile = store.get_profile() or {}

    contacts = []

    # Search across platforms in parallel
    linkedin_contacts, reddit_contacts, github_contacts = await asyncio.gather(
        _search_linkedin_public(company, job_title, profile),
        _search_reddit(company, profile),
        _search_github(company, profile),
        return_exceptions=True
    )

    for result in [linkedin_contacts, reddit_contacts, github_contacts]:
        if isinstance(result, list):
            contacts.extend(result)

    if not contacts:
        return []

    # Score and flag contacts
    scored = []
    for contact in contacts:
        flags = _assign_flags(contact, profile)
        priority = _calculate_priority(flags)

        if priority < 0.2:  # Skip low-value contacts
            continue

        contact_data = {
            "application_id": application_id,
            "person_name":    contact.get("name", ""),
            "person_title":   contact.get("title", ""),
            "company_name":   company,
            "platform":       contact.get("platform", ""),
            "profile_url":    contact.get("url", ""),
            "contact_handle": contact.get("handle", ""),
            "flags":          json.dumps([f[0] for f in flags]),
            "flag_labels":    json.dumps([f[1] for f in flags]),
            "draft_message":  "",  # Generated after scoring
            "priority_score": priority,
        }

        # Generate draft message
        contact_data["draft_message"] = _generate_message(
            contact, company, job_title, flags, insight, profile
        )

        # Save to DB
        store.save_contact(contact_data)
        scored.append(contact_data)

    # Sort by priority
    scored.sort(key=lambda x: x["priority_score"], reverse=True)

    # Auto-send on LinkedIn/Reddit if connected and high priority
    await _auto_send_where_possible(scored[:3], store)

    print(f"[PeopleFinder] Found {len(scored)} contacts for {company}")
    return scored


async def _search_linkedin_public(company: str, job_title: str,
                                   profile: dict) -> list[dict]:
    """Search LinkedIn public profiles for employees at target company."""
    contacts = []
    keywords = [
        f'"{company}" engineer intern recruiter site:linkedin.com/in',
        f'"{company}" talent acquisition university recruiting site:linkedin.com',
        f'"{company}" software engineer AI ML site:linkedin.com/in',
    ]

    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        for query in keywords[:2]:
            try:
                resp = await client.get(
                    "https://www.google.com/search",
                    params={"q": query, "num": 6},
                    headers={"User-Agent": random.choice(USER_AGENTS)},
                )
                soup = BeautifulSoup(resp.text, "lxml")
                for g in soup.select("div.g"):
                    link = g.select_one("a")
                    title_el = g.select_one("h3")
                    if not link or not title_el:
                        continue
                    url = link.get("href", "")
                    if "linkedin.com/in/" not in url:
                        continue
                    title_text = title_el.get_text(strip=True)
                    name = title_text.split(" - ")[0].split(" | ")[0].strip()
                    role = title_text.split(" - ")[1] if " - " in title_text else ""
                    contacts.append({
                        "name": name,
                        "title": role,
                        "url": url,
                        "platform": "linkedin",
                        "handle": url.split("/in/")[-1].split("/")[0] if "/in/" in url else "",
                        "active_signals": _check_recruiter_signals(title_text),
                    })
                await asyncio.sleep(random.uniform(1, 2.5))
            except Exception:
                continue

    return contacts[:5]


async def _search_reddit(company: str, profile: dict) -> list[dict]:
    """Find Reddit users who have posted about working at the company."""
    contacts = []
    queries = [
        f'site:reddit.com "I work at {company}" OR "I worked at {company}"',
        f'site:reddit.com "{company}" AMA OR "ask me anything"',
        f'site:reddit.com "{company}" employee engineer',
    ]

    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        for query in queries[:2]:
            try:
                resp = await client.get(
                    "https://www.google.com/search",
                    params={"q": query, "num": 5},
                    headers={"User-Agent": random.choice(USER_AGENTS)},
                )
                soup = BeautifulSoup(resp.text, "lxml")
                for g in soup.select("div.g"):
                    link = g.select_one("a")
                    snippet = g.select_one("div.VwiC3b")
                    if not link:
                        continue
                    url = link.get("href", "")
                    if "reddit.com" not in url:
                        continue
                    snippet_text = snippet.get_text() if snippet else ""
                    # Extract username from Reddit URL or snippet
                    username = ""
                    if "/user/" in url:
                        username = url.split("/user/")[-1].split("/")[0]
                    elif "u/" in snippet_text:
                        m = re.search(r'u/(\w+)', snippet_text)
                        if m:
                            username = m.group(1)
                    if username:
                        contacts.append({
                            "name": f"u/{username}",
                            "title": f"{company} employee/alumni",
                            "url": f"https://reddit.com/user/{username}",
                            "platform": "reddit",
                            "handle": username,
                            "active_signals": True,
                        })
                await asyncio.sleep(random.uniform(1, 2))
            except Exception:
                continue

    return contacts[:3]


async def _search_github(company: str, profile: dict) -> list[dict]:
    """Find GitHub contributors from the company's org."""
    contacts = []
    company_slug = company.lower().replace(" ", "")
    urls_to_try = [
        f"https://github.com/{company_slug}",
        f"https://github.com/orgs/{company_slug}/people",
    ]

    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        for url in urls_to_try:
            try:
                resp = await client.get(
                    url,
                    headers={"User-Agent": random.choice(USER_AGENTS)},
                )
                soup = BeautifulSoup(resp.text, "lxml")
                # Find member links
                member_links = soup.select("a[href^='/'][href*='?']:not([href*='#'])")
                for link in member_links[:5]:
                    href = link.get("href", "")
                    if href.count("/") == 1 and href != f"/{company_slug}":
                        username = href.strip("/")
                        if username and len(username) > 1:
                            contacts.append({
                                "name": username,
                                "title": f"GitHub contributor @ {company}",
                                "url": f"https://github.com/{username}",
                                "platform": "github",
                                "handle": username,
                                "active_signals": False,
                            })
                if contacts:
                    break
                await asyncio.sleep(1)
            except Exception:
                continue

    return contacts[:4]


def _check_recruiter_signals(text: str) -> bool:
    """Check if a person's title/text suggests they're an active recruiter."""
    signals = ["recruiter", "talent", "hiring", "acquisition", "people ops",
               "hr", "university relations", "campus"]
    return any(s in text.lower() for s in signals)


def _assign_flags(contact: dict, profile: dict) -> list[tuple]:
    """Assign relevant flags to a contact based on profile overlap."""
    flags = []

    user_schools = _extract_schools(profile.get("background_text", ""))
    user_employers = _extract_employers(profile.get("background_text", ""))

    title_lower = (contact.get("title") or "").lower()
    name_lower = (contact.get("name") or "").lower()

    # Check school match
    for school in user_schools:
        if school.lower() in title_lower or school.lower() in name_lower:
            flags.append(("same_school", FLAGS["same_school"][1]))
            break

    # Check employer match
    for employer in user_employers:
        if employer.lower() in title_lower:
            flags.append(("same_employer", FLAGS["same_employer"][1]))
            break

    # Active recruiter
    if contact.get("active_signals") or _check_recruiter_signals(title_lower):
        flags.append(("recruiter", FLAGS["recruiter"][1]))

    # High engagement (Reddit AMA, lots of posts)
    if contact.get("platform") == "reddit" and contact.get("active_signals"):
        flags.append(("high_engage", FLAGS["high_engage"][1]))

    # Multi-flag = high impact
    if len(flags) >= 2:
        flags.append(("high_impact", FLAGS["high_impact"][1]))

    return flags


def _calculate_priority(flags: list) -> float:
    """Score a contact 0-1 based on flags."""
    weights = {
        "mutual": 0.4,
        "same_school": 0.35,
        "same_employer": 0.3,
        "distant": 0.2,
        "recruiter": 0.35,
        "high_impact": 0.25,
        "high_engage": 0.2,
    }
    score = 0.1  # base
    for flag_key, _ in flags:
        score += weights.get(flag_key, 0.1)
    return min(1.0, score)


def _generate_message(contact: dict, company: str, job_title: str,
                       flags: list, insight: dict, profile: dict) -> str:
    """Generate a tailored outreach message for this contact."""
    router = get_router()

    name = contact.get("name", "")
    platform = contact.get("platform", "linkedin")
    flag_labels = [f[1] for f in flags]

    user_name = profile.get("full_name", "")
    background_short = (profile.get("background_text") or "")[:200]
    portfolio = profile.get("portfolio_url", "")

    connection_note = ""
    if "Same School" in str(flag_labels):
        connection_note = "Mention the school connection naturally."
    elif "Shared Past Employer" in str(flag_labels):
        connection_note = "Reference the shared employer briefly."
    elif "Active Recruiter" in str(flag_labels):
        connection_note = "They recruit actively — be direct about your interest."

    prompt = f"""Write a short, professional outreach message from {user_name} to {name} at {company}.
Platform: {platform} (keep it platform-appropriate in length and tone)
Purpose: Express genuine interest in the {job_title} role and ask for advice/insight
User background: {background_short}
Portfolio: {portfolio}
Company tone: {insight.get('tone', 'professional')}
{connection_note}

Rules:
- Max 120 words
- Genuine and specific, not generic
- Professional but conversational for {platform}
- Do NOT ask them to refer you directly — just express interest and ask a thoughtful question
- No subject line needed — just the message body
- Start naturally, not with "I am reaching out because"

Message:"""

    try:
        msg = router.complete(prompt, smart=False, max_tokens=250).strip()
        return msg
    except Exception:
        return (
            f"Hi {name.split()[0] if name else 'there'}, I came across your profile while "
            f"researching {company} — I'm very interested in the {job_title} role there. "
            f"Would you be open to sharing any advice about the team or culture? "
            f"I'd really appreciate any insight. Thank you!"
        )


async def _auto_send_where_possible(contacts: list[dict], store):
    """Auto-send messages on LinkedIn and Reddit if OAuth tokens available."""
    linkedin_token = store.get("linkedin_token")
    reddit_token = store.get("reddit_token")

    for contact in contacts:
        platform = contact.get("platform")
        if platform == "linkedin" and linkedin_token:
            success = await _send_linkedin_message(contact, linkedin_token)
            if success:
                store.conn.execute(
                    "UPDATE extra_effort_contacts SET sent=1, sent_at=datetime('now') WHERE profile_url=?",
                    (contact.get("profile_url"),)
                )
                store.conn.commit()
        elif platform == "reddit" and reddit_token:
            success = await _send_reddit_dm(contact, reddit_token)
            if success:
                store.conn.execute(
                    "UPDATE extra_effort_contacts SET sent=1, sent_at=datetime('now') WHERE profile_url=?",
                    (contact.get("profile_url"),)
                )
                store.conn.commit()


async def _send_linkedin_message(contact: dict, token: str) -> bool:
    """Send LinkedIn InMail/connection request via API."""
    # LinkedIn messaging API requires member URN
    # For now logs intent — full implementation needs LinkedIn OAuth app approval
    print(f"[PeopleFinder] LinkedIn auto-message queued: {contact.get('name')}")
    return False  # Will be True once LinkedIn OAuth app is approved


async def _send_reddit_dm(contact: dict, token: str) -> bool:
    """Send Reddit DM via Reddit API."""
    try:
        username = contact.get("handle", "")
        message = contact.get("draft_message", "")
        if not username or not message:
            return False

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://oauth.reddit.com/api/compose",
                headers={
                    "Authorization": f"bearer {token}",
                    "User-Agent": "AutoApplyAI/1.0",
                },
                data={
                    "api_type": "json",
                    "to": username,
                    "subject": "Question about your experience",
                    "text": message,
                }
            )
            return resp.status_code == 200
    except Exception as e:
        print(f"[PeopleFinder] Reddit DM failed: {e}")
        return False


def _extract_schools(text: str) -> list[str]:
    """Extract school names from background text."""
    schools = ["purdue", "mit", "stanford", "harvard", "cmu", "georgia tech",
               "university", "college", "institute of technology"]
    found = []
    text_lower = text.lower()
    for school in schools:
        if school in text_lower:
            found.append(school)
    return found


def _extract_employers(text: str) -> list[str]:
    """Extract past employer names from background text."""
    # Look for "at [Company]" or "worked at [Company]" patterns
    matches = re.findall(r'(?:at|@|for)\s+([A-Z][a-zA-Z\s&]+?)(?:\s+as|\s+where|\.|,|\n)', text)
    return [m.strip() for m in matches if len(m.strip()) > 2][:5]
