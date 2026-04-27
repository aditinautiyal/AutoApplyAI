"""
ui/approval_dialog.py
Manual approval dialog shown before every application submission.
Shows: job details, company research summary, cover letter, planned form answers.
User can Approve, Edit cover letter, or Skip.
Only appears when review_mode is enabled in settings.
"""

import json
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTextEdit, QScrollArea, QWidget, QFrame, QTabWidget,
    QSplitter
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont, QColor

BG      = "#0d1117"
SURFACE = "#161b22"
BORDER  = "#30363d"
ACCENT  = "#58a6ff"
GREEN   = "#3fb950"
YELLOW  = "#d29922"
RED     = "#f85149"
TEXT    = "#e6edf3"
MUTED   = "#8b949e"

STYLE = f"""
QDialog, QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-family: 'Segoe UI', sans-serif;
    font-size: 13px;
}}
QTextEdit, QLabel {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 8px;
}}
QPushButton {{
    border-radius: 6px;
    padding: 10px 24px;
    font-weight: bold;
    font-size: 13px;
}}
QTabWidget::pane {{
    border: 1px solid {BORDER};
    background: {SURFACE};
}}
QTabBar::tab {{
    background: {BG};
    color: {MUTED};
    padding: 8px 16px;
    border: none;
}}
QTabBar::tab:selected {{
    color: {TEXT};
    border-bottom: 2px solid {ACCENT};
}}
QScrollArea {{ border: none; background: {BG}; }}
"""


