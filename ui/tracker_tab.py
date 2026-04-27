"""
ui/tracker_tab.py
Applications tab — sortable table on the left, full detail panel on the right.
Click any row to see: cover letter sent, company research, job description,
recruiter responses, and a notes field.
Extracted from main.py so it can be imported cleanly.
"""

import json
from datetime import date
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QTextEdit, QSplitter, QFrame, QTabWidget,
    QLineEdit, QComboBox
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


class ApplicationDetailPanel(QWidget):
    """Right-side panel that shows full details for the selected application."""

    def __init__(self):
        super().__init__()
        self.store = get_store()
        self._current_app = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 0, 0, 0)
        layout.setSpacing(0)

        # Empty state
        self.empty_label = QLabel("← Select an application to view details")
        self.empty_label.setStyleSheet(
            f"color: {MUTED}; font-size: 13px; padding: 40px;"
        )
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_label)

        # Detail content (hidden until row selected)
        self.detail_widget = QWidget()
        self.detail_widget.setVisible(False)
        dl = QVBoxLayout(self.detail_widget)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(12)

        # Header: company + role + status pill
        self.header_frame = QFrame()
        self.header_frame.setStyleSheet(
            f"background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px;"
        )
        hl = QVBoxLayout(self.header_frame)
        hl.setContentsMargins(16, 12, 16, 12)

        self.detail_title = QLabel()
        self.detail_title.setStyleSheet(
            f"font-size: 15px; font-weight: bold; color: {TEXT};"
        )
        self.detail_title.setWordWrap(True)
        hl.addWidget(self.detail_title)

        self.detail_meta = QLabel()
        self.detail_meta.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        hl.addWidget(self.detail_meta)

        self.detail_status = QLabel()
        self.detail_status.setStyleSheet(f"font-size: 12px; font-weight: bold;")
        hl.addWidget(self.detail_status)

        dl.addWidget(self.header_frame)

        # Tabs: Cover Letter / Research / Job Description / Response
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(f"""
            QTabWidget::pane {{
                border: 1px solid {BORDER};
                background: {SURFACE};
                border-radius: 6px;
            }}
            QTabBar::tab {{
                background: {BG};
                color: {MUTED};
                padding: 7px 14px;
                border: none;
                font-size: 12px;
            }}
            QTabBar::tab:selected {{
                color: {TEXT};
                border-bottom: 2px solid {ACCENT};
            }}
        """)

        # Cover Letter tab
        cl_widget = QWidget()
        cl_layout = QVBoxLayout(cl_widget)
        cl_layout.setContentsMargins(8, 8, 8, 8)
        self.cover_letter_view = QTextEdit()
        self.cover_letter_view.setReadOnly(True)
        self.cover_letter_view.setStyleSheet(
            f"background: {BG}; border: none; color: {TEXT}; "
            f"font-size: 13px; line-height: 1.6;"
        )
        cl_layout.addWidget(self.cover_letter_view)
        self.tabs.addTab(cl_widget, "✍️ Cover Letter")

        # Research tab
        res_widget = QWidget()
        res_layout = QVBoxLayout(res_widget)
        res_layout.setContentsMargins(8, 8, 8, 8)
        self.research_view = QTextEdit()
        self.research_view.setReadOnly(True)
        self.research_view.setStyleSheet(
            f"background: {BG}; border: none; color: {TEXT}; font-size: 12px;"
        )
        res_layout.addWidget(self.research_view)
        self.tabs.addTab(res_widget, "🔍 Research")

        # Job Description tab
        jd_widget = QWidget()
        jd_layout = QVBoxLayout(jd_widget)
        jd_layout.setContentsMargins(8, 8, 8, 8)
        self.jd_view = QTextEdit()
        self.jd_view.setReadOnly(True)
        self.jd_view.setStyleSheet(
            f"background: {BG}; border: none; color: {TEXT}; font-size: 12px;"
        )
        jd_layout.addWidget(self.jd_view)
        self.tabs.addTab(jd_widget, "📄 Job Post")

        # Response tab
        resp_widget = QWidget()
        resp_layout = QVBoxLayout(resp_widget)
        resp_layout.setContentsMargins(8, 8, 8, 8)
        self.response_view = QTextEdit()
        self.response_view.setReadOnly(True)
        self.response_view.setStyleSheet(
            f"background: {BG}; border: none; color: {TEXT}; font-size: 12px;"
        )
        resp_layout.addWidget(self.response_view)
        self.tabs.addTab(resp_widget, "📬 Response")

        # Notes tab (editable)
        notes_widget = QWidget()
        notes_layout = QVBoxLayout(notes_widget)
        notes_layout.setContentsMargins(8, 8, 8, 8)
        self.notes_edit = QTextEdit()
        self.notes_edit.setPlaceholderText("Add personal notes about this application...")
        self.notes_edit.setStyleSheet(
            f"background: {BG}; border: none; color: {TEXT}; font-size: 12px;"
        )
        save_notes_btn = QPushButton("💾 Save Notes")
        save_notes_btn.setStyleSheet(
            f"background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 5px; "
            f"padding: 6px 14px; color: {TEXT}; font-size: 12px;"
        )
        save_notes_btn.clicked.connect(self._save_notes)
        notes_layout.addWidget(self.notes_edit)
        notes_layout.addWidget(save_notes_btn)
        self.tabs.addTab(notes_widget, "📝 Notes")

        dl.addWidget(self.tabs)

        # Action buttons at bottom
        btn_row = QHBoxLayout()

        self.open_url_btn = QPushButton("🌐 Open Job URL")
        self.open_url_btn.setStyleSheet(
            f"background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 5px; "
            f"padding: 7px 14px; color: {TEXT}; font-size: 12px;"
        )
        self.open_url_btn.clicked.connect(self._open_url)
        btn_row.addWidget(self.open_url_btn)
        btn_row.addStretch()

        dl.addLayout(btn_row)
        layout.addWidget(self.detail_widget)

    def load_application(self, app: dict):
        """Populate the detail panel with the selected application's data."""
        self._current_app = app
        self.empty_label.setVisible(False)
        self.detail_widget.setVisible(True)

        # Header
        title = app.get("job_title", "Unknown Role")
        company = app.get("company_name", "Unknown Company")
        self.detail_title.setText(f"{title} — {company}")

        meta_parts = []
        applied = (app.get("applied_at") or app.get("created_at", ""))[:10]
        if applied:
            meta_parts.append(f"📅 {applied}")
        platform = app.get("platform", "")
        if platform:
            meta_parts.append(f"🔗 {platform}")
        score = app.get("score")
        if score:
            meta_parts.append(f"⭐ {float(score):.1f}/10 fit")
        self.detail_meta.setText("   •   ".join(meta_parts))

        # Status + response
        status = app.get("status", "")
        response_type = app.get("response_type", "")
        status_colors = {
            "submitted": GREEN, "failed": RED, "paused": YELLOW,
            "skipped": MUTED, "researching": ACCENT, "applying": ACCENT,
        }
        response_colors = {
            "interview": "#ffd700", "offer": GREEN,
            "rejection": RED, "info_needed": YELLOW,
        }
        color = status_colors.get(status, MUTED)
        status_text = f"Status: {status.upper()}"
        if response_type:
            resp_color = response_colors.get(response_type, ACCENT)
            self.detail_status.setText(
                f'<span style="color:{color}">{status_text}</span>   '
                f'<span style="color:{resp_color}">● {response_type.upper().replace("_", " ")}</span>'
            )
            self.detail_status.setTextFormat(Qt.TextFormat.RichText)
        else:
            self.detail_status.setText(status_text)
            self.detail_status.setStyleSheet(f"color: {color}; font-size: 12px; font-weight: bold;")

        # Cover letter
        cover_letter = app.get("cover_letter") or ""
        if cover_letter:
            self.cover_letter_view.setPlainText(cover_letter)
            # Highlight the Response tab if there's something there
            self.tabs.setCurrentIndex(0)
        else:
            self.cover_letter_view.setPlainText("No cover letter stored for this application.")

        # Company research — pull from company_profiles DB if available
        research_text = self._load_research(company)
        self.research_view.setPlainText(research_text)

        # Job description — stored in job_url or notes
        job_desc = app.get("notes") or ""
        # Try to get from company profile's raw research
        if not job_desc:
            job_desc = f"Job URL: {app.get('job_url', '')}\nATS URL: {app.get('ats_url', '')}"
        self.jd_view.setPlainText(job_desc)

        # Response
        response_text = app.get("response_text") or ""
        if response_text:
            self.response_view.setPlainText(
                f"Type: {response_type.upper().replace('_', ' ')}\n\n{response_text}"
            )
            self.tabs.setTabText(3, f"📬 Response ({'!' if response_type == 'interview' else '•'})")
        else:
            self.response_view.setPlainText("No employer response received yet.")

        # Notes
        notes = app.get("notes") or ""
        self.notes_edit.setPlainText(notes)

    def _load_research(self, company: str) -> str:
        """Load company research from DB and format it."""
        try:
            store = self.store
            profile = store.get_company_profile(company)
            if not profile:
                return f"No research data stored for {company} yet."

            def parse_json_list(val):
                if not val:
                    return []
                if isinstance(val, list):
                    return val
                try:
                    return json.loads(val)
                except Exception:
                    return [str(val)]

            lines = []
            if profile.get("personality"):
                lines.append(f"🏢 COMPANY PERSONALITY\n{profile['personality']}\n")

            vals = parse_json_list(profile.get("core_values"))
            if vals:
                lines.append("💡 CORE VALUES\n• " + "\n• ".join(vals) + "\n")

            signals = parse_json_list(profile.get("culture_signals"))
            if signals:
                lines.append("📡 CULTURE SIGNALS\n• " + "\n• ".join(signals[:8]) + "\n")

            kws = parse_json_list(profile.get("keywords"))
            if kws:
                lines.append(f"🔑 KEYWORDS USED IN APPLICATION\n{', '.join(kws)}\n")

            if profile.get("tone"):
                lines.append(f"🎙 TONE MATCHED: {profile['tone']}\n")

            lines.append(f"📊 Sources researched: {profile.get('source_count', 0)}")

            return "\n".join(lines) if lines else f"Research profile found but empty for {company}."
        except Exception as e:
            return f"Could not load research: {e}"

    def _save_notes(self):
        if not self._current_app:
            return
        notes = self.notes_edit.toPlainText().strip()
        self.store.update_application(self._current_app["id"], {"notes": notes})
        self._current_app["notes"] = notes

    def _open_url(self):
        if not self._current_app:
            return
        import webbrowser
        url = self._current_app.get("ats_url") or self._current_app.get("job_url", "")
        if url:
            webbrowser.open(url)


