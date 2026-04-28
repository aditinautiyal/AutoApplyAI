"""
tracks/workday_handler.py
Full Workday ATS automation.

Flow:
1. Navigate to Workday job URL
2. Click Apply
3. If no account → auto-create one using applicant email + generated password
4. Check Gmail for verification email → click verification link
5. Fill multi-step application form (contact info, work experience, education,
   resume upload, cover letter, screening questions)
6. Submit

Workday is used by thousands of mid-to-large companies (Salesforce, Nvidia,
Uber, Airbnb, etc.) so getting this right unlocks a huge portion of real
tech applications.
"""

import asyncio
import random
import re
import string
import time
from typing import Optional
from core.settings_store import get_store
from core.api_router import get_router
from tracks.cover_letter_gen import generate_form_answer


# ── Workday selectors (Workday uses data-automation-id heavily) ───────────────

SEL = {
    # Job page
    "apply_btn": [
        "[data-automation-id='applyButton']",
        "[data-automation-id='applyButtonTop']",
        "a[href*='apply']:not([href*='already'])",
        "button:has-text('Apply')",
        "button:has-text('Apply Now')",
        "a:has-text('Apply Now')",
    ],

    # Account creation / login gate
    "create_account_link": [
        "[data-automation-id='createAccountLink']",
        "a:has-text('Create Account')",
        "button:has-text('Create Account')",
        "a:has-text('Sign Up')",
    ],
    "existing_account_link": [
        "[data-automation-id='signInLink']",
        "a:has-text('Sign In')",
        "button:has-text('Sign In')",
    ],

    # Account creation form
    "account_email":    ["[data-automation-id='email']", "input[type='email']"],
    "account_password": ["[data-automation-id='password']", "input[type='password'][id*='password']"],
    "account_verify_pw":["[data-automation-id='verifyPassword']", "input[id*='verify'], input[id*='confirm']"],
    "account_fname":    ["[data-automation-id='firstName']", "input[id*='firstName']"],
    "account_lname":    ["[data-automation-id='lastName']", "input[id*='lastName']"],
    "create_account_submit": [
        "[data-automation-id='createAccountSubmitButton']",
        "button:has-text('Create Account')",
        "button[type='submit']",
    ],

    # Verification
    "email_verified_indicator": [
        "text=Email verified",
        "text=Account created",
        "text=Welcome",
        "[data-automation-id='welcomeMessage']",
    ],

    # Application form navigation
    "next_btn": [
        "[data-automation-id='nextButton']",
        "[data-automation-id='bottom-navigation-next-button']",
        "button:has-text('Next')",
        "button:has-text('Save and Continue')",
        "button[aria-label*='Next']",
    ],
    "submit_btn": [
        "[data-automation-id='submitButton']",
        "[data-automation-id='bottom-navigation-next-button']",
        "button:has-text('Submit')",
        "button:has-text('Review')",
        "button:has-text('I Agree')",
    ],

    # Form fields
    "resume_upload": [
        "input[type='file'][data-automation-id*='resume']",
        "input[type='file']",
    ],
    "cover_letter_field": [
        "textarea[data-automation-id*='coverLetter']",
        "textarea[placeholder*='cover']",
        "[data-automation-id='coverletter'] textarea",
        "div[data-automation-id='richText'] [contenteditable]",
    ],

    # Confirmation
    "confirmation": [
        "text=Thank you",
        "text=Application submitted",
        "text=successfully submitted",
        "[data-automation-id='applicationSubmittedMessage']",
        "h1:has-text('Thank')",
        "h2:has-text('Thank')",
    ],
}

WD_PASSWORD_KEY = "workday_app_password"


def _generate_password() -> str:
    """Generate a strong password for Workday accounts."""
    chars = string.ascii_letters + string.digits + "!@#$%"
    pwd = (
        random.choice(string.ascii_uppercase) +
        random.choice(string.ascii_lowercase) +
        random.choice(string.digits) +
        random.choice("!@#$%") +
        "".join(random.choices(chars, k=10))
    )
    return "".join(random.sample(pwd, len(pwd)))


def _get_or_create_password() -> str:
    """Get stored Workday password or generate and store a new one."""
    store = get_store()
    pwd = store.get(WD_PASSWORD_KEY)
    if not pwd:
        pwd = _generate_password()
        store.set(WD_PASSWORD_KEY, pwd)
        print(f"[Workday] Generated app password (stored encrypted)")
    return pwd


