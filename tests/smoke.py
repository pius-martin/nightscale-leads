"""Offline smoke test — runs without Postgres or a Microsoft account.

Replaces every db.* function with an in-memory fake and stubs graph_mail's
network calls, then drives the real Flask app through its test client:
every page is rendered, every form is posted (with and without CSRF token).

Run:  python tests/smoke.py
"""
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AZURE_CLIENT_ID", "test-client")
os.environ.setdefault("AZURE_TENANT_ID", "test-tenant")
os.environ["APP_PASSWORD"] = ""

import db
import graph_mail


# ---------------------------------------------------------------------------
# In-memory fake for db.*
# ---------------------------------------------------------------------------
class FakeState:
    def __init__(self):
        self.arten = []
        self.contacts = {}
        self.next_cid = 1
        self.templates = {}  # (art, variant) -> dict
        self.next_tid = 1
        self.settings = {}
        self.ms_accounts = {}
        self.sent_log = []


S = FakeState()


def _full_name(row):
    row["name"] = f"{row['first_name']} {row['last_name']}".strip()
    return row


def install_fake_db():
    db.init_db = lambda: None
    db.list_arten = lambda: sorted(S.arten, key=str.lower)
    db.add_art = lambda name: S.arten.append(name.strip()) if name.strip() and name.strip() not in S.arten else None
    db.delete_art = lambda name: S.arten.remove(name) if name in S.arten else None
    db.art_usage_count = lambda name: sum(1 for c in S.contacts.values() if c["art"] == name)

    def add_contact(firma, first_name, last_name, email, art, notes="", pos_system=""):
        cid = S.next_cid
        S.next_cid += 1
        S.contacts[cid] = _full_name({
            "id": cid, "firma": firma.strip(), "first_name": first_name.strip(),
            "last_name": last_name.strip(), "email": email.strip(), "art": art.strip(),
            "pos_system": pos_system.strip(), "notes": notes.strip(), "created_at": None,
        })
    db.add_contact = add_contact

    def update_contact(cid, firma, first_name, last_name, email, art, notes="", pos_system=""):
        S.contacts[cid].update(_full_name({
            "firma": firma.strip(), "first_name": first_name.strip(),
            "last_name": last_name.strip(), "email": email.strip(),
            "art": art.strip(), "pos_system": pos_system.strip(), "notes": notes.strip(),
        }))
    db.update_contact = update_contact

    db.delete_contact = lambda cid: S.contacts.pop(cid, None)
    db.get_contact = lambda cid: dict(S.contacts[cid]) if cid in S.contacts else None
    db.list_contacts = lambda art=None: [
        dict(c) for c in S.contacts.values() if art is None or c["art"] == art
    ]
    db.sent_contact_ids = lambda art: {
        l["contact_id"] for l in S.sent_log
        if l["art"] == art and l["status"] == "sent" and l["contact_id"] is not None
    }

    def upsert_template(art, variant, subject, body, footer=""):
        key = (art.strip(), variant)
        row = S.templates.get(key) or {"id": S.next_tid, "art": art.strip(), "variant": variant}
        if key not in S.templates:
            S.next_tid += 1
        row.update(subject=subject, body=body, footer=footer or "")
        S.templates[key] = row
    db.upsert_template = upsert_template

    db.get_template = lambda art, variant="personal": (
        dict(S.templates[(art, variant)]) if (art, variant) in S.templates else None
    )
    db.get_templates_for_art = lambda art: {
        v: dict(row) for (a, v), row in S.templates.items() if a == art
    }
    db.list_templates = lambda: [dict(r) for r in S.templates.values()]
    db.delete_template = lambda tid: [
        S.templates.pop(k) for k, v in list(S.templates.items()) if v["id"] == tid
    ]

    db.get_setting = lambda key, default="": S.settings.get(key, default)
    db.set_setting = lambda key, value: S.settings.__setitem__(key, value)
    db.all_settings = lambda: dict(S.settings)
    db.delete_setting = lambda key: S.settings.pop(key, None)

    db.list_ms_accounts = lambda: [
        {"username": u, "home_account_id": h} for u, h in sorted(S.ms_accounts.items())
    ]
    db.get_ms_account_cache = lambda u: ""
    db.upsert_ms_account = lambda u, h, blob: S.ms_accounts.__setitem__(u, h)
    db.delete_ms_account = lambda u: S.ms_accounts.pop(u, None)
    db.delete_all_ms_accounts = lambda: S.ms_accounts.clear()

    def log_send(contact_id, contact_email, art, subject, status, error=""):
        S.sent_log.append({
            "id": len(S.sent_log) + 1, "contact_id": contact_id,
            "contact_email": contact_email, "art": art, "subject": subject,
            "status": status, "error": error, "sent_at": None,
        })
    db.log_send = log_send
    db.list_log = lambda limit=100: list(reversed(S.sent_log))[:limit]


