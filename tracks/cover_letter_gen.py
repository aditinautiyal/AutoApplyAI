"""
tracks/cover_letter_gen.py
Generates tailored cover letters using company research + user profile.

KEY FIX: generate_cover_letter() ALWAYS produces a real cover letter,
never a refusal or analysis. If the job is a poor fit, it still writes
the best possible cover letter AND creates a separate inbox notification
flagging the concern — so the user sees the warning without the cover
letter being corrupted.
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
    store = get_store()
    banned = ["AutoApplyAI", "Auto Apply AI", "AutoApply", "auto apply"]
    extra = store.get("cover_letter_banned_terms", [])
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except Exception:
            extra = []
    banned.extend(extra)
    return banned


def _is_refusal(text: str) -> bool:
    """
    Detects if the AI wrote a refusal/analysis instead of a cover letter.
    Returns True if the text looks like a refusal, False if it's a real CL.
    """
    refusal_signals = [
        "i recommend", "would not serve", "cannot bridge", "i'm happy to write",
        "i'd be happy to write", "applying could reflect poorly",
        "gap that", "my recommendation", "none of these appear",
        "would likely damage", "hiring teams for", "cover letter cannot",
        "writing a cover letter for this", "applying would",
        "i suggest skipping", "i would advise against",
        "this role isn't", "this isn't a good fit",
        "the profile doesn't", "aditi's profile",
    ]
    text_lower = text.lower()
    return any(signal in text_lower for signal in refusal_signals)


def _flag_poor_fit(job_title: str, company: str, concern: str, app_id: int = None):
    """
    Creates an inbox notification flagging a poor fit concern.
    Completely separate from the cover letter — doesn't interfere with it.
    """
    try:
        store = get_store()
        store.add_notification(
            notif_type="clarification",
            title=f"⚠️ Fit concern flagged: {job_title} @ {company}",
            message=(
                f"AutoApplyAI applied to this role but flagged a potential fit concern:\n\n"
                f"{concern}\n\n"
                f"The cover letter was still written and submitted. "
                f"Review if you'd like to follow up or withdraw."
            ),
            application_id=app_id,
        )
    except Exception:
        pass


def generate_cover_letter(
    job_title: str,
    company: str,
    job_description: str,
    insight: dict,
    app_id: int = None,
) -> str:
    """
    ALWAYS generates a real cover letter. Never writes a refusal.

    If the job seems like a poor fit, it:
    1. Still writes the best possible cover letter
    2. Creates a separate inbox notification flagging the concern
    3. Returns the cover letter unchanged

    This means the approval dialog always shows a real cover letter,
    and fit concerns appear in the Inbox tab separately.
    """
    router = get_router()
    store  = get_store()
    profile = store.get_profile() or {}

    user_context    = _build_user_context(store, profile)
    insight_context = _build_insight_context(insight)
    advice_context  = _get_advice_context("cover_letter")
    banned_names    = _get_banned_project_names()

    banned_instruction = (
        "NEVER mention any of these project names: "
        + ", ".join(f'"{b}"' for b in banned_names)
        + ". If referencing automation projects, describe the technical skills "
        "involved without naming the project itself."
    )

    custom_instructions = store.get("cover_letter_custom_instructions", "")

    system = f"""You are an expert job application cover letter writer.

CRITICAL RULES — these override everything else:
1. ALWAYS write a real, complete cover letter. NEVER write analysis, recommendations,
   explanations, or refusals. Even if the fit seems imperfect, write the best
   cover letter you can using whatever overlap exists.
2. Start directly with the cover letter content — no preamble like "Here is a cover letter"
3. Do NOT start with "I am writing to express my interest" or similar
4. Do NOT use buzzwords: "passion", "leverage", "synergy", "dynamic", "innovative"
5. 3-4 paragraphs, 250-350 words maximum
6. End with confident, forward-looking next steps
7. {banned_instruction}
{"8. Additional instructions: " + custom_instructions if custom_instructions else ""}

If the fit is imperfect, find ANY genuine connection between the applicant's
background and the role — transferable skills, adjacent experience, relevant
coursework, genuine interest — and build the letter around that. A real letter
that is a stretch is always better than a refusal."""

    prompt = f"""Write a cover letter for this application.

APPLICANT PROFILE:
{user_context}

JOB:
Title: {job_title}
Company: {company}
Description: {job_description[:600]}

COMPANY RESEARCH & STRATEGY:
{insight_context}

PROVEN ADVICE TO APPLY:
{advice_context}