async def _try_selectors(page, selectors: list[str], action: str = "click",
                          value: str = "", timeout: int = 5000):
    """Try multiple selectors until one works."""
    for sel in selectors:
        try:
            el = await page.wait_for_selector(sel, timeout=timeout, state="visible")
            if el:
                if action == "click":
                    await el.click()
                elif action == "fill":
                    await el.fill(value)
                elif action == "check":
                    return await el.is_visible()
                return True
        except Exception:
            continue
    return False


async def _type_human(page, selector: str, text: str):
    """Type with human-like delays."""
    try:
        el = await page.query_selector(selector)
        if el:
            await el.click()
            await el.fill("")
            for char in text:
                await el.type(char, delay=random.randint(40, 120))
                if random.random() < 0.03:
                    await asyncio.sleep(random.uniform(0.1, 0.3))
    except Exception:
        pass


async def _delay(min_s: float = 1.0, max_s: float = 2.5):
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _wait_for_gmail_verification(email: str, timeout_seconds: int = 120) -> Optional[str]:
    """
    Polls Gmail for a Workday verification email and returns the verification link.
    Requires Gmail OAuth to be connected.
    """
    store = get_store()
    if not store.get("gmail_token"):
        print("[Workday] Gmail not connected — cannot auto-verify email")
        return None

    print(f"[Workday] Waiting for verification email at {email}...")

    try:
        from email_handler.gmail_sender import GmailClient
        gmail = GmailClient()

        start = time.time()
        while time.time() - start < timeout_seconds:
            # Check Gmail for Workday verification
            try:
                svc = gmail._get_service()
                results = svc.users().messages().list(
                    userId="me",
                    q="from:workday subject:verify newer_than:5m",
                    maxResults=5,
                ).execute()

                messages = results.get("messages", [])
                for msg_ref in messages:
                    msg = svc.users().messages().get(
                        userId="me",
                        id=msg_ref["id"],
                        format="full",
                    ).execute()

                    # Get email body
                    parts = msg.get("payload", {}).get("parts", [])
                    body = ""
                    for part in parts:
                        if part.get("mimeType") == "text/html":
                            import base64
                            data = part.get("body", {}).get("data", "")
                            if data:
                                body = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                                break
                        elif part.get("mimeType") == "text/plain":
                            import base64
                            data = part.get("body", {}).get("data", "")
                            if data:
                                body = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")

                    # Find verification link
                    links = re.findall(r'https?://[^\s"<>]+verify[^\s"<>]*', body, re.IGNORECASE)
                    if not links:
                        links = re.findall(r'https?://[^\s"<>]+token[^\s"<>]*', body, re.IGNORECASE)

                    if links:
                        print(f"[Workday] Verification link found!")
                        return links[0]

            except Exception as e:
                print(f"[Workday] Gmail check error: {e}")

            await asyncio.sleep(10)

        print("[Workday] Verification email timeout — proceeding without verification")
        return None

    except Exception as e:
        print(f"[Workday] Gmail verification error: {e}")
        return None


async def _create_workday_account(page, profile: dict) -> bool:
    """
    Creates a new Workday account using the applicant's email.
    Returns True if account created successfully, False otherwise.
    """
    email    = profile.get("email", "")
    fname    = (profile.get("full_name") or "").split()[0] if profile.get("full_name") else ""
    lname    = (profile.get("full_name") or "").split()[-1] if profile.get("full_name") else ""
    password = _get_or_create_password()

    print(f"[Workday] Creating account for {email}...")

    # Click "Create Account"
    created = await _try_selectors(page, SEL["create_account_link"], timeout=8000)
    if not created:
        print("[Workday] Could not find Create Account link")
        return False

    await _delay(1, 2)

    # Fill account creation form
    for sel in SEL["account_email"]:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.fill(email)
                break
        except Exception:
            continue

    await _delay(0.5, 1)

    # Password fields
    pw_filled = False
    for sel in SEL["account_password"]:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.fill(password)
                pw_filled = True
                break
        except Exception:
            continue

    if pw_filled:
        await _delay(0.3, 0.8)
        for sel in SEL["account_verify_pw"]:
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.fill(password)
                    break
            except Exception:
                continue

    # Name fields
    await _delay(0.3, 0.8)
    for sel in SEL["account_fname"]:
        try:
            el = await page.query_selector(sel)
            if el and fname:
                await el.fill(fname)
                break
        except Exception:
            continue

    for sel in SEL["account_lname"]:
        try:
            el = await page.query_selector(sel)
            if el and lname:
                await el.fill(lname)
                break
        except Exception:
            continue

    await _delay(1, 2)

    # Submit account creation
    submitted = await _try_selectors(page, SEL["create_account_submit"], timeout=5000)
    if not submitted:
        print("[Workday] Could not submit account creation form")
        return False

    await _delay(2, 4)

    # Try to get verification link from Gmail
    verify_link = await _wait_for_gmail_verification(email, timeout_seconds=90)
    if verify_link:
        try:
            await page.goto(verify_link, wait_until="networkidle", timeout=20000)
            await _delay(2, 3)
            print("[Workday] Email verified successfully")
        except Exception as e:
            print(f"[Workday] Error navigating to verification link: {e}")

    return True


