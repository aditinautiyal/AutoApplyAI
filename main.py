"""
main.py
AutoApplyAI — main entry point.
First launch → setup wizard.
Subsequent launches → full dashboard.
Starts: fast tracks, slow lanes, all discovery sources, inbox monitor.
"""

import sys
import json
import time
from datetime import date
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget,
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QTextEdit, QLineEdit,
    QSpinBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QStatusBar, QProgressBar
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QColor, QFont

from core.settings_store import get_store
from core.api_router import get_router
from onboarding.setup_wizard import SetupWizard
from tracks.track_manager import TrackManager
from slow_lane.slow_lane_manager import SlowLaneManager
from discovery.discovery_manager import DiscoveryManager
from notifications.inbox_monitor_loop import InboxMonitorLoop
from discovery.job_pool import get_pool

# ─── Palette ──────────────────────────────────────────────────────────────────
BG      = "#0d1117"
SURFACE = "#161b22"
BORDER  = "#30363d"
ACCENT  = "#58a6ff"
GREEN   = "#3fb950"
YELLOW  = "#d29922"
RED     = "#f85149"
TEXT    = "#e6edf3"
MUTED   = "#8b949e"

MAIN_STYLE = f"""
QMainWindow, QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-family: 'Segoe UI', 'SF Pro Display', sans-serif;
    font-size: 13px;
}}
QTabWidget::pane {{
    border: 1px solid {BORDER};
    background-color: {SURFACE};
    border-radius: 6px;
}}
QTabBar::tab {{
    background-color: {BG};
    color: {MUTED};
    padding: 10px 20px;
    border: none;
    font-size: 13px;
}}
QTabBar::tab:selected {{
    color: {TEXT};
    border-bottom: 2px solid {ACCENT};
}}
QTabBar::tab:hover {{ color: {TEXT}; }}
QPushButton {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 8px 18px;
    color: {TEXT};
}}
QPushButton:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}
QPushButton#start {{
    background-color: {GREEN}; border: none; color: white;
    font-weight: bold; padding: 10px 24px;
}}
QPushButton#stop {{
    background-color: {RED}; border: none; color: white;
    font-weight: bold; padding: 10px 24px;
}}
QTableWidget {{
    background-color: {SURFACE}; border: 1px solid {BORDER};
    border-radius: 6px; gridline-color: {BORDER};
}}
QTableWidget::item {{ padding: 8px; border-bottom: 1px solid {BORDER}; }}
QHeaderView::section {{
    background-color: {BG}; color: {MUTED}; padding: 8px;
    border: none; border-bottom: 1px solid {BORDER}; font-size: 12px;
}}
QLineEdit, QTextEdit {{
    background-color: {SURFACE}; border: 1px solid {BORDER};
    border-radius: 6px; padding: 8px 12px; color: {TEXT};
}}
QScrollArea {{ border: none; }}
QStatusBar {{ color: {MUTED}; font-size: 12px; }}
QProgressBar {{
    border: 1px solid {BORDER}; border-radius: 4px;
    background-color: {SURFACE}; height: 4px;
}}
QProgressBar::chunk {{ background-color: {ACCENT}; border-radius: 4px; }}
"""


# ─── Cross-thread signal bridge ───────────────────────────────────────────────
class Signals(QObject):
    track_update  = pyqtSignal(int, str, str)
    slow_update   = pyqtSignal(str, str, str)
    pool_update   = pyqtSignal(dict)
    app_submitted = pyqtSignal(dict)
    notification  = pyqtSignal(dict)
    log_message   = pyqtSignal(str)

signals = Signals()


