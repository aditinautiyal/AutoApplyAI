"""
email_handler/gmail_sender.py
Gmail OAuth integration.
Sends cold emails for applications. Monitors inbox for employer replies.
Scopes: gmail.send + gmail.readonly only — cannot touch contacts or other data.
"""

import base64
import json
import os
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional
from core.settings_store import get_store

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

TOKEN_PATH = Path.home() / ".autoapplyai" / "gmail_token.json"
CREDS_PATH = Path.home() / ".autoapplyai" / "gmail_creds.json"

# Employer response keywords for categorization
INTERVIEW_KEYWORDS = ["interview", "schedule", "call", "chat", "meet", "next steps", "excited to"]
REJECTION_KEYWORDS = ["unfortunately", "not moving forward", "decided to", "other candidates", "position has been filled"]
INFO_KEYWORDS = ["could you", "please provide", "we need", "follow up", "additional information"]


class GmailClient:
    def __init__(self):
        self.store = get_store()
        self._service = None

    def _get_service(self):
        """Build and return Gmail API service."""
        if self._service:
            return self._service
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build

            creds = None
            if TOKEN_PATH.exists():
                creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                else:
                    if not CREDS_PATH.exists():
                        raise FileNotFoundError(
                            "Gmail credentials not found. "
                            "Download OAuth credentials from Google Cloud Console "
                            f"and save to: {CREDS_PATH}"
                        )
                    flow = InstalledAppFlow.from_client_secrets_file(
                        str(CREDS_PATH), SCOPES
                    )
                    creds = flow.run_local_server(port=0)

                TOKEN_PATH.write_text(creds.to_json())

            self._service = build("gmail", "v1", credentials=creds)
            return self._service

        except Exception as e:
            raise RuntimeError(f"Gmail auth failed: {e}")

    def is_connected(self) -> bool:
        try:
            svc = self._get_service()
            svc.users().getProfile(userId="me").execute()
            return True
        except Exception:
            return False

    def get_email_address(self) -> Optional[str]:
        try:
            profile = self._get_service().users().getProfile(userId="me").execute()
            return profile.get("emailAddress")
        except Exception:
            return None

    def send_email(self, to: str, subject: str, body: str,
                   from_name: str = "") -> bool:
        """Send a plain text email."""
        try:
            msg = MIMEMultipart("alternative")
            profile = self.store.get_profile() or {}
            sender_name = from_name or profile.get("full_name", "")
            sender_email = self.get_email_address() or ""

            msg["From"] = f"{sender_name} <{sender_email}>" if sender_name else sender_email
            msg["To"] = to
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain"))

            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            self._get_service().users().messages().send(
                userId="me",
                body={"raw": raw}
            ).execute()

            print(f"[Gmail] Sent email to {to}: {subject}")
            return True

        except Exception as e:
            print(f"[Gmail] Send failed to {to}: {e}")
            return False

    def check_inbox_for_replies(self, days_back: int = 30) -> list[dict]:
        """
        Scan inbox for emails that look like employer responses.
        Returns list of categorized reply dicts.
        """
        try:
            svc = self._get_service()
            # Query for emails in the last N days
            after_timestamp = int(time.time()) - (days_back * 86400)
            query = f"after:{after_timestamp} in:inbox -from:me"

            results = svc.users().messages().list(
                userId="me",
                q=query,
                maxResults=50
            ).execute()

            messages = results.get("messages", [])
            replies = []

            for msg_ref in messages:
                try:
                    msg = svc.users().messages().get(
                        userId="me",
                        id=msg_ref["id"],
                        format="full"
                    ).execute()
                    parsed = self._parse_message(msg)
                    if parsed and self._is_likely_employer_email(parsed):
                        replies.append(parsed)
                except Exception:
                    continue

            return replies

        except Exception as e:
            print(f"[Gmail] Inbox check failed: {e}")
            return []

    def _parse_message(self, msg: dict) -> Optional[dict]:
        """Extract headers and snippet from Gmail message."""
        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        return {
            "id": msg["id"],
            "from": headers.get("from", ""),
            "subject": headers.get("subject", ""),
            "date": headers.get("date", ""),
            "snippet": msg.get("snippet", ""),
            "category": "unknown",
        }

    def _is_likely_employer_email(self, email: dict) -> bool:
        """Filter out newsletters, promotions — keep likely job responses."""
        subject_lower = (email["subject"] or "").lower()
        snippet_lower = (email["snippet"] or "").lower()
        text = subject_lower + " " + snippet_lower

        # Must mention something job-related
        job_signals = ["application", "position", "role", "opportunity",
                       "candidate", "interview", "hiring", "recruiter"]
        return any(signal in text for signal in job_signals)

    def categorize_reply(self, email: dict) -> str:
        """Categorize an employer email as interview/rejection/info/unknown."""
        text = (email["subject"] + " " + email["snippet"]).lower()

        if any(kw in text for kw in INTERVIEW_KEYWORDS):
            return "interview"
        if any(kw in text for kw in REJECTION_KEYWORDS):
            return "rejection"
        if any(kw in text for kw in INFO_KEYWORDS):
            return "info_needed"
        return "response"


