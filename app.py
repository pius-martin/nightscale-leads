import csv
import hmac
import io
import os
import re
import threading
from functools import wraps
from urllib.parse import urlparse
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, session
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv

import db
import graph_mail

load_dotenv()

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
            _db_ready["error"] = str(e)
            return _db_ready["error"]
    return None


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


@app.context_processor
def inject_globals():
    try:
        accounts = graph_mail.list_accounts()
        signed_in = bool(accounts)
        account = accounts[0]["username"] if accounts else None
        settings = db.all_settings() if _db_ready["ok"] else {}
    except Exception:
        accounts, signed_in, account, settings = [], False, None, {}
    return {
        "signed_in": signed_in,
        "account": account,
        "ms_accounts": accounts,
        "settings": settings,
    }


MISSING_MARKER = "xxx"


def render_template_text(text: str, contact: dict) -> str:
    """Replace {{firma}}, {{first_name}}, {{last_name}}, {{name}}, {{email}}, {{art}} placeholders.
    Missing or empty fields are filled with MISSING_MARKER so the gap is visible
    in the preview rather than an empty hole."""
    def repl(m):
        key = m.group(1).strip().lower()
        value = str(contact.get(key, "") or "").strip()
        return value if value else MISSING_MARKER
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", repl, text or "")


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
        art = request.form.get("art", "").strip()
        if art and art not in arten:
            db.add_art(art)
        firma = request.form.get("firma", "").strip()
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        if not firma and not first_name and not last_name:
            flash("Provide at least a company or a name.", "error")
            return redirect(url_for("contacts"))
        db.add_contact(
            firma=firma,
            first_name=first_name,
            last_name=last_name,
            email=request.form.get("email", ""),
            art=art,
            notes=request.form.get("notes", ""),
        )
        return redirect(url_for("contacts"))
    items = db.list_contacts()
    return render_template("contacts.html", contacts=items, arten=arten)


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
        flash(f"Could not parse file: {e}", "error")
        return redirect(url_for("contacts"))
    added, skipped, errors = _import_rows(rows)
    msg = f"Imported {added} contact{'' if added == 1 else 's'}"
    if skipped:
        msg += f", skipped {skipped}"
        if errors:
            msg += " (" + "; ".join(errors[:3]) + ("…" if len(errors) > 3 else "") + ")"
    flash(msg + ".", "success" if skipped == 0 else "error")
    return redirect(url_for("contacts"))