# ─── Dashboard Tab ────────────────────────────────────────────────────────────
class DashboardTab(QWidget):
    def __init__(self, fast_manager: TrackManager,
                 slow_manager: SlowLaneManager,
                 discovery: DiscoveryManager,
                 inbox: InboxMonitorLoop):
        super().__init__()
        self.fast = fast_manager
        self.slow = slow_manager
        self.discovery = discovery
        self.inbox = inbox
        self.store = get_store()
        self._is_running = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # ── Title + controls ──
        header = QHBoxLayout()
        title = QLabel("⚡ AutoApplyAI")
        title.setStyleSheet(f"font-size: 22px; font-weight: bold; color: {TEXT};")
        header.addWidget(title)
        header.addStretch()

        self.start_btn = QPushButton("▶  Start Applying")
        self.start_btn.setObjectName("start")
        self.start_btn.clicked.connect(self._start_all)

        self.stop_btn = QPushButton("⏹  Stop Everything")
        self.stop_btn.setObjectName("stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_all)

        from PyQt6.QtWidgets import QCheckBox
        self.review_toggle = QCheckBox("🔍 Review each application before submitting")
        self.review_toggle.setChecked(True)
        self.review_toggle.setStyleSheet(f"color: {YELLOW}; font-weight: bold;")
        self.review_toggle.stateChanged.connect(self._toggle_review_mode)

        header.addWidget(self.start_btn)
        header.addWidget(self.stop_btn)
        layout.addLayout(header)
        layout.addWidget(self.review_toggle)

        # ── Stat cards ──
        stats_row = QHBoxLayout()
        self.stat_cards = {}
        for key, label_text, color in [
            ("applied_today", "Applied Today",   ACCENT),
            ("total_applied", "Total Applied",   TEXT),
            ("responses",     "Responses",       GREEN),
            ("pool_size",     "Jobs in Queue",   YELLOW),
            ("interviews",    "Interviews",      "#ffd700"),
        ]:
            card = self._make_stat_card(label_text, "0", color)
            self.stat_cards[key] = card
            stats_row.addWidget(card)
        layout.addLayout(stats_row)

        # ── Fast tracks ──
        fast_label = QLabel("Fast Tracks")
        fast_label.setStyleSheet(f"color:{MUTED}; font-size:12px; font-weight:bold;")
        layout.addWidget(fast_label)
        self.track_frame = QVBoxLayout()
        self.track_rows: dict[int, QLabel] = {}
        layout.addLayout(self.track_frame)

        # ── Slow lanes ──
        slow_label = QLabel("Slow Lane (Human-Paced)")
        slow_label.setStyleSheet(f"color:{MUTED}; font-size:12px; font-weight:bold;")
        layout.addWidget(slow_label)
        self.slow_frame = QHBoxLayout()
        self.slow_rows: dict[str, QLabel] = {}
        layout.addLayout(self.slow_frame)

        # ── Discovery status ──
        disc_label = QLabel("Discovery Sources")
        disc_label.setStyleSheet(f"color:{MUTED}; font-size:12px; font-weight:bold;")
        layout.addWidget(disc_label)
        self.disc_status = QLabel("Not started")
        self.disc_status.setStyleSheet(f"color:{MUTED}; font-size:12px;")
        layout.addWidget(self.disc_status)

        # ── Activity log ──
        log_label = QLabel("Activity Log")
        log_label.setStyleSheet(f"color:{MUTED}; font-size:12px; font-weight:bold;")
        layout.addWidget(log_label)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(180)
        self.log.setStyleSheet(
            f"font-family: monospace; font-size: 12px; "
            f"background:{SURFACE}; border:1px solid {BORDER}; border-radius:6px;"
        )
        layout.addWidget(self.log)
        layout.addStretch()

        signals.track_update.connect(self._on_track_update)
        signals.slow_update.connect(self._on_slow_update)
        signals.log_message.connect(self._append_log)

        self.timer = QTimer()
        self.timer.timeout.connect(self._refresh_stats)
        self.timer.start(5000)

    def _make_stat_card(self, label: str, value: str, color: str) -> QFrame:
        card = QFrame()
        card.setStyleSheet(
            f"background:{SURFACE}; border:1px solid {BORDER}; border-radius:8px; padding:2px;"
        )
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 12, 16, 12)
        val_lbl = QLabel(value)
        val_lbl.setObjectName("value")
        val_lbl.setStyleSheet(f"font-size:26px; font-weight:bold; color:{color};")
        lbl = QLabel(label)
        lbl.setStyleSheet(f"font-size:11px; color:{MUTED};")
        cl.addWidget(val_lbl)
        cl.addWidget(lbl)
        return card

    def _update_stat(self, key: str, value: str):
        card = self.stat_cards.get(key)
        if card:
            v = card.findChild(QLabel, "value")
            if v:
                v.setText(value)

    def _toggle_review_mode(self, state):
        enabled = bool(state)
        self.store.set("review_mode", enabled)
        if enabled:
            self.review_toggle.setStyleSheet(f"color: {YELLOW}; font-weight: bold;")
            self._append_log("🔍 Review mode ON — you will approve every application")
        else:
            self.review_toggle.setStyleSheet(f"color: {MUTED};")
            self._append_log("⚡ Review mode OFF — applications submit automatically")

    def _start_all(self):
        store = self.store
        num_tracks = store.get("track_count", 2)
        self.fast.start(num_tracks)
        self.slow.start()
        self.discovery.start()
        self.inbox.start()
        # VPN monitor — alerts when Google gets rate-limited
        from discovery.vpn_monitor import get_vpn_monitor
        get_vpn_monitor().start()
        self._is_running = True
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.disc_status.setText(f"✅ All discovery sources running")
        self._append_log(f"▶ Started {num_tracks} fast tracks + slow lanes + discovery")
        self._append_log("🔍 Advice scraper warming up in background...")

    def _stop_all(self):
        self.fast.stop()
        self.slow.stop()
        self.discovery.stop()
        self.inbox.stop()
        from discovery.vpn_monitor import get_vpn_monitor
        get_vpn_monitor().stop()
        self._is_running = False
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.disc_status.setText("⏹ Stopped")
        self._append_log("⏹ All systems stopped")

    def _on_track_update(self, track_id: int, status: str, message: str):
        if track_id not in self.track_rows:
            row = QLabel()
            row.setStyleSheet(
                f"background:{SURFACE}; border:1px solid {BORDER}; "
                f"border-radius:6px; padding:8px; font-size:12px;"
            )
            self.track_rows[track_id] = row
            self.track_frame.addWidget(row)

        icons = {
            "working": "🔵", "researching": "🔍", "writing": "✍️",
            "applying": "📝", "done": "✅", "idle": "⏳",
            "error": "❌", "starting": "🟡",
        }
        icon = icons.get(status, "•")
        self.track_rows[track_id].setText(
            f"{icon}  Track {track_id}  —  {message}"
        )
        if status == "done":
            self._append_log(f"✅ {message}")
        elif status == "error":
            self._append_log(f"❌ Track {track_id}: {message}")

    def _on_slow_update(self, lane: str, status: str, message: str):
        if lane not in self.slow_rows:
            row = QLabel()
            row.setStyleSheet(
                f"background:{SURFACE}; border:1px solid {BORDER}; "
                f"border-radius:6px; padding:8px; font-size:12px;"
            )
            self.slow_rows[lane] = row
            self.slow_frame.addWidget(row)

        icons = {
            "running": "🐢", "applied": "✅", "error": "❌",
            "starting": "🟡", "idle": "⏳",
        }
        icon = icons.get(status, "•")
        self.slow_rows[lane].setText(f"{icon}  {lane.title()} Easy Apply  —  {message}")

    def _refresh_stats(self):
        pool = get_pool()
        stats = pool.stats()
        self._update_stat("pool_size", str(stats.get("queue_size", 0)))

        apps = self.store.get_applications()
        self._update_stat("total_applied", str(len(apps)))

        today_str = date.today().strftime("%Y-%m-%d")
        today_count = sum(
            1 for a in apps
            if (a.get("applied_at") or a.get("created_at", "")).startswith(today_str)
        )
        self._update_stat("applied_today", str(today_count))

        interviews = sum(1 for a in apps if a.get("response_type") == "interview")
        responses = sum(1 for a in apps if a.get("response_type"))
        self._update_stat("responses", str(responses))
        self._update_stat("interviews", str(interviews))

    def _append_log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log.append(f"[{ts}]  {msg}")


