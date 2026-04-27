"""
core/success_tracker.py
Closes the feedback loop — when an application gets a positive response,
updates success scores in the advice DB for templates/keywords that were used.
Over time this makes every cover letter and cold email better.
"""

import re
from core.settings_store import get_store


def record_positive_response(app_id: int, response_type: str):
    """
    Called when an application gets interview/offer response.
    Finds what advice/templates were used and increments their success score.
    """
    store = get_store()

    # Get the application record
    apps = store.get_applications()
    app = next((a for a in apps if a["id"] == app_id), None)
    if not app:
        return

    cover_letter = app.get("cover_letter", "") or ""
    if not cover_letter:
        return

    # Extract keywords and phrases from the cover letter that match advice DB content
    _update_advice_scores(cover_letter, store)

    # Update template success rates
    _update_template_scores(cover_letter, store)

    print(f"[SuccessTracker] Updated advice scores for app {app_id} ({response_type})")


def _update_advice_scores(cover_letter: str, store):
    """
    Increment success_score for any advice items whose keywords appear
    in this successful cover letter.
    """
    from research.advice_scraper import update_advice_success

    # Pull all advice items and check for overlap
    cursor = store.conn.execute(
        "SELECT id, content, tags FROM advice_db WHERE advice_type IN ('cover_letter', 'general')"
    )
    rows = cursor.fetchall()

    cover_lower = cover_letter.lower()
    for row_id, content, tags in rows:
        # Check if any key phrase from this advice is reflected in the letter
        content_words = set(
            w for w in re.sub(r'[^a-z\s]', '', content.lower()).split()
            if len(w) > 4  # Skip short words
        )
        letter_words = set(
            w for w in re.sub(r'[^a-z\s]', '', cover_lower).split()
            if len(w) > 4
        )
        overlap = content_words & letter_words
        overlap_ratio = len(overlap) / max(len(content_words), 1)

        # If >30% of advice keywords appear in the letter, credit it
        if overlap_ratio > 0.30:
            store.conn.execute(
                "UPDATE advice_db SET success_score = success_score + 1.0 WHERE id = ?",
                (row_id,)
            )

    store.conn.commit()


def _update_template_scores(cover_letter: str, store):
    """Track which structural patterns in successful letters appear most often."""
    # Common structural signals
    patterns = [
        ("opens_with_company_specific", r'^(When|What|At|The\s+team|I\'ve been)', 0.5),
        ("uses_numbers", r'\b\d[\d,]+\b', 0.3),
        ("mentions_impact", r'\b(impact|result|achiev|built|launch|creat)', 0.3),
        ("has_specific_ask", r'(love to|happy to|would welcome)\s+\w+\s+(chat|call|talk|discuss)', 0.4),
        ("references_company_value", r'(value|mission|culture|team|approach)', 0.2),
    ]

    for pattern_name, regex, weight in patterns:
        if re.search(regex, cover_letter, re.IGNORECASE):
            store.conn.execute("""
                INSERT INTO template_success (template_type, template_text, use_count, response_count, success_rate)
                VALUES (?, ?, 1, 1, 1.0)
                ON CONFLICT DO UPDATE SET
                    use_count = use_count + 1,
                    response_count = response_count + 1,
                    success_rate = CAST(response_count + 1 AS REAL) / (use_count + 1),
                    updated_at = datetime('now')
            """, (pattern_name, pattern_name))

    store.conn.commit()


def record_application_sent(app_id: int):
    """
    Called for every submission — increments use_count on advice used.
    Only response_count gets updated on positive response.
    """
    store = get_store()
    apps = store.get_applications()
    app = next((a for a in apps if a["id"] == app_id), None)
    if not app:
        return

    cover_letter = app.get("cover_letter", "") or ""
    if not cover_letter:
        return

    patterns = [
        ("opens_with_company_specific", r'^(When|What|At|The\s+team|I\'ve been)'),
        ("uses_numbers", r'\b\d[\d,]+\b'),
        ("mentions_impact", r'\b(impact|result|achiev|built|launch|creat)'),
        ("has_specific_ask", r'(love to|happy to|would welcome)\s+\w+\s+(chat|call|talk|discuss)'),
        ("references_company_value", r'(value|mission|culture|team|approach)'),
    ]

    for pattern_name, regex in patterns:
        if re.search(regex, cover_letter, re.IGNORECASE):
            store.conn.execute("""
                INSERT INTO template_success (template_type, template_text, use_count, response_count, success_rate)
                VALUES (?, ?, 1, 0, 0.0)
                ON CONFLICT DO UPDATE SET
                    use_count = use_count + 1,
                    success_rate = CAST(response_count AS REAL) / (use_count + 1),
                    updated_at = datetime('now')
            """, (pattern_name, pattern_name))

    store.conn.commit()
