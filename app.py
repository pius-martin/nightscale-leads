import csv
import hmac
import io
import logging
import os
import random
import re
import secrets
import threading
from functools import wraps
from urllib.parse import urlparse
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, session
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv

import db
import graph_mail

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("nightscale")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-key-change-me")
app.config.update(
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,  # 30 days
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV", "production") == "production",
)

# Trust X-Forwarded-* headers from the upstream proxy (Cloudflare/Railway).
# x_prefix lets the app run under a path like /leadscrap; url_for() will
# include the prefix automatically when the proxy forwards X-Forwarded-Prefix.
app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=1, x_proto=1, x_host=1, x_prefix=1,
)

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")


def _safe_next(target: str | None) -> str:
    """Only allow same-origin relative paths to prevent open redirect."""
    if not target:
        return ""
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return ""
    if not target.startswith("/") or target.startswith("//"):
        return ""
    return target


def _current_full_path() -> str:
    """Path including any reverse-proxy prefix and the original query string.
    Used as the ?next= value so login redirects survive the /leadscrap prefix."""
    p = request.script_root + request.path
    if request.query_string:
        p += "?" + request.query_string.decode()
    return p

_db_ready = {"ok": False, "error": None}
_db_lock = threading.Lock()


def _ensure_db():
    if _db_ready["ok"]:
        return None
    with _db_lock:
        if _db_ready["ok"]:
            return None
        try:
            db.init_db()
            _db_ready["ok"] = True
            _db_ready["error"] = None
        except Exception as e:
            log.exception("Database initialization failed")
            _db_ready["error"] = str(e)
            return _db_ready["error"]
    return None


def _csrf_token() -> str:
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def _check_csrf():
    """Validate the CSRF token on every POST. Returns a response on failure."""
    submitted = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    expected = session.get("_csrf") or ""
    if expected and submitted and hmac.compare_digest(submitted, expected):
        return None
    log.warning("CSRF check failed for %s %s", request.method, request.path)
    if request.endpoint == "auth_start":
        return jsonify({"error": "Session expired — reload the page and try again."}), 403
    flash("Your session expired — please try that again.", "error")
    ref = ""
    if request.referrer:
        p = urlparse(request.referrer)
        if p.netloc == request.host:
            ref = p.path + (("?" + p.query) if p.query else "")
    return redirect(ref or url_for("contacts"))


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not APP_PASSWORD:
            return f(*args, **kwargs)
        if session.get("authed"):
            return f(*args, **kwargs)
        return redirect(url_for("login", next=_safe_next(_current_full_path())))
    return wrapper


@app.before_request
def gate():
    if request.method == "POST":
        failure = _check_csrf()
        if failure:
            return failure
    # Allow static files and login page through without auth
    if request.endpoint in {"login", "static", "health"}:
        return None
    # Try to ensure DB is ready
    err = _ensure_db()
    if err:
        return render_template("db_error.html", error=err), 503
    if APP_PASSWORD and not session.get("authed"):
        return redirect(url_for("login", next=_safe_next(_current_full_path())))
    return None


def _signature_for_account(username: str | None) -> str:
    """Account-specific signature with fallback to the global one."""
    if username:
        sig = db.get_setting(f"email_signature_{username}", "")
        if sig:
            return sig
    return db.get_setting("email_signature", "")


def _sender_name_for(username: str | None) -> str:
    if username:
        v = db.get_setting(f"sender_name_{username}", "")
        if v:
            return v
    return db.get_setting("sender_name", "")


def _sender_email_for(username: str | None) -> str:
    if username:
        v = db.get_setting(f"sender_email_{username}", "")
        if v:
            return v
    return db.get_setting("sender_email", "") or os.environ.get("SENDER_EMAIL", "")


SAMPLE_COMPANIES = [
    "Acme GmbH", "Beta Holdings", "Café Sonne", "Restaurant Alpina",
    "Vorarlberger Hof", "Bäckerei Müller", "Hotel Bergblick", "Studio Nord",
]
SAMPLE_FIRSTNAMES = ["Anna", "Max", "Lisa", "Tom", "Sarah", "Klaus", "Hannah", "Felix"]
SAMPLE_LASTNAMES = ["Mustermann", "Müller", "Schmid", "Weber", "Berger", "Huber", "Bauer", "Fischer"]