async def _sign_in_workday(page, profile: dict) -> bool:
    """Signs in to an existing Workday account."""
    email    = profile.get("email", "")
    password = _get_or_create_password()

    print(f"[Workday] Signing in as {email}...")

    await _try_selectors(page, SEL["existing_account_link"], timeout=5000)
    await _delay(1, 2)

    for sel in SEL["account_email"]:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.fill(email)
                break
        except Exception:
            continue

    await _delay(0.3, 0.8)

    for sel in SEL["account_password"]:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.fill(password)
                break
        except Exception:
            continue

    await _delay(0.5, 1)

    # Submit sign in
    try:
        await page.keyboard.press("Enter")
    except Exception:
        await _try_selectors(page, ["button[type='submit']", "button:has-text('Sign In')"])

    await _delay(2, 4)
    return True


async def _fill_workday_form_step(page, profile: dict, insight: dict,
                                   cover_letter: str, job_title: str,
                                   company: str) -> bool:
    """
    Fills one step of the Workday multi-step application form.
    Returns True if this was the final step (submitted), False to continue.
    """
    store = get_store()
    await _delay(1, 2)

    # ── Resume upload ──────────────────────────────────────────────────────────
    for sel in SEL["resume_upload"]:
        try:
            el = await page.query_selector(sel)
            if el and profile.get("resume_path"):
                await el.set_input_files(profile["resume_path"])
                await _delay(2, 3)
                print("[Workday] Resume uploaded")
                break
        except Exception:
            continue

    # ── Cover letter ───────────────────────────────────────────────────────────
    for sel in SEL["cover_letter_field"]:
        try:
            el = await page.query_selector(sel)
            if el:
                tag = (await el.evaluate("el => el.tagName")).lower()
                if tag == "textarea":
                    await el.fill(cover_letter)
                else:
                    await el.click()
                    await page.keyboard.type(cover_letter[:2000])
                await _delay(0.5, 1)
                print("[Workday] Cover letter filled")
                break
        except Exception:
            continue

    # ── Standard text fields ───────────────────────────────────────────────────
    full_name = profile.get("full_name", "") or ""
    field_map = {
        "firstName":       full_name.split()[0] if full_name else "",
        "lastName":        full_name.split()[-1] if full_name else "",
        "email":           profile.get("email", ""),
        "phone":           profile.get("phone", ""),
        "phoneNumber":     profile.get("phone", ""),
        "address":         (profile.get("address") or "").split(",")[0].strip(),
        "city":            (profile.get("address") or "").split(",")[0].strip(),
        "linkedIn":        profile.get("linkedin_url", ""),
        "linkedin":        profile.get("linkedin_url", ""),
        "website":         profile.get("portfolio_url", ""),
        "portfolioUrl":    profile.get("portfolio_url", ""),
        "github":          profile.get("github_url", ""),
        "gpa":             profile.get("gpa", ""),
        "graduationDate":  profile.get("graduation_date", ""),
    }

    # Fill by data-automation-id
    for field_key, value in field_map.items():
        if not value:
            continue
        try:
            el = await page.query_selector(
                f"[data-automation-id='{field_key}'] input, "
                f"[data-automation-id='{field_key}']"
            )
            if el:
                tag = (await el.evaluate("el => el.tagName")).lower()
                if tag == "input":
                    await el.fill(value)
                    await _delay(0.2, 0.5)
        except Exception:
            continue

    # ── Generic visible input fields ───────────────────────────────────────────
    inputs = await page.query_selector_all(
        "input:not([type='hidden']):not([type='file']):not([type='checkbox'])"
        ":not([type='radio']):not([type='submit']), textarea"
    )

    for inp in inputs:
        try:
            # Get label text
            inp_id = await inp.get_attribute("id") or ""
            label_text = ""

            if inp_id:
                label = await page.query_selector(f"label[for='{inp_id}']")
                if label:
                    label_text = (await label.inner_text()).lower().strip()

            if not label_text:
                label_text = (
                    await inp.get_attribute("placeholder") or
                    await inp.get_attribute("aria-label") or
                    await inp.get_attribute("name") or ""
                ).lower()

            if not label_text:
                continue

            # Map label to value
            value = _map_label_to_value(label_text, profile, cover_letter)

            if not value:
                # Check learned answers
                value = store.find_learned_answer(label_text) or ""

            if not value and len(label_text) > 8:
                # Generate answer for unknown question
                value = generate_form_answer(label_text, job_title, company, insight)

            if value:
                tag = (await inp.evaluate("el => el.tagName")).lower()
                if tag == "textarea":
                    await inp.fill(str(value))
                else:
                    current = await inp.input_value()
                    if not current:  # Don't overwrite already-filled fields
                        await inp.fill(str(value))
                await _delay(0.2, 0.6)

        except Exception:
            continue

    # ── Dropdowns / selects ────────────────────────────────────────────────────
    selects = await page.query_selector_all("select")
    for sel_el in selects:
        try:
            label_text = ""
            sel_id = await sel_el.get_attribute("id") or ""
            if sel_id:
                label = await page.query_selector(f"label[for='{sel_id}']")
                if label:
                    label_text = (await label.inner_text()).lower()

            if not label_text:
                label_text = (await sel_el.get_attribute("aria-label") or "").lower()

            # Common dropdowns
            if "country" in label_text:
                await sel_el.select_option(label="United States")
            elif "state" in label_text:
                addr = profile.get("address", "")
                state = addr.split(",")[-1].strip() if "," in addr else "IN"
                try:
                    await sel_el.select_option(label=state)
                except Exception:
                    await sel_el.select_option(index=1)
            elif "authorization" in label_text or "sponsor" in label_text:
                try:
                    await sel_el.select_option(label="Yes")
                except Exception:
                    pass
            elif "experience" in label_text or "years" in label_text:
                try:
                    await sel_el.select_option(label="0-1 years")
                except Exception:
                    try:
                        await sel_el.select_option(index=1)
                    except Exception:
                        pass

            await _delay(0.2, 0.5)
        except Exception:
            continue

    # ── Checkboxes (legal agreements, etc.) ───────────────────────────────────
    checkboxes = await page.query_selector_all("input[type='checkbox']")
    for cb in checkboxes:
        try:
            is_checked = await cb.is_checked()
            label_text = ""
            cb_id = await cb.get_attribute("id") or ""
            if cb_id:
                label = await page.query_selector(f"label[for='{cb_id}']")
                if label:
                    label_text = (await label.inner_text()).lower()

            # Auto-check agreement/consent checkboxes
            if not is_checked and any(kw in label_text for kw in [
                "agree", "accept", "consent", "acknowledge", "confirm",
                "terms", "privacy", "certify", "authorize"
            ]):
                await cb.click()
                await _delay(0.2, 0.5)
        except Exception:
            continue

    await _delay(1, 2)

    # ── Check if this is the submit step ──────────────────────────────────────
    for sel_str in SEL["confirmation"]:
        try:
            el = await page.query_selector(sel_str)
            if el:
                print("[Workday] Application submitted successfully!")
                return True
        except Exception:
            continue

    # ── Try submit button first, then next ────────────────────────────────────
    for sel_str in SEL["submit_btn"]:
        try:
            el = await page.query_selector(sel_str)
            if el:
                text = (await el.inner_text()).lower()
                await el.click()
                await _delay(2, 4)

                # Check for confirmation after submit
                for conf_sel in SEL["confirmation"]:
                    try:
                        conf = await page.wait_for_selector(conf_sel, timeout=5000)
                        if conf:
                            print("[Workday] Submitted!")
                            return True
                    except Exception:
                        continue

                if any(w in text for w in ["submit", "send"]):
                    return True
                return False  # Was Next button, continue
        except Exception:
            continue

    # Try Next button
    await _try_selectors(page, SEL["next_btn"], timeout=3000)
    return False