Write the complete cover letter now. Start with the first line of the letter itself.
{banned_instruction}"""

    cover_letter = router.complete(
        prompt, system=system, smart=True, max_tokens=700
    ).strip()

    # Safety check — if it still wrote a refusal, force-regenerate with stricter prompt
    if _is_refusal(cover_letter):
        print(f"[CoverLetter] Refusal detected for {company} — force regenerating...")

        # Flag the fit concern in inbox
        _flag_poor_fit(
            job_title, company,
            "AutoApplyAI initially flagged this as a potential poor fit. "
            "Review the cover letter and role before any follow-up.",
            app_id
        )

        # Force regenerate with an even more explicit prompt
        force_prompt = f"""Write a cover letter from {profile.get('full_name', 'the applicant')}
to {company} for the {job_title} role.

The applicant has a computer science and AI/ML background.
Find any connection between their technical background and this role.
Focus on transferable skills: problem-solving, technical thinking, learning ability,
building systems, data analysis, or any relevant coursework.

Write 3 paragraphs, 200-250 words. Start with the first sentence of the letter.
Do not explain, analyze, or refuse. Just write the letter.

Applicant background summary:
{(profile.get('background_text') or '')[:400]}

Role: {job_title} at {company}"""

        cover_letter = router.complete(
            force_prompt, system="You write cover letters. Always produce a letter, never a refusal.",
            smart=False, max_tokens=500
        ).strip()

    # Final safety — if STILL a refusal after two attempts, use a solid template
    if _is_refusal(cover_letter):
        name = profile.get("full_name", "")
        cover_letter = (
            f"I'm excited to apply for the {job_title} role at {company}. "
            f"As a Computer Science student at Purdue University with hands-on "
            f"experience building AI-powered systems and autonomous pipelines, "
            f"I bring a strong technical foundation and a track record of shipping "
            f"real software independently.\n\n"
            f"My work building production-grade async Python systems, integrating "
            f"large language model APIs, and designing encrypted data persistence "
            f"layers has given me deep experience with the kind of engineering "
            f"challenges that matter in fast-moving technical environments. "
            f"I'm drawn to {company} because of the opportunity to apply these "
            f"skills at scale alongside people who take technical craft seriously.\n\n"
            f"I'd welcome the chance to discuss how my background fits the "
            f"{job_title} role. Thank you for your consideration."
        )
        # Flag this too
        _flag_poor_fit(
            job_title, company,
            "Cover letter fell back to template — the role may be a significant stretch. "
            "Review before any follow-up.",
            app_id
        )

    return cover_letter


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
    Always returns a real answer — never a refusal.
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
Always write a real, direct answer — never refuse or explain why you can't answer.

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
- Answer ONLY — no preamble
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
    """Generate subject + body for cold outreach. Returns (subject, body)."""
    router = get_router()
    store  = get_store()
    profile = store.get_profile() or {}
    banned_names = _get_banned_project_names()
    banned_instruction = (
        "Never mention these project names: "
        + ", ".join(f'"{b}"' for b in banned_names) + "."
    )

    name       = profile.get("full_name", "")
    background = (profile.get("background_text") or "")[:250]
    strengths  = (profile.get("strengths_text") or "")[:150]
    portfolio  = profile.get("portfolio_url", "")
    linkedin   = profile.get("linkedin_url", "")

    vals = insight.get("core_values", [])
    if isinstance(vals, str):
        try:
            vals = json.loads(vals)
        except Exception:
            vals = []

    tone           = insight.get("tone", "professional")
    unique_insight = insight.get("unique_insight", "")
    cold_advice    = _get_advice_context("cold_email")
    greeting = f"Hi {recipient_name.split()[0]}," if recipient_name else "Hi there,"

    system = f"""You write concise cold outreach emails for job applications.
Keep them under 200 words, specific to the company, professional.
ALWAYS write the email — never refuse. {banned_instruction}"""

    body_prompt = f"""Write a cold email from {name} to a recruiter at {company}
about the {job_title} role.

Background: {background}
Strengths: {strengths}
Portfolio: {portfolio}
LinkedIn: {linkedin}
Company tone: {tone}
Unique angle: {unique_insight}

Start with: {greeting}
Under 200 words. Genuine and specific. {banned_instruction}"""

    body = router.complete(body_prompt, system=system, smart=False, max_tokens=400).strip()

    subject_prompt = f"""One-line email subject for cold outreach from {name}
about the {job_title} role at {company}.
Under 55 characters. Return ONLY the subject text."""

    subject = router.complete(subject_prompt, max_tokens=25).strip().strip('"\'')

    return subject, body