def _random_contact(art: str, variant: str, recipient_email: str) -> dict:
    """Build a fake contact for test-send previews. The variant controls
    whether names are filled in (personal) or left blank (anonymous)."""
    contact = {
        "firma": random.choice(SAMPLE_COMPANIES),
        "first_name": "",
        "last_name": "",
        "email": recipient_email,
        "art": art,
    }
    if variant == "personal":
        contact["first_name"] = random.choice(SAMPLE_FIRSTNAMES)
        contact["last_name"] = random.choice(SAMPLE_LASTNAMES)
    return contact


@app.context_processor
def inject_globals():
    try:
        accounts = graph_mail.list_accounts()
        signed_in = bool(accounts)
        account = accounts[0]["username"] if accounts else None
        settings = db.all_settings() if _db_ready["ok"] else {}
    except Exception:
        log.exception("inject_globals failed; rendering without account/settings context")
        accounts, signed_in, account, settings = [], False, None, {}
    return {
        "signed_in": signed_in,
        "account": account,
        "ms_accounts": accounts,
        "settings": settings,
        "csrf_token": _csrf_token(),
    }


MISSING_MARKER = "xxx"
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
# Inline style applied to every substituted value in HTML output. Forces the
# substituted text to render in exactly the same color/style as the surrounding
# text, defeating Apple Mail data detectors and other clients that color
# detected entities (company names, etc.) differently.
_VAR_STYLE = (
    "color:#000000 !important;"
    "background:transparent !important;"
    "background-color:transparent !important;"
    "text-decoration:none !important;"
    "font:inherit;"
)


def render_template_text(text: str, contact: dict, html_safe: bool = False) -> str:
    """Replace {{firma}}, {{first_name}}, {{last_name}}, {{name}}, {{email}}, {{art}}.
    Missing fields render as MISSING_MARKER. When html_safe is True, the
    substituted value is wrapped in a <span> with explicit black color and
    x-apple-data-detectors="false" so Apple Mail does NOT auto-style
    detected entities (company names, etc.) in gray or another tint."""
    def repl(m):
        key = m.group(1).strip().lower()
        value = str(contact.get(key, "") or "").strip()
        if not value:
            value = MISSING_MARKER
        if html_safe:
            return f'<span x-apple-data-detectors="false" style="{_VAR_STYLE}">{value}</span>'
        return value
    return _PLACEHOLDER_RE.sub(repl, text or "")


@app.route("/health")
def health():
    return {"ok": True}


@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PASSWORD:
        return redirect(url_for("contacts"))
    error = None
    if request.method == "POST":
        submitted = request.form.get("password", "")
        if hmac.compare_digest(submitted, APP_PASSWORD):
            session.permanent = True
            session["authed"] = True
            nxt = _safe_next(request.args.get("next")) or url_for("contacts")
            return redirect(nxt)
        error = "Wrong password."
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    return redirect(url_for("contacts"))


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def _valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match((value or "").strip()))


def _validate_contact_form(form) -> dict:
    """Returns {field: message} for the add/edit contact forms."""
    errors = {}
    email = (form.get("email") or "").strip()
    if not email:
        errors["email"] = "Email is required."
    elif not _valid_email(email):
        errors["email"] = "That doesn't look like a valid email address."
    if not (form.get("art") or "").strip():
        errors["art"] = "Pick a type."
    firma = (form.get("firma") or "").strip()
    first = (form.get("first_name") or "").strip()
    last = (form.get("last_name") or "").strip()
    if not firma and not first and not last:
        errors["firma"] = "Provide at least a company or a name."
    return errors


_HEADER_ALIASES = {
    "firma": {"firma", "company", "firmenname", "organization", "organisation"},
    "first_name": {"first_name", "firstname", "first", "vorname"},
    "last_name": {"last_name", "lastname", "last", "nachname", "surname"},
    "email": {"email", "e-mail", "mail", "e_mail"},
    "art": {"art", "type", "typ", "category", "kategorie", "tag"},
    "notes": {"notes", "notizen", "note", "comment", "kommentar"},
}


def _normalize_header(h: str) -> str:
    h = (h or "").strip().lower().replace("-", "_").replace(" ", "_")
    for canonical, aliases in _HEADER_ALIASES.items():
        if h in aliases:
            return canonical
    return ""


