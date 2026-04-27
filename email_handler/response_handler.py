"""
email_handler/response_handler.py
Handles employer responses from Gmail.
On positive response (interview/offer): updates advice DB success scores.
On info needed: creates notification, pauses application.
On rejection: logs cleanly, no action needed.
"""

from core.settings_store import get_store
from core.success_tracker import record_positive_response


def handle_response(application_id: int, response_type: str,
                    response_text: str, sender: str):
    """
    Central handler for all categorized employer responses.
    Called by InboxMonitor after categorizing an email.
    """
    store = get_store()

    # Update the application record
    store.update_application(application_id, {
        "response_type": response_type,
        "response_text": response_text[:500],
    })

    if response_type == "interview":
        _handle_interview(application_id, sender, store)

    elif response_type == "offer":
        _handle_offer(application_id, sender, store)

    elif response_type == "info_needed":
        _handle_info_needed(application_id, response_text, store)

    elif response_type == "rejection":
        _handle_rejection(application_id, store)


def _handle_interview(app_id: int, sender: str, store):
    """Interview request — celebrate, update scores, notify user."""
    apps = store.get_applications()
    app = next((a for a in apps if a["id"] == app_id), None)
    company = app.get("company_name", "Unknown") if app else "Unknown"

    # Update advice DB — this cover letter worked
    record_positive_response(app_id, "interview")

    # Create high-priority notification
    store.add_notification(
        notif_type="interview",
        title=f"🎉 Interview request from {company}!",
        message=(
            f"From: {sender}\n\n"
            f"An interview has been requested for your application to {company}. "
            f"Check your email and respond promptly."
        ),
        application_id=app_id,
    )
    print(f"[ResponseHandler] 🎉 INTERVIEW: {company}")


def _handle_offer(app_id: int, sender: str, store):
    """Offer — update scores strongly, notify."""
    apps = store.get_applications()
    app = next((a for a in apps if a["id"] == app_id), None)
    company = app.get("company_name", "Unknown") if app else "Unknown"

    # Offer = strong positive signal, double the weight
    record_positive_response(app_id, "offer")
    record_positive_response(app_id, "offer")  # Twice = extra weight

    store.add_notification(
        notif_type="interview",
        title=f"🏆 Offer received from {company}!",
        message=f"From: {sender}\n\nCongratulations! Review your email for offer details.",
        application_id=app_id,
    )
    print(f"[ResponseHandler] 🏆 OFFER: {company}")


def _handle_info_needed(app_id: int, response_text: str, store):
    """Employer needs more info — notify user to respond."""
    apps = store.get_applications()
    app = next((a for a in apps if a["id"] == app_id), None)
    company = app.get("company_name", "Unknown") if app else "Unknown"

    store.add_notification(
        notif_type="info_needed",
        title=f"❓ {company} needs more information",
        message=(
            f"The employer replied asking for more information:\n\n"
            f"{response_text[:300]}\n\n"
            f"Reply to their email directly — this has been flagged for your attention."
        ),
        application_id=app_id,
    )
    print(f"[ResponseHandler] ❓ INFO NEEDED: {company}")


def _handle_rejection(app_id: int, store):
    """Rejection — log cleanly, no notification needed."""
    apps = store.get_applications()
    app = next((a for a in apps if a["id"] == app_id), None)
    company = app.get("company_name", "Unknown") if app else "Unknown"
    print(f"[ResponseHandler] Rejection logged: {company}")