# ─── Tracker Tab ──────────────────────────────────────────────────────────────
class TrackerTab(QWidget):
    def __init__(self):
        super().__init__()
        self.store = get_store()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)

        header = QHBoxLayout()
        header.addWidget(QLabel("📋 All Applications"))
        refresh_btn = QPushButton("🔄 Refresh")
        refresh_btn.clicked.connect(self._load)
        header.addStretch()
        header.addWidget(refresh_btn)
        layout.addLayout(header)

        stats_bar = QLabel()
        stats_bar.setStyleSheet(f"color:{MUTED}; font-size:12px;")
        stats_bar.setObjectName("stats_bar")
        layout.addWidget(stats_bar)
        self.stats_bar = stats_bar

        self.table = QTableWidget()
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels([
            "Date", "Company", "Role", "Platform", "Score",
            "Status", "Response", "Lane"
        ])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)

        self._load()
        signals.app_submitted.connect(lambda _: self._load())

        self.timer = QTimer()
        self.timer.timeout.connect(self._load)
        self.timer.start(30000)

    def _load(self):
        apps = self.store.get_applications(limit=500)
        self.table.setRowCount(len(apps))

        submitted = sum(1 for a in apps if a.get("status") == "submitted")
        responses = sum(1 for a in apps if a.get("response_type"))
        interviews = sum(1 for a in apps if a.get("response_type") == "interview")
        self.stats_bar.setText(
            f"Total: {len(apps)}  |  Submitted: {submitted}  |  "
            f"Responses: {responses}  |  Interviews: {interviews}"
        )

        status_colors = {
            "submitted": GREEN, "researching": ACCENT, "applying": ACCENT,
            "failed": RED, "paused": YELLOW, "queued": MUTED,
        }

        for i, app in enumerate(apps):
            status = app.get("status", "")
            response_type = app.get("response_type", "")
            color = status_colors.get(status, TEXT)

            vals = [
                (app.get("applied_at") or app.get("created_at", ""))[:10],
                app.get("company_name", ""),
                app.get("job_title", ""),
                app.get("platform", ""),
                f"{app.get('score', 0):.1f}" if app.get("score") else "—",
                status,
                response_type or "—",
                app.get("lane_type", "fast"),
            ]
            for j, text in enumerate(vals):
                item = QTableWidgetItem(str(text))
                if j == 5:
                    item.setForeground(QColor(color))
                if j == 6 and response_type == "interview":
                    item.setForeground(QColor("#ffd700"))
                self.table.setItem(i, j, item)


