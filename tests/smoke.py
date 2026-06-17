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
import leadgen


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
        self.suppression = {}


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

    def add_contact(firma, first_name, last_name, email, art, notes="", pos_system="",
                    source="", role="", employees="", revenue="", locations="", website=""):
        cid = S.next_cid
        S.next_cid += 1
        S.contacts[cid] = _full_name({
            "id": cid, "firma": firma.strip(), "first_name": first_name.strip(),
            "last_name": last_name.strip(), "email": email.strip(), "art": art.strip(),
            "pos_system": pos_system.strip(), "status": "new", "source": source.strip(),
            "role": role.strip(), "employees": employees.strip(), "revenue": revenue.strip(),
            "locations": str(locations).strip(), "website": website.strip(),
            "notes": notes.strip(), "created_at": None,
        })
    db.add_contact = add_contact
    db.list_contact_roles = lambda: sorted({c.get("role") for c in S.contacts.values() if c.get("role")})
    db.all_contact_emails = lambda: {c["email"].lower() for c in S.contacts.values()}
    db.set_contact_status = lambda cid, status: S.contacts[cid].update(status=status)
    db.add_suppression = lambda email, reason="": S.suppression.__setitem__((email or "").strip().lower(), reason)
    db.remove_suppression = lambda email: S.suppression.pop((email or "").strip().lower(), None)
    db.is_suppressed = lambda email: (email or "").strip().lower() in S.suppression
    db.suppressed_emails = lambda: set(S.suppression)
    db.list_suppression = lambda: [{"email": e, "reason": r, "created_at": None} for e, r in S.suppression.items()]

    def update_contact(cid, firma, first_name, last_name, email, art, notes="", pos_system="",
                       role="", employees="", revenue="", locations="", website=""):
        S.contacts[cid].update(_full_name({
            "firma": firma.strip(), "first_name": first_name.strip(),
            "last_name": last_name.strip(), "email": email.strip(),
            "art": art.strip(), "pos_system": pos_system.strip(), "role": role.strip(),
            "employees": employees.strip(), "revenue": revenue.strip(),
            "locations": str(locations).strip(), "website": website.strip(),
            "notes": notes.strip(),
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
# Avoid real DNS-over-HTTPS calls during the offline test.
app_module._domain_has_mail = lambda domain: True
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


def wait_job_done(client, url, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(url)
        if r.status_code == 404:
            return
        if (r.get_json() or {}).get("status") in ("done", "idle", None):
            return
        time.sleep(0.05)
    raise SystemExit(f"job at {url} did not finish in time")


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

    # re-importing the same file → all rows are now duplicates
    r = c.post("/contacts/import", data={
        "csrf_token": token, "file": (io.BytesIO(csv_data.encode()), "again.csv"),
    }, content_type="multipart/form-data", follow_redirects=True)
    ok(b"duplicate email" in r.data, "duplicate rows flagged on re-import")
    ok(len(S.contacts) == 2, "no new contacts from duplicate re-import")

    # suppressed addresses are refused on import
    db.add_suppression("blocked@x.com", "manual")
    csv_sup = "company,email,type\nBlocked Co,blocked@x.com,Investor\n"
    r = c.post("/contacts/import", data={
        "csrf_token": token, "file": (io.BytesIO(csv_sup.encode()), "sup.csv"),
    }, content_type="multipart/form-data", follow_redirects=True)
    ok(b"suppression list" in r.data, "suppressed email skipped on import")
    ok(len(S.contacts) == 2, "suppressed email not added")

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

    # --- unsubscribe flow (public, token-authenticated, no CSRF)
    utok = app_module.make_unsubscribe_token("lead@firma.at")
    r = c.get(f"/u/{utok}")
    ok(r.status_code == 200 and b"Unsubscribe" in r.data, "unsubscribe page renders")
    ok(not db.is_suppressed("lead@firma.at"), "GET does not opt out (scanner-safe)")
    r = c.post(f"/u/{utok}")
    ok(r.status_code == 200 and db.is_suppressed("lead@firma.at"), "POST opts out without CSRF token")
    ok(c.get("/u/garbage-token").status_code == 400, "invalid unsubscribe token → 400")

    # --- suppressed contacts are never emailed
    db.add_contact("Suppressed GmbH", "", "", "skip@firma.at", "Investor")
    db.add_suppression("skip@firma.at", "manual")
    SENT_MAILS.clear()
    c.post("/send/run", data={
        "csrf_token": token, "art": "Investor", "mode": "all", "account": "sender@example.com",
    })
    wait_send_done(c)
    ok(all(m["to"] != "skip@firma.at" for m in SENT_MAILS), "suppressed contact skipped on send")

    # --- lead generation: pure helpers
    ok(leadgen.detect_pos("Powered by LightSpeed POS") == "Lightspeed", "detect_pos finds system")
    ok(leadgen.extract_emails("x a@b.com y a@b.com z d@e.org") == ["a@b.com", "d@e.org"],
       "extract_emails dedupes")
    ok("restaurant" in leadgen.build_overpass_query((1, 2, 3, 4), ["restaurant"], 5),
       "overpass query builds")
    parsed = leadgen.parse_overpass({"elements": [
        {"tags": {"name": "X", "contact:email": "x@y.com"}}, {"tags": {}}]})
    ok(len(parsed) == 1 and parsed[0]["email"] == "x@y.com", "parse_overpass keeps named + email")

    # --- name validation: only plausible person names survive
    import enrich as _enrich
    ok(_enrich.plausible_name("Max Huber"), "plausible_name accepts a real name")
    ok(_enrich.plausible_name("Anna-Lena Müller"), "plausible_name accepts hyphenated name")
    ok(not _enrich.plausible_name("Geschäftsführer Huber"), "plausible_name rejects title word")
    ok(not _enrich.plausible_name("Impressum"), "plausible_name rejects single word")
    ok(not _enrich.plausible_name("MAX HUBER"), "plausible_name rejects all-caps")
    ok(not _enrich.plausible_name("Team 5"), "plausible_name rejects digits/stopword")
    ok(_enrich.clean_name("Geschäftsführer Huber") == "", "clean_name blanks implausible names")
    ok(_enrich.clean_name("Max Huber") == "Max Huber", "clean_name keeps plausible names")
    ok(not _enrich.plausible_name("Bereich Hotellerie"), "plausible_name rejects noun phrase")
    ok(not _enrich.plausible_name("Schreiben Sie"), "plausible_name rejects German function words")

    # --- generic mailbox detection + name-from-local
    ok(_enrich._is_generic_local("info"), "info@ is generic")
    ok(_enrich._is_generic_local("info-muc"), "info-muc@ is generic")
    ok(_enrich._is_generic_local("news"), "news@ is generic")
    ok(not _enrich._is_generic_local("max.huber"), "personal local is not generic")
    ok(_enrich.name_from_local("max.huber") == "Max Huber", "name derived from local part")
    ok(_enrich.name_from_local("m.huber") == "", "initials are not a name")
    ok(_enrich.name_from_local("info") == "", "generic local yields no name")

    # --- FreeRegexEnricher: no name for shared mailboxes, name only when anchored
    free = _enrich.FreeRegexEnricher()
    txt = (
        "Schreiben Sie uns. Bereich Hotellerie. E-Mail: news@elaya-hotels.com . "
        + ("Lorem ipsum dolor sit amet. " * 6)
        + "Geschäftsführer Max Huber. E-Mail: max.huber@webbistro.at ."
    )
    fc = {c2["email"]: c2 for c2 in free.enrich({"name": "X"}, txt)["contacts"]}
    ok(fc["news@elaya-hotels.com"]["name"] == "", "shared mailbox gets no name")
    ok(fc["max.huber@webbistro.at"]["name"] == "Max Huber", "anchored/personal mailbox keeps name")

    # --- criteria matching during search (server-side filter)
    bm = app_module._business_matches
    biz_gf = {"employees": "", "revenue": "", "locations": "", "pos_system": "",
              "contacts": [{"role": "geschaeftsfuehrung", "email": "a@x.at"},
                           {"role": "allgemein", "email": "info@x.at"}]}
    m, kept = bm(biz_gf, {"roles": ["geschaeftsfuehrung"]})
    ok(m and len(kept) == 1 and kept[0]["role"] == "geschaeftsfuehrung",
       "role criterion keeps only matching contacts")
    m, kept = bm({"contacts": [{"role": "allgemein", "email": "info@x.at"}]},
                 {"roles": ["geschaeftsfuehrung"]})
    ok(not m, "business without a wanted-role contact is excluded")
    # unknown firmographics never exclude
    m, _ = bm(biz_gf, {"min_employees": 50})
    ok(m, "unknown employees does not exclude")
    m, _ = bm({"employees": "12", "contacts": [{"role": "allgemein", "email": "i@x.at"}]},
              {"min_employees": 50})
    ok(not m, "known employees below minimum excludes")
    m, _ = bm({"locations": "1", "contacts": [{"email": "i@x.at"}]}, {"betrieb": "chain"})
    ok(not m, "single location excluded when chain requested")
    m, _ = bm({"locations": "", "contacts": [{"email": "i@x.at"}]}, {"betrieb": "chain"})
    ok(m, "unknown locations not excluded by chain filter")
    m, kept = bm({"contacts": [{"email": "a@x.at"}, {"email": ""}]}, {"email_only": True})
    ok(m and len(kept) == 1, "email_only drops contacts without an email")

    # --- ClaudeEnricher mapping (mocked SDK client, no network/key)
    import enrich as enrich_mod

    class _FakeContact:
        def __init__(self, **kw): self.__dict__.update(kw)

    class _FakeExtract:
        legal_name = "Web Bistro GmbH"; industry = "Gastronomy"
        employees = "11-50"; revenue = "€2M"; locations = "2"; pos_system = "Lightspeed"
        contacts = [
            _FakeContact(name="Max Huber", role="Geschäftsführer", email="max@wb.at", phone=""),
            _FakeContact(name="", role="weird-role", email="", phone=""),  # dropped (no email)
        ]

    class _FakeParse:
        parsed_output = _FakeExtract()

    ce = enrich_mod.ClaudeEnricher.__new__(enrich_mod.ClaudeEnricher)
    ce._model, ce._web_search = "claude-opus-4-8", False
    ce._client = type("C", (), {"messages": type("M", (), {"parse": staticmethod(lambda **kw: _FakeParse())})()})()
    mapped = ce.enrich({"name": "Web Bistro"}, "irrelevant text")
    ok(mapped["employees"] == "11-50" and mapped["pos_system"] == "Lightspeed", "ClaudeEnricher maps firmographics")
    ok(len(mapped["contacts"]) == 1, "ClaudeEnricher drops contacts without email")
    ok(mapped["contacts"][0]["role"] == "geschaeftsfuehrung", "ClaudeEnricher normalizes role label")

    # --- lead generation: full job with network mocked (FreeRegexEnricher path,
    #     since no ANTHROPIC_API_KEY is set in the test environment)
    leadgen.geocode_region = lambda region: (47.0, 9.0, 47.6, 10.0)
    leadgen.fetch_overpass = lambda query: {"elements": [
        {"type": "node", "tags": {"name": "Cafe Direct", "contact:email": "hallo@cafedirect.at"}},
        {"type": "node", "tags": {"name": "Web Bistro", "website": "http://webbistro.at"}},
    ]}
    impressum = (
        "Impressum. UID-Nummer: ATU12345678. Unser Team besteht aus 25 Mitarbeiter. "
        "Website powered by gastrofix. "
        "Geschäftsführer Max Huber. E-Mail: max@webbistro.at . "
        + ("Lorem ipsum dolor sit amet consectetur adipiscing elit. " * 6)
        + "Leitung Marketing Lisa Berger. E-Mail: marketing@webbistro.at ."
    )
    leadgen.collect_site_text = lambda website, max_chars=30000: impressum
    r = c.post("/leads/run", data={
        "csrf_token": token, "region": "Bregenz", "categories": ["restaurant", "cafe"], "limit": "10",
    }, follow_redirects=True)
    ok(r.status_code == 200, "lead search starts")
    wait_job_done(c, "/leads/status")
    job = c.get("/leads/status").get_json()
    ok(len(job["results"]) == 2, f"two businesses found (got {len(job['results'])})")
    bistro = next(x for x in job["results"] if x["name"] == "Web Bistro")
    ok(bistro["employees"] == "25", "employee count extracted from Impressum")
    ok(bistro["pos_system"] == "Gastrofix", "POS system detected")
    gf = next((c2 for c2 in bistro["contacts"] if c2["role"] == "geschaeftsfuehrung"), None)
    ok(gf and gf["email"] == "max@webbistro.at", "managing director contact extracted with role")
    ok(gf and gf["name"] == "Max Huber", "managing director name extracted")
    ok(any(c2["role"] == "marketing" and c2["email"] == "marketing@webbistro.at"
           for c2 in bistro["contacts"]), "marketing contact extracted with role")
    cafe = next(x for x in job["results"] if x["name"] == "Cafe Direct")
    ok(any(c2["email"] == "hallo@cafedirect.at" for c2 in cafe["contacts"]), "OSM email folded in as contact")

    before = len(S.contacts)
    new_rows = [c2["row"] for b in job["results"] for c2 in b["contacts"] if c2["state"] == "new"]
    ok(len(new_rows) == 3, f"three importable contacts (got {len(new_rows)})")
    c.post("/leads/import", data={"csrf_token": token, "art": "Investor", "row": new_rows})
    ok(len(S.contacts) == before + 3, "selected lead contacts imported")
    imported = [x for x in S.contacts.values() if x["source"] == "osm+web"]
    ok(any(x["role"] == "geschaeftsfuehrung" and x["employees"] == "25" for x in imported),
       "imported GF contact keeps role + firmographics")
    # re-importing the same rows must not duplicate
    c.post("/leads/import", data={"csrf_token": token, "art": "Investor", "row": new_rows})
    ok(len(S.contacts) == before + 3, "duplicate lead import is a no-op")

    # --- edited import: per-row fields override the snapshot, invalid email is skipped
    job2 = c.get("/leads/status").get_json()
    new2 = [c2["row"] for b in job2["results"] for c2 in b["contacts"] if c2["state"] == "new"]
    # all current emails are now duplicates, so edit one to a fresh valid address
    edit_row = new2[0] if new2 else next(c2["row"] for b in job2["results"] for c2 in b["contacts"])
    base = len(S.contacts)
    c.post("/leads/import", data={
        "csrf_token": token, "art": "Investor", "row": [edit_row],
        f"email_{edit_row}": "fresh.lead@example-new.at",
        f"name_{edit_row}": "Erika Mustermann",
        f"role_{edit_row}": "marketing",
        f"employees_{edit_row}": "99",
    })
    ok(len(S.contacts) == base + 1, "edited row imported with new email")
    edited = next(x for x in S.contacts.values() if x["email"] == "fresh.lead@example-new.at")
    ok(edited["first_name"] == "Erika" and edited["role"] == "marketing" and edited["employees"] == "99",
       "edited name/role/firmographics persisted")
    # an invalid edited email must be rejected
    bad = len(S.contacts)
    c.post("/leads/import", data={
        "csrf_token": token, "art": "Investor", "row": [edit_row],
        f"email_{edit_row}": "not-an-email",
    })
    ok(len(S.contacts) == bad, "edited row with invalid email skipped")

    # --- criteria search end-to-end: only matching businesses come back
    app_module._leadgen_job["status"] = "idle"
    c.post("/leads/run", data={
        "csrf_token": token, "region": "Bregenz", "categories": ["restaurant", "cafe"],
        "limit": "10", "want_role": "geschaeftsfuehrung",
    }, follow_redirects=True)
    wait_job_done(c, "/leads/status")
    cjob = c.get("/leads/status").get_json()
    ok(cjob["results"], "criteria search returns matches")
    ok(all(all(ct["role"] == "geschaeftsfuehrung" for ct in b["contacts"]) for b in cjob["results"]),
       "criteria search returns only geschaeftsfuehrung contacts")
    ok(all(b["contacts"] for b in cjob["results"]), "no empty businesses in criteria results")
    ok(cjob["matched"] == len(cjob["results"]) and cjob["scanned"] >= cjob["matched"],
       "job tracks matched + scanned counts")

    # --- contact delete
    cid = next(iter(S.contacts))
    c.post(f"/contacts/{cid}/delete", data={"csrf_token": token})
    ok(cid not in S.contacts, "contact deleted")

    print(f"\nSMOKE TEST PASSED — {CHECKS['passed']} checks")


if __name__ == "__main__":
    main()
