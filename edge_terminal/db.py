"""
edge_terminal/db.py
Local SQLite log storage for the offline Edge Gate Terminal.
"""
import sqlite3
import json
from datetime import datetime, timedelta

DB_PATH = "edge_terminal_logs_default.db"

# Time offset in minutes — set by conductor to correct clock drift
_time_offset_minutes: int = 0

def set_time_offset(minutes: int):
    global _time_offset_minutes
    _time_offset_minutes = minutes

def get_time_offset() -> int:
    return _time_offset_minutes

def get_adjusted_now() -> datetime:
    """Returns datetime.now() adjusted by the configured offset."""
    return datetime.now() + timedelta(minutes=_time_offset_minutes)

def set_db_path(exam_id: int):
    global DB_PATH
    DB_PATH = f"edge_logs_exam_{exam_id}.db"
    init_db()

def init_db():
    """Create the local attendance log table if it does not exist."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS attendance_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reg_no TEXT NOT NULL,
                name TEXT,
                session_id INTEGER,
                session_label TEXT,
                center_name TEXT,
                timestamp TEXT NOT NULL,
                status TEXT NOT NULL,
                confidence REAL,
                synced INTEGER DEFAULT 0
            )
        """)
        conn.commit()

def insert_log(reg_no: str, name: str, session_id: int, session_label: str, 
               center_name: str, status: str, confidence: float):
    ts = get_adjusted_now().strftime("%Y-%m-%d %I:%M:%S %p")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO attendance_logs (reg_no, name, session_id, session_label, center_name, timestamp, status, confidence) VALUES (?,?,?,?,?,?,?,?)",
            (reg_no, name, session_id, session_label, center_name, ts, status, confidence)
        )
        conn.commit()
    return ts

def get_all_logs():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM attendance_logs ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]

def check_already_verified(reg_no: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        res = conn.execute("SELECT COUNT(*) FROM attendance_logs WHERE reg_no=? AND status='PASS'", (reg_no,)).fetchone()[0]
        return res > 0

def get_summary():
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM attendance_logs").fetchone()[0]
        passed = conn.execute("SELECT COUNT(*) FROM attendance_logs WHERE status='PASS'").fetchone()[0]
        failed = conn.execute("SELECT COUNT(*) FROM attendance_logs WHERE status='FAIL'").fetchone()[0]
        unsynced = conn.execute("SELECT COUNT(*) FROM attendance_logs WHERE synced=0").fetchone()[0]
    return {"total": total, "passed": passed, "failed": failed, "unsynced": unsynced}

def mark_logs_synced():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE attendance_logs SET synced=1")
        conn.commit()

def mark_exam_completed():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS exam_status (is_locked INTEGER)")
        conn.execute("INSERT INTO exam_status (is_locked) VALUES (1)")
        conn.commit()

def is_exam_completed() -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        try:
            res = conn.execute("SELECT is_locked FROM exam_status LIMIT 1").fetchone()
            return res is not None and res[0] == 1
        except Exception:
            return False