# ─── Inbox Tab ────────────────────────────────────────────────────────────────
class InboxTab(QWidget):
    def __init__(self):
        super().__init__()
        self.store = get_store()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        layout.addWidget(QLabel("📬 Applications Needing Your Input"))
        layout.addWidget(QLabel(
            "These are paused. Answer and the application automatically resumes — "
            "all other tracks keep running unaffected."
        ))

        self.count_label = QLabel()
        self.count_label.setStyleSheet(f"color:{YELLOW}; font-weight:bold;")
        layout.addWidget(self.count_label)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.content = QWidget()
        self.cl = QVBoxLayout(self.content)
        self.cl.setSpacing(12)
        self.scroll.setWidget(self.content)
        layout.addWidget(self.scroll)

        self._load()
        signals.notification.connect(lambda _: self._load())

        self.timer = QTimer()
        self.timer.timeout.connect(self._load)
        self.timer.start(10000)

    def _load(self):
        while self.cl.count():
            item = self.cl.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        notifs = self.store.get_pending_notifications()
        self.count_label.setText(
            f"{len(notifs)} item{'s' if len(notifs) != 1 else ''} waiting"
            if notifs else ""
        )

        if not notifs:
            empty = QLabel("✅  Nothing pending — all applications running smoothly!")
            empty.setStyleSheet(f"color:{GREEN}; padding:20px;")
            self.cl.addWidget(empty)
        else:
            for notif in notifs:
                self.cl.addWidget(self._make_card(notif))

        self.cl.addStretch()

    def _make_card(self, notif: dict) -> QFrame:
        card = QFrame()
        type_colors = {
            "interview": "#ffd700", "clarification": YELLOW,
            "info_needed": ACCENT, "sms_verification": RED,
        }
        border_color = type_colors.get(notif.get("notif_type", ""), YELLOW)
        card.setStyleSheet(
            f"background:{SURFACE}; border:1px solid {border_color}; "
            f"border-radius:8px; padding:4px;"
        )
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 12, 16, 12)

        title = QLabel(notif.get("title", ""))
        title.setStyleSheet(f"font-weight:bold; color:{border_color};")
        cl.addWidget(title)

        msg = QLabel(notif.get("message", ""))
        msg.setWordWrap(True)
        msg.setStyleSheet(f"color:{TEXT}; font-size:12px;")
        cl.addWidget(msg)

        inp = QTextEdit()
        inp.setPlaceholderText("Type your answer here...")
        inp.setMaximumHeight(70)
        cl.addWidget(inp)

        btn_row = QHBoxLayout()
        submit = QPushButton("✅  Submit Answer")
        submit.setStyleSheet(
            f"background:{GREEN}; border:none; border-radius:5px; "
            f"padding:7px 16px; color:white; font-weight:bold;"
        )
        submit.clicked.connect(
            lambda _, n=notif, i=inp: self._submit(n, i.toPlainText().strip())
        )
        btn_row.addWidget(submit)
        btn_row.addStretch()
        cl.addLayout(btn_row)
        return card

    def _submit(self, notif: dict, answer: str):
        if not answer:
            return
        self.store.resolve_notification(notif["id"], answer)
        self.store.save_learned_answer(
            question_pattern=notif.get("title", ""),
            answer=answer,
            tags=["user_clarification"]
        )
        app_id = notif.get("application_id")
        if app_id:
            pool = get_pool()
            pool.resume_job(str(app_id))
            self.store.update_application(app_id, {"status": "queued"})
        self._load()


