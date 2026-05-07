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
    first_name TEXT NOT NULL DEFAULT '',
    last_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL,
    art TEXT NOT NULL,
    notes TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS templates (
    id SERIAL PRIMARY KEY,
    art TEXT NOT NULL,
    variant TEXT NOT NULL DEFAULT 'personal',
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    footer TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (art, variant)
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

MIGRATIONS = """
-- Ensure new columns exist on legacy tables
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS first_name TEXT NOT NULL DEFAULT '';
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS last_name TEXT NOT NULL DEFAULT '';
ALTER TABLE templates ADD COLUMN IF NOT EXISTS footer TEXT NOT NULL DEFAULT '';
ALTER TABLE templates ADD COLUMN IF NOT EXISTS variant TEXT NOT NULL DEFAULT 'personal';

-- Replace the old single-art unique constraint with a (art, variant) one
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'templates_art_key') THEN
        ALTER TABLE templates DROP CONSTRAINT templates_art_key;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'templates_art_variant_key') THEN
        ALTER TABLE templates ADD CONSTRAINT templates_art_variant_key UNIQUE (art, variant);
    END IF;
END $$;

-- Migrate single 'name' into first_name/last_name and drop the old column
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='contacts' AND column_name='name') THEN
        UPDATE contacts SET
            first_name = SPLIT_PART(COALESCE(name, ''), ' ', 1),
            last_name = CASE
                WHEN POSITION(' ' IN COALESCE(name, '')) > 0
                    THEN TRIM(SUBSTRING(name FROM POSITION(' ' IN name) + 1))
                ELSE ''
            END
        WHERE first_name = '' AND last_name = '' AND name IS NOT NULL;
        ALTER TABLE contacts DROP COLUMN name;
    END IF;
END $$;
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
        cur.execute(MIGRATIONS)
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
def _attach_full_name(row: dict) -> dict:
    fn = (row.get("first_name") or "").strip()
    ln = (row.get("last_name") or "").strip()
    row["name"] = f"{fn} {ln}".strip()
    return row


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
        return [_attach_full_name(dict(r)) for r in cur.fetchall()]


def add_contact(firma, first_name, last_name, email, art, notes=""):
    with get_conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO contacts (firma, first_name, last_name, email, art, notes) VALUES (%s, %s, %s, %s, %s, %s)",
            (firma.strip(), first_name.strip(), last_name.strip(), email.strip(), art.strip(), notes.strip()),
        )


def update_contact(cid, firma, first_name, last_name, email, art, notes=""):
    with get_conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE contacts SET firma=%s, first_name=%s, last_name=%s, email=%s, art=%s, notes=%s WHERE id=%s",
            (firma.strip(), first_name.strip(), last_name.strip(), email.strip(), art.strip(), notes.strip(), cid),
        )


def delete_contact(cid):
    with get_conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM contacts WHERE id=%s", (cid,))


def get_contact(cid):
    with get_conn() as c:
        cur = _dict_cursor(c)
        cur.execute("SELECT * FROM contacts WHERE id=%s", (cid,))
        row = cur.fetchone()
        return _attach_full_name(dict(row)) if row else None


def sent_contact_ids(art: str) -> set:
    """Returns set of contact_ids that have already received a 'sent' email for this art."""
    with get_conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT contact_id FROM sent_log
            WHERE art=%s AND status='sent' AND contact_id IS NOT NULL
            """,
            (art,),
        )
        return {r[0] for r in cur.fetchall()}


# Templates
VARIANTS = ("personal", "anonymous")


def list_templates():
    with get_conn() as c:
        cur = _dict_cursor(c)
        cur.execute("SELECT * FROM templates ORDER BY LOWER(art), variant")
        return [dict(r) for r in cur.fetchall()]


def get_template(art: str, variant: str = "personal"):
    if variant not in VARIANTS:
        variant = "personal"
    with get_conn() as c:
        cur = _dict_cursor(c)
        cur.execute("SELECT * FROM templates WHERE art=%s AND variant=%s", (art, variant))
        row = cur.fetchone()
        return dict(row) if row else None


def get_templates_for_art(art: str) -> dict:
    """Return {variant: row} for a given art."""
    with get_conn() as c:
        cur = _dict_cursor(c)
        cur.execute("SELECT * FROM templates WHERE art=%s", (art,))
        return {r["variant"]: dict(r) for r in cur.fetchall()}


def pick_template_for_contact(art: str, contact: dict):
    has_name = bool(
        (contact.get("first_name") or "").strip()
        or (contact.get("last_name") or "").strip()
    )
    primary = "personal" if has_name else "anonymous"
    t = get_template(art, primary)
    if t is None:
        # fallback to the other variant if only one exists
        other = "anonymous" if primary == "personal" else "personal"
        t = get_template(art, other)
    return t


def upsert_template(art, variant, subject, body, footer=""):
    if variant not in VARIANTS:
        variant = "personal"
    with get_conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO templates (art, variant, subject, body, footer, updated_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (art, variant) DO UPDATE SET
                subject = EXCLUDED.subject,
                body = EXCLUDED.body,
                footer = EXCLUDED.footer,
                updated_at = NOW()
            """,
            (art.strip(), variant, subject, body, footer or ""),
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
