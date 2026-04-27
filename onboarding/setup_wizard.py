"""
onboarding/setup_wizard.py
First-launch onboarding wizard. Collects everything needed before the app runs.
Multi-step flow. All data saved to SettingsStore permanently.
"""

import sys
import json
from pathlib import Path
from PyQt6.QtWidgets import (
    QWizard, QWizardPage, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QTextEdit, QPushButton, QCheckBox, QFileDialog,
    QComboBox, QSpinBox, QGroupBox, QScrollArea, QWidget,
    QProgressBar, QMessageBox, QSlider, QGridLayout, QFrame
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QFont, QPixmap, QColor, QPalette

from core.settings_store import get_store
from core.api_router import get_router

# ─── Color palette ────────────────────────────────────────────────────────────
BG       = "#0d1117"
SURFACE  = "#161b22"
BORDER   = "#30363d"
ACCENT   = "#58a6ff"
ACCENT2  = "#3fb950"
TEXT     = "#e6edf3"
MUTED    = "#8b949e"
DANGER   = "#f85149"

STYLE = f"""
QWizard, QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-family: 'Segoe UI', 'SF Pro Display', sans-serif;
    font-size: 13px;
}}
QWizardPage {{
    background-color: {BG};
}}
QLineEdit, QTextEdit, QComboBox, QSpinBox {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 8px 12px;
    color: {TEXT};
    font-size: 13px;
}}
QLineEdit:focus, QTextEdit:focus {{
    border: 1px solid {ACCENT};
}}
QPushButton {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 8px 18px;
    color: {TEXT};
    font-size: 13px;
}}
QPushButton:hover {{
    background-color: {ACCENT};
    color: white;
    border: 1px solid {ACCENT};
}}
QPushButton#accent {{
    background-color: {ACCENT};
    border: none;
    color: white;
    font-weight: bold;
}}
QPushButton#accent:hover {{
    background-color: #79c0ff;
}}
QPushButton#success {{
    background-color: {ACCENT2};
    border: none;
    color: white;
    font-weight: bold;
}}
QCheckBox {{
    color: {TEXT};
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 1px solid {BORDER};
    background-color: {SURFACE};
}}
QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border: 1px solid {ACCENT};
}}
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 8px;
    margin-top: 16px;
    padding-top: 16px;
    font-weight: bold;
    color: {MUTED};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 4px;
}}
QLabel#heading {{
    font-size: 20px;
    font-weight: bold;
    color: {TEXT};
}}
QLabel#subheading {{
    font-size: 13px;
    color: {MUTED};
}}
QLabel#accent {{
    color: {ACCENT};
    font-weight: bold;
}}
QScrollArea {{
    border: none;
}}
QProgressBar {{
    border: 1px solid {BORDER};
    border-radius: 4px;
    background-color: {SURFACE};
    height: 6px;
    text-align: center;
}}
QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 4px;
}}
"""


def label(text, style="normal"):
    lbl = QLabel(text)
    if style == "heading":
        lbl.setObjectName("heading")
    elif style == "subheading":
        lbl.setObjectName("subheading")
    elif style == "accent":
        lbl.setObjectName("accent")
    lbl.setWordWrap(True)
    return lbl


def field(placeholder="", password=False, multiline=False, height=80):
    if multiline:
        w = QTextEdit()
        w.setPlaceholderText(placeholder)
        w.setMinimumHeight(height)
        return w
    else:
        w = QLineEdit()
        w.setPlaceholderText(placeholder)
        if password:
            w.setEchoMode(QLineEdit.EchoMode.Password)
        return w


# ─── Page 1: Welcome ──────────────────────────────────────────────────────────
class WelcomePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("")
        layout = QVBoxLayout()
        layout.setSpacing(16)
        layout.setContentsMargins(40, 40, 40, 40)

        layout.addStretch()
        layout.addWidget(label("⚡ AutoApplyAI", "heading"))
        layout.addWidget(label(
            "Your autonomous job application engine.\n"
            "Set up once. Apply everywhere. Indefinitely.",
            "subheading"
        ))
        layout.addSpacing(20)

        info = QFrame()
        info.setStyleSheet(f"background:{SURFACE}; border-radius:8px; padding:16px; border:1px solid {BORDER};")
        info_layout = QVBoxLayout(info)
        for line in [
            "🔍  Finds jobs across the entire internet",
            "🧠  Deep-researches every company before applying",
            "✍️   Writes tailored cover letters and answers",
            "📬  Sends cold emails and follows up automatically",
            "🔄  Runs 24/7 in the background while you work",
        ]:
            info_layout.addWidget(label(line))
        layout.addWidget(info)

        layout.addSpacing(20)
        layout.addWidget(label(
            "This wizard takes about 5 minutes. All data is saved locally "
            "and encrypted. You only do this once.",
            "subheading"
        ))
        layout.addStretch()
        self.setLayout(layout)


# ─── Page 2: Personal Info ────────────────────────────────────────────────────
class PersonalInfoPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Personal Information")
        layout = QVBoxLayout()
        layout.setSpacing(12)
        layout.setContentsMargins(40, 20, 40, 20)

        layout.addWidget(label("Contact Details", "heading"))
        layout.addWidget(label("This is pre-filled into every application form.", "subheading"))
        layout.addSpacing(10)

        grid = QGridLayout()
        grid.setSpacing(10)

        self.name    = field("Full Name")
        self.email   = field("Email Address")
        self.phone   = field("Phone Number")
        self.address = field("City, State (e.g. West Lafayette, IN)")
        self.linkedin = field("LinkedIn URL")
        self.github   = field("GitHub URL")
        self.portfolio = field("Portfolio/Website URL")
        self.grad_date = field("Graduation Date (e.g. May 2026)")
        self.gpa      = field("GPA (optional)")
        self.work_auth = QComboBox()
        self.work_auth.addItems([
            "US Citizen",
            "Permanent Resident",
            "F-1 Student (OPT eligible)",
            "H-1B",
            "Other"
        ])

        rows = [
            ("Full Name *",        self.name),
            ("Email *",            self.email),
            ("Phone *",            self.phone),
            ("Location *",         self.address),
            ("LinkedIn",           self.linkedin),
            ("GitHub",             self.github),
            ("Portfolio URL",      self.portfolio),
            ("Graduation Date *",  self.grad_date),
            ("GPA",                self.gpa),
            ("Work Authorization", self.work_auth),
        ]
        for i, (lbl_text, widget) in enumerate(rows):
            grid.addWidget(QLabel(lbl_text), i, 0)
            grid.addWidget(widget, i, 1)

        layout.addLayout(grid)
        self.setLayout(layout)

    def get_data(self):
        return {
            "full_name":       self.name.text().strip(),
            "email":           self.email.text().strip(),
            "phone":           self.phone.text().strip(),
            "address":         self.address.text().strip(),
            "linkedin_url":    self.linkedin.text().strip(),
            "github_url":      self.github.text().strip(),
            "portfolio_url":   self.portfolio.text().strip(),
            "graduation_date": self.grad_date.text().strip(),
            "gpa":             self.gpa.text().strip(),
            "work_auth":       self.work_auth.currentText(),
        }

    def validatePage(self):
        data = self.get_data()
        if not all([data["full_name"], data["email"], data["phone"]]):
            QMessageBox.warning(self, "Required Fields",
                "Please fill in Name, Email, and Phone.")
            return False
        return True


# ─── Page 3: Resume Upload ────────────────────────────────────────────────────
class ResumePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Upload Your Resume")
        self.resume_path = None
        self.parsed_data = None

        layout = QVBoxLayout()
        layout.setSpacing(12)
        layout.setContentsMargins(40, 20, 40, 20)

        layout.addWidget(label("Resume (PDF)", "heading"))
        layout.addWidget(label(
            "Upload your resume PDF. The AI will parse all information "
            "and use it across every application.",
            "subheading"
        ))
        layout.addSpacing(16)

        self.upload_btn = QPushButton("📄  Choose PDF File")
        self.upload_btn.setObjectName("accent")
        self.upload_btn.setMinimumHeight(44)
        self.upload_btn.clicked.connect(self._pick_file)
        layout.addWidget(self.upload_btn)

        self.file_label = label("No file selected", "subheading")
        layout.addWidget(self.file_label)

        self.parse_btn = QPushButton("🧠  Parse Resume with AI")
        self.parse_btn.setEnabled(False)
        self.parse_btn.clicked.connect(self._parse)
        layout.addWidget(self.parse_btn)

        self.status = label("", "accent")
        layout.addWidget(self.status)

        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("Parsed resume data will appear here...")
        self.preview.setMinimumHeight(200)
        layout.addWidget(self.preview)

        layout.addStretch()
        self.setLayout(layout)

    def _pick_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Resume PDF", "", "PDF Files (*.pdf)"
        )
        if path:
            self.resume_path = path
            self.file_label.setText(f"✅  {Path(path).name}")
            self.parse_btn.setEnabled(True)
            self.status.setText("")

    def _parse(self):
        if not self.resume_path:
            return
        self.parse_btn.setEnabled(False)
        self.status.setText("Parsing... (this takes ~20 seconds)")

        from onboarding.resume_parser import parse_resume, resume_to_summary_text
        try:
            self.parsed_data = parse_resume(self.resume_path)
            summary = resume_to_summary_text(self.parsed_data)
            self.preview.setPlainText(summary)
            self.status.setText("✅  Resume parsed successfully!")
            self.parse_btn.setText("✅  Parsed")
        except Exception as e:
            self.status.setText(f"❌  Error: {e}")
            self.parse_btn.setEnabled(True)

    def validatePage(self):
        if not self.resume_path:
            QMessageBox.warning(self, "Resume Required",
                "Please upload your resume PDF.")
            return False
        if not self.parsed_data:
            reply = QMessageBox.question(
                self, "Skip Parsing?",
                "Resume not parsed yet. Parse it for best results. Skip anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            return reply == QMessageBox.StandardButton.Yes
        return True


# ─── Page 4: Background & Strengths ──────────────────────────────────────────
class BackgroundPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Your Background")
        layout = QVBoxLayout()
        layout.setSpacing(12)
        layout.setContentsMargins(40, 20, 40, 20)

        layout.addWidget(label("Tell the AI everything about you", "heading"))
        layout.addWidget(label(
            "The more detail you provide, the better every application will be. "
            "Write as much as you want — no limit. The AI reads all of it.",
            "subheading"
        ))
        layout.addSpacing(10)

        layout.addWidget(QLabel("Your full background, experience, and story:"))
        self.background = field(
            "Describe your background in detail. Include: your major, relevant "
            "coursework, research experience, projects you're proud of, what "
            "drives you, how you got into CS/AI, any leadership roles, clubs, "
            "hackathons, competitions, internships, side projects, entrepreneurial "
            "work, anything that makes you you...",
            multiline=True, height=160
        )
        layout.addWidget(self.background)

        layout.addWidget(QLabel("Your key strengths and skills (be specific):"))
        self.strengths = field(
            "What are you genuinely good at? Specific technologies, frameworks, "
            "languages, types of problems you solve well, soft skills that are "
            "real, ways you've demonstrated leadership or impact...",
            multiline=True, height=120
        )
        layout.addWidget(self.strengths)

        layout.addWidget(QLabel("Anything else you want on every application?"))
        self.extra = field(
            "Anything specific you want always mentioned, or topics to avoid...",
            multiline=True, height=80
        )
        layout.addWidget(self.extra)

        self.setLayout(layout)

    def get_data(self):
        return {
            "background_text": self.background.toPlainText().strip(),
            "strengths_text":  self.strengths.toPlainText().strip(),
        }

    def validatePage(self):
        if len(self.background.toPlainText().strip()) < 50:
            QMessageBox.warning(self, "More Detail Needed",
                "Please write at least a few sentences about your background.")
            return False
        return True


# ─── Page 5: Job Preferences ─────────────────────────────────────────────────
class PreferencesPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Job Preferences")
        layout = QVBoxLayout()
        layout.setSpacing(12)
        layout.setContentsMargins(40, 20, 40, 20)

        layout.addWidget(label("What are you looking for?", "heading"))
        layout.addWidget(label(
            "The AI uses these to score every job before applying. "
            "Priority matches fill application slots first.",
            "subheading"
        ))
        layout.addSpacing(10)

        # Job types
        jt_group = QGroupBox("Job Types")
        jt_layout = QGridLayout()
        self.job_type_checks = {}
        job_types = [
            "Software Engineering Intern", "AI/ML Engineering Intern",
            "Data Science Intern", "Research Intern", "Product Management Intern",
            "Full-Stack Development Intern", "Backend Engineering Intern",
            "Business/Economics Intern", "Startup (any role)",
            "Fellowship", "Full-Time Entry Level"
        ]
        for i, jt in enumerate(job_types):
            cb = QCheckBox(jt)
            cb.setChecked(jt in ["Software Engineering Intern", "AI/ML Engineering Intern"])
            self.job_type_checks[jt] = cb
            jt_layout.addWidget(cb, i // 2, i % 2)
        jt_group.setLayout(jt_layout)
        layout.addWidget(jt_group)

        # Locations
        layout.addWidget(QLabel("Preferred Locations (comma separated):"))
        self.locations = field("Chicago IL, Dallas TX, Remote, West Lafayette IN")
        self.locations.setText("Chicago IL, Dallas TX, Remote")
        layout.addWidget(self.locations)

        # Salary
        sal_layout = QHBoxLayout()
        sal_layout.addWidget(QLabel("Pay range ($/hr):"))
        self.sal_min = QSpinBox()
        self.sal_min.setRange(0, 200)
        self.sal_min.setValue(20)
        self.sal_max = QSpinBox()
        self.sal_max.setRange(0, 300)
        self.sal_max.setValue(35)
        sal_layout.addWidget(self.sal_min)
        sal_layout.addWidget(QLabel("to"))
        sal_layout.addWidget(self.sal_max)
        sal_layout.addStretch()
        layout.addLayout(sal_layout)

        # Dream criteria
        layout.addWidget(QLabel("Dream job criteria (the AI prioritizes these):"))
        self.dream = field(
            "e.g. Near Chicago or Dallas, housing stipend included, "
            "top-tier AI/ML company, pays $25+/hr, strong mentorship...",
            multiline=True, height=80
        )
        self.dream.setPlainText(
            "Near Chicago or Dallas preferred. Housing included is a big plus. "
            "AI/ML focus company. $20-35/hr. Strong learning environment."
        )
        layout.addWidget(self.dream)

        # Work style
        ws_layout = QHBoxLayout()
        ws_layout.addWidget(QLabel("Work style:"))
        self.work_style = QComboBox()
        self.work_style.addItems(["Any", "Remote preferred", "Hybrid preferred", "On-site preferred"])
        ws_layout.addWidget(self.work_style)
        ws_layout.addStretch()
        layout.addLayout(ws_layout)

        self.setLayout(layout)

    def get_data(self):
        selected_types = [jt for jt, cb in self.job_type_checks.items() if cb.isChecked()]
        return {
            "job_types":       json.dumps(selected_types),
            "locations":       self.locations.text().strip(),
            "salary_min":      self.sal_min.value(),
            "salary_max":      self.sal_max.value(),
            "dream_criteria":  self.dream.toPlainText().strip(),
            "target_roles":    ", ".join(selected_types),
        }


# ─── Page 6: Platforms ────────────────────────────────────────────────────────
class PlatformsPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Platforms & Discovery")
        layout = QVBoxLayout()
        layout.setSpacing(12)
        layout.setContentsMargins(40, 20, 40, 20)

        layout.addWidget(label("Where should AutoApplyAI look?", "heading"))
        layout.addWidget(label(
            "Fast Lane: direct ATS applications (bulk, unlimited, no account needed).\n"
            "Slow Lane: platform Easy Apply (logged in, human-paced).",
            "subheading"
        ))
        layout.addSpacing(10)

        disc_group = QGroupBox("Discovery Sources (Fast Lane)")
        disc_layout = QVBoxLayout()
        self.disc_checks = {}
        sources = [
            ("Google ATS Deep Search", True,
             "Finds Greenhouse/Lever/Workday links via Google — the main bulk source"),
            ("Indeed RSS Feed", True, "Indeed job feed — no scraping, just feed parsing"),
            ("Handshake Feed", True, "Best for internships and campus recruiting"),
            ("USAJobs Feed", False, "Government and federal research positions"),
            ("LinkedIn Public Listings", True, "Public job listings without needing login"),
            ("Reddit Job Posts", True, "r/forhire, r/cscareerquestions, r/MachineLearning"),
            ("Deep Web Scan", True, "Broad Google queries across all platforms and forums"),
            ("Startup Boards", True, "YC, AngelList, Wellfound — early-stage startups"),
        ]
        for key, default, desc in sources:
            cb = QCheckBox(f"{key}  —  {desc}")
            cb.setChecked(default)
            self.disc_checks[key] = cb
            disc_layout.addWidget(cb)
        disc_group.setLayout(disc_layout)
        layout.addWidget(disc_group)

        slow_group = QGroupBox("Slow Lane (Human-Paced Easy Apply)")
        slow_layout = QVBoxLayout()
        self.slow_checks = {}
        slow_sources = [
            ("LinkedIn Easy Apply", True,
             "Logged in — human-paced, 5-20 min delays between apps"),
            ("Indeed Easy Apply", True, "Same approach — slower and staggered"),
        ]
        for key, default, desc in slow_sources:
            cb = QCheckBox(f"{key}  —  {desc}")
            cb.setChecked(default)
            self.slow_checks[key] = cb
            slow_layout.addWidget(cb)
        slow_group.setLayout(slow_layout)
        layout.addWidget(slow_group)

        # Track count
        track_layout = QHBoxLayout()
        track_layout.addWidget(QLabel("Parallel fast-lane tracks:"))
        self.track_count = QSpinBox()
        self.track_count.setRange(1, 8)
        self.track_count.setValue(2)
        self.track_count.setToolTip("Each track applies to one job simultaneously. 2 recommended to start.")
        track_layout.addWidget(self.track_count)
        track_layout.addWidget(label(" (start with 2, increase after testing)", "subheading"))
        track_layout.addStretch()
        layout.addLayout(track_layout)

        self.setLayout(layout)

    def get_data(self):
        enabled_sources = [k for k, cb in self.disc_checks.items() if cb.isChecked()]
        enabled_slow = [k for k, cb in self.slow_checks.items() if cb.isChecked()]
        return {
            "platforms":     json.dumps(enabled_sources),
            "slow_platforms": json.dumps(enabled_slow),
            "track_count":   self.track_count.value(),
        }


# ─── Page 7: API Keys & OAuth ─────────────────────────────────────────────────
class APIKeysPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("API Keys & Connections")
        layout = QVBoxLayout()
        layout.setSpacing(12)
        layout.setContentsMargins(40, 20, 40, 20)

        layout.addWidget(label("Connect the AI and your accounts", "heading"))
        layout.addWidget(label(
            "API keys are encrypted and stored only on your machine.",
            "subheading"
        ))
        layout.addSpacing(10)

        # AI key
        ai_group = QGroupBox("AI API Key (required — pick one)")
        ai_layout = QVBoxLayout()
        ai_layout.addWidget(label(
            "Claude API (recommended): console.anthropic.com\n"
            "OpenAI API (alternative): platform.openai.com",
            "subheading"
        ))
        self.claude_key = field("Claude API Key (sk-ant-...)", password=True)
        self.openai_key = field("OpenAI API Key (sk-...)", password=True)
        self.test_btn = QPushButton("🔌  Test Connection")
        self.test_btn.clicked.connect(self._test_api)
        self.api_status = label("", "subheading")
        ai_layout.addWidget(QLabel("Claude API Key:"))
        ai_layout.addWidget(self.claude_key)
        ai_layout.addWidget(QLabel("OR — OpenAI API Key:"))
        ai_layout.addWidget(self.openai_key)
        ai_layout.addWidget(self.test_btn)
        ai_layout.addWidget(self.api_status)
        ai_group.setLayout(ai_layout)
        layout.addWidget(ai_group)

        # GPTZero
        gz_group = QGroupBox("GPTZero API (optional — humanizer check)")
        gz_layout = QVBoxLayout()
        gz_layout.addWidget(label(
            "Free tier: 100 checks/month. Get key at gptzero.me\n"
            "Leave blank to use local humanizer only.",
            "subheading"
        ))
        self.gptzero_key = field("GPTZero API Key (optional)", password=True)
        gz_layout.addWidget(self.gptzero_key)
        gz_group.setLayout(gz_layout)
        layout.addWidget(gz_group)

        # OAuth info
        oauth_group = QGroupBox("Account Connections (set up after onboarding)")
        oauth_layout = QVBoxLayout()
        oauth_layout.addWidget(label(
            "Gmail, LinkedIn, GitHub, and Reddit OAuth will be set up in Settings "
            "after this wizard. You can start applying immediately — OAuth just "
            "adds cold email sending and platform messaging.",
            "subheading"
        ))
        oauth_group.setLayout(oauth_layout)
        layout.addWidget(oauth_group)

        layout.addStretch()
        self.setLayout(layout)

    def _test_api(self):
        store = get_store()
        claude_key = self.claude_key.text().strip()
        openai_key = self.openai_key.text().strip()

        if claude_key:
            store.set("claude_api_key", claude_key)
        elif openai_key:
            store.set("openai_api_key", openai_key)
        else:
            self.api_status.setText("❌  Enter at least one API key.")
            return

        self.api_status.setText("Testing...")
        try:
            router = get_router()
            success, msg = router.test_connection()
            self.api_status.setText(f"✅  {msg}" if success else f"❌  {msg}")
        except Exception as e:
            self.api_status.setText(f"❌  {e}")

    def get_data(self):
        return {
            "claude_api_key":  self.claude_key.text().strip(),
            "openai_api_key":  self.openai_key.text().strip(),
            "gptzero_api_key": self.gptzero_key.text().strip(),
        }

    def validatePage(self):
        d = self.get_data()
        if not d["claude_api_key"] and not d["openai_api_key"]:
            QMessageBox.warning(self, "API Key Required",
                "Please enter at least one AI API key.")
            return False
        return True


# ─── Page 8: Common Q&A ──────────────────────────────────────────────────────
class CommonQAPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Pre-Set Answers")
        layout = QVBoxLayout()
        layout.setSpacing(10)
        layout.setContentsMargins(40, 20, 40, 20)

        layout.addWidget(label("Common application questions", "heading"))
        layout.addWidget(label(
            "These answers are pre-filled whenever these questions appear. "
            "The AI adapts them to context but uses these as the source.",
            "subheading"
        ))
        layout.addSpacing(8)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setSpacing(10)

        self.answers = {}
        questions = [
            ("visa_sponsorship",
             "Do you require visa sponsorship now or in the future?",
             "No, I do not require visa sponsorship."),
            ("us_authorized",
             "Are you legally authorized to work in the US?",
             "Yes, I am legally authorized to work in the United States."),
            ("18_plus",
             "Are you 18 years of age or older?",
             "Yes."),
            ("references_available",
             "Are references available upon request?",
             "Yes, references available upon request."),
            ("start_date",
             "When is your earliest available start date?",
             "I am available to start May 2025 or earlier if needed."),
            ("relocate",
             "Are you willing to relocate?",
             "Yes, I am open to relocation for the right opportunity."),
            ("why_you",
             "Why should we hire you? / What makes you a good fit?",
             ""),
            ("greatest_strength",
             "What is your greatest strength?",
             ""),
            ("career_goals",
             "What are your career goals?",
             ""),
        ]
        for key, q, default in questions:
            cl.addWidget(QLabel(q))
            w = QTextEdit()
            w.setPlaceholderText("Your answer...")
            w.setPlainText(default)
            w.setMinimumHeight(60)
            w.setMaximumHeight(100)
            self.answers[key] = w
            cl.addWidget(w)

        scroll.setWidget(content)
        layout.addWidget(scroll)
        self.setLayout(layout)

    def get_data(self):
        return {k: w.toPlainText().strip() for k, w in self.answers.items()}


# ─── Page 9: Done ─────────────────────────────────────────────────────────────
class FinishPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("")
        layout = QVBoxLayout()
        layout.setSpacing(16)
        layout.setContentsMargins(40, 40, 40, 40)

        layout.addStretch()
        layout.addWidget(label("🚀  You're all set!", "heading"))
        layout.addWidget(label(
            "AutoApplyAI is ready to start finding and applying to jobs for you.",
            "subheading"
        ))
        layout.addSpacing(20)

        info = QFrame()
        info.setStyleSheet(f"background:{SURFACE}; border-radius:8px; padding:16px; border:1px solid {BORDER};")
        il = QVBoxLayout(info)
        for line in [
            "✅  Profile saved and encrypted locally",
            "✅  Job preferences configured",
            "✅  AI API connected",
            "➡️   Connect Gmail and LinkedIn in Settings for full automation",
            "➡️   Click Start in the dashboard to begin applying",
        ]:
            il.addWidget(label(line))
        layout.addWidget(info)
        layout.addStretch()
        self.setLayout(layout)


# ─── Main Wizard ──────────────────────────────────────────────────────────────
class SetupWizard(QWizard):
    setup_complete = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.store = get_store()
        self.setWindowTitle("AutoApplyAI — Setup")
        self.setMinimumSize(700, 600)
        self.setStyleSheet(STYLE)
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage)

        self.welcome  = WelcomePage()
        self.personal = PersonalInfoPage()
        self.resume   = ResumePage()
        self.background = BackgroundPage()
        self.prefs    = PreferencesPage()
        self.platforms = PlatformsPage()
        self.api_keys = APIKeysPage()
        self.qa_page  = CommonQAPage()
        self.finish_p = FinishPage()

        for page in [self.welcome, self.personal, self.resume, self.background,
                     self.prefs, self.platforms, self.api_keys, self.qa_page,
                     self.finish_p]:
            self.addPage(page)

        self.finished.connect(self._on_finish)

    def _on_finish(self, result):
        if result != QWizard.DialogCode.Accepted:
            return

        store = self.store
        router = get_router()

        # Save API keys first (needed by router)
        api_data = self.api_keys.get_data()
        if api_data["claude_api_key"]:
            store.set("claude_api_key", api_data["claude_api_key"])
        if api_data["openai_api_key"]:
            store.set("openai_api_key", api_data["openai_api_key"])
        if api_data["gptzero_api_key"]:
            store.set("gptzero_api_key", api_data["gptzero_api_key"])

        # Save profile
        profile = {}
        profile.update(self.personal.get_data())
        profile.update(self.background.get_data())
        profile.update(self.prefs.get_data())

        plat_data = self.platforms.get_data()
        profile["platforms"] = plat_data["platforms"]
        store.set("slow_platforms", plat_data["slow_platforms"])
        store.set("track_count", plat_data["track_count"])

        # Attach resume
        if self.resume.resume_path:
            profile["resume_path"] = self.resume.resume_path
        if self.resume.parsed_data:
            profile["resume_parsed"] = json.dumps(self.resume.parsed_data)

        store.save_profile(profile)

        # Save common Q&A as learned answers
        for key, answer in self.qa_page.get_data().items():
            if answer:
                store.save_learned_answer(key, answer, tags=["pre-set"])

        store.mark_onboarded()
        self.setup_complete.emit()