# ─── AI Chat Tab ──────────────────────────────────────────────────────────────
class AIChatTab(QWidget):
    def __init__(self, fast_manager: TrackManager, slow_manager: SlowLaneManager):
        super().__init__()
        self.fast = fast_manager
        self.slow = slow_manager
        self.store = get_store()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)

        layout.addWidget(QLabel("🤖 AI Command Chat"))
        layout.addWidget(QLabel(
            'Tell AutoApplyAI what to change. Examples: '
            '"add another track" / "focus on ML jobs only" / '
            '"pause slow lane" / "show me stats" / "target Chicago only"'
        ))

        self.display = QTextEdit()
        self.display.setReadOnly(True)
        self.display.setStyleSheet(
            f"font-size:13px; background:{SURFACE}; border:1px solid {BORDER}; border-radius:6px;"
        )
        layout.addWidget(self.display)

        input_row = QHBoxLayout()
        self.inp = QLineEdit()
        self.inp.setPlaceholderText("Type a command...")
        self.inp.returnPressed.connect(self._send)
        send_btn = QPushButton("Send")
        input_row.addWidget(self.inp)
        input_row.addWidget(send_btn)
        send_btn.clicked.connect(self._send)
        layout.addLayout(input_row)

        self._append("AutoApplyAI",
            "Ready! I can change your settings, adjust targets, add/remove tracks, "
            "toggle platforms, or tell you what's happening."
        )

    def _send(self):
        msg = self.inp.text().strip()
        if not msg:
            return
        self.inp.clear()
        self._append("You", msg)
        self._process(msg)

    def _process(self, command: str):
        import re
        router = get_router()
        store = self.store
        pool = get_pool()

        pool_stats = pool.stats()
        apps = store.get_applications()
        track_count = store.get("track_count", 2)
        profile = store.get_profile() or {}

        system = """You control AutoApplyAI, an autonomous job application system.
User gives natural language commands. Return JSON:
{
  "response": "What to say to the user (concise, helpful)",
  "actions": [
    {"type": "set_track_count", "value": 3},
    {"type": "update_setting", "key": "target_roles", "value": "ML engineer intern"},
    {"type": "pause_slow_lane"},
    {"type": "resume_slow_lane"}
  ]
}
Only include actions if something should actually change.
Action types: set_track_count | update_setting | pause_slow_lane | resume_slow_lane | none"""

        context = f"""System state:
- Fast tracks running: {track_count}
- Jobs queued: {pool_stats.get('queue_size', 0)}
- Total applied: {len(apps)}
- Target roles: {profile.get('target_roles', '')}
- Locations: {profile.get('locations', '')}
- Slow lanes: {store.get('slow_platforms', [])}

Command: {command}"""

        try:
            resp = router.complete(context, system=system, smart=False, max_tokens=500)
            resp = resp.strip()
            if resp.startswith("```"):
                resp = re.sub(r"```[a-z]*\n?", "", resp).strip().rstrip("```")
            data = json.loads(resp)

            for action in data.get("actions", []):
                atype = action.get("type")
                if atype == "set_track_count":
                    count = int(action["value"])
                    self.fast.set_track_count(count)
                    store.set("track_count", count)
                elif atype == "update_setting":
                    store.set(action["key"], action["value"])
                    profile_fields = {
                        "target_roles", "locations", "job_types",
                        "salary_min", "salary_max", "dream_criteria"
                    }
                    if action["key"] in profile_fields:
                        store.save_profile({action["key"]: action["value"]})
                elif atype == "pause_slow_lane":
                    self.slow.stop()
                elif atype == "resume_slow_lane":
                    self.slow.start()

            self._append("AutoApplyAI", data.get("response", "Done!"))

        except Exception as e:
            self._append("AutoApplyAI", f"Understood your request but hit an error: {e}")

    def _append(self, sender: str, message: str):
        ts = time.strftime("%H:%M")
        color = ACCENT if sender == "AutoApplyAI" else GREEN
        self.display.append(
            f'<span style="color:{color}; font-weight:bold;">[{ts}] {sender}:</span> '
            f'{message}<br>'
        )


