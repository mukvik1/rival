import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
DB_PATH = os.getenv("DB_PATH", "rival.db")


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                phone TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                master_id TEXT NOT NULL,
                service_id TEXT NOT NULL,
                visit_date TEXT NOT NULL,
                visit_time TEXT NOT NULL,
                phone TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
            );

            CREATE INDEX IF NOT EXISTS idx_bookings_user
                ON bookings(telegram_id, visit_date, visit_time);
            """
        )


def now_iso():
    return datetime.now(KYIV).isoformat(timespec="seconds")


def upsert_user(telegram_id: int, username: str | None, first_name: str | None, phone: str | None = None):
    now = now_iso()
    with conn() as c:
        c.execute(
            """
            INSERT INTO users(telegram_id, username, first_name, phone, created_at, updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                phone=COALESCE(excluded.phone, users.phone),
                updated_at=excluded.updated_at
            """,
            (telegram_id, username, first_name, phone, now, now),
        )


def get_user(telegram_id: int):
    with conn() as c:
        row = c.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
        return dict(row) if row else None


def create_booking(telegram_id: int, master_id: str, service_id: str, visit_date: str, visit_time: str, phone: str):
    now = now_iso()
    with conn() as c:
        cur = c.execute(
            """
            INSERT INTO bookings(telegram_id, master_id, service_id, visit_date, visit_time, phone, status, created_at, updated_at)
            VALUES(?,?,?,?,?,?, 'pending', ?, ?)
            """,
            (telegram_id, master_id, service_id, visit_date, visit_time, phone, now, now),
        )
        return cur.lastrowid


def get_booking(booking_id: int):
    with conn() as c:
        row = c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        return dict(row) if row else None


def get_user_bookings(telegram_id: int, limit: int = 10):
    with conn() as c:
        rows = c.execute(
            """
            SELECT * FROM bookings
            WHERE telegram_id=? AND status != 'cancelled'
            ORDER BY visit_date ASC, visit_time ASC
            LIMIT ?
            """,
            (telegram_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_pending_bookings(limit: int = 20):
    with conn() as c:
        rows = c.execute(
            """
            SELECT * FROM bookings
            WHERE status='pending'
            ORDER BY visit_date ASC, visit_time ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def set_booking_status(booking_id: int, status: str):
    if status not in {"pending", "confirmed", "cancelled"}:
        raise ValueError("Unsupported booking status")
    with conn() as c:
        c.execute(
            "UPDATE bookings SET status=?, updated_at=? WHERE id=?",
            (status, now_iso(), booking_id),
        )
        row = c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        return dict(row) if row else None
