"""
ui/extra_effort_tab.py
Shows people to contact for each application.
Sorted by priority/flags. One-click copy or auto-send where possible.
"""

import json
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QTextEdit, QComboBox, QApplication
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont
from core.settings_store import get_store

BG      = "#0d1117"
SURFACE = "#161b22"
BORDER  = "#30363d"
ACCENT  = "#58a6ff"
GREEN   = "#3fb950"
YELLOW  = "#d29922"
RED     = "#f85149"
TEXT    = "#e6edf3"
MUTED   = "#8b949e"

FLAG_COLORS = {
    "🔴": "#f85149",
    "🟠": "#e3834e",
    "🟡": "#d29922",
    "🟢": "#3fb950",
    "🔵": "#58a6ff",
    "⭐": "#ffd700",
    "💬": "#a78bfa",
}


class ContactCard(QFrame):
    def __init__(self, contact: dict):
        super().__init__()
        self.contact = contact
        self._build()

    def _build(self):
        flags_raw = self.contact.get("flags", "[]")
        flag_labels_raw = self.contact.get("flag_labels", "[]")
        try:
            flags = json.loads(flags_raw)
            flag_labels = json.loads(flag_labels_raw)
        except Exception:
            flags = []
            flag_labels = []

        priority = self.contact.get("priority_score", 0)
        border_color = YELLOW if priority > 0.6 else BORDER
        if "⭐" in flags:
            border_color = "#ffd700"

        self.setStyleSheet(f"""
            QFrame {{
                background: {SURFACE};
                border: 1px solid {border_color};
                border-radius: 8px;
                padding: 4px;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(16, 12, 16, 12)

        # Header row
        header = QHBoxLayout()
        name_lbl = QLabel(self.contact.get("person_name", "Unknown"))
        name_lbl.setStyleSheet(f"font-weight: bold; font-size: 14px; color: {TEXT};")
        header.addWidget(name_lbl)

        priority_lbl = QLabel(f"Priority: {priority:.0%}")
        priority_lbl.setStyleSheet(f"color: {YELLOW}; font-size: 12px;")
        header.addStretch()
        header.addWidget(priority_lbl)
        layout.addLayout(header)

        # Title and company
        title_lbl = QLabel(
            f"{self.contact.get('person_title', '')} — {self.contact.get('company_name', '')}"
        )
        title_lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        layout.addWidget(title_lbl)

        # Platform and URL
        platform = self.contact.get("platform", "")
        url = self.contact.get("profile_url", "")
        if url:
            url_lbl = QLabel(f"📍 {platform.title()}: {url[:60]}...")
            url_lbl.setStyleSheet(f"color: {ACCENT}; font-size: 11px;")
            layout.addWidget(url_lbl)

        # Flags
        if flags:
            flags_layout = QHBoxLayout()
            for flag, label in zip(flags, flag_labels):
                flag_lbl = QLabel(f"{flag} {label}")
                color = FLAG_COLORS.get(flag, TEXT)
                flag_lbl.setStyleSheet(
                    f"color: {color}; font-size: 11px; "
                    f"background: {BG}; border-radius: 4px; padding: 2px 6px;"
                )
                flags_layout.addWidget(flag_lbl)
            flags_layout.addStretch()
            layout.addLayout(flags_layout)

        # Draft message
        msg = self.contact.get("draft_message", "")
        if msg:
            msg_label = QLabel("Draft Message:")
            msg_label.setStyleSheet(f"color: {MUTED}; font-size: 11px; margin-top: 4px;")
            layout.addWidget(msg_label)

            msg_display = QTextEdit()
            msg_display.setPlainText(msg)
            msg_display.setReadOnly(True)
            msg_display.setMaximumHeight(80)
            msg_display.setStyleSheet(
                f"background: {BG}; border: 1px solid {BORDER}; "
                f"border-radius: 4px; font-size: 12px; color: {TEXT}; padding: 6px;"
            )
            layout.addWidget(msg_display)

        # Action buttons
        btn_row = QHBoxLayout()
        sent = self.contact.get("sent", 0)

        if sent:
            sent_lbl = QLabel("✅ Message sent")
            sent_lbl.setStyleSheet(f"color: {GREEN}; font-size: 12px;")
            btn_row.addWidget(sent_lbl)
        else:
            copy_btn = QPushButton("📋 Copy Message")
            copy_btn.setStyleSheet(
                f"background: {SURFACE}; border: 1px solid {BORDER}; "
                f"border-radius: 4px; padding: 6px 12px; color: {TEXT}; font-size: 12px;"
            )
            copy_btn.clicked.connect(lambda: self._copy_message(msg))
            btn_row.addWidget(copy_btn)

            open_btn = QPushButton("🔗 Open Profile")
            open_btn.setStyleSheet(
                f"background: {SURFACE}; border: 1px solid {ACCENT}; "
                f"border-radius: 4px; padding: 6px 12px; color: {ACCENT}; font-size: 12px;"
            )
            if url:
                open_btn.clicked.connect(lambda: self._open_url(url))
            btn_row.addWidget(open_btn)

        btn_row.addStretch()
        layout.addLayout(btn_row)

    def _copy_message(self, msg: str):
        QApplication.clipboard().setText(msg)

    def _open_url(self, url: str):
        import webbrowser
        webbrowser.open(url)


class ExtraEffortTab(QWidget):
    def __init__(self):
        super().__init__()
        self.store = get_store()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        # Header
        layout.addWidget(QLabel("🎯 Extra Effort — People to Contact"))
        layout.addWidget(QLabel(
            "These contacts can boost your application. Highest priority at top. "
            "Auto-sent where possible — draft only for personal accounts."
        ))

        # Filter bar
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter:"))
        self.filter_combo = QComboBox()
        self.filter_combo.addItems([
            "All contacts", "Not yet sent", "Sent", "High priority (⭐)",
            "Same school (🟠)", "Active recruiter (🔵)"
        ])
        self.filter_combo.currentTextChanged.connect(self._load)
        filter_row.addWidget(self.filter_combo)

        refresh_btn = QPushButton("🔄 Refresh")
        refresh_btn.clicked.connect(self._load)
        filter_row.addWidget(refresh_btn)
        filter_row.addStretch()
        layout.addLayout(filter_row)

        # Scroll area for contact cards
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setSpacing(10)
        self.scroll.setWidget(self.content)
        layout.addWidget(self.scroll)

        self._load()

        # Auto-refresh every 30s
        self.timer = QTimer()
        self.timer.timeout.connect(self._load)
        self.timer.start(30000)

    def _load(self):
        # Clear
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        filter_text = self.filter_combo.currentText()
        show_sent = "Sent" in filter_text

        contacts = self.store.get_contacts(sent=show_sent)

        # Apply additional filters
        if "High priority" in filter_text:
            contacts = [c for c in contacts if c.get("priority_score", 0) >= 0.6]
        elif "Same school" in filter_text:
            contacts = [c for c in contacts if "🟠" in (c.get("flags") or "")]
        elif "Active recruiter" in filter_text:
            contacts = [c for c in contacts if "🔵" in (c.get("flags") or "")]
        elif "Not yet sent" in filter_text:
            contacts = [c for c in contacts if not c.get("sent")]

        if not contacts:
            empty = QLabel("No contacts found for this filter.")
            empty.setStyleSheet(f"color: {MUTED}; padding: 20px;")
            self.content_layout.addWidget(empty)
        else:
            for contact in contacts[:50]:
                card = ContactCard(contact)
                self.content_layout.addWidget(card)

        self.content_layout.addStretch()
