"""
ui/settings_tab.py
Full settings panel. API keys, OAuth connections, track count,
platform toggles, profile editing. All saved to persistent store.
"""

import json
import subprocess
import sys
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QTextEdit, QSpinBox, QCheckBox, QGroupBox,
    QScrollArea, QFileDialog, QComboBox, QMessageBox, QTabWidget
)
from PyQt6.QtCore import Qt
from core.settings_store import get_store
from core.api_router import get_router

BG      = "#0d1117"
SURFACE = "#161b22"
BORDER  = "#30363d"
ACCENT  = "#58a6ff"
GREEN   = "#3fb950"
RED     = "#f85149"
TEXT    = "#e6edf3"
MUTED   = "#8b949e"

FIELD_STYLE = (
    f"background:{SURFACE}; border:1px solid {BORDER}; border-radius:6px; "
    f"padding:8px 12px; color:{TEXT}; font-size:13px;"
)
GROUP_STYLE = (
    f"QGroupBox {{ border:1px solid {BORDER}; border-radius:8px; margin-top:16px; "
    f"padding-top:16px; color:{MUTED}; font-weight:bold; }} "
    f"QGroupBox::title {{ subcontrol-origin:margin; left:12px; padding:0 4px; }}"
)
BTN_STYLE = (
    f"background:{SURFACE}; border:1px solid {BORDER}; border-radius:6px; "
    f"padding:8px 16px; color:{TEXT};"
)
BTN_ACCENT = (
    f"background:{ACCENT}; border:none; border-radius:6px; "
    f"padding:8px 16px; color:white; font-weight:bold;"
)
BTN_SUCCESS = (
    f"background:{GREEN}; border:none; border-radius:6px; "
    f"padding:8px 16px; color:white; font-weight:bold;"
)


def _label(text, muted=False):
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color: {MUTED if muted else TEXT}; font-size: 13px;")
    return lbl


class APISettingsPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.store = get_store()
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Claude key
        ai_group = QGroupBox("AI API Keys")
        ai_group.setStyleSheet(GROUP_STYLE)
        ag = QVBoxLayout(ai_group)

        ag.addWidget(_label("Claude API Key (recommended):"))
        self.claude_key = QLineEdit()
        self.claude_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.claude_key.setStyleSheet(FIELD_STYLE)
        self.claude_key.setPlaceholderText("sk-ant-...")
        ag.addWidget(self.claude_key)

        ag.addWidget(_label("OR — OpenAI API Key:"))
        self.openai_key = QLineEdit()
        self.openai_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.openai_key.setStyleSheet(FIELD_STYLE)
        self.openai_key.setPlaceholderText("sk-...")
        ag.addWidget(self.openai_key)

        ag.addWidget(_label("GPTZero API Key (optional — free tier: 100 checks/month):"))
        self.gptzero_key = QLineEdit()
        self.gptzero_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.gptzero_key.setStyleSheet(FIELD_STYLE)
        self.gptzero_key.setPlaceholderText("Leave blank to use local check")
        ag.addWidget(self.gptzero_key)

        test_row = QHBoxLayout()
        test_btn = QPushButton("🔌 Test Connection")
        test_btn.setStyleSheet(BTN_STYLE)
        test_btn.clicked.connect(self._test_api)
        self.api_status = _label("", muted=True)
        test_row.addWidget(test_btn)
        test_row.addWidget(self.api_status)
        test_row.addStretch()
        ag.addLayout(test_row)
        layout.addWidget(ai_group)

        # OAuth group
        oauth_group = QGroupBox("OAuth Connections")
        oauth_group.setStyleSheet(GROUP_STYLE)
        og = QVBoxLayout(oauth_group)

        for platform, label_text in [
            ("gmail",    "📧 Gmail (send cold emails + monitor inbox)"),
            ("linkedin", "💼 LinkedIn (Easy Apply + send messages)"),
            ("github",   "🐙 GitHub (auto-handle OAuth in forms)"),
            ("reddit",   "🔺 Reddit (send DMs to contacts)"),
        ]:
            row = QHBoxLayout()
            row.addWidget(_label(label_text))
            row.addStretch()
            status = self.store.get(f"{platform}_token")
            status_lbl = QLabel("✅ Connected" if status else "⚪ Not connected")
            status_lbl.setStyleSheet(f"color: {GREEN if status else MUTED};")
            connect_btn = QPushButton(f"Connect {platform.title()}")
            connect_btn.setStyleSheet(BTN_STYLE)
            connect_btn.clicked.connect(lambda _, p=platform: self._connect_oauth(p))
            row.addWidget(status_lbl)
            row.addWidget(connect_btn)
            og.addLayout(row)

        layout.addWidget(oauth_group)

        # Save button
        save_btn = QPushButton("💾 Save API Settings")
        save_btn.setStyleSheet(BTN_ACCENT)
        save_btn.clicked.connect(self._save)
        layout.addWidget(save_btn)
        layout.addStretch()

        self._load_existing()

    def _load_existing(self):
        store = self.store
        key = store.get("claude_api_key")
        if key:
            self.claude_key.setText("••••••••••••••••")
        key2 = store.get("openai_api_key")
        if key2:
            self.openai_key.setText("••••••••••••••••")

    def _test_api(self):
        self._save()
        try:
            router = get_router()
            # Force reinit
            router._anthropic_client = None
            router._openai_client = None
            success, msg = router.test_connection()
            self.api_status.setText(f"{'✅' if success else '❌'} {msg}")
        except Exception as e:
            self.api_status.setText(f"❌ {e}")

    def _save(self):
        store = self.store
        claude = self.claude_key.text().strip()
        openai = self.openai_key.text().strip()
        gtz = self.gptzero_key.text().strip()
        if claude and "•" not in claude:
            store.set("claude_api_key", claude)
        if openai and "•" not in openai:
            store.set("openai_api_key", openai)
        if gtz and "•" not in gtz:
            store.set("gptzero_api_key", gtz)
        self.api_status.setText("✅ Saved")

    def _connect_oauth(self, platform: str):
        if platform == "gmail":
            QMessageBox.information(self, "Gmail OAuth",
                "To connect Gmail:\n\n"
                "1. Go to console.cloud.google.com\n"
                "2. Create a project → Enable Gmail API\n"
                "3. Create OAuth credentials (Desktop app type)\n"
                "4. Download credentials.json\n"
                "5. Save it to: ~/.autoapplyai/gmail_creds.json\n\n"
                "Then restart AutoApplyAI — it will open a browser to authorize."
            )
        else:
            QMessageBox.information(self, f"{platform.title()} OAuth",
                f"{platform.title()} OAuth setup — coming soon.\n"
                "For now, manual login is used for this platform."
            )


class AutomationSettingsPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.store = get_store()
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Track count
        tracks_group = QGroupBox("Fast Lane Tracks")
        tracks_group.setStyleSheet(GROUP_STYLE)
        tg = QVBoxLayout(tracks_group)
        tg.addWidget(_label("Number of parallel application tracks (each applies to one job simultaneously):"))
        track_row = QHBoxLayout()
        self.track_spin = QSpinBox()
        self.track_spin.setRange(1, 10)
        self.track_spin.setValue(self.store.get("track_count", 2))
        self.track_spin.setStyleSheet(FIELD_STYLE)
        track_row.addWidget(self.track_spin)
        track_row.addWidget(_label("(Start with 2. Increase as budget allows.)", muted=True))
        track_row.addStretch()
        tg.addLayout(track_row)
        layout.addWidget(tracks_group)

        # Platforms
        plat_group = QGroupBox("Discovery Sources")
        plat_group.setStyleSheet(GROUP_STYLE)
        pg = QVBoxLayout(plat_group)

        stored_platforms = self.store.get("platforms", [])
        if isinstance(stored_platforms, str):
            try:
                stored_platforms = json.loads(stored_platforms)
            except Exception:
                stored_platforms = []

        self.platform_checks = {}
        all_platforms = [
            "Google ATS Deep Search", "Indeed RSS Feed", "Handshake Feed",
            "USAJobs Feed", "LinkedIn Public Listings", "Reddit Job Posts",
            "Deep Web Scan", "Startup Boards"
        ]
        for plat in all_platforms:
            cb = QCheckBox(plat)
            cb.setStyleSheet(f"color: {TEXT};")
            cb.setChecked(plat in stored_platforms or not stored_platforms)
            self.platform_checks[plat] = cb
            pg.addWidget(cb)
        layout.addWidget(plat_group)

        # Slow lane
        slow_group = QGroupBox("Slow Lane (Human-Paced Easy Apply)")
        slow_group.setStyleSheet(GROUP_STYLE)
        sg = QVBoxLayout(slow_group)
        stored_slow = self.store.get("slow_platforms", [])
        if isinstance(stored_slow, str):
            try:
                stored_slow = json.loads(stored_slow)
            except Exception:
                stored_slow = []

        self.slow_checks = {}
        for plat in ["LinkedIn Easy Apply", "Indeed Easy Apply"]:
            cb = QCheckBox(plat)
            cb.setStyleSheet(f"color: {TEXT};")
            cb.setChecked(plat in stored_slow or not stored_slow)
            self.slow_checks[plat] = cb
            sg.addWidget(cb)
        layout.addWidget(slow_group)

        # Humanizer threshold
        hum_group = QGroupBox("Humanizer Threshold")
        hum_group.setStyleSheet(GROUP_STYLE)
        hg = QVBoxLayout(hum_group)
        hg.addWidget(_label("Maximum allowed AI detection % (default 75%). Lower = more retries, higher quality."))
        hum_row = QHBoxLayout()
        self.hum_threshold = QSpinBox()
        self.hum_threshold.setRange(40, 95)
        self.hum_threshold.setSuffix("%")
        self.hum_threshold.setValue(int(self.store.get("humanizer_threshold", 75)))
        self.hum_threshold.setStyleSheet(FIELD_STYLE)
        hum_row.addWidget(self.hum_threshold)
        hum_row.addStretch()
        hg.addLayout(hum_row)
        layout.addWidget(hum_group)

        save_btn = QPushButton("💾 Save Automation Settings")
        save_btn.setStyleSheet(BTN_ACCENT)
        save_btn.clicked.connect(self._save)
        layout.addWidget(save_btn)
        layout.addStretch()

    def _save(self):
        store = self.store
        store.set("track_count", self.track_spin.value())
        enabled = [k for k, cb in self.platform_checks.items() if cb.isChecked()]
        store.set("platforms", json.dumps(enabled))
        slow_enabled = [k for k, cb in self.slow_checks.items() if cb.isChecked()]
        store.set("slow_platforms", json.dumps(slow_enabled))
        store.set("humanizer_threshold", self.hum_threshold.value())
        QMessageBox.information(self, "Saved", "Automation settings saved!")


class ProfileEditPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.store = get_store()
        layout = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setSpacing(10)

        profile = self.store.get_profile() or {}
        self.fields = {}

        simple_fields = [
            ("full_name", "Full Name"),
            ("email", "Email"),
            ("phone", "Phone"),
            ("address", "Location"),
            ("linkedin_url", "LinkedIn URL"),
            ("github_url", "GitHub URL"),
            ("portfolio_url", "Portfolio URL"),
            ("graduation_date", "Graduation Date"),
            ("gpa", "GPA"),
            ("salary_min", "Min Salary ($/hr)"),
            ("salary_max", "Max Salary ($/hr)"),
            ("locations", "Target Locations (comma separated)"),
        ]

        for key, label_text in simple_fields:
            cl.addWidget(_label(label_text))
            w = QLineEdit()
            w.setStyleSheet(FIELD_STYLE)
            w.setText(str(profile.get(key) or ""))
            cl.addWidget(w)
            self.fields[key] = w

        for key, label_text in [
            ("background_text", "Full Background (detailed)"),
            ("strengths_text", "Key Strengths"),
            ("dream_criteria", "Dream Job Criteria"),
            ("target_roles", "Target Roles"),
        ]:
            cl.addWidget(_label(label_text))
            w = QTextEdit()
            w.setStyleSheet(FIELD_STYLE)
            w.setPlainText(str(profile.get(key) or ""))
            w.setMinimumHeight(100)
            cl.addWidget(w)
            self.fields[key] = w

        # Resume re-upload
        cl.addWidget(_label("Resume PDF:"))
        resume_row = QHBoxLayout()
        self.resume_label = _label(profile.get("resume_path") or "No resume uploaded", muted=True)
        resume_btn = QPushButton("📄 Change Resume")
        resume_btn.setStyleSheet(BTN_STYLE)
        resume_btn.clicked.connect(self._change_resume)
        resume_row.addWidget(self.resume_label)
        resume_row.addWidget(resume_btn)
        resume_row.addStretch()
        cl.addLayout(resume_row)
        cl.addStretch()

        scroll.setWidget(content)
        layout.addWidget(scroll)

        save_btn = QPushButton("💾 Save Profile")
        save_btn.setStyleSheet(BTN_ACCENT)
        save_btn.clicked.connect(self._save)
        layout.addWidget(save_btn)

    def _change_resume(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Resume PDF", "", "PDF Files (*.pdf)")
        if path:
            self.store.save_profile({"resume_path": path})
            self.resume_label.setText(path)
            from onboarding.resume_parser import parse_resume
            try:
                parsed = parse_resume(path)
                self.store.save_profile({"resume_parsed": json.dumps(parsed)})
            except Exception:
                pass

    def _save(self):
        updates = {}
        for key, widget in self.fields.items():
            if isinstance(widget, QTextEdit):
                updates[key] = widget.toPlainText().strip()
            else:
                updates[key] = widget.text().strip()
        self.store.save_profile(updates)
        QMessageBox.information(self, "Saved", "Profile updated!")


class SettingsTab(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.addWidget(QLabel("⚙️ Settings"))

        inner = QTabWidget()
        inner.addTab(APISettingsPanel(), "🔑 API Keys & OAuth")
        inner.addTab(AutomationSettingsPanel(), "⚙️ Automation")
        inner.addTab(ProfileEditPanel(), "👤 Edit Profile")
        layout.addWidget(inner)
