"""
ui/research_library_tab.py
Creator-only browsable view of:
- Company personality profiles collected so far
- Advice database ranked by frequency and success
- Template success rates
"""

import json
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTabWidget, QTableWidget, QTableWidgetItem, QHeaderView,
    QTextEdit, QLineEdit, QScrollArea, QFrame, QSplitter
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from core.settings_store import get_store

BG      = "#0d1117"
SURFACE = "#161b22"
BORDER  = "#30363d"
ACCENT  = "#58a6ff"
GREEN   = "#3fb950"
YELLOW  = "#d29922"
TEXT    = "#e6edf3"
MUTED   = "#8b949e"


class CompanyProfilesView(QWidget):
    def __init__(self):
        super().__init__()
        self.store = get_store()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        # Search bar
        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search company name...")
        self.search.textChanged.connect(self._load)
        search_row.addWidget(self.search)
        refresh_btn = QPushButton("🔄")
        refresh_btn.setMaximumWidth(40)
        refresh_btn.clicked.connect(self._load)
        search_row.addWidget(refresh_btn)
        layout.addLayout(search_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Company list
        self.company_table = QTableWidget()
        self.company_table.setColumnCount(3)
        self.company_table.setHorizontalHeaderLabels(["Company", "Sources", "Last Updated"])
        self.company_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.company_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.company_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.company_table.selectionModel().selectionChanged.connect(self._on_select)
        splitter.addWidget(self.company_table)

        # Detail view
        detail_widget = QWidget()
        detail_layout = QVBoxLayout(detail_widget)
        self.detail_title = QLabel("Select a company to view profile")
        self.detail_title.setStyleSheet(f"font-size: 15px; font-weight: bold; color: {TEXT};")
        detail_layout.addWidget(self.detail_title)
        self.detail_text = QTextEdit()
        self.detail_text.setReadOnly(True)
        self.detail_text.setStyleSheet(
            f"background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; "
            f"font-size: 12px; color: {TEXT}; padding: 10px;"
        )
        detail_layout.addWidget(self.detail_text)
        splitter.addWidget(detail_widget)
        splitter.setSizes([300, 500])

        layout.addWidget(splitter)
        self._load()
        self._profiles = []

    def _load(self):
        search = self.search.text().lower()
        cursor = self.store.conn.execute("""
            SELECT company_name, source_count, last_updated, personality,
                   core_values, culture_signals, keywords, tone
            FROM company_profiles
            ORDER BY source_count DESC
        """)
        cols = [d[0] for d in cursor.description]
        rows = [dict(zip(cols, r)) for r in cursor.fetchall()]

        if search:
            rows = [r for r in rows if search in r["company_name"].lower()]

        self._profiles = rows
        self.company_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            self.company_table.setItem(i, 0, QTableWidgetItem(row["company_name"]))
            self.company_table.setItem(i, 1, QTableWidgetItem(str(row["source_count"])))
            updated = (row["last_updated"] or "")[:10]
            self.company_table.setItem(i, 2, QTableWidgetItem(updated))

    def _on_select(self):
        rows = self.company_table.selectedItems()
        if not rows:
            return
        idx = self.company_table.currentRow()
        if idx >= len(self._profiles):
            return
        profile = self._profiles[idx]
        self.detail_title.setText(profile["company_name"])

        def _parse(val):
            if not val:
                return []
            try:
                return json.loads(val)
            except Exception:
                return [val]

        lines = []
        if profile.get("personality"):
            lines.append(f"🏢 Personality:\n{profile['personality']}\n")
        if profile.get("core_values"):
            vals = _parse(profile["core_values"])
            lines.append(f"💡 Core Values:\n• " + "\n• ".join(vals) + "\n")
        if profile.get("culture_signals"):
            sigs = _parse(profile["culture_signals"])
            lines.append(f"📡 Culture Signals:\n• " + "\n• ".join(sigs[:8]) + "\n")
        if profile.get("keywords"):
            kws = _parse(profile["keywords"])
            lines.append(f"🔑 Keywords to use: {', '.join(kws)}\n")
        if profile.get("tone"):
            lines.append(f"🎙 Tone: {profile['tone']}\n")
        lines.append(f"📊 Sources scraped: {profile['source_count']}")

        self.detail_text.setPlainText("\n".join(lines))


class AdviceDBView(QWidget):
    def __init__(self):
        super().__init__()
        self.store = get_store()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        # Filter
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Type:"))
        from PyQt6.QtWidgets import QComboBox
        self.type_filter = QComboBox()
        self.type_filter.addItems([
            "all", "cover_letter", "cold_email", "resume", "networking",
            "recruiter_message", "general"
        ])
        self.type_filter.currentTextChanged.connect(self._load)
        filter_row.addWidget(self.type_filter)
        filter_row.addStretch()
        layout.addLayout(filter_row)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Type", "Advice", "Mentions", "Success"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(True)
        layout.addWidget(self.table)

        self._load()

    def _load(self):
        advice_type = self.type_filter.currentText()
        query = """
            SELECT advice_type, content, mention_count, success_score
            FROM advice_db
        """
        params = []
        if advice_type != "all":
            query += " WHERE advice_type=?"
            params.append(advice_type)
        query += " ORDER BY (mention_count * (1.0 + success_score)) DESC LIMIT 200"

        cursor = self.store.conn.execute(query, params)
        rows = cursor.fetchall()
        self.table.setRowCount(len(rows))
        for i, (atype, content, mentions, success) in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(atype))
            self.table.setItem(i, 1, QTableWidgetItem(content))
            count_item = QTableWidgetItem(str(mentions))
            count_item.setForeground(QColor(ACCENT))
            self.table.setItem(i, 2, count_item)
            success_item = QTableWidgetItem(f"{success:.1f}")
            success_item.setForeground(QColor(GREEN))
            self.table.setItem(i, 3, success_item)
        self.table.resizeRowsToContents()


class ResearchLibraryTab(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)

        layout.addWidget(QLabel("📚 Research Library"))
        layout.addWidget(QLabel(
            "All company profiles and advice collected so far. "
            "This grows automatically as more applications are processed."
        ))

        inner_tabs = QTabWidget()
        inner_tabs.addTab(CompanyProfilesView(), "🏢 Company Profiles")
        inner_tabs.addTab(AdviceDBView(), "💡 Advice Database")
        layout.addWidget(inner_tabs)
