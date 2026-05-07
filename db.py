import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    firma TEXT NOT NULL,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    art TEXT NOT NULL,
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    art TEXT NOT NULL UNIQUE,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sent_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER,
    contact_email TEXT NOT NULL,
    art TEXT NOT NULL,
    subject TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT DEFAULT '',
    sent_at TEXT DEFAULT (datetime('now'))
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as c:
        c.executescript(SCHEMA)


# Contacts
def list_contacts(art: str | None = None):
    with get_conn() as c:
        if art:
            rows = c.execute(
                "SELECT * FROM contacts WHERE art = ? ORDER BY firma COLLATE NOCASE",
                (art,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM contacts ORDER BY firma COLLATE NOCASE"
            ).fetchall()
        return [dict(r) for r in rows]


def add_contact(firma, name, email, art, notes=""):
    with get_conn() as c:
        c.execute(
            "INSERT INTO contacts (firma, name, email, art, notes) VALUES (?, ?, ?, ?, ?)",
            (firma.strip(), name.strip(), email.strip(), art.strip(), notes.strip()),
        )


def update_contact(cid, firma, name, email, art, notes=""):
    with get_conn() as c:
        c.execute(
            "UPDATE contacts SET firma=?, name=?, email=?, art=?, notes=? WHERE id=?",
            (firma.strip(), name.strip(), email.strip(), art.strip(), notes.strip(), cid),
        )


def delete_contact(cid):
    with get_conn() as c:
        c.execute("DELETE FROM contacts WHERE id=?", (cid,))


def get_contact(cid):
    with get_conn() as c:
        row = c.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone()
        return dict(row) if row else None


def list_arten():
    with get_conn() as c:
        rows = c.execute(
            "SELECT DISTINCT art FROM contacts UNION SELECT art FROM templates ORDER BY art COLLATE NOCASE"
        ).fetchall()
        return [r["art"] for r in rows if r["art"]]


# Templates
def list_templates():
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM templates ORDER BY art COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]


def get_template_by_art(art: str):
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM templates WHERE art=?", (art,)
        ).fetchone()
        return dict(row) if row else None


def upsert_template(art, subject, body):
    with get_conn() as c:
        c.execute(
            """
            INSERT INTO templates (art, subject, body, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(art) DO UPDATE SET
                subject=excluded.subject,
                body=excluded.body,
                updated_at=datetime('now')
            """,
            (art.strip(), subject, body),
        )


def delete_template(tid):
    with get_conn() as c:
        c.execute("DELETE FROM templates WHERE id=?", (tid,))


# Settings
def get_setting(key: str, default: str = "") -> str:
    with get_conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_conn() as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def all_settings() -> dict:
    with get_conn() as c:
        rows = c.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}


# Log
def log_send(contact_id, contact_email, art, subject, status, error=""):
    with get_conn() as c:
        c.execute(
            "INSERT INTO sent_log (contact_id, contact_email, art, subject, status, error) VALUES (?, ?, ?, ?, ?, ?)",
            (contact_id, contact_email, art, subject, status, error),
        )


def list_log(limit=100):
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM sent_log ORDER BY sent_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
