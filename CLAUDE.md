# Project Rules

nightscale-leads — Flask-Webapp für Lead-Management + Massen-Mail über Microsoft 365 (Graph API): Kontakte mit Rollen pflegen, Templates pro Rolle mit Platzhaltern, Versand an alle Kontakte einer Rolle mit einem Klick.

## Quick Start ("Start nightscale-leads")
- **Repo:** `/Users/piusmartin/nightscale-leads`. Noch keine `.claude/settings.local.json` — beim ersten Start die Standard-Allowlist anlegen (siehe globaler Repo-Schnellstart).
- **Lokal starten:** `source .venv/bin/activate && python app.py` → `http://localhost:5000`. venv fehlt? `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`.
- **Env** (`.env`, gitignored): `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `FLASK_SECRET_KEY`, `SENDER_EMAIL`, `APP_PASSWORD`, optional `DATABASE_URL` (sonst SQLite).
- **Microsoft-Login:** Device-Code-Flow ("Microsoft verbinden" → Code auf microsoft.com/devicelogin). Token-Cache `.token_cache.bin` ist sensibel + gitignored.
- **Layout:** `app.py` (Flask-Routes), `db.py` (Models), `graph_mail.py` (Graph-Integration), `templates/` (Jinja2), `static/`.

## Deployment
- Railway (Nixpacks, `Procfile`/gunicorn, `railway.json`); Postgres via Reference-Var `${{Postgres.DATABASE_URL}}`; Domain per CNAME (World4You DNS).
- Öffentliches Deployment nur mit Zugriffsschutz (`APP_PASSWORD`/Basic-Auth bzw. Cloudflare Access).

## Besonderheiten
- Azure App Registration braucht nur delegiertes `Mail.Send` — kein Admin-Consent nötig.
