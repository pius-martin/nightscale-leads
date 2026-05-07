import os
import re
import threading
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from dotenv import load_dotenv

import db
import graph_mail

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-key-change-me")

db.init_db()


def render_template_text(text: str, contact: dict) -> str:
    """Replace {{firma}}, {{name}}, {{email}}, {{art}} placeholders."""
    def repl(m):
        key = m.group(1).strip().lower()
        return str(contact.get(key, "") or "")
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", repl, text or "")


@app.context_processor
def inject_globals():
    return {
        "signed_in": graph_mail.is_signed_in(),
        "account": graph_mail.signed_in_account(),
        "settings": db.all_settings(),
    }


@app.route("/")
def index():
    return redirect(url_for("contacts"))


# ---------- Contacts ----------
@app.route("/contacts", methods=["GET", "POST"])
def contacts():
    if request.method == "POST":
        db.add_contact(
            firma=request.form.get("firma", ""),
            name=request.form.get("name", ""),
            email=request.form.get("email", ""),
            art=request.form.get("art", ""),
            notes=request.form.get("notes", ""),
        )
        return redirect(url_for("contacts"))
    items = db.list_contacts()
    arten = db.list_arten()
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
            name=request.form.get("name", ""),
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


# ---------- Templates ----------
@app.route("/templates", methods=["GET", "POST"])
def templates_view():
    if request.method == "POST":
        art = request.form.get("art", "").strip()
        subject = request.form.get("subject", "")
        body = request.form.get("body", "")
        if art:
            db.upsert_template(art, subject, body)
        return redirect(url_for("templates_view", art=art))

    arten = db.list_arten()
    selected = request.args.get("art") or (arten[0] if arten else "")
    current = db.get_template_by_art(selected) if selected else None
    all_templates = db.list_templates()
    return render_template(
        "templates.html",
        arten=arten,
        selected=selected,
        current=current,
        all_templates=all_templates,
    )


@app.route("/templates/<int:tid>/delete", methods=["POST"])
def delete_template(tid):
    db.delete_template(tid)
    return redirect(url_for("templates_view"))


# ---------- Send ----------
@app.route("/send", methods=["GET"])
def send_view():
    arten = db.list_arten()
    selected = request.args.get("art") or (arten[0] if arten else "")
    template = db.get_template_by_art(selected) if selected else None
    contacts_for_art = db.list_contacts(art=selected) if selected else []

    previews = []
    if template and contacts_for_art:
        for c in contacts_for_art:
            previews.append({
                "contact": c,
                "subject": render_template_text(template["subject"], c),
                "body": render_template_text(template["body"], c),
            })
    log = db.list_log(50)
    return render_template(
        "send.html",
        arten=arten,
        selected=selected,
        template=template,
        previews=previews,
        log=log,
    )


@app.route("/send/run", methods=["POST"])
def send_run():
    art = request.form.get("art", "").strip()
    if not art:
        flash("Keine Art ausgewählt.", "error")
        return redirect(url_for("send_view"))
    if not graph_mail.is_signed_in():
        flash("Nicht bei Microsoft angemeldet.", "error")
        return redirect(url_for("auth_view"))

    template = db.get_template_by_art(art)
    if not template:
        flash("Keine Vorlage für diese Art.", "error")
        return redirect(url_for("send_view", art=art))

    contacts_for_art = db.list_contacts(art=art)
    sender_name = db.get_setting("sender_name", "")
    sender_email = db.get_setting("sender_email", "") or os.environ.get("SENDER_EMAIL", "")
    sent, failed = 0, 0
    for c in contacts_for_art:
        subject = render_template_text(template["subject"], c)
        body = render_template_text(template["body"], c)
        body_html = body if "<" in body and ">" in body else body.replace("\n", "<br>")
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
    flash(f"{sent} gesendet, {failed} fehlgeschlagen.", "success" if failed == 0 else "error")
    return redirect(url_for("send_view", art=art))


# ---------- Settings ----------
@app.route("/settings", methods=["GET", "POST"])
def settings_view():
    if request.method == "POST":
        db.set_setting("sender_name", request.form.get("sender_name", "").strip())
        db.set_setting("sender_email", request.form.get("sender_email", "").strip())
        flash("Einstellungen gespeichert.", "success")
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
    # Run completion in background thread (blocks until user logs in)
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
