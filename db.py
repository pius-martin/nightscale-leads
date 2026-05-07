import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. On Railway, link the Postgres service "
            "via Variables → Add Reference → ${{Postgres.DATABASE_URL}}."
        )
    return url


SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id SERIAL PRIMARY KEY,
    firma TEXT NOT NULL,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    art TEXT NOT NULL,
    notes TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS templates (
    id SERIAL PRIMARY KEY,
    art TEXT NOT NULL UNIQUE,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS arten (
    name TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sent_log (
    id SERIAL PRIMARY KEY,
    contact_id INTEGER,
    contact_email TEXT NOT NULL,
    art TEXT NOT NULL,
    subject TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT DEFAULT '',
    sent_at TIMESTAMPTZ DEFAULT NOW()
);
"""


@contextmanager
def get_conn():
    conn = psycopg2.connect(_database_url())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _dict_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    with get_conn() as c, c.cursor() as cur:
        cur.execute(SCHEMA)
        # Backfill arten from existing data
        cur.execute(
            """
            INSERT INTO arten (name)
            SELECT DISTINCT art FROM contacts
            WHERE art IS NOT NULL AND art <> ''
            ON CONFLICT DO NOTHING
            """
        )
        cur.execute(
            """
            INSERT INTO arten (name)
            SELECT DISTINCT art FROM templates
            WHERE art IS NOT NULL AND art <> ''
            ON CONFLICT DO NOTHING
            """
        )


# Arten (master list)
def list_arten():
    with get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT name FROM arten ORDER BY LOWER(name)")
        return [r[0] for r in cur.fetchall()]


def add_art(name: str):
    name = name.strip()
    if not name:
        return
    with get_conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO arten (name) VALUES (%s) ON CONFLICT DO NOTHING",
            (name,),
        )


def delete_art(name: str):
    with get_conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM arten WHERE name=%s", (name,))


def art_usage_count(name: str) -> int:
    with get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM contacts WHERE art=%s", (name,))
        return cur.fetchone()[0]


# Contacts
def list_contacts(art: str | None = None):
    with get_conn() as c:
        cur = _dict_cursor(c)
        if art:
            cur.execute(
                "SELECT * FROM contacts WHERE art = %s ORDER BY LOWER(firma)",
                (art,),
            )
        else:
            cur.execute("SELECT * FROM contacts ORDER BY LOWER(firma)")
        return [dict(r) for r in cur.fetchall()]


def add_contact(firma, name, email, art, notes=""):
    with get_conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO contacts (firma, name, email, art, notes) VALUES (%s, %s, %s, %s, %s)",
            (firma.strip(), name.strip(), email.strip(), art.strip(), notes.strip()),
        )


def update_contact(cid, firma, name, email, art, notes=""):
    with get_conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE contacts SET firma=%s, name=%s, email=%s, art=%s, notes=%s WHERE id=%s",
            (firma.strip(), name.strip(), email.strip(), art.strip(), notes.strip(), cid),
        )


def delete_contact(cid):
    with get_conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM contacts WHERE id=%s", (cid,))


def get_contact(cid):
    with get_conn() as c:
        cur = _dict_cursor(c)
        cur.execute("SELECT * FROM contacts WHERE id=%s", (cid,))
        row = cur.fetchone()
        return dict(row) if row else None


# Templates
def list_templates():
    with get_conn() as c:
        cur = _dict_cursor(c)
        cur.execute("SELECT * FROM templates ORDER BY LOWER(art)")
        return [dict(r) for r in cur.fetchall()]


def get_template_by_art(art: str):
    with get_conn() as c:
        cur = _dict_cursor(c)
        cur.execute("SELECT * FROM templates WHERE art=%s", (art,))
        row = cur.fetchone()
        return dict(row) if row else None


def upsert_template(art, subject, body):
    with get_conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO templates (art, subject, body, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (art) DO UPDATE SET
                subject = EXCLUDED.subject,
                body = EXCLUDED.body,
                updated_at = NOW()
            """,
            (art.strip(), subject, body),
        )


def delete_template(tid):
    with get_conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM templates WHERE id=%s", (tid,))


# Settings
def get_setting(key: str, default: str = "") -> str:
    with get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key=%s", (key,))
        row = cur.fetchone()
        return row[0] if row else default


def set_setting(key: str, value: str):
    with get_conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO settings (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (key, value),
        )


def all_settings() -> dict:
    with get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT key, value FROM settings")
        return {k: v for k, v in cur.fetchall()}


def delete_setting(key: str):
    with get_conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM settings WHERE key=%s", (key,))


# Log
def log_send(contact_id, contact_email, art, subject, status, error=""):
    with get_conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO sent_log (contact_id, contact_email, art, subject, status, error) VALUES (%s, %s, %s, %s, %s, %s)",
            (contact_id, contact_email, art, subject, status, error),
        )


def list_log(limit=100):
    with get_conn() as c:
        cur = _dict_cursor(c)
        cur.execute(
            "SELECT * FROM sent_log ORDER BY sent_at DESC LIMIT %s",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]
