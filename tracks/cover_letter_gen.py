"""
tracks/cover_letter_gen.py
Generates tailored cover letters using company research + user profile.
Integrates advice DB — pulls top-ranked tips into every generation.
Uses Claude Sonnet (smart) for cover letters, Haiku for form answers.
"""

import json
import re
from core.api_router import get_router
from core.settings_store import get_store
from onboarding.resume_parser import resume_to_summary_text


def _build_user_context(store, profile: dict) -> str:
    parts = []
    if profile.get("full_name"):
        parts.append(f"Applicant: {profile['full_name']}")
    if profile.get("graduation_date"):
        parts.append(f"Graduating: {profile['graduation_date']}")
    if profile.get("background_text"):
        parts.append(f"\nBackground:\n{profile['background_text']}")
    if profile.get("strengths_text"):
        parts.append(f"\nKey strengths:\n{profile['strengths_text']}")

    resume_parsed = profile.get("resume_parsed")
    if resume_parsed:
        try:
            parsed = json.loads(resume_parsed)
            resume_summary = resume_to_summary_text(parsed)
            parts.append(f"\nResume highlights:\n{resume_summary}")
        except Exception:
            pass

    for key in ["career_goals", "greatest_strength", "why_you"]:
        answer = store.find_learned_answer(key)
        if answer:
            parts.append(f"\n{key.replace('_', ' ').title()}: {answer}")

    return "\n".join(parts)


def _get_advice_context(advice_type: str = "cover_letter") -> str:
    try:
        from research.advice_scraper import get_advice_context_for_generation
        return get_advice_context_for_generation(advice_type)
    except Exception:
        return ""


def _build_insight_context(insight: dict) -> str:
    parts = []
    if insight.get("personality"):
        parts.append(f"Company personality: {insight['personality']}")

    vals = insight.get("core_values", [])
    if isinstance(vals, str):
        try:
            vals = json.loads(vals)
        except Exception:
            vals = []
    if vals:
        parts.append(f"What they value: {', '.join(vals)}")

    if insight.get("what_they_want"):
        parts.append(f"What this role needs: {insight['what_they_want']}")

    pts = insight.get("talking_points", [])
    if isinstance(pts, list) and pts:
        parts.append(f"Key angles to mention: {'; '.join(pts)}")

    kws = insight.get("keywords", [])
    if isinstance(kws, str):
        try:
            kws = json.loads(kws)
        except Exception:
            kws = []
    if kws:
        parts.append(f"Keywords to weave in naturally: {', '.join(kws)}")

    if insight.get("unique_insight"):
        parts.append(f"Unique insight to reference: {insight['unique_insight']}")
    if insight.get("tone"):
        parts.append(f"Tone to match: {insight['tone']}")

    avoid = insight.get("avoid", [])
    if isinstance(avoid, str):
        try:
            avoid = json.loads(avoid)
        except Exception:
            avoid = []
    if avoid:
        parts.append(f"Avoid: {', '.join(avoid)}")

    return "\n".join(parts)


def _get_banned_project_names() -> list[str]:
    """
    Returns project names that must never appear in cover letters.
    AutoApplyAI and any variant should never be mentioned — recruiters
    seeing it would assume the application is bot-generated.
    """
    store = get_store()
    # Core ban list — always enforced
    banned = [
        "AutoApplyAI",
        "Auto Apply AI",
        "AutoApply",
        "auto apply",
    ]
    # Also pull any user-defined banned terms from settings
    extra = store.get("cover_letter_banned_terms", [])
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except Exception:
            extra = []
    banned.extend(extra)
    return banned


def generate_cover_letter(
    job_title: str,
    company: str,
    job_description: str,
    insight: dict,
) -> str:
    """
    Generate a tailored cover letter.
    Integrates: user profile + company research + proven advice from DB.
    Uses Sonnet (smart=True) — quality matters here.
    """
    router = get_router()
    store = get_store()
    profile = store.get_profile() or {}

    user_context = _build_user_context(store, profile)
    insight_context = _build_insight_context(insight)
    advice_context = _get_advice_context("cover_letter")
    banned_names = _get_banned_project_names()

    # Build the banned terms instruction string
    banned_instruction = (
        "- NEVER mention any of these project names under any circumstances, "
        "even as examples of automation or AI work: "
        + ", ".join(f'"{b}"' for b in banned_names)
        + ". If referencing automation projects, describe the technical skills "
        "involved (e.g. 'built an async Python automation pipeline') without naming "
        "the project itself."
    )

    # Pull any custom instructions the user has set via AI Chat
    custom_instructions = store.get("cover_letter_custom_instructions", "")

    system = f"""You are an expert job application writer. Write cover letters that:
- Sound like a real, thoughtful person wrote them — not AI-generated
- Are specific to this exact company and role — not generic
- Demonstrate genuine research into company culture and values
- Use natural, confident professional language with varied sentence length
- Are 3-4 paragraphs, 250-350 words maximum
- Never start with "I am writing to express my interest" or similar
- Start with something specific and engaging about the company or role
- End with confident, forward-looking next steps
- Incorporate proven best practices naturally, not mechanically
{banned_instruction}
{"Additional instructions: " + custom_instructions if custom_instructions else ""}"""

    prompt = f"""Write a cover letter for this application.

APPLICANT PROFILE:
{user_context}

JOB:
Title: {job_title}
Company: {company}
Description: {job_description[:600]}

COMPANY RESEARCH & STRATEGY:
{insight_context}

PROVEN ADVICE TO APPLY (weave in naturally):
{advice_context}

INSTRUCTIONS:
- Reference the company's actual personality and values naturally
- Connect specific experiences from the applicant's background to what this company needs
- If there's a unique insight, weave it in to show depth of research
- Sound enthusiastic but grounded — real, not performative
- Do NOT start with "I am writing to apply"
- Do NOT use buzzwords: "passion", "leverage", "synergy", "dynamic", "innovative"
- Keywords should appear naturally in sentences, not listed
- Apply the proven advice but keep it feeling authentic
- Write the full cover letter only — no subject line, no [placeholders]
- {banned_instruction}"""

    cover_letter = router.complete(
        prompt, system=system, smart=True, max_tokens=700
    )
    return cover_letter.strip()


