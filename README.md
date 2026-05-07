# Nightscale Leads

Schlanke Web-App zur Lead-Verwaltung und automatischem Mailversand über Microsoft 365 (Graph API).

## Features

- **Kontakte**: Firma, Name, E-Mail, Art (z.B. Investor, Partner, Lead), Notizen
- **Templates**: pro „Art" eine Vorlage mit Rich-Text-Editor (Fett, Kursiv, Listen, Links etc.)
- **Versand**: alle Kontakte einer Art mit einem Klick
- **Platzhalter**: `{{firma}}`, `{{name}}`, `{{email}}`, `{{art}}` werden pro Empfänger ersetzt
- **Absender-Name**: konfigurierbar in den Einstellungen
- **Versand-Log** mit Erfolg/Fehler pro Mail
- Modernes dunkles UI

## Setup

### 1. Azure App Registration (einmalig)

Gemäß deiner Anleitung: App-Registration `nightscale-mail-sender`, Public Client = Yes,
Mail.Send (Delegated) → Client- und Tenant-ID notieren.

### 2. Lokal installieren

```bash
git clone <repo>
cd nightscale-leads
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# .env editieren: AZURE_CLIENT_ID, AZURE_TENANT_ID, FLASK_SECRET_KEY, SENDER_EMAIL
python app.py
```

App läuft auf http://localhost:5000

### 3. Microsoft verbinden

1. Im Browser auf „Microsoft verbinden" klicken
2. Code in dem geöffneten Tab eingeben, mit `pius@nightscale.ai` einloggen
3. Token wird in `token_cache.bin` lokal gecached

### 4. Einstellungen

Unter `/settings`:
- **Absender-Name**: z.B. „Pius Martin · Nightscale" — wird im Posteingang angezeigt
- **Absender-E-Mail (optional)**: leer lassen für das angemeldete Konto. Override
  funktioniert nur wenn du Send-As-Rechte hast (Shared Mailbox).

## Deployment

### Option A — World4You „MyService Webspace" mit Python

World4You-Standardpläne (Webspace) sind primär PHP/MySQL. Für Python brauchst du
einen **Webspace mit Python-Support** (Passenger/WSGI) oder einen **Server vServer**.

Wenn dein Plan Python via Passenger unterstützt (wie bei den meisten neueren Webspaces):

1. Per FTP/SFTP alle Dateien ins Webspace-Verzeichnis hochladen (z.B. `/leads/`).
2. SSH-Zugang nutzen (im World4You Kundenmenü aktivieren) und installieren:
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
3. `passenger_wsgi.py` (für Passenger) ist im Repo enthalten.
4. Im World4You-Kundenmenü: **Domain → Subdomain anlegen**, z.B. `leads.deine-domain.at`,
   und auf den App-Ordner zeigen lassen.
5. `.env` direkt am Server ausfüllen (Werte aus Azure).
6. **Wichtig**: Beim ersten Login Device-Code im Browser des Servers
   _oder_ lokal abschließen (das Token-File `token_cache.bin` dann auf den Server kopieren).

### Option B — vServer / VPS (empfohlen)

Auf einem kleinen Linux-VPS mit Nginx-Reverse-Proxy + Gunicorn:

```bash
gunicorn -w 2 -b 127.0.0.1:8000 app:app
```

Nginx-Config:
```
server {
    server_name leads.deine-domain.at;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

DNS bei World4You: A-Record `leads.deine-domain.at` → VPS-IP.

### Option C — Lokal nur, Domain später

Du kannst die App einfach am Mac laufen lassen. Wenn du später öffentlich
hosten willst, hilft Option B.

## Sicherheit

- `.env`, `data.db`, `token_cache.bin` sind in `.gitignore` und dürfen NIE
  ins Repo committet werden.
- Der Token-Cache ist eine Login-Session — wer die Datei hat, kann in deinem
  Namen Mails senden. Behandle sie wie ein Passwort.
- Die App selbst hat kein Login. Wenn sie öffentlich erreichbar wird, **muss**
  ein Reverse-Proxy (Nginx) mit Basic-Auth oder ein Cloudflare-Access davor.

## Datei-Struktur

```
app.py              Flask-Routen
db.py               SQLite (Kontakte, Templates, Settings, Log)
graph_mail.py       Microsoft Graph + MSAL Device-Code-Flow
templates/          Jinja2-HTML
static/style.css    Dark-Theme
requirements.txt
passenger_wsgi.py   für World4You / Passenger
.env.example
```
