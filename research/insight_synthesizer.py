"""
research/insight_synthesizer.py
Claude synthesizes raw research into a company personality profile
and specific application strategy. Saves to DB for reuse.
"""

import json
import re
from core.api_router import get_router
from core.settings_store import get_store


def synthesize(research_data: dict) -> dict:
    """
    Takes raw research dict from company_researcher.
    Returns structured insight dict used by cover_letter_gen and answer_gen.
    Also saves company profile to DB.
    """
    # If already synthesized from cache, return as-is
    if research_data.get("from_cache") and research_data.get("personality"):
        return research_data

    company = research_data["company"]
    job_title = research_data["job_title"]
    job_description = research_data.get("job_description", "")

    # Compile all raw text into one prompt-friendly block
    all_text_parts = []
    for snippet in research_data.get("snippets", [])[:30]:
        all_text_parts.append(f"- {snippet}")
    for full_text in research_data.get("full_texts", [])[:10]:
        all_text_parts.append(full_text[:1500])

    raw_combined = "\n\n".join(all_text_parts)

    if not raw_combined.strip():
        # No research found — return minimal insight
        return _minimal_insight(company, job_title, job_description)

    router = get_router()
    store = get_store()

    system = """You are an expert at analyzing company culture and creating 
    targeted job application strategy. Return ONLY valid JSON."""

    prompt = f"""Analyze all the research below about {company} and the {job_title} role.
Extract signals about what this company ACTUALLY values — including casual mentions,
complaints, praise, and off-hand comments. Look for recurring themes even if not
explicitly stated as "company values."

Return a JSON object with exactly these keys:
{{
  "personality": "2-3 sentence description of the company's actual personality and vibe",
  "core_values": ["value1", "value2", "value3", "value4"],
  "culture_signals": ["specific signal from research", "another signal", ...],
  "tone": "professional|casual|startup|academic|corporate|innovative",
  "keywords": ["keyword to emphasize in application", ...],
  "avoid": ["topics or tones to avoid", ...],
  "what_they_want": "What this specific role at this company really needs",
  "talking_points": ["specific angle to mention in cover letter", ...],
  "unique_insight": "One non-obvious thing from the research most applicants wouldn't know"
}}

Job description context:
{job_description[:500]}

Research data ({research_data.get('sources_count', 0)} sources):
{raw_combined[:6000]}"""

    try:
        response = router.complete(prompt, system=system, smart=False, max_tokens=1500)
        response = response.strip()
        if response.startswith("```"):
            response = re.sub(r"```[a-z]*\n?", "", response).strip().rstrip("```")
        insight = json.loads(response)
    except Exception as e:
        print(f"[Synthesizer] Parse error for {company}: {e}")
        insight = _minimal_insight(company, job_title, job_description)

    insight["company"] = company
    insight["job_title"] = job_title

    # Save to company profiles DB
    _save_to_db(company, insight, raw_combined, research_data.get("sources_count", 0))

    return insight


def _save_to_db(company: str, insight: dict, raw_research: str, source_count: int):
    """Persist the company personality profile."""
    store = get_store()
    store.save_company_profile({
        "company_name":    company,
        "personality":     insight.get("personality", ""),
        "core_values":     json.dumps(insight.get("core_values", [])),
        "culture_signals": json.dumps(insight.get("culture_signals", [])),
        "keywords":        json.dumps(insight.get("keywords", [])),
        "tone":            insight.get("tone", "professional"),
        "red_flags":       json.dumps(insight.get("avoid", [])),
        "raw_research":    raw_research[:5000],
        "source_count":    source_count,
    })


def _minimal_insight(company: str, job_title: str, job_description: str) -> dict:
    """Fallback when no research is found (e.g. very small startup)."""
    router = get_router()
    prompt = f"""Based only on the company name "{company}" and job title "{job_title}",
infer what kind of company this likely is and what they'd value.

Job description: {job_description[:300]}

Return JSON:
{{
  "personality": "...",
  "core_values": [],
  "culture_signals": [],
  "tone": "professional",
  "keywords": [],
  "avoid": [],
  "what_they_want": "...",
  "talking_points": [],
  "unique_insight": "Limited public research available — focus on genuine enthusiasm and adaptability."
}}"""
    try:
        resp = router.complete(prompt, max_tokens=800)
        resp = resp.strip()
        if resp.startswith("```"):
            resp = re.sub(r"```[a-z]*\n?", "", resp).strip().rstrip("```")
        insight = json.loads(resp)
    except Exception:
        insight = {
            "personality": f"{company} appears to be an innovative organization.",
            "core_values": ["innovation", "excellence", "teamwork"],
            "culture_signals": [],
            "tone": "professional",
            "keywords": ["innovation", "impact", "growth"],
            "avoid": [],
            "what_they_want": "A strong technical candidate with genuine enthusiasm.",
            "talking_points": [],
            "unique_insight": "Limited public research — be authentic and focus on fit.",
        }

    insight["company"] = company
    insight["job_title"] = job_title
    return insight