SENT_MAILS = []


def install_fake_graph():
    def send_mail(to_email, subject, body_html, sender_name=None, sender_email=None, account=None):
        SENT_MAILS.append({"to": to_email, "subject": subject, "account": account})
        return True
    graph_mail.send_mail = send_mail
    graph_mail.start_device_flow = lambda: {
        "user_code": "ABC123", "verification_uri": "https://microsoft.com/devicelogin",
        "message": "go", "expires_in": 900,
    }
    graph_mail.complete_device_flow = lambda: True


install_fake_db()
install_fake_graph()

import app as app_module  # noqa: E402  (must come after the fakes)

app_module.APP_PASSWORD = ""
flask_app = app_module.app
flask_app.config["TESTING"] = True

CHECKS = {"passed": 0}


def ok(cond, label):
    if not cond:
        print(f"FAIL: {label}")
        sys.exit(1)
    CHECKS["passed"] += 1
    print(f"  ok: {label}")


def get_csrf(client):
    client.get("/contacts")  # mints the token via inject_globals
    with client.session_transaction() as sess:
        return sess["_csrf"]


def wait_send_done(client, timeout=10):
    """Wait for a background send job to finish (no-op while send is sync)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get("/send/status")
        if r.status_code == 404:
            return
        status = (r.get_json() or {}).get("status")
        if status in ("done", "idle", None):
            return
        time.sleep(0.05)
    raise SystemExit("send job did not finish in time")


def main():
    c = flask_app.test_client()

    # --- seed data
    S.arten.append("Investor")
    S.ms_accounts["sender@example.com"] = "home-id"

    # --- every page renders
    for path in ("/contacts", "/types", "/templates", "/send", "/settings", "/auth", "/health"):
        r = c.get(path)
        ok(r.status_code == 200, f"GET {path} -> 200")
    ok(c.get("/").status_code == 302, "GET / redirects")

    token = get_csrf(c)

    # --- CSRF: every POST without a token must be rejected (no state change)
    before = (len(S.contacts), len(S.arten), len(S.templates))
    for path, data in [
        ("/contacts", {"firma": "Evil Co", "email": "evil@x.com", "art": "Investor"}),
        ("/types", {"name": "Evil"}),
        ("/templates", {"art": "Investor", "variant": "personal", "subject": "s", "body": "b"}),
        ("/send/run", {"art": "Investor", "mode": "new", "account": "sender@example.com"}),
        ("/logout", {}),
    ]:
        r = c.post(path, data=data)
        ok(r.status_code == 302, f"POST {path} without CSRF -> redirected away")
    r = c.post("/auth/start")
    ok(r.status_code == 403, "POST /auth/start without CSRF -> 403 JSON")
    ok((len(S.contacts), len(S.arten), len(S.templates)) == before, "no state change without CSRF")

    # --- contacts: add (valid), add (invalid email), edit, delete
    r = c.post("/contacts", data={
        "csrf_token": token, "firma": "Acme GmbH", "first_name": "Jane",
        "last_name": "Doe", "email": "jane@acme.com", "art": "Investor",
        "pos_system": "Lightspeed", "notes": "",
    }, follow_redirects=True)
    ok(len(S.contacts) == 1, "valid contact added")
    ok(next(iter(S.contacts.values()))["pos_system"] == "Lightspeed", "pos_system stored on add")

    r = c.post("/contacts", data={
        "csrf_token": token, "firma": "Bad Co", "email": "not-an-email", "art": "Investor",
    })
    ok(r.status_code == 200 and b"field-error" in r.data, "invalid email re-renders with field-error")
    ok(b"Bad Co" in r.data, "entered values preserved on validation error")
    ok(len(S.contacts) == 1, "invalid contact NOT saved")

    cid = next(iter(S.contacts))
    r = c.post(f"/contacts/{cid}/edit", data={
        "csrf_token": token, "firma": "Acme GmbH", "first_name": "Jane",
        "last_name": "Doe", "email": "bad@", "art": "Investor",
    })
    ok(r.status_code == 200 and b"field-error" in r.data, "edit with invalid email re-renders")
    r = c.post(f"/contacts/{cid}/edit", data={
        "csrf_token": token, "firma": "Acme GmbH", "first_name": "Jane",
        "last_name": "Doe", "email": "jane.new@acme.com", "art": "Investor",
    })
    ok(S.contacts[cid]["email"] == "jane.new@acme.com", "edit saved")

    # --- import: good + bad rows -> import report
    csv_data = (
        "company,first_name,last_name,email,type,pos_system,notes\n"
        "Beta Holdings,,,info@beta.com,Investor,Vectron,\n"
        "NoMail GmbH,,,,Investor,,\n"
        "BadMail AG,,,not-an-email,Investor,,\n"
    )
    r = c.post("/contacts/import", data={
        "csrf_token": token, "file": (io.BytesIO(csv_data.encode()), "import.csv"),
    }, content_type="multipart/form-data", follow_redirects=True)
    ok(b"Import report" in r.data, "import report rendered")
    ok(b"missing email" in r.data and b"invalid email" in r.data, "import errors listed in full")
    ok(len(S.contacts) == 2, "1 of 3 import rows added")
    ok(any(x["pos_system"] == "Vectron" for x in S.contacts.values()), "pos_system imported from CSV")

    # --- types
    c.post("/types", data={"csrf_token": token, "name": "Partner"})
    ok("Partner" in S.arten, "type added")
    c.post("/types/delete", data={"csrf_token": token, "name": "Partner"})
    ok("Partner" not in S.arten, "type deleted")

    # --- templates: save + test-send
    c.post("/templates", data={
        "csrf_token": token, "art": "Investor", "variant": "personal",
        "subject": "Hello {{first_name}}", "body": "<p>Hi {{first_name}} at {{firma}}</p>",
    })
    c.post("/templates", data={
        "csrf_token": token, "art": "Investor", "variant": "anonymous",
        "subject": "Hello {{firma}}", "body": "<p>Hi {{firma}}</p>",
    })
    ok(len(S.templates) == 2, "both template variants saved")
    SENT_MAILS.clear()
    r = c.post("/templates/test-send", data={
        "csrf_token": token, "art": "Investor", "variant": "personal",
        "account": "sender@example.com",
    }, follow_redirects=True)
    ok(len(SENT_MAILS) == 1 and SENT_MAILS[0]["to"] == "sender@example.com", "test send delivered to account")

    # --- send: invalid account rejected, valid account runs the batch
    SENT_MAILS.clear()
    r = c.post("/send/run", data={
        "csrf_token": token, "art": "Investor", "mode": "new", "account": "",
    }, follow_redirects=True)
    ok(len(SENT_MAILS) == 0, "send with empty account refused")
    r = c.post("/send/run", data={
        "csrf_token": token, "art": "Investor", "mode": "new", "account": "stranger@nope.com",
    }, follow_redirects=True)
    ok(len(SENT_MAILS) == 0, "send with unknown account refused")

    r = c.post("/send/run", data={
        "csrf_token": token, "art": "Investor", "mode": "new", "account": "sender@example.com",
    }, follow_redirects=True)
    ok(r.status_code == 200, "send run accepted")
    wait_send_done(c)
    ok(len(SENT_MAILS) == 2, f"batch sent one mail per contact (got {len(SENT_MAILS)})")
    ok(all(m["account"] == "sender@example.com" for m in SENT_MAILS), "batch used the picked account")
    sent_ids = {l["contact_id"] for l in S.sent_log if l["status"] == "sent"}
    ok(len(sent_ids) == 2, "send log written per contact")

    # mode=new skips already-sent
    SENT_MAILS.clear()
    c.post("/send/run", data={
        "csrf_token": token, "art": "Investor", "mode": "new", "account": "sender@example.com",
    })
    wait_send_done(c)
    ok(len(SENT_MAILS) == 0, "mode=new skips already-sent contacts")

    # --- send page renders with previews
    r = c.get("/send?art=Investor")
    ok(r.status_code == 200 and b"Preview" in r.data, "send page shows previews")

    # --- settings: invalid sender email rejected, valid saved
    r = c.post("/settings/account", data={
        "csrf_token": token, "username": "sender@example.com", "sender_email": "broken@",
        "sender_name": "X", "email_signature": "",
    }, follow_redirects=True)
    ok("sender_email_sender@example.com" not in S.settings, "invalid sender email not saved")
    c.post("/settings/account", data={
        "csrf_token": token, "username": "sender@example.com",
        "sender_email": "noreply@example.com", "sender_name": "Nightscale", "email_signature": "<p>sig</p>",
    })
    ok(S.settings.get("sender_email_sender@example.com") == "noreply@example.com", "valid settings saved")

    # --- auth endpoints
    r = c.post("/auth/start", headers={"X-CSRF-Token": token})
    ok(r.status_code == 200 and r.get_json().get("user_code") == "ABC123", "auth start returns flow info")
    r = c.get("/auth/status")
    js = r.get_json()
    ok("flow" in js and "accounts" in js, "auth status exposes flow state")
    c.post("/auth/signout", data={"csrf_token": token, "username": "sender@example.com"})
    ok("sender@example.com" not in S.ms_accounts, "account disconnect works")

    # --- contact delete
    cid = next(iter(S.contacts))
    c.post(f"/contacts/{cid}/delete", data={"csrf_token": token})
    ok(cid not in S.contacts, "contact deleted")

    print(f"\nSMOKE TEST PASSED — {CHECKS['passed']} checks")


if __name__ == "__main__":
    main()