def _parse_csv(stream) -> list[dict]:
    text = stream.read()
    if isinstance(text, bytes):
        for enc in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                text = text.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    # Detect delimiter (comma or semicolon)
    sniffer = csv.Sniffer()
    sample = text[:2048]
    try:
        dialect = sniffer.sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    rows = []
    for raw in reader:
        rows.append({_normalize_header(k): (v or "").strip() for k, v in raw.items() if k})
    return rows


def _parse_xlsx(file_storage) -> list[dict]:
    from openpyxl import load_workbook
    wb = load_workbook(filename=file_storage, read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        return []
    headers = [_normalize_header(str(h or "")) for h in header_row]
    rows = []
    for r in rows_iter:
        d = {h: (str(v).strip() if v is not None else "") for h, v in zip(headers, r) if h}
        if any(d.values()):
            rows.append(d)
    return rows


def _import_rows(rows: list[dict]) -> tuple[int, int, list[str]]:
    """Insert valid rows. Returns (added, skipped, error messages)."""
    added, skipped, errors = 0, 0, []
    existing_arten = set(db.list_arten())
    for i, r in enumerate(rows, start=2):  # row 1 is the header
        email = r.get("email", "").strip()
        firma = r.get("firma", "").strip()
        art = r.get("art", "").strip()
        if not email:
            skipped += 1
            errors.append(f"row {i}: missing email")
            continue
        if not art:
            skipped += 1
            errors.append(f"row {i}: missing type")
            continue
        if not _valid_email(email):
            skipped += 1
            errors.append(f"row {i}: invalid email '{email}'")
            continue
        if not firma and not r.get("first_name") and not r.get("last_name"):
            skipped += 1
            errors.append(f"row {i}: needs at least company or a name")
            continue
        if art not in existing_arten:
            db.add_art(art)
            existing_arten.add(art)
        db.add_contact(
            firma=firma,
            first_name=r.get("first_name", ""),
            last_name=r.get("last_name", ""),
            email=email,
            art=art,
            notes=r.get("notes", ""),
        )
        added += 1
    return added, skipped, errors


# ---------- Contacts ----------
@app.route("/contacts", methods=["GET", "POST"])
def contacts():
    arten = db.list_arten()
    if request.method == "POST":
        form_errors = _validate_contact_form(request.form)
        if form_errors:
            items = db.list_contacts()
            return render_template(
                "contacts.html", contacts=items, arten=arten,
                form=request.form, form_errors=form_errors,
            )
        art = request.form.get("art", "").strip()
        if art and art not in arten:
            db.add_art(art)
        db.add_contact(
            firma=request.form.get("firma", "").strip(),
            first_name=request.form.get("first_name", "").strip(),
            last_name=request.form.get("last_name", "").strip(),
            email=request.form.get("email", ""),
            art=art,
            notes=request.form.get("notes", ""),
        )
        flash("Contact added.", "success")
        return redirect(url_for("contacts"))
    items = db.list_contacts()
    import_report = session.pop("import_report", None)
    return render_template(
        "contacts.html", contacts=items, arten=arten, import_report=import_report,
    )


@app.route("/contacts/import", methods=["POST"])
def contacts_import():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("No file selected.", "error")
        return redirect(url_for("contacts"))
    name = file.filename.lower()
    try:
        if name.endswith(".csv"):
            rows = _parse_csv(file.stream)
        elif name.endswith((".xlsx", ".xlsm")):
            rows = _parse_xlsx(file)
        else:
            flash("Unsupported file type. Use .csv or .xlsx.", "error")
            return redirect(url_for("contacts"))
    except Exception as e:
        log.exception("Import file parse failed: %s", file.filename)
        flash(f"Could not parse file: {e}", "error")
        return redirect(url_for("contacts"))
    added, skipped, errors = _import_rows(rows)
    log.info("Import: %s added, %s skipped (%s)", added, skipped, file.filename)
    msg = f"Imported {added} contact{'' if added == 1 else 's'}"
    if skipped:
        msg += f", skipped {skipped} — see the import report below"
        # Session cookies have a ~4 KB limit; cap the row-error list.
        session["import_report"] = {
            "added": added,
            "skipped": skipped,
            "errors": errors[:50],
            "truncated": max(0, len(errors) - 50),
        }
    flash(msg + ".", "success" if skipped == 0 else "error")
    return redirect(url_for("contacts"))


@app.route("/contacts/<int:cid>/edit", methods=["GET", "POST"])
def edit_contact(cid):
    contact = db.get_contact(cid)
    if not contact:
        return redirect(url_for("contacts"))
    arten = db.list_arten()
    if request.method == "POST":
        form_errors = _validate_contact_form(request.form)
        if form_errors:
            # Re-render with the submitted (unsaved) values so nothing is lost
            submitted = dict(contact)
            for k in ("firma", "first_name", "last_name", "email", "art", "notes"):
                submitted[k] = request.form.get(k, "")
            return render_template(
                "edit_contact.html", contact=submitted, arten=arten,
                form_errors=form_errors,
            )
        db.update_contact(
            cid,
            firma=request.form.get("firma", ""),
            first_name=request.form.get("first_name", ""),
            last_name=request.form.get("last_name", ""),
            email=request.form.get("email", ""),
            art=request.form.get("art", ""),
            notes=request.form.get("notes", ""),
        )
        flash("Contact saved.", "success")
        return redirect(url_for("contacts"))
    return render_template("edit_contact.html", contact=contact, arten=arten)


@app.route("/contacts/<int:cid>/delete", methods=["POST"])
def delete_contact(cid):
    db.delete_contact(cid)
    return redirect(url_for("contacts"))


# ---------- Types (Arten) ----------
@app.route("/types", methods=["GET", "POST"])
def types_view():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if name:
            db.add_art(name)
        return redirect(url_for("types_view"))
    arten = db.list_arten()
    groups = []
    for a in arten:
        contacts_for_a = db.list_contacts(art=a)
        groups.append({"name": a, "contacts": contacts_for_a, "count": len(contacts_for_a)})
    return render_template("types.html", groups=groups)


@app.route("/types/delete", methods=["POST"])
def delete_type():
    name = request.form.get("name", "").strip()
    if name:
        db.delete_art(name)
    return redirect(url_for("types_view"))


# ---------- Templates ----------
@app.route("/templates", methods=["GET", "POST"])
def templates_view():
    arten = db.list_arten()
    if request.method == "POST":
        art = request.form.get("art", "").strip()
        variant = request.form.get("variant", "personal").strip()
        if variant not in db.VARIANTS:
            variant = "personal"
        subject = request.form.get("subject", "")
        body = request.form.get("body", "")
        footer = request.form.get("footer", "")
        if art:
            if art not in arten:
                db.add_art(art)
            db.upsert_template(art, variant, subject, body, footer)
        return redirect(url_for("templates_view", art=art, variant=variant))

    selected = request.args.get("art") or (arten[0] if arten else "")
    variant = request.args.get("variant", "personal")
    if variant not in db.VARIANTS:
        variant = "personal"
    variants_for_art = db.get_templates_for_art(selected) if selected else {}
    current = variants_for_art.get(variant)
    return render_template(
        "templates.html",
        arten=arten,
        selected=selected,
        variant=variant,
        variants=list(db.VARIANTS),
        variants_for_art=variants_for_art,
        current=current,
    )


@app.route("/templates/<int:tid>/delete", methods=["POST"])
def delete_template(tid):
    db.delete_template(tid)
    return redirect(url_for("templates_view"))


@app.route("/templates/test-send", methods=["POST"])
def template_test_send():
    art = request.form.get("art", "").strip()
    variant = request.form.get("variant", "personal").strip()
    account = request.form.get("account", "").strip()
    if variant not in db.VARIANTS:
        variant = "personal"
    if not art:
        flash("No type selected.", "error")
        return redirect(url_for("templates_view"))
    if not account:
        flash("Pick an account to send the test from.", "error")
        return redirect(url_for("templates_view", art=art, variant=variant))
    template = db.get_template(art, variant)
    if not template:
        flash(f"No {variant} template for {art} yet.", "error")
        return redirect(url_for("templates_view", art=art, variant=variant))

    contact = _random_contact(art, variant, account)
    sender_name = _sender_name_for(account)
    sender_email = _sender_email_for(account)
    signature = _signature_for_account(account)

    subject = "[TEST] " + render_template_text(template["subject"], contact)
    body_html = _wrap_email_html(
        _compose_body(template["body"], template.get("footer", ""), signature, contact)
    )
    try:
        graph_mail.send_mail(
            account, subject, body_html,
            sender_name=sender_name or None,
            sender_email=sender_email or None,
            account=account,
        )
        who = f"{contact['first_name']} {contact['last_name']} · {contact['firma']}".strip(" ·")
        flash(f"Test sent to {account} (rendered as: {who}).", "success")
    except Exception as e:
        log.exception("Test send failed (art=%s, variant=%s, account=%s)", art, variant, account)
        flash(f"Test send failed: {e}", "error")
    return redirect(url_for("templates_view", art=art, variant=variant))


def _to_html(text: str) -> str:
    """Convert content to HTML. If it already contains tags, leave as-is.
    Plain text: split into paragraphs on blank lines, single newlines as <br>."""
    if not text:
        return ""
    if "<" in text and ">" in text:
        return text
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return "".join("<p>" + p.replace("\n", "<br>") + "</p>" for p in paragraphs)


def _compose_body(template_body: str, template_footer: str, signature: str, contact: dict) -> str:
    """Stitch body + footer + signature into one HTML body. Each <p> from
    Quill represents one Enter press and should render as a line break, not
    a full paragraph break. Empty paragraphs (Enter twice) keep their
    line-height and become the only place a real blank line appears."""
    parts = []
    for raw in (template_body, template_footer, signature):
        rendered = render_template_text(raw or "", contact, html_safe=True)
        html = _to_html(rendered)
        if html:
            parts.append(html)
    return "".join(parts)


# Email shell:
# - meta name="color-scheme" + supported-color-schemes: declare we're aware
#   of dark mode so clients don't apply their own (broken) auto-inversion
# - x-apple-disable-message-reformatting: stops Apple Mail's heuristic
#   reformatting that produced the dark "highlight" boxes around bolded
#   words and detected entities (company names etc.)
# - format-detection: disables iOS data detectors that auto-link/style
#   phone numbers, addresses, dates, etc.
# Inline CSS only — most email clients strip <style> in <head>, so colors
# are forced explicitly on body and links to keep contrast in both modes.
EMAIL_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="only light">
<meta name="supported-color-schemes" content="only light">
<meta name="x-apple-disable-message-reformatting">
<meta name="format-detection" content="telephone=no,date=no,address=no,email=no,url=no">
<style>
  :root {{ color-scheme: only light; supported-color-schemes: only light; }}
  body, body * {{ background-color: transparent !important; }}
  body {{
    margin: 0;
    padding: 16px 18px;
    background-color: #ffffff !important;
    color: #000000;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    font-size: 14px;
    line-height: 1.5;
  }}
  /* Each Enter in the editor becomes a <p>; render as a line break.
     A real blank line only appears when the writer pressed Enter twice,
     which Quill stores as an empty <p><br></p> — its line-height is the gap. */
  p {{ margin: 0 !important; color: #000000; }}
  p:empty {{ margin: 0 !important; min-height: 1em; }}
  br {{ line-height: 1.5; }}
  a {{ color: #0a66c2; }}
  strong, b {{ font-weight: 600; color: inherit; }}
  em, i {{ color: inherit; }}
  ul, ol {{ margin: 0 0 0.85em; padding-left: 22px; }}
  blockquote {{ margin: 0 0 0.85em; padding-left: 12px; border-left: 2px solid #d0d0d0; color: #555; }}
  /* Signature/footer blocks: tighten consecutive single-line paragraphs */
  .sig p, .footer p {{ margin: 0; }}
</style>
</head>
<body x-apple-data-detectors="false" style="margin:0;padding:16px 18px;background-color:#ffffff;color:#000000;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;font-size:14px;line-height:1.5;">
{content}
</body>
</html>"""


def _wrap_email_html(inner_html: str) -> str:
    return EMAIL_HTML_TEMPLATE.format(content=inner_html or "")


# ---------- Send ----------
def _contact_variant(c: dict) -> str:
    has_name = bool((c.get("first_name") or "").strip() or (c.get("last_name") or "").strip())
    return "personal" if has_name else "anonymous"


# Batch sends run in a background thread so big campaigns don't hit the
# request timeout. Single shared job (Procfile runs one worker); progress is
# polled via /send/status. If the process restarts mid-job, "Send to new"
# resumes safely because sent_log dedupes already-sent contacts.
_send_job_lock = threading.Lock()
_send_job = {
    "status": "idle",  # idle | running | done
    "art": "", "mode": "", "account": "",
    "total": 0, "sent": 0, "failed": 0, "skipped": 0,
    "current": "", "error": "", "reported": True,
}


def _send_job_snapshot() -> dict:
    with _send_job_lock:
        return dict(_send_job)


def _run_send_job(art: str, mode: str, account: str):
    try:
        variants_for_art = db.get_templates_for_art(art)
        contacts_for_art = db.list_contacts(art=art)
        already = db.sent_contact_ids(art) if mode == "new" else set()
        targets = [c for c in contacts_for_art if not (mode == "new" and c["id"] in already)]
        skipped = len(contacts_for_art) - len(targets)
        with _send_job_lock:
            _send_job.update(total=len(targets), skipped=skipped)
        sender_name = _sender_name_for(account)
        sender_email = _sender_email_for(account)
        signature = _signature_for_account(account)
        for c in targets:
            with _send_job_lock:
                _send_job["current"] = c["email"]
            variant = _contact_variant(c)
            template = variants_for_art.get(variant) or variants_for_art.get(
                "personal" if variant == "anonymous" else "anonymous"
            )
            if not template:
                db.log_send(c["id"], c["email"], art, "", "failed", f"no template for variant {variant}")
                with _send_job_lock:
                    _send_job["failed"] += 1
                continue
            subject = render_template_text(template["subject"], c)
            inner = _compose_body(template["body"], template.get("footer", ""), signature, c)
            body_html = _wrap_email_html(inner)
            try:
                graph_mail.send_mail(
                    c["email"], subject, body_html,
                    sender_name=sender_name or None,
                    sender_email=sender_email or None,
                    account=account,
                )
                db.log_send(c["id"], c["email"], art, subject, "sent")
                with _send_job_lock:
                    _send_job["sent"] += 1
            except Exception as e:
                log.exception("Send failed (contact=%s, email=%s, art=%s)", c["id"], c["email"], art)
                db.log_send(c["id"], c["email"], art, subject, "failed", str(e))
                with _send_job_lock:
                    _send_job["failed"] += 1
        log.info(
            "Send job finished (art=%s, mode=%s): %s sent, %s failed, %s skipped",
            art, mode, _send_job["sent"], _send_job["failed"], skipped,
        )
    except Exception as e:
        log.exception("Send job crashed (art=%s, mode=%s)", art, mode)
        with _send_job_lock:
            _send_job["error"] = str(e)
    finally:
        with _send_job_lock:
            _send_job["status"] = "done"
            _send_job["current"] = ""


@app.route("/send", methods=["GET"])
def send_view():
    arten = db.list_arten()
    selected = request.args.get("art") or (arten[0] if arten else "")
    variants_for_art = db.get_templates_for_art(selected) if selected else {}
    contacts_for_art = db.list_contacts(art=selected) if selected else []
    already = db.sent_contact_ids(selected) if selected else set()
    signature = db.get_setting("email_signature", "")

    previews = []
    new_count = 0
    incomplete_count = 0
    missing_variants = set()
    for c in contacts_for_art:
        variant = _contact_variant(c)
        template = variants_for_art.get(variant) or variants_for_art.get(
            "personal" if variant == "anonymous" else "anonymous"
        )
        if not template:
            missing_variants.add(variant)
            continue
        sent_before = c["id"] in already
        if not sent_before:
            new_count += 1
        subject = render_template_text(template["subject"], c)
        body = _compose_body(template["body"], template.get("footer", ""), signature, c)
        incomplete = MISSING_MARKER in subject or MISSING_MARKER in body
        if incomplete:
            incomplete_count += 1
        previews.append({
            "contact": c,
            "variant": variant,
            "template_variant": template["variant"],
            "subject": subject,
            "body": body,
            "already_sent": sent_before,
            "incomplete": incomplete,
        })
    send_log = db.list_log(50)
    job = _send_job_snapshot()
    # Show a finished job's summary exactly once, then mark it reported.
    job_summary = None
    if job["status"] == "done" and not job["reported"]:
        job_summary = job
        with _send_job_lock:
            _send_job["reported"] = True
    return render_template(
        "send.html",
        arten=arten,
        selected=selected,
        has_template=bool(variants_for_art),
        previews=previews,
        new_count=new_count,
        total_count=len(previews),
        incomplete_count=incomplete_count,
        missing_variants=sorted(missing_variants),
        marker=MISSING_MARKER,
        log=send_log,
        send_job=job if job["status"] == "running" else None,
        job_summary=job_summary,
    )


@app.route("/send/run", methods=["POST"])
def send_run():
    art = request.form.get("art", "").strip()
    mode = request.form.get("mode", "new")  # 'new' or 'all'
    account = request.form.get("account", "").strip()
    if not art:
        flash("No type selected.", "error")
        return redirect(url_for("send_view"))
    if mode not in ("new", "all"):
        flash("Invalid send mode.", "error")
        return redirect(url_for("send_view", art=art))
    connected = {a["username"] for a in graph_mail.list_accounts()}
    if not connected:
        flash("No Microsoft account connected.", "error")
        return redirect(url_for("auth_view"))
    if not account or account not in connected:
        flash("Pick which account to send from.", "error")
        return redirect(url_for("send_view", art=art))

    if not db.get_templates_for_art(art):
        flash("No template for this type.", "error")
        return redirect(url_for("send_view", art=art))

    with _send_job_lock:
        if _send_job["status"] == "running":
            flash("A send is already running — wait for it to finish.", "error")
            return redirect(url_for("send_view", art=art))
        _send_job.update(
            status="running", art=art, mode=mode, account=account,
            total=0, sent=0, failed=0, skipped=0, current="", error="",
            reported=False,
        )
    log.info("Send job started (art=%s, mode=%s, account=%s)", art, mode, account)
    threading.Thread(target=_run_send_job, args=(art, mode, account), daemon=True).start()
    return redirect(url_for("send_view", art=art))


@app.route("/send/status")
def send_status():
    return jsonify(_send_job_snapshot())


# ---------- Settings ----------
@app.route("/settings", methods=["GET"])
def settings_view():
    accounts = graph_mail.list_accounts()
    # Per-account fields with fallback to legacy global settings so existing
    # users see their old values pre-filled on the first connected account.
    account_settings = {}
    legacy_name = db.get_setting("sender_name", "")
    legacy_email = db.get_setting("sender_email", "")
    legacy_sig = db.get_setting("email_signature", "")
    for i, a in enumerate(accounts):
        u = a["username"]
        s = {
            "sender_name": db.get_setting(f"sender_name_{u}", ""),
            "sender_email": db.get_setting(f"sender_email_{u}", ""),
            "email_signature": db.get_setting(f"email_signature_{u}", ""),
        }
        if i == 0:
            s["sender_name"] = s["sender_name"] or legacy_name
            s["sender_email"] = s["sender_email"] or legacy_email
            s["email_signature"] = s["email_signature"] or legacy_sig
        account_settings[u] = s
    return render_template("settings.html", account_settings=account_settings)


@app.route("/settings/account", methods=["POST"])
def settings_account():
    username = request.form.get("username", "").strip()
    if not username:
        flash("Missing account.", "error")
        return redirect(url_for("settings_view"))
    sender_email = request.form.get("sender_email", "").strip()
    if sender_email and not _valid_email(sender_email):
        flash(f"'{sender_email}' is not a valid sender email. Nothing was saved.", "error")
        return redirect(url_for("settings_view") + f"#acc-{username}")
    db.set_setting(f"sender_name_{username}", request.form.get("sender_name", "").strip())
    db.set_setting(f"sender_email_{username}", request.form.get("sender_email", "").strip())
    db.set_setting(f"email_signature_{username}", request.form.get("email_signature", ""))
    flash(f"Saved settings for {username}.", "success")
    return redirect(url_for("settings_view") + f"#acc-{username}")


# ---------- Auth (Microsoft) ----------
@app.route("/auth")
def auth_view():
    return render_template("auth.html")


@app.route("/auth/start", methods=["POST"])
def auth_start():
    try:
        info = graph_mail.start_device_flow()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    t = threading.Thread(target=graph_mail.complete_device_flow, daemon=True)
    t.start()
    return jsonify(info)


@app.route("/auth/status")
def auth_status():
    accs = graph_mail.list_accounts()
    return jsonify({
        "signed_in": bool(accs),
        "account": accs[0]["username"] if accs else None,
        "accounts": [a["username"] for a in accs],
        "flow": graph_mail.get_flow_state(),
    })


@app.route("/auth/signout", methods=["POST"])
def auth_signout():
    username = request.form.get("username", "").strip() or None
    graph_mail.sign_out(username)
    if request.form.get("from") == "settings":
        return redirect(url_for("settings_view"))
    return redirect(url_for("auth_view"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