# ─── Main Window ──────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AutoApplyAI")
        self.setMinimumSize(1100, 720)
        self.setStyleSheet(MAIN_STYLE)

        self.fast_manager = TrackManager(
            status_callback=lambda t, s, m: signals.track_update.emit(t, s, m)
        )
        self.slow_manager = SlowLaneManager(
            status_callback=lambda lane, s, m: signals.slow_update.emit(lane, s, m)
        )
        self.discovery = DiscoveryManager(
            status_callback=lambda src, msg: signals.log_message.emit(f"[{src}] {msg}")
        )
        self.inbox = InboxMonitorLoop(
            on_new_response=lambda r: signals.notification.emit(r)
        )

        from ui.extra_effort_tab import ExtraEffortTab
        from ui.research_library_tab import ResearchLibraryTab
        from ui.settings_tab import SettingsTab
        from ui.tracker_tab import TrackerTab as FullTrackerTab

        tabs = QTabWidget()
        tabs.addTab(
            DashboardTab(self.fast_manager, self.slow_manager,
                         self.discovery, self.inbox),
            "⚡ Dashboard"
        )
        tabs.addTab(FullTrackerTab(),                        "📋 Applications")
        tabs.addTab(InboxTab(),                              "📬 Inbox")
        tabs.addTab(ExtraEffortTab(),                        "🎯 Extra Effort")
        tabs.addTab(ResearchLibraryTab(),                    "📚 Research Library")
        tabs.addTab(AIChatTab(self.fast_manager, self.slow_manager), "🤖 AI Chat")
        tabs.addTab(SettingsTab(),                           "⚙️ Settings")

        self.setCentralWidget(tabs)

        # ── Approval queue timer (polls every 300ms for background track requests) ──
        self._approval_timer = QTimer()
        self._approval_timer.timeout.connect(self._check_approval_queue)
        self._approval_timer.start(300)

        status = QStatusBar()
        status.showMessage(
            "AutoApplyAI ready — click ▶ Start Applying on the Dashboard to begin"
        )
        self.setStatusBar(status)

    # BUG FIX: closeEvent must be a class method, NOT a nested function inside __init__
    def closeEvent(self, event):
        self.fast_manager.stop()
        self.slow_manager.stop()
        self.discovery.stop()
        self.inbox.stop()
        event.accept()

    def _check_approval_queue(self):
        """Poll for background track approval requests and show dialog on main thread."""
        from ui.approval_queue import get_pending_request
        request = get_pending_request()
        if not request:
            return
        job_data, insight, cover_letter, done_event, result = request
        from ui.approval_dialog import ApprovalDialog
        dialog = ApprovalDialog(job_data, insight, cover_letter, self)
        dialog.exec()
        action, edited_cl = dialog.get_result()
        result["action"] = action
        result["cover_letter"] = edited_cl
        done_event.set()


# ─── Entry Point ──────────────────────────────────────────────────────────────

# BUG FIX: Module-level reference prevents Python GC from destroying the window
_main_window = None


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("AutoApplyAI")

    store = get_store()

    if not store.is_onboarded():
        wizard = SetupWizard()
        wizard.setup_complete.connect(lambda: _show_main(app))
        wizard.show()
    else:
        _show_main(app)

    sys.exit(app.exec())


def _show_main(app: QApplication):
    global _main_window
    # BUG FIX: store in module-level var so Python doesn't garbage-collect it
    # Previously: window = MainWindow() — local var, GC'd instantly = invisible window
    _main_window = MainWindow()
    _main_window.showMaximized()
    _main_window.raise_()
    _main_window.activateWindow()


if __name__ == "__main__":
    main()