@app.route("/contacts/<int:cid>/edit", methods=["GET", "POST"])
def edit_contact(cid):
    contact = db.get_contact(cid)
    if not contact:
        return redirect(url_for("contacts"))
    if request.method == "POST":
        db.update_contact(
            cid,
            firma=request.form.get("firma", ""),
            first_name=request.form.get("first_name", ""),
            last_name=request.form.get("last_name", ""),
            email=request.form.get("email", ""),
            art=request.form.get("art", ""),
            notes=request.form.get("notes", ""),
        )
        return redirect(url_for("contacts"))
    arten = db.list_arten()
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
    """Stitch body + footer + signature into one HTML body. Each section is
    already block-level (Quill <p> tags or plain text wrapped in <p>), so we
    concatenate without adding extra <br><br> which caused doubled spacing."""
    parts = []
    for raw in (template_body, template_footer, signature):
        rendered = render_template_text(raw or "", contact)
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
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="only light">
<meta name="supported-color-schemes" content="only light">
<meta name="x-apple-disable-message-reformatting">
<meta name="format-detection" content="telephone=no,date=no,address=no,email=no,url=no">
<title>Nightscale</title>
<style>
  /* Force light theme on every element so Apple Mail / Outlook don't auto-invert */
  :root {{ color-scheme: only light; supported-color-schemes: only light; }}
  body, body * {{ background-color:transparent !important; }}
  body {{ margin:0; padding:0; background-color:#ffffff !important; color:#1a1a1a !important; }}
  .wrap {{ max-width:640px; margin:0 auto; padding:24px; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif; color:#1a1a1a !important; background-color:#ffffff !important; line-height:1.55; font-size:15px; }}
  .wrap p {{ margin:0 0 1em; color:#1a1a1a !important; }}
  .wrap a {{ color:#0a66c2 !important; text-decoration:underline; }}
  .wrap strong, .wrap b {{ font-weight:600; color:#1a1a1a !important; background-color:transparent !important; }}
  .wrap em, .wrap i {{ color:#1a1a1a !important; }}
  .wrap ul, .wrap ol {{ margin:0 0 1em; padding-left:24px; color:#1a1a1a !important; }}
  .wrap blockquote {{ margin:0 0 1em; padding:8px 14px; border-left:3px solid #d0d0d0; color:#444 !important; }}
</style>
</head>
<body>
<div class="wrap" style="color:#1a1a1a;background-color:#ffffff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;line-height:1.55;font-size:15px;max-width:640px;margin:0 auto;padding:24px;">{content}</div>
</body>
</html>"""


def _wrap_email_html(inner_html: str) -> str:
    return EMAIL_HTML_TEMPLATE.format(content=inner_html or "")


# ---------- Send ----------
def _contact_variant(c: dict) -> str:
    has_name = bool((c.get("first_name") or "").strip() or (c.get("last_name") or "").strip())
    return "personal" if has_name else "anonymous"


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
    log = db.list_log(50)
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
        log=log,
    )


@app.route("/send/run", methods=["POST"])
def send_run():
    art = request.form.get("art", "").strip()
    mode = request.form.get("mode", "new")  # 'new' or 'all'
    account = request.form.get("account", "").strip() or None
    if not art:
        flash("No type selected.", "error")
        return redirect(url_for("send_view"))
    if not graph_mail.list_accounts():
        flash("No Microsoft account connected.", "error")
        return redirect(url_for("auth_view"))

    variants_for_art = db.get_templates_for_art(art)
    if not variants_for_art:
        flash("No template for this type.", "error")
        return redirect(url_for("send_view", art=art))

    contacts_for_art = db.list_contacts(art=art)
    already = db.sent_contact_ids(art) if mode == "new" else set()
    sender_name = db.get_setting("sender_name", "")
    sender_email = db.get_setting("sender_email", "") or os.environ.get("SENDER_EMAIL", "")
    signature = db.get_setting("email_signature", "")
    sent, failed, skipped = 0, 0, 0
    for c in contacts_for_art:
        if mode == "new" and c["id"] in already:
            skipped += 1
            continue
        variant = _contact_variant(c)
        template = variants_for_art.get(variant) or variants_for_art.get(
            "personal" if variant == "anonymous" else "anonymous"
        )
        if not template:
            failed += 1
            db.log_send(c["id"], c["email"], art, "", "failed", f"no template for variant {variant}")
            continue
        subject = render_template_text(template["subject"], c)
        inner = _compose_body(template["body"], template.get("footer", ""), signature, c)
        body_html = _wrap_email_html(inner)
        try:
            graph_mail.send_mail(
                c["email"],
                subject,
                body_html,
                sender_name=sender_name or None,
                sender_email=sender_email or None,
                account=account,
            )
            db.log_send(c["id"], c["email"], art, subject, "sent")
            sent += 1
        except Exception as e:
            db.log_send(c["id"], c["email"], art, subject, "failed", str(e))
            failed += 1
    parts = [f"Sent {sent}"]
    if skipped:
        parts.append(f"skipped {skipped} already-sent")
    if failed:
        parts.append(f"{failed} failed")
    flash(", ".join(parts) + ".", "success" if failed == 0 else "error")
    return redirect(url_for("send_view", art=art))


# ---------- Settings ----------
@app.route("/settings", methods=["GET", "POST"])
def settings_view():
    if request.method == "POST":
        db.set_setting("sender_name", request.form.get("sender_name", "").strip())
        db.set_setting("sender_email", request.form.get("sender_email", "").strip())
        db.set_setting("email_signature", request.form.get("email_signature", ""))
        flash("Saved.", "success")
        return redirect(url_for("settings_view"))
    return render_template("settings.html")


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
    return jsonify({
        "signed_in": graph_mail.is_signed_in(),
        "account": graph_mail.signed_in_account(),
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
