"""
core/settings_store.py
Encrypted persistent settings using SQLite + Fernet encryption.
All data survives app close. Only wiped on explicit sign-out request.
user_id field on all records — ready for multi-user without schema changes.
"""

import sqlite3
import json
import os
import hashlib
from pathlib import Path
from cryptography.fernet import Fernet
from typing import Any, Optional
import base64

# App data directory — platform appropriate
APP_DIR = Path.home() / ".autoapplyai"
APP_DIR.mkdir(exist_ok=True)
DB_PATH = APP_DIR / "data.db"
KEY_PATH = APP_DIR / ".key"
DEFAULT_USER_ID = 1  # Single user for now — field exists for future multi-user


def _get_or_create_key() -> bytes:
    """Load or generate encryption key. Stored locally."""
    if KEY_PATH.exists():
        return KEY_PATH.read_bytes()
    key = Fernet.generate_key()
    KEY_PATH.write_bytes(key)
    KEY_PATH.chmod(0o600)  # Owner read/write only
    return key


def _fernet() -> Fernet:
    return Fernet(_get_or_create_key())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()


class SettingsStore:
    """
    Thread-safe encrypted settings store.
    Usage:
        store = SettingsStore()
        store.set("claude_api_key", "sk-...")
        key = store.get("claude_api_key")
    """

    # Keys that are encrypted at rest
    SENSITIVE_KEYS = {
        "claude_api_key", "openai_api_key", "gptzero_api_key",
        "gmail_token", "linkedin_token", "reddit_token", "github_token",
        "master_password"
    }

    def __init__(self):
        self.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self._init_tables()

    def _init_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                user_id     INTEGER NOT NULL DEFAULT 1,
                key         TEXT    NOT NULL,
                value       TEXT,
                encrypted   INTEGER NOT NULL DEFAULT 0,
                updated_at  TEXT    DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, key)
            );

            CREATE TABLE IF NOT EXISTS user_profile (
                user_id         INTEGER PRIMARY KEY DEFAULT 1,
                full_name       TEXT,
                email           TEXT,
                phone           TEXT,
                address         TEXT,
                linkedin_url    TEXT,
                github_url      TEXT,
                portfolio_url   TEXT,
                graduation_date TEXT,
                gpa             TEXT,
                work_auth       TEXT,
                visa_status     TEXT,
                background_text TEXT,
                strengths_text  TEXT,
                resume_path     TEXT,
                resume_parsed   TEXT,
                salary_min      INTEGER,
                salary_max      INTEGER,
                locations       TEXT,
                job_types       TEXT,
                target_roles    TEXT,
                dream_criteria  TEXT,
                platforms       TEXT,
                updated_at      TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS learned_answers (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL DEFAULT 1,
                question_pattern TEXT NOT NULL,
                answer      TEXT NOT NULL,
                tags        TEXT,
                use_count   INTEGER DEFAULT 1,
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS applications (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL DEFAULT 1,
                job_title       TEXT,
                company_name    TEXT,
                job_url         TEXT,
                ats_url         TEXT,
                platform        TEXT,
                status          TEXT DEFAULT 'pending',
                cover_letter    TEXT,
                resume_used     TEXT,
                applied_at      TEXT,
                response_at     TEXT,
                response_type   TEXT,
                response_text   TEXT,
                track_id        INTEGER,
                lane_type       TEXT DEFAULT 'fast',
                score           REAL,
                notes           TEXT,
                paused_reason   TEXT,
                created_at      TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS company_profiles (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name    TEXT UNIQUE NOT NULL,
                domain          TEXT,
                personality     TEXT,
                core_values     TEXT,
                culture_signals TEXT,
                red_flags       TEXT,
                tone            TEXT,
                keywords        TEXT,
                raw_research    TEXT,
                source_count    INTEGER DEFAULT 0,
                last_updated    TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS job_profiles (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id      INTEGER REFERENCES company_profiles(id),
                job_title       TEXT NOT NULL,
                what_they_want  TEXT,
                common_questions TEXT,
                success_signals TEXT,
                keywords        TEXT,
                last_updated    TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS advice_db (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                advice_type     TEXT NOT NULL,
                content         TEXT NOT NULL,
                source_url      TEXT,
                source_platform TEXT,
                mention_count   INTEGER DEFAULT 1,
                success_score   REAL DEFAULT 0.0,
                tags            TEXT,
                created_at      TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS template_success (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                template_type   TEXT NOT NULL,
                template_text   TEXT NOT NULL,
                use_count       INTEGER DEFAULT 0,
                response_count  INTEGER DEFAULT 0,
                success_rate    REAL DEFAULT 0.0,
                keywords        TEXT,
                updated_at      TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS extra_effort_contacts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id  INTEGER REFERENCES applications(id),
                user_id         INTEGER NOT NULL DEFAULT 1,
                person_name     TEXT,
                person_title    TEXT,
                company_name    TEXT,
                platform        TEXT,
                profile_url     TEXT,
                contact_handle  TEXT,
                flags           TEXT,
                flag_labels     TEXT,
                draft_message   TEXT,
                sent            INTEGER DEFAULT 0,
                sent_at         TEXT,
                response        TEXT,
                priority_score  REAL DEFAULT 0.0,
                created_at      TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL DEFAULT 1,
                application_id  INTEGER REFERENCES applications(id),
                notif_type      TEXT NOT NULL,
                title           TEXT,
                message         TEXT,
                status          TEXT DEFAULT 'pending',
                user_response   TEXT,
                created_at      TEXT DEFAULT (datetime('now')),
                resolved_at     TEXT
            );
        """)
        self.conn.commit()

    # --- Settings key/value store ---

    def set(self, key: str, value: Any, user_id: int = DEFAULT_USER_ID):
        """Store a setting. Sensitive keys are auto-encrypted."""
        str_value = json.dumps(value) if not isinstance(value, str) else value
        is_sensitive = key in self.SENSITIVE_KEYS
        stored = encrypt(str_value) if is_sensitive else str_value
        self.conn.execute("""
            INSERT INTO settings (user_id, key, value, encrypted, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(user_id, key) DO UPDATE SET
                value=excluded.value,
                encrypted=excluded.encrypted,
                updated_at=excluded.updated_at
        """, (user_id, key, stored, int(is_sensitive)))
        self.conn.commit()

    def get(self, key: str, default=None, user_id: int = DEFAULT_USER_ID) -> Any:
        """Retrieve a setting, auto-decrypting if needed."""
        row = self.conn.execute(
            "SELECT value, encrypted FROM settings WHERE user_id=? AND key=?",
            (user_id, key)
        ).fetchone()
        if not row:
            return default
        value, encrypted = row
        if encrypted:
            value = decrypt(value)
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value

    def delete(self, key: str, user_id: int = DEFAULT_USER_ID):
        self.conn.execute(
            "DELETE FROM settings WHERE user_id=? AND key=?", (user_id, key)
        )
        self.conn.commit()

    # --- User profile ---

    def save_profile(self, profile: dict, user_id: int = DEFAULT_USER_ID):
        """Upsert user profile."""
        profile["user_id"] = user_id
        cols = ", ".join(profile.keys())
        placeholders = ", ".join("?" * len(profile))
        updates = ", ".join(
            f"{k}=excluded.{k}" for k in profile if k != "user_id"
        )
        self.conn.execute(f"""
            INSERT INTO user_profile ({cols})
            VALUES ({placeholders})
            ON CONFLICT(user_id) DO UPDATE SET {updates}, updated_at=datetime('now')
        """, list(profile.values()))
        self.conn.commit()

    def get_profile(self, user_id: int = DEFAULT_USER_ID) -> Optional[dict]:
        """Return full user profile as dict."""
        row = self.conn.execute(
            "SELECT * FROM user_profile WHERE user_id=?", (user_id,)
        ).fetchone()
        if not row:
            return None
        cols = [d[0] for d in self.conn.execute(
            "SELECT * FROM user_profile WHERE user_id=?", (user_id,)
        ).description]
        # Re-run to get description properly
        cursor = self.conn.execute(
            "SELECT * FROM user_profile WHERE user_id=?", (user_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        return dict(zip([d[0] for d in cursor.description], row))

    # --- Learned answers ---

    def save_learned_answer(self, question_pattern: str, answer: str,
                             tags: list = None, user_id: int = DEFAULT_USER_ID):
        """Store a new answer learned from user clarification."""
        existing = self.conn.execute("""
            SELECT id, use_count FROM learned_answers
            WHERE user_id=? AND question_pattern=?
        """, (user_id, question_pattern)).fetchone()

        if existing:
            self.conn.execute("""
                UPDATE learned_answers SET answer=?, use_count=use_count+1,
                updated_at=datetime('now') WHERE id=?
            """, (answer, existing[0]))
        else:
            self.conn.execute("""
                INSERT INTO learned_answers (user_id, question_pattern, answer, tags)
                VALUES (?, ?, ?, ?)
            """, (user_id, question_pattern, answer, json.dumps(tags or [])))
        self.conn.commit()

    def find_learned_answer(self, question_text: str,
                             user_id: int = DEFAULT_USER_ID) -> Optional[str]:
        """Find best matching stored answer for a question."""
        answers = self.conn.execute("""
            SELECT question_pattern, answer, use_count FROM learned_answers
            WHERE user_id=? ORDER BY use_count DESC
        """, (user_id,)).fetchall()

        question_lower = question_text.lower()
        for pattern, answer, _ in answers:
            if any(word in question_lower for word in pattern.lower().split()):
                return answer
        return None

    # --- Application logging ---

    def log_application(self, data: dict, user_id: int = DEFAULT_USER_ID) -> int:
        data["user_id"] = user_id
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" * len(data))
        cursor = self.conn.execute(
            f"INSERT INTO applications ({cols}) VALUES ({placeholders})",
            list(data.values())
        )
        self.conn.commit()
        return cursor.lastrowid

    def update_application(self, app_id: int, updates: dict):
        sets = ", ".join(f"{k}=?" for k in updates)
        self.conn.execute(
            f"UPDATE applications SET {sets} WHERE id=?",
            list(updates.values()) + [app_id]
        )
        self.conn.commit()

    def get_applications(self, user_id: int = DEFAULT_USER_ID,
                          status: str = None, limit: int = 500) -> list:
        query = "SELECT * FROM applications WHERE user_id=?"
        params = [user_id]
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cursor = self.conn.execute(query, params)
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    # --- Company profiles ---

    def save_company_profile(self, data: dict) -> int:
        existing = self.conn.execute(
            "SELECT id FROM company_profiles WHERE company_name=?",
            (data["company_name"],)
        ).fetchone()

        if existing:
            sets = ", ".join(
                f"{k}=?" for k in data if k != "company_name"
            )
            sets += ", last_updated=datetime('now'), source_count=source_count+1"
            self.conn.execute(
                f"UPDATE company_profiles SET {sets} WHERE company_name=?",
                [v for k, v in data.items() if k != "company_name"] + [data["company_name"]]
            )
            self.conn.commit()
            return existing[0]
        else:
            cols = ", ".join(data.keys())
            placeholders = ", ".join("?" * len(data))
            cursor = self.conn.execute(
                f"INSERT INTO company_profiles ({cols}) VALUES ({placeholders})",
                list(data.values())
            )
            self.conn.commit()
            return cursor.lastrowid

    def get_company_profile(self, company_name: str) -> Optional[dict]:
        cursor = self.conn.execute(
            "SELECT * FROM company_profiles WHERE company_name=?", (company_name,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        return dict(zip([d[0] for d in cursor.description], row))

    # --- Notifications ---

    def add_notification(self, notif_type: str, title: str, message: str,
                          application_id: int = None,
                          user_id: int = DEFAULT_USER_ID) -> int:
        cursor = self.conn.execute("""
            INSERT INTO notifications (user_id, application_id, notif_type, title, message)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, application_id, notif_type, title, message))
        self.conn.commit()
        return cursor.lastrowid

    def get_pending_notifications(self, user_id: int = DEFAULT_USER_ID) -> list:
        cursor = self.conn.execute("""
            SELECT * FROM notifications WHERE user_id=? AND status='pending'
            ORDER BY created_at DESC
        """, (user_id,))
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def resolve_notification(self, notif_id: int, user_response: str):
        self.conn.execute("""
            UPDATE notifications SET status='resolved', user_response=?,
            resolved_at=datetime('now') WHERE id=?
        """, (user_response, notif_id))
        self.conn.commit()

    # --- Extra effort contacts ---

    def save_contact(self, data: dict, user_id: int = DEFAULT_USER_ID) -> int:
        data["user_id"] = user_id
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" * len(data))
        cursor = self.conn.execute(
            f"INSERT INTO extra_effort_contacts ({cols}) VALUES ({placeholders})",
            list(data.values())
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_contacts(self, user_id: int = DEFAULT_USER_ID,
                      sent: bool = False) -> list:
        cursor = self.conn.execute("""
            SELECT * FROM extra_effort_contacts
            WHERE user_id=? AND sent=?
            ORDER BY priority_score DESC
        """, (user_id, int(sent)))
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def is_onboarded(self, user_id: int = DEFAULT_USER_ID) -> bool:
        """Check if user has completed onboarding."""
        return bool(self.get("onboarding_complete", False, user_id))

    def mark_onboarded(self, user_id: int = DEFAULT_USER_ID):
        self.set("onboarding_complete", True, user_id)

    def close(self):
        self.conn.close()


# Singleton
_store_instance = None

def get_store() -> SettingsStore:
    global _store_instance
    if _store_instance is None:
        _store_instance = SettingsStore()
    return _store_instance