class ApprovalDialog(QDialog):
    """
    Blocking dialog that shows one application for review.
    Returns: 'approve', 'skip', or 'stop'
    Also returns edited cover letter if user made changes.
    """

    def __init__(self, job_data: dict, insight: dict,
                 cover_letter: str, parent=None):
        super().__init__(parent)
        self.job_data = job_data
        self.insight = insight
        self.result_action = "skip"
        self.final_cover_letter = cover_letter

        self.setWindowTitle(
            f"Review Application — {job_data.get('title', '')} @ {job_data.get('company', '')}"
        )
        self.setMinimumSize(900, 650)
        self.setStyleSheet(STYLE)
        self.setModal(True)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.raise_()
        self._build(cover_letter)

    def _build(self, cover_letter: str):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        # ── Header ──────────────────────────────────────────────
        header = QFrame()
        header.setStyleSheet(
            f"background:{SURFACE}; border:1px solid {BORDER}; border-radius:8px; padding:4px;"
        )
        hl = QVBoxLayout(header)
        hl.setContentsMargins(16, 12, 16, 12)

        title_lbl = QLabel(
            f"{self.job_data.get('title', 'Unknown Role')} at "
            f"{self.job_data.get('company', 'Unknown Company')}"
        )
        title_lbl.setStyleSheet(f"font-size:16px; font-weight:bold; color:{TEXT}; border:none; padding:0;")
        hl.addWidget(title_lbl)

        meta_parts = []
        if self.job_data.get("location"):
            meta_parts.append(f"📍 {self.job_data['location']}")
        if self.job_data.get("platform"):
            meta_parts.append(f"🔗 {self.job_data['platform']}")
        if self.job_data.get("score"):
            meta_parts.append(f"⭐ Fit score: {self.job_data['score']:.1f}/10")
        if meta_parts:
            meta_lbl = QLabel("   •   ".join(meta_parts))
            meta_lbl.setStyleSheet(f"color:{MUTED}; font-size:12px; border:none; padding:0;")
            hl.addWidget(meta_lbl)

        url = self.job_data.get("url") or self.job_data.get("ats_url", "")
        if url:
            url_lbl = QLabel(f"🌐 {url[:80]}")
            url_lbl.setStyleSheet(f"color:{ACCENT}; font-size:11px; border:none; padding:0;")
            url_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            hl.addWidget(url_lbl)

        layout.addWidget(header)

        # ── Tabs: Cover Letter / Research / Job Description ──────
        tabs = QTabWidget()

        # Cover Letter tab (editable)
        cl_widget = QWidget()
        cl_layout = QVBoxLayout(cl_widget)
        cl_layout.setContentsMargins(0, 8, 0, 0)

        cl_note = QLabel(
            "✏️  You can edit the cover letter before approving. "
            "Changes are saved and used for this application."
        )
        cl_note.setStyleSheet(f"color:{YELLOW}; font-size:12px; border:none; padding:4px 0;")
        cl_layout.addWidget(cl_note)

        self.cover_letter_edit = QTextEdit()
        self.cover_letter_edit.setPlainText(cover_letter)
        self.cover_letter_edit.setMinimumHeight(300)
        cl_layout.addWidget(self.cover_letter_edit)
        tabs.addTab(cl_widget, "✍️  Cover Letter")

        # Research tab
        research_widget = QWidget()
        rl = QVBoxLayout(research_widget)
        rl.setContentsMargins(0, 8, 0, 0)

        research_text = QTextEdit()
        research_text.setReadOnly(True)
        research_text.setPlainText(self._format_research())
        rl.addWidget(research_text)
        tabs.addTab(research_widget, "🔍  Company Research")

        # Job description tab
        jd_widget = QWidget()
        jdl = QVBoxLayout(jd_widget)
        jdl.setContentsMargins(0, 8, 0, 0)

        jd_text = QTextEdit()
        jd_text.setReadOnly(True)
        jd_text.setPlainText(
            self.job_data.get("description", "No description available.")
        )
        jdl.addWidget(jd_text)
        tabs.addTab(jd_widget, "📄  Job Description")

        layout.addWidget(tabs)

        # ── AI score indicator ────────────────────────────────────
        ai_score = self.job_data.get("ai_score", None)
        if ai_score is not None:
            score_color = GREEN if ai_score <= 0.6 else YELLOW if ai_score <= 0.75 else RED
            score_lbl = QLabel(
                f"🤖 Humanizer score: {ai_score:.0%} AI detected  "
                f"({'✅ passes threshold' if ai_score <= 0.75 else '⚠️ above threshold but proceeding'})"
            )
            score_lbl.setStyleSheet(
                f"color:{score_color}; font-size:12px; border:none; padding:0;"
            )
            layout.addWidget(score_lbl)

        # ── Action buttons ────────────────────────────────────────
        btn_row = QHBoxLayout()

        skip_btn = QPushButton("⏭  Skip This Job")
        skip_btn.setStyleSheet(
            f"background:{SURFACE}; border:1px solid {BORDER}; color:{MUTED};"
        )
        skip_btn.clicked.connect(self._skip)

        stop_btn = QPushButton("⏹  Stop Applying")
        stop_btn.setStyleSheet(
            f"background:{RED}; border:none; color:white;"
        )
        stop_btn.clicked.connect(self._stop)

        approve_btn = QPushButton("✅  Approve & Submit")
        approve_btn.setStyleSheet(
            f"background:{GREEN}; border:none; color:white;"
        )
        approve_btn.setMinimumWidth(180)
        approve_btn.clicked.connect(self._approve)

        btn_row.addWidget(stop_btn)
        btn_row.addWidget(skip_btn)
        btn_row.addStretch()

        counter_lbl = QLabel(
            "Review carefully — this will be submitted to the company."
        )
        counter_lbl.setStyleSheet(f"color:{MUTED}; font-size:11px; border:none; padding:0;")
        btn_row.addWidget(counter_lbl)
        btn_row.addWidget(approve_btn)

        layout.addLayout(btn_row)

    def _format_research(self) -> str:
        """Format insight dict into readable text."""
        lines = []
        insight = self.insight

        if insight.get("personality"):
            lines.append(f"COMPANY PERSONALITY:\n{insight['personality']}\n")

        vals = insight.get("core_values", [])
        if isinstance(vals, str):
            try:
                vals = json.loads(vals)
            except Exception:
                vals = []
        if vals:
            lines.append(f"CORE VALUES:\n• " + "\n• ".join(vals) + "\n")

        signals = insight.get("culture_signals", [])
        if isinstance(signals, str):
            try:
                signals = json.loads(signals)
            except Exception:
                signals = []
        if signals:
            lines.append(f"CULTURE SIGNALS:\n• " + "\n• ".join(signals[:6]) + "\n")

        if insight.get("what_they_want"):
            lines.append(f"WHAT THIS ROLE NEEDS:\n{insight['what_they_want']}\n")

        if insight.get("unique_insight"):
            lines.append(f"UNIQUE INSIGHT:\n{insight['unique_insight']}\n")

        kws = insight.get("keywords", [])
        if isinstance(kws, str):
            try:
                kws = json.loads(kws)
            except Exception:
                kws = []
        if kws:
            lines.append(f"KEYWORDS USED:\n{', '.join(kws)}\n")

        if insight.get("tone"):
            lines.append(f"TONE MATCHED: {insight['tone']}")

        return "\n".join(lines) if lines else "No research data available for this company."

    def _approve(self):
        self.final_cover_letter = self.cover_letter_edit.toPlainText().strip()
        self.result_action = "approve"
        self.accept()

    def _skip(self):
        self.result_action = "skip"
        self.reject()

    def _stop(self):
        self.result_action = "stop"
        self.reject()

    def get_result(self) -> tuple[str, str]:
        """Returns (action, cover_letter). Action: 'approve', 'skip', 'stop'."""
        return self.result_action, self.final_cover_letter
