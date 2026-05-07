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

## Deployment auf Railway (empfohlen)

Railway hostet die App, eine Subdomain bei World4You zeigt per CNAME drauf.

### 1. Railway-Projekt anlegen

1. Auf https://railway.app einloggen (mit GitHub).
2. **+ New Project → Deploy from GitHub repo** → `pius-martin/nightscale-leads` wählen.
3. Branch: `claude/contact-email-automation-wOaF9` (oder vorher in `main` mergen).
4. Railway erkennt Python automatisch und baut über Nixpacks; die `Procfile` wird
   verwendet.

### 2. Environment Variables setzen

Im Railway-Service unter **Variables**:

| Variable           | Wert                                         |
|--------------------|----------------------------------------------|
| `AZURE_CLIENT_ID`  | `42f15015-c60a-4472-97ca-d19f2417c66e`        |
| `AZURE_TENANT_ID`  | `bc7f9f38-30a5-4eda-a455-0f714ba84fab`        |
| `FLASK_SECRET_KEY` | Langer Zufallsstring (z.B. `openssl rand -hex 32`) |
| `APP_PASSWORD`     | Passwort für den App-Zugang                   |
| `SENDER_EMAIL`     | `pius@nightscale.ai`                          |
| `DATA_DIR`         | `/data`                                       |

### 3. Volume für persistente Daten

SQLite und Token-Cache müssen Restarts/Deploys überleben:

1. Service → **Settings → Volumes → + New Volume**
2. Mount Path: `/data`
3. Größe: 1 GB reicht.

Ohne Volume verlierst du Kontakte, Templates und den Microsoft-Login bei jedem
Redeploy.

### 4. Erster Start + Microsoft-Login

1. Railway gibt dir eine URL wie `https://nightscale-leads-production.up.railway.app`.
2. Aufrufen → mit deinem `APP_PASSWORD` einloggen.
3. „Microsoft verbinden" klicken → Code merken → im neuen Tab auf
   https://microsoft.com/devicelogin Code eingeben → mit `pius@nightscale.ai` einloggen.
4. Token wird in `/data/token_cache.bin` gespeichert (überlebt Deploys).

### 5. World4You-Domain anbinden

In Railway: Service → **Settings → Networking → Custom Domain** → z.B.
`leads.nightscale.ai` eingeben. Railway zeigt dir einen CNAME-Target (z.B.
`xyz.up.railway.app`).

Im **World4You Kundenmenü** → Domain → DNS-Verwaltung:
- Neuer **CNAME-Record**:
  - Name/Host: `leads`
  - Ziel: der von Railway gezeigte Wert (`xyz.up.railway.app`)
  - TTL: Standard (3600)

Nach einigen Minuten ist `https://leads.nightscale.ai` live (Railway stellt
automatisch ein Let's-Encrypt-Zertifikat aus).

> **Hinweis**: Für die Apex-Domain (`nightscale.ai` ohne Subdomain) braucht
> es ALIAS/ANAME, das World4You nicht anbietet. Daher Subdomain nehmen.

## Lokal entwickeln

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
