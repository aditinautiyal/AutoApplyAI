"""
tracks/humanizer_check.py
Checks cover letter AI detection score before submission.
GPTZero API if key available, local heuristic fallback otherwise.
Threshold: pass if AI score <= 75%.
Retries with humanization prompt if fails.
"""

import re
import requests
from core.settings_store import get_store
from core.api_router import get_router

AI_THRESHOLD = 0.75  # Pass if AI probability <= this


def check_humanness(text: str) -> tuple[float, str]:
    """
    Check how AI-like the text is.
    Returns (ai_probability 0.0-1.0, source_used).
    0.0 = fully human, 1.0 = fully AI.
    """
    store = get_store()
    gptzero_key = store.get("gptzero_api_key")

    if gptzero_key:
        try:
            return _gptzero_check(text, gptzero_key)
        except Exception as e:
            print(f"[Humanizer] GPTZero failed: {e}, using local check")

    return _local_check(text), "local"


def _gptzero_check(text: str, api_key: str) -> tuple[float, str]:
    """GPTZero API check."""
    resp = requests.post(
        "https://api.gptzero.me/v2/predict/text",
        headers={
            "x-api-key": api_key,
            "Content-Type": "application/json",
        },
        json={"document": text, "multilingual": False},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    doc = data.get("documents", [{}])[0]
    ai_prob = doc.get("completely_generated_prob", 0.5)
    return float(ai_prob), "gptzero"


def _local_check(text: str) -> float:
    """
    Local heuristic AI detection.
    Looks for patterns common in AI-generated text.
    Returns estimated AI probability.
    """
    score = 0.0
    word_count = len(text.split())

    if word_count < 10:
        return 0.3

    # Overused AI phrases — each adds to AI probability
    ai_phrases = [
        "i am writing to express", "i am excited to", "i am passionate about",
        "leverage my skills", "dynamic team", "fast-paced environment",
        "i would be a great fit", "i am confident that", "synergy",
        "i look forward to hearing", "thank you for considering",
        "i am eager to", "honed my skills", "invaluable experience",
        "i am thrilled", "furthermore,", "in conclusion,", "to summarize",
        "it is worth noting", "it should be noted",
    ]

    text_lower = text.lower()
    ai_phrase_hits = sum(1 for p in ai_phrases if p in text_lower)
    score += min(0.4, ai_phrase_hits * 0.08)

    # Sentence length uniformity (AI tends to be uniform)
    sentences = re.split(r'[.!?]+', text)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 5]
    if len(sentences) > 2:
        lengths = [len(s.split()) for s in sentences]
        avg = sum(lengths) / len(lengths)
        variance = sum((l - avg) ** 2 for l in lengths) / len(lengths)
        if variance < 20:  # Very uniform = likely AI
            score += 0.15

    # Paragraph structure uniformity
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    if len(paragraphs) > 2:
        para_lengths = [len(p.split()) for p in paragraphs]
        max_len = max(para_lengths)
        min_len = min(para_lengths)
        if max_len > 0 and min_len / max_len > 0.7:
            score += 0.10

    # Oxford commas and formal connectors
    formal_connectors = ["moreover,", "additionally,", "consequently,",
                          "nevertheless,", "therefore,", "thus,", "hence,"]
    connector_hits = sum(1 for c in formal_connectors if c in text_lower)
    score += min(0.15, connector_hits * 0.05)

    return min(1.0, score)


def humanize_text(text: str, company: str, job_title: str,
                   attempt: int = 1) -> str:
    """
    Rewrite text to be less AI-detectable.
    Called when check_humanness returns above threshold.
    """
    router = get_router()

    style_variations = [
        "Make it slightly more conversational and direct. Vary sentence lengths more.",
        "Use more active voice and specific examples. Cut any filler phrases.",
        "Make it sound like a confident person talking, not writing formally.",
    ]
    style = style_variations[min(attempt - 1, len(style_variations) - 1)]

    prompt = f"""Rewrite this cover letter to sound more natural and less AI-generated.
{style}

Rules:
- Keep ALL the specific content, examples, and company references
- Do NOT add generic filler
- Do NOT start with "I am writing to apply"
- Vary sentence structure — some short, some medium
- Remove any overly formal connectors (moreover, additionally, therefore)
- Keep it 250-350 words
- Preserve the mention of {company} and the {job_title} role specifically

Original text:
{text}

Rewritten version (text only, no preamble):"""

    return router.complete(prompt, smart=False, max_tokens=700).strip()


def ensure_humanized(text: str, company: str = "", job_title: str = "",
                      max_attempts: int = 3) -> tuple[str, float, int]:
    """
    Full pipeline: check → rewrite if needed → check again.
    Returns (final_text, final_ai_score, attempts_used).
    """
    current_text = text

    for attempt in range(1, max_attempts + 1):
        ai_prob, source = check_humanness(current_text)
        print(f"[Humanizer] Attempt {attempt}: AI score {ai_prob:.2f} ({source}), threshold {AI_THRESHOLD}")

        if ai_prob <= AI_THRESHOLD:
            return current_text, ai_prob, attempt

        if attempt < max_attempts:
            print(f"[Humanizer] Score {ai_prob:.2f} above threshold — rewriting...")
            current_text = humanize_text(current_text, company, job_title, attempt)

    # Return best version even if still above threshold
    final_prob, _ = check_humanness(current_text)
    print(f"[Humanizer] Final score after {max_attempts} attempts: {final_prob:.2f}")
    return current_text, final_prob, max_attempts
