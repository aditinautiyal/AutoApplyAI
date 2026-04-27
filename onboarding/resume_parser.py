"""
onboarding/resume_parser.py
Parses PDF resume into structured profile data.
Uses pdfplumber for text extraction, Claude to structure it.
"""

import json
import re
from pathlib import Path
from typing import Optional
from core.api_router import get_router


def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract raw text from PDF resume."""
    try:
        import pdfplumber
        text_parts = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    text_parts.append(text)
        return "\n".join(text_parts)
    except Exception as e:
        raise RuntimeError(f"Failed to read PDF: {e}")


def parse_resume(pdf_path: str) -> dict:
    """
    Full resume parse. Returns structured dict with all extractable info.
    Uses Claude to structure the raw text intelligently.
    """
    raw_text = extract_text_from_pdf(pdf_path)
    if not raw_text.strip():
        raise RuntimeError("Could not extract text from PDF. Try a non-scanned PDF.")

    router = get_router()

    system = """You are a resume parser. Extract ALL information from the resume text 
    and return ONLY valid JSON with no markdown, no explanation."""

    prompt = f"""Parse this resume and return a JSON object with these exact keys:
{{
  "full_name": "",
  "email": "",
  "phone": "",
  "address": "",
  "linkedin_url": "",
  "github_url": "",
  "portfolio_url": "",
  "graduation_date": "",
  "gpa": "",
  "university": "",
  "major": "",
  "work_experience": [
    {{
      "company": "",
      "title": "",
      "start_date": "",
      "end_date": "",
      "description": "",
      "skills_used": []
    }}
  ],
  "education": [
    {{
      "institution": "",
      "degree": "",
      "field": "",
      "date": "",
      "gpa": ""
    }}
  ],
  "skills": [],
  "projects": [
    {{
      "name": "",
      "description": "",
      "technologies": [],
      "url": ""
    }}
  ],
  "certifications": [],
  "awards": [],
  "summary": ""
}}

Resume text:
{raw_text}"""

    try:
        response = router.complete(prompt, system=system, smart=False, max_tokens=3000)
        # Strip any accidental markdown
        response = response.strip()
        if response.startswith("```"):
            response = re.sub(r"```[a-z]*\n?", "", response).strip().rstrip("```")
        parsed = json.loads(response)
        parsed["raw_text"] = raw_text
        parsed["source_path"] = str(pdf_path)
        return parsed
    except json.JSONDecodeError:
        # Fallback: return raw text with basic regex extraction
        return _fallback_parse(raw_text, pdf_path)


def _fallback_parse(raw_text: str, pdf_path: str) -> dict:
    """Basic regex fallback if AI parsing fails."""
    email_match = re.search(r'[\w.+-]+@[\w-]+\.\w+', raw_text)
    phone_match = re.search(r'[\+\(]?[0-9][0-9\s\-\(\)]{7,}[0-9]', raw_text)

    return {
        "full_name": "",
        "email": email_match.group() if email_match else "",
        "phone": phone_match.group() if phone_match else "",
        "address": "",
        "skills": [],
        "work_experience": [],
        "education": [],
        "projects": [],
        "raw_text": raw_text,
        "source_path": str(pdf_path),
        "parse_method": "fallback"
    }


def resume_to_summary_text(parsed: dict) -> str:
    """
    Convert parsed resume to a rich text summary for use in AI prompts.
    Keeps token usage efficient while preserving all key info.
    """
    parts = []

    if parsed.get("full_name"):
        parts.append(f"Name: {parsed['full_name']}")
    if parsed.get("email"):
        parts.append(f"Email: {parsed['email']}")
    if parsed.get("university"):
        gpa = f" (GPA: {parsed['gpa']})" if parsed.get("gpa") else ""
        parts.append(f"Education: {parsed['university']} — {parsed.get('major','')}{gpa}, graduating {parsed.get('graduation_date','')}")

    if parsed.get("work_experience"):
        parts.append("Work Experience:")
        for exp in parsed["work_experience"]:
            parts.append(f"  - {exp.get('title','')} at {exp.get('company','')} ({exp.get('start_date','')} to {exp.get('end_date','')}): {exp.get('description','')}")

    if parsed.get("projects"):
        parts.append("Projects:")
        for p in parsed["projects"]:
            tech = ", ".join(p.get("technologies", []))
            parts.append(f"  - {p.get('name','')}: {p.get('description','')} [{tech}]")

    if parsed.get("skills"):
        parts.append(f"Skills: {', '.join(parsed['skills'])}")

    if parsed.get("certifications"):
        parts.append(f"Certifications: {', '.join(parsed['certifications'])}")

    return "\n".join(parts)