def generate_form_answer(
    question: str,
    job_title: str,
    company: str,
    insight: dict,
    max_words: int = 150,
) -> str:
    """
    Generate an answer to a specific application form question.
    Checks learned_answers first. Falls back to Haiku generation.
    """
    store = get_store()
    banned_names = _get_banned_project_names()
    banned_instruction = (
        "Never mention these project names: "
        + ", ".join(f'"{b}"' for b in banned_names)
        + ". Describe technical skills without naming the project."
    )

    stored = store.find_learned_answer(question)
    if stored and len(stored) > 10:
        router = get_router()
        vals = insight.get("core_values", [])
        if isinstance(vals, str):
            try:
                vals = json.loads(vals)
            except Exception:
                vals = []

        adapt_prompt = f"""Adapt this stored answer to fit this specific company and role.
Keep the core content but adjust tone and specific references.

Original answer: {stored}

Role: {job_title} at {company}
Tone: {insight.get('tone', 'professional')}
Company values: {', '.join(vals[:3]) if vals else ''}
Max words: {max_words}
{banned_instruction}

Return only the adapted answer:"""
        try:
            return router.complete(adapt_prompt, max_tokens=300).strip()
        except Exception:
            return stored

    router = get_router()
    profile = store.get_profile() or {}
    user_context = _build_user_context(store, profile)

    vals = insight.get("core_values", [])
    if isinstance(vals, str):
        try:
            vals = json.loads(vals)
        except Exception:
            vals = []

    prompt = f"""Answer this application question for the candidate.

Question: {question}

Candidate:
{user_context[:1500]}

Role: {job_title} at {company}
Tone: {insight.get('tone', 'professional')}
Company values: {', '.join(vals[:4]) if vals else ''}

Rules:
- {max_words} words or fewer
- Natural, human — not corporate AI speak
- Specific, not generic
- Relate to company values where relevant
- Answer ONLY — no preamble like "Here is my answer:"
- {banned_instruction}

Answer:"""

    answer = router.complete(prompt, smart=False, max_tokens=350).strip()

    store.save_learned_answer(
        question_pattern=question[:100],
        answer=answer,
        tags=["generated", job_title, company]
    )

    return answer


def generate_cold_email_body(
    company: str,
    job_title: str,
    insight: dict,
    recipient_name: str = "",
) -> tuple[str, str]:
    """
    Generate subject + body for a cold outreach email.
    Returns (subject, body).
    """
    router = get_router()
    store = get_store()
    profile = store.get_profile() or {}
    banned_names = _get_banned_project_names()
    banned_instruction = (
        "Never mention these project names: "
        + ", ".join(f'"{b}"' for b in banned_names) + "."
    )

    name = profile.get("full_name", "")
    background = (profile.get("background_text") or "")[:250]
    strengths = (profile.get("strengths_text") or "")[:150]
    portfolio = profile.get("portfolio_url", "")
    linkedin = profile.get("linkedin_url", "")

    vals = insight.get("core_values", [])
    if isinstance(vals, str):
        try:
            vals = json.loads(vals)
        except Exception:
            vals = []

    tone = insight.get("tone", "professional")
    unique_insight = insight.get("unique_insight", "")
    cold_email_advice = _get_advice_context("cold_email")
    greeting = f"Hi {recipient_name.split()[0]}," if recipient_name else "Hi there,"

    system = f"""You write highly effective cold outreach emails for job applications.
Keep them: brief (150-200 words), specific to the company, professional,
and end with a clear single ask. Never use generic openers.
{banned_instruction}"""

    body_prompt = f"""Write a cold email from {name} to a recruiter at {company} about the {job_title} role.

Background: {background}
Strengths: {strengths}
Portfolio: {portfolio}
LinkedIn: {linkedin}

Company: {insight.get('personality', '')}
They value: {', '.join(vals[:3]) if vals else 'excellence'}
Tone: {tone}
Unique angle: {unique_insight}

Proven cold email tips to apply naturally:
{cold_email_advice}

Start with: {greeting}
Body only, no subject. Under 200 words. Genuine and specific.
{banned_instruction}"""

    body = router.complete(body_prompt, system=system, smart=False, max_tokens=400).strip()

    subject_prompt = f"""One-line email subject for cold outreach from {name}
about the {job_title} role at {company}.
Under 55 characters. Specific and compelling. Return ONLY the subject text."""

    subject = router.complete(subject_prompt, max_tokens=25).strip().strip('"\'')

    return subject, body