class TrackerTab(QWidget):
    """Applications tab — table + detail panel side by side."""

    def __init__(self):
        super().__init__()
        self.store = get_store()
        self._all_apps = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        # ── Top bar: stats + search + filter ──────────────────────────────────
        top_bar = QHBoxLayout()
        top_bar.addWidget(QLabel("📋 All Applications"))

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search company or role...")
        self.search_box.setMaximumWidth(220)
        self.search_box.setStyleSheet(
            f"background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 5px; "
            f"padding: 6px 10px; color: {TEXT}; font-size: 12px;"
        )
        self.search_box.textChanged.connect(self._apply_filter)

        self.status_filter = QComboBox()
        self.status_filter.addItems([
            "All", "submitted", "failed", "paused", "skipped",
            "researching", "applying"
        ])
        self.status_filter.setStyleSheet(
            f"background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 5px; "
            f"padding: 5px 10px; color: {TEXT}; font-size: 12px;"
        )
        self.status_filter.currentTextChanged.connect(self._apply_filter)

        refresh_btn = QPushButton("🔄 Refresh")
        refresh_btn.setStyleSheet(
            f"background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 5px; "
            f"padding: 6px 14px; color: {TEXT}; font-size: 12px;"
        )
        refresh_btn.clicked.connect(self._load)

        top_bar.addStretch()
        top_bar.addWidget(self.search_box)
        top_bar.addWidget(self.status_filter)
        top_bar.addWidget(refresh_btn)
        layout.addLayout(top_bar)

        # Stats bar
        self.stats_bar = QLabel()
        self.stats_bar.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        layout.addWidget(self.stats_bar)

        # ── Splitter: table left, detail panel right ───────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setStyleSheet(f"QSplitter::handle {{ background: {BORDER}; }}")

        # Table
        table_widget = QWidget()
        table_layout = QVBoxLayout(table_widget)
        table_layout.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels([
            "Date", "Company", "Role", "Status", "Response", "Score"
        ])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setStyleSheet(f"""
            QTableWidget {{
                background: {SURFACE};
                border: 1px solid {BORDER};
                border-radius: 6px;
                color: {TEXT};
                font-size: 12px;
            }}
            QTableWidget::item {{
                padding: 8px 6px;
                border-bottom: 1px solid {BG};
            }}
            QTableWidget::item:selected {{
                background: #1c2333;
                color: {TEXT};
            }}
            QHeaderView::section {{
                background: {BG};
                color: {MUTED};
                padding: 8px 6px;
                border: none;
                border-bottom: 1px solid {BORDER};
                font-size: 11px;
                font-weight: bold;
            }}
        """)
        self.table.selectionModel().selectionChanged.connect(self._on_row_selected)
        table_layout.addWidget(self.table)
        splitter.addWidget(table_widget)

        # Detail panel
        self.detail_panel = ApplicationDetailPanel()
        splitter.addWidget(self.detail_panel)
        splitter.setSizes([480, 620])

        layout.addWidget(splitter)

        self._load()

        # Auto-refresh every 30s
        self.timer = QTimer()
        self.timer.timeout.connect(self._load)
        self.timer.start(30000)

    def _load(self):
        self._all_apps = self.store.get_applications(limit=1000)
        self._update_stats(self._all_apps)
        self._apply_filter()

    def _update_stats(self, apps: list):
        submitted = sum(1 for a in apps if a.get("status") == "submitted")
        responses = sum(1 for a in apps if a.get("response_type"))
        interviews = sum(1 for a in apps if a.get("response_type") == "interview")
        today_str = date.today().strftime("%Y-%m-%d")
        today = sum(
            1 for a in apps
            if (a.get("applied_at") or a.get("created_at", "")).startswith(today_str)
        )
        self.stats_bar.setText(
            f"Total: {len(apps)}  |  Today: {today}  |  Submitted: {submitted}  "
            f"|  Responses: {responses}  |  Interviews: {interviews}"
        )

    def _apply_filter(self):
        search = self.search_box.text().lower().strip()
        status_filter = self.status_filter.currentText()

        filtered = self._all_apps
        if status_filter != "All":
            filtered = [a for a in filtered if a.get("status") == status_filter]
        if search:
            filtered = [
                a for a in filtered
                if search in (a.get("company_name") or "").lower()
                or search in (a.get("job_title") or "").lower()
            ]

        self._populate_table(filtered)

    def _populate_table(self, apps: list):
        self.table.setRowCount(len(apps))
        self._displayed_apps = apps

        status_colors = {
            "submitted": GREEN, "failed": RED, "paused": YELLOW,
            "skipped": MUTED, "researching": ACCENT, "applying": ACCENT,
        }
        response_colors = {
            "interview": "#ffd700", "offer": GREEN,
            "rejection": RED, "info_needed": YELLOW,
        }

        for i, app in enumerate(apps):
            status = app.get("status", "")
            response_type = app.get("response_type", "")
            status_color = status_colors.get(status, MUTED)
            response_color = response_colors.get(response_type, TEXT)

            date_str = (app.get("applied_at") or app.get("created_at", ""))[:10]
            score = app.get("score")
            score_str = f"{float(score):.1f}" if score else "—"

            row_data = [
                (date_str, MUTED),
                (app.get("company_name", ""), TEXT),
                (app.get("job_title", ""), TEXT),
                (status, status_color),
                (response_type or "—", response_color if response_type else MUTED),
                (score_str, ACCENT if score else MUTED),
            ]

            for j, (text, color) in enumerate(row_data):
                item = QTableWidgetItem(str(text))
                item.setForeground(QColor(color))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(i, j, item)

        self.table.resizeColumnToContents(0)
        self.table.resizeColumnToContents(3)
        self.table.resizeColumnToContents(4)
        self.table.resizeColumnToContents(5)

    def _on_row_selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        idx = rows[0].row()
        if hasattr(self, "_displayed_apps") and idx < len(self._displayed_apps):
            self.detail_panel.load_application(self._displayed_apps[idx])