def _map_label_to_value(label: str, profile: dict, cover_letter: str) -> str:
    """Map a form field label to the correct profile value."""
    full_name = profile.get("full_name", "") or ""
    addr      = profile.get("address", "") or ""

    mapping = {
        "first":        full_name.split()[0] if full_name else "",
        "last":         full_name.split()[-1] if full_name else "",
        "name":         full_name,
        "email":        profile.get("email", ""),
        "phone":        profile.get("phone", ""),
        "address":      addr.split(",")[0].strip(),
        "street":       addr.split(",")[0].strip(),
        "city":         addr.split(",")[0].strip(),
        "zip":          "",
        "postal":       "",
        "linkedin":     profile.get("linkedin_url", ""),
        "github":       profile.get("github_url", ""),
        "website":      profile.get("portfolio_url", ""),
        "portfolio":    profile.get("portfolio_url", ""),
        "cover":        cover_letter,
        "letter":       cover_letter,
        "gpa":          profile.get("gpa", ""),
        "graduation":   profile.get("graduation_date", ""),
        "degree":       "Bachelor of Science",
        "major":        "Computer Science",
        "university":   "Purdue University",
        "school":       "Purdue University",
        "authorized":   "Yes",
        "sponsorship":  "No",
        "sponsor":      "No",
        "visa":         "No",
        "salary":       str(profile.get("salary_min", 20)),
        "start":        "May 2026",
        "available":    "May 2026",
        "relocate":     "Yes",
    }

    for key, value in mapping.items():
        if key in label and value:
            return str(value)

    return ""


