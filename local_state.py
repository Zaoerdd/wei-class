"""Persistent local controls and confirmed attendance history."""
import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class LocalState:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS attendance (
                account TEXT NOT NULL, course_id TEXT NOT NULL, sign_id TEXT NOT NULL,
                confirmed_at TEXT NOT NULL, student_name TEXT, course_name TEXT,
                task_name TEXT, sign_type TEXT, message TEXT, sign_rank INTEGER,
                PRIMARY KEY (account, course_id, sign_id))""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def listening_enabled(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='listening'").fetchone()
            return bool(row and row[0] == "1")

    def set_listening(self, enabled):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('listening', ?)", ("1" if enabled else "0",))

    @staticmethod
    def account_key(openid, profile):
        student = str((profile or {}).get("student_number") or "").strip()
        identity = "student:" + student if student else "openid:" + openid
        return hashlib.sha256(identity.encode()).hexdigest()

    def has_success(self, openid, profile, course_id, sign_id):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM attendance WHERE account=? AND course_id=? AND sign_id=?",
                              (self.account_key(openid, profile), str(course_id), str(sign_id))).fetchone() is not None

    def record_success(self, openid, profile, meta):
        if not meta.get("sign_completed") or meta.get("is_error"):
            return
        if meta.get("course_id") is None or meta.get("sign_id") is None:
            return
        with self.connect() as db:
            db.execute("""INSERT OR IGNORE INTO attendance VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                self.account_key(openid, profile), str(meta["course_id"]), str(meta["sign_id"]),
                datetime.now().astimezone().isoformat(), (profile or {}).get("name"),
                meta.get("course_name"), meta.get("task_name"), meta.get("sign_type"),
                meta.get("frontend_message") or meta.get("status_message"), meta.get("sign_rank")))

    def history(self):
        with self.connect() as db:
            rows = db.execute("""SELECT confirmed_at, student_name, course_id, sign_id, course_name,
                task_name, sign_type, message, sign_rank FROM attendance ORDER BY confirmed_at DESC LIMIT 100""").fetchall()
            count = db.execute("SELECT COUNT(*) FROM attendance").fetchone()[0]
            return {"records": [dict(row) for row in rows], "total": count}
