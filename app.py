import hmac
import os
import re
import threading
from functools import wraps
from urllib.parse import urlparse
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, session
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
        return redirect(url_for("login", next=_safe_next(request.path)))
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
        return redirect(url_for("login", next=_safe_next(request.path)))
    return None


@app.context_processor
def inject_globals():
    try:
        signed_in = graph_mail.is_signed_in()
        account = graph_mail.signed_in_account()
        settings = db.all_settings() if _db_ready["ok"] else {}
    except Exception:
        signed_in, account, settings = False, None, {}
    return {"signed_in": signed_in, "account": account, "settings": settings}


def render_template_text(text: str, contact: dict) -> str:
    """Replace {{firma}}, {{name}}, {{email}}, {{art}} placeholders."""
    def repl(m):
        key = m.group(1).strip().lower()
        return str(contact.get(key, "") or "")
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


# ---------- Contacts ----------
@app.route("/contacts", methods=["GET", "POST"])
def contacts():
    arten = db.list_arten()
    if request.method == "POST":
        art = request.form.get("art", "").strip()
        if art and art not in arten:
            db.add_art(art)
        db.add_contact(
            firma=request.form.get("firma", ""),
            first_name=request.form.get("first_name", ""),
            last_name=request.form.get("last_name", ""),
            email=request.form.get("email", ""),
            art=art,
            notes=request.form.get("notes", ""),
        )
        return redirect(url_for("contacts"))
    items = db.list_contacts()
    return render_template("contacts.html", contacts=items, arten=arten)


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
        subject = request.form.get("subject", "")
        body = request.form.get("body", "")
        footer = request.form.get("footer", "")
        if art:
            if art not in arten:
                db.add_art(art)
            db.upsert_template(art, subject, body, footer)
        return redirect(url_for("templates_view", art=art))

    selected = request.args.get("art") or (arten[0] if arten else "")
    current = db.get_template_by_art(selected) if selected else None
    return render_template(
        "templates.html",
        arten=arten,
        selected=selected,
        current=current,
    )


@app.route("/templates/<int:tid>/delete", methods=["POST"])
def delete_template(tid):
    db.delete_template(tid)
    return redirect(url_for("templates_view"))


def _to_html(text: str) -> str:
    if not text:
        return ""
    return text if "<" in text and ">" in text else text.replace("\n", "<br>")


def _compose_body(template_body: str, template_footer: str, signature: str, contact: dict) -> str:
    body_html = _to_html(render_template_text(template_body, contact))
    footer_html = _to_html(render_template_text(template_footer or "", contact))
    sig_html = _to_html(render_template_text(signature or "", contact))
    parts = [p for p in (body_html, footer_html, sig_html) if p]
    return "<br><br>".join(parts)


# ---------- Send ----------
@app.route("/send", methods=["GET"])
def send_view():
    arten = db.list_arten()
    selected = request.args.get("art") or (arten[0] if arten else "")
    template = db.get_template_by_art(selected) if selected else None
    contacts_for_art = db.list_contacts(art=selected) if selected else []
    already = db.sent_contact_ids(selected) if selected else set()
    signature = db.get_setting("email_signature", "")

    previews = []
    new_count = 0
    if template and contacts_for_art:
        for c in contacts_for_art:
            sent_before = c["id"] in already
            if not sent_before:
                new_count += 1
            previews.append({
                "contact": c,
                "subject": render_template_text(template["subject"], c),
                "body": _compose_body(template["body"], template.get("footer", ""), signature, c),
                "already_sent": sent_before,
            })
    log = db.list_log(50)
    return render_template(
        "send.html",
        arten=arten,
        selected=selected,
        template=template,
        previews=previews,
        new_count=new_count,
        total_count=len(previews),
        log=log,
    )


@app.route("/send/run", methods=["POST"])
def send_run():
    art = request.form.get("art", "").strip()
    mode = request.form.get("mode", "new")  # 'new' or 'all'
    if not art:
        flash("No type selected.", "error")
        return redirect(url_for("send_view"))
    if not graph_mail.is_signed_in():
        flash("Not connected to Microsoft.", "error")
        return redirect(url_for("auth_view"))

    template = db.get_template_by_art(art)
    if not template:
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
        subject = render_template_text(template["subject"], c)
        body_html = _compose_body(template["body"], template.get("footer", ""), signature, c)
        try:
            graph_mail.send_mail(
                c["email"],
                subject,
                body_html,
                sender_name=sender_name or None,
                sender_email=sender_email or None,
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
    graph_mail.sign_out()
    return redirect(url_for("auth_view"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