def generate_cold_email(company: str, job_title: str,
                         recruiter_name: str, recruiter_email: str,
                         insight: dict) -> tuple[str, str]:
    """
    Generate a cold email subject and body using research insights.
    Uses best-performing template from advice DB.
    Returns (subject, body).
    """
    from core.api_router import get_router
    from core.settings_store import get_store

    router = get_router()
    store = get_store()
    profile = store.get_profile() or {}

    # Build user summary
    name = profile.get("full_name", "")
    background = profile.get("background_text", "")[:300]
    strengths = profile.get("strengths_text", "")[:200]
    portfolio = profile.get("portfolio_url", "")
    linkedin = profile.get("linkedin_url", "")

    # Company insight
    personality = insight.get("personality", "")
    values = insight.get("core_values", [])
    if isinstance(values, str):
        import json as _json
        values = _json.loads(values)
    tone = insight.get("tone", "professional")
    unique_insight = insight.get("unique_insight", "")

    greeting = f"Hi {recruiter_name.split()[0]}," if recruiter_name else "Hi there,"

    system = """You write highly effective cold outreach emails for job applications.
Emails should be: brief (150-200 words max), specific to the company,
professional, confident, and end with a clear ask.
Never start with 'I am writing to' or 'I hope this email finds you well'."""

    prompt = f"""Write a cold email from {name} to a recruiter at {company} about the {job_title} role.

Applicant background: {background}
Key strengths: {strengths}
Portfolio: {portfolio}
LinkedIn: {linkedin}

Company insight: {personality}
They value: {', '.join(values[:3]) if values else 'innovation and excellence'}
Tone match: {tone}
Unique angle to use: {unique_insight}

Start the email with: {greeting}

Write the email body only (no subject line).
Be specific about why THIS company. Keep it under 200 words."""

    body = router.complete(prompt, system=system, smart=False, max_tokens=400).strip()

    # Subject line
    subject_prompt = f"""Write a compelling email subject line for a cold outreach from {name} 
about the {job_title} role at {company}.
Keep it under 60 characters. Make it specific and intriguing.
Return ONLY the subject line text, nothing else."""

    subject = router.complete(subject_prompt, max_tokens=30).strip()
    subject = subject.strip('"\'')

    return subject, body


def send_cold_email_for_application(app_id: int, company: str,
                                     job_title: str, insight: dict) -> bool:
    """
    Auto-send a cold email for a submitted application if recruiter email findable.
    """
    store = get_store()
    gmail = GmailClient()

    if not gmail.is_connected():
        print("[ColdEmail] Gmail not connected — skipping")
        return False

    # Try to find recruiter email from company domain
    recruiter_email = _find_recruiter_email(company)
    if not recruiter_email:
        return False

    subject, body = generate_cold_email(
        company, job_title, "", recruiter_email, insight
    )

    success = gmail.send_email(recruiter_email, subject, body)
    if success:
        store.update_application(app_id, {"notes": f"Cold email sent to {recruiter_email}"})

    return success


def _find_recruiter_email(company: str) -> Optional[str]:
    """Try common recruiting email patterns for a company."""
    # Common patterns — try these in order
    company_clean = company.lower().replace(" ", "").replace(",", "").replace(".", "")
    patterns = [
        f"recruiting@{company_clean}.com",
        f"careers@{company_clean}.com",
        f"jobs@{company_clean}.com",
        f"talent@{company_clean}.com",
        f"university@{company_clean}.com",
    ]
    # For now return the most likely one — future: verify via email validation API
    return patterns[0]


class InboxMonitor:
    """Periodically checks Gmail for employer responses and logs them."""

    def __init__(self):
        self.store = get_store()
        self.gmail = GmailClient()

    def run_check(self):
        """Single check cycle — call periodically."""
        if not self.gmail.is_connected():
            return

        replies = self.gmail.check_inbox_for_replies(days_back=30)
        new_count = 0

        for reply in replies:
            category = self.gmail.categorize_reply(reply)
            reply["category"] = category

            subject = reply.get("subject", "").lower()
            sender  = reply.get("from", "").lower()

            apps = self.store.get_applications(status="submitted")
            for app in apps:
                company = (app.get("company_name") or "").lower()
                if company and (company in sender or company in subject):
                    if not app.get("response_type"):
                        # Use the central response handler
                        from email_handler.response_handler import handle_response
                        handle_response(
                            application_id=app["id"],
                            response_type=category,
                            response_text=reply.get("snippet", ""),
                            sender=reply.get("from", ""),
                        )
                        new_count += 1
                        break

        if new_count:
            print(f"[InboxMonitor] Found {new_count} new employer responses")