# ─── Main entry point ─────────────────────────────────────────────────────────

async def fill_workday_application(
    page,
    job_url: str,
    job_title: str,
    company: str,
    insight: dict,
    cover_letter: str,
    app_id: int,
) -> bool:
    """
    Main Workday handler. Called from track_worker when a Workday URL is detected.
    Returns True if application submitted successfully.
    """
    store   = get_store()
    profile = store.get_profile() or {}

    print(f"[Workday] Starting application: {job_title} @ {company}")

    try:
        # Navigate to job
        await page.goto(job_url, wait_until="networkidle", timeout=30000)
        await _delay(2, 3)

        # Click Apply button
        applied = await _try_selectors(page, SEL["apply_btn"], timeout=10000)
        if not applied:
            print("[Workday] Could not find Apply button")
            return False

        await _delay(2, 4)
        print("[Workday] Clicked Apply")

        # Handle account gate
        # Check if we need to create/sign in to an account
        needs_auth = False
        for sel_str in SEL["create_account_link"] + SEL["existing_account_link"]:
            try:
                el = await page.query_selector(sel_str)
                if el and await el.is_visible():
                    needs_auth = True
                    break
            except Exception:
                continue

        if needs_auth:
            # Check if we already have a stored password (means account exists)
            existing_pwd = store.get(WD_PASSWORD_KEY)

            if existing_pwd:
                # Try signing in first
                print("[Workday] Attempting sign in with existing account...")
                signed_in = await _sign_in_workday(page, profile)
                await _delay(2, 3)

                # If sign-in failed, create new account
                current_url = page.url
                still_on_auth = any(kw in current_url.lower() for kw in
                                    ["login", "signin", "sign-in", "auth"])
                if still_on_auth:
                    print("[Workday] Sign in failed, creating new account...")
                    await _create_workday_account(page, profile)
            else:
                await _create_workday_account(page, profile)

            await _delay(2, 4)

        # Fill multi-step form — up to 15 steps
        max_steps = 15
        for step in range(max_steps):
            print(f"[Workday] Filling step {step + 1}...")

            is_done = await _fill_workday_form_step(
                page, profile, insight, cover_letter, job_title, company
            )

            if is_done:
                print(f"[Workday] ✅ Application submitted: {job_title} @ {company}")
                return True

            await _delay(1.5, 3)

            # Check for confirmation after each step
            for conf_sel in SEL["confirmation"]:
                try:
                    el = await page.query_selector(conf_sel)
                    if el:
                        print(f"[Workday] ✅ Confirmed submitted!")
                        return True
                except Exception:
                    continue

        print("[Workday] Reached max steps without confirmation")
        return False

    except Exception as e:
        print(f"[Workday] Error: {e}")
        return False


def is_workday_url(url: str) -> bool:
    """Check if a URL is a Workday ATS URL."""
    if not url:
        return False
    url_lower = url.lower()
    return any(kw in url_lower for kw in [
        "myworkdayjobs.com",
        "workday.com",
        "wd1.myworkdayjobs",
        "wd3.myworkdayjobs",
        "wd5.myworkdayjobs",
    ])
