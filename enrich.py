"""Lead enrichment: turn scraped website text into rich, structured data —
named decision-maker contacts (with role + email) plus firmographics
(employees, revenue, locations, POS system).

Two engines, selected at runtime by get_enricher():
- ClaudeEnricher  — used when ANTHROPIC_API_KEY is set. Sends the scraped text
  to Claude and gets back a validated structured object. Optionally enables the
  server-side web_search tool (LEADGEN_WEB_SEARCH=1) to research beyond the site.
- FreeRegexEnricher — no key required. Heuristic extraction from the Impressum /
  contact pages (managing-director names + role-mailboxes + AT identifiers).

Both return the same dict shape so the rest of the app is engine-agnostic:
    {legal_name, industry, employees, revenue, locations, pos_system, uid,
     contacts: [{name, role, email, phone}]}
where role is one of ROLES.
"""
import logging
import os
import re

import leadgen

logger = logging.getLogger("nightscale.enrich")

ROLES = ("geschaeftsfuehrung", "marketing", "vertrieb", "allgemein")
ROLE_LABELS = {
    "geschaeftsfuehrung": "Geschäftsführung",
    "marketing": "Marketing",
    "vertrieb": "Vertrieb",
    "allgemein": "General",
}

_ROLE_KEYWORDS = [
    (re.compile(r"gesch[äa]ftsf[üu]hr|inhaber|eigent[üu]mer|owner|ceo|managing director|gr[üu]nder|prokurist", re.I), "geschaeftsfuehrung"),
    (re.compile(r"marketing|kommunikation|social\s*media|\bpr\b|presse", re.I), "marketing"),
    (re.compile(r"vertrieb|\bsales\b|verkauf|kundenbetreuung", re.I), "vertrieb"),
]

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_NAME_RE = re.compile(r"\b([A-ZÄÖÜ][a-zäöüß]+)\s+([A-ZÄÖÜ][a-zäöüß]+)\b")
# Titles / role words that get mistaken for a first name — blanked before
# name extraction so "Geschäftsführer Max Huber" yields "Max Huber".
_TITLE_RE = re.compile(
    r"\b(gesch[äa]ftsf[üu]hrer(in)?|inhaber(in)?|eigent[üu]mer(in)?|owner|ceo|"
    r"gr[üu]nder(in)?|prokurist(in)?|leitung|leiter(in)?|marketing|vertrieb|sales|"
    r"verkauf|kommunikation|presse|herr|frau|dr|mag|ing|dipl|prof|kontakt|team)\b\.?",
    re.I,
)
_UID_RE = re.compile(r"\bATU\d{8}\b")
_EMP_RE = re.compile(r"(\d{1,5})\s*(?:Mitarbeiter|Besch[äa]ftigte|Mitarbeitende|employees)", re.I)
_GENERIC_LOCALS = {"info", "office", "kontakt", "contact", "mail", "hello", "welcome", "anfrage"}


def normalize_role(value: str) -> str:
    v = (value or "").strip().lower()
    if v in ROLES:
        return v
    return _role_for(v)


def _role_for(context: str) -> str:
    for rx, role in _ROLE_KEYWORDS:
        if rx.search(context or ""):
            return role
    return "allgemein"


def _clean_emails(text: str) -> list[str]:
    out, seen = [], set()
    for e in _EMAIL_RE.findall(text or ""):
        el = e.lower()
        if el.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
            continue
        if "example.com" in el or "@sentry" in el or "@2x" in el:
            continue
        if el in seen:
            continue
        seen.add(el)
        out.append(e)
    return out


def _empty_result(business: dict) -> dict:
    return {
        "legal_name": business.get("name", ""), "industry": "",
        "employees": "", "revenue": "", "locations": "", "uid": "",
        "pos_system": business.get("pos_system", ""), "contacts": [],
    }


class FreeRegexEnricher:
    name = "free"

    def enrich(self, business: dict, site_text: str) -> dict:
        text = site_text or ""
        result = _empty_result(business)
        result["pos_system"] = leadgen.detect_pos(text) or business.get("pos_system", "")
        m = _EMP_RE.search(text)
        if m:
            result["employees"] = m.group(1)
        uid = _UID_RE.search(text)
        if uid:
            result["uid"] = uid.group(0)
        low = text.lower()
        for email in _clean_emails(text):
            idx = low.find(email.lower())
            window = text[max(0, idx - 180):idx]
            local = email.split("@")[0].lower()
            role = _role_for(window)
            names = _NAME_RE.findall(_TITLE_RE.sub(" ", window))
            name = " ".join(names[-1]) if names else ""
            result["contacts"].append({"name": name, "role": role, "email": email, "phone": ""})
        return result


_PROMPT = """You are extracting B2B lead data from a company's own website text.
Company name (from OpenStreetMap): {name}
Website: {website}

From the text below, extract:
- legal_name, industry, employees (count or band, e.g. "11-50"), revenue (band if stated), locations (number of sites if stated), pos_system (point-of-sale / booking / shop system if detectable)
- contacts: named people with a business email. For each, classify role as exactly one of: geschaeftsfuehrung (managing director / owner / CEO), marketing, vertrieb (sales), allgemein (generic info@/office@ or unclear). The Impressum legally lists the managing director — prefer real names over generic mailboxes.

Only use information present in the text. Leave a field empty if unknown. Do not invent emails or names.

--- WEBSITE TEXT ---
{text}
"""


def _build_prompt(business: dict, site_text: str) -> str:
    return _PROMPT.format(
        name=business.get("name", ""), website=business.get("website", ""),
        text=site_text or "",
    )


class ClaudeEnricher:
    name = "claude"

    def __init__(self, api_key: str, model: str, web_search: bool = False):
        import anthropic  # imported lazily so the free path needs no SDK
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._web_search = web_search

    def enrich(self, business: dict, site_text: str) -> dict:
        try:
            if self._web_search:
                data = self._extract_web_search(business, site_text)
            else:
                resp = self._client.messages.parse(
                    model=self._model, max_tokens=4000,
                    messages=[{"role": "user", "content": _build_prompt(business, site_text)}],
                    output_format=LeadExtract,
                )
                data = resp.parsed_output
            return self._to_dict(business, data)
        except Exception:
            logger.exception("Claude enrichment failed for %s; using free fallback", business.get("name"))
            return FreeRegexEnricher().enrich(business, site_text)

    def _extract_web_search(self, business: dict, site_text: str):
        import json
        prompt = _build_prompt(business, site_text) + (
            "\n\nUse the web_search tool to fill employees, revenue, locations, and "
            "decision-maker names/emails from beyond this site (company registry, news, "
            "directories). Then reply with ONLY a JSON object: {\"legal_name\":\"\","
            "\"industry\":\"\",\"employees\":\"\",\"revenue\":\"\",\"locations\":\"\","
            "\"pos_system\":\"\",\"contacts\":[{\"name\":\"\",\"role\":\"\",\"email\":\"\",\"phone\":\"\"}]}"
        )
        messages = [{"role": "user", "content": prompt}]
        tools = [{"type": "web_search_20260209", "name": "web_search"}]
        for _ in range(4):
            resp = self._client.messages.create(
                model=self._model, max_tokens=4000, tools=tools, messages=messages,
            )
            if resp.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": resp.content})
                continue
            break
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        match = re.search(r"\{.*\}", text, re.S)
        return json.loads(match.group(0)) if match else {}

    def _to_dict(self, business: dict, data) -> dict:
        if data is None:
            return _empty_result(business)
        get = (lambda k: getattr(data, k, "")) if not isinstance(data, dict) else data.get
        raw_contacts = get("contacts") or []
        contacts = []
        for c in raw_contacts:
            cg = (lambda k: getattr(c, k, "")) if not isinstance(c, dict) else c.get
            email = (cg("email") or "").strip()
            if not email:
                continue
            contacts.append({
                "name": (cg("name") or "").strip(),
                "role": normalize_role(cg("role") or ""),
                "email": email,
                "phone": (cg("phone") or "").strip(),
            })
        return {
            "legal_name": (get("legal_name") or business.get("name", "")).strip() if (get("legal_name") or "").strip() else business.get("name", ""),
            "industry": (get("industry") or "").strip(),
            "employees": str(get("employees") or "").strip(),
            "revenue": str(get("revenue") or "").strip(),
            "locations": str(get("locations") or "").strip(),
            "uid": str(get("uid") or "").strip() if isinstance(data, dict) else "",
            "pos_system": (get("pos_system") or business.get("pos_system", "")).strip() or business.get("pos_system", ""),
            "contacts": contacts,
        }


# Pydantic schema for structured extraction (only needed for the Claude path).
try:
    from pydantic import BaseModel

    class Contact(BaseModel):
        name: str = ""
        role: str = "allgemein"
        email: str = ""
        phone: str = ""

    class LeadExtract(BaseModel):
        legal_name: str = ""
        industry: str = ""
        employees: str = ""
        revenue: str = ""
        locations: str = ""
        pos_system: str = ""
        contacts: list[Contact] = []

    _HAVE_PYDANTIC = True
except Exception:  # pragma: no cover - pydantic ships with the anthropic SDK
    _HAVE_PYDANTIC = False
    LeadExtract = None


def get_enricher():
    """Pick the enrichment engine based on the environment."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key and _HAVE_PYDANTIC:
        try:
            return ClaudeEnricher(
                key,
                os.environ.get("LEADGEN_MODEL", "claude-opus-4-8"),
                web_search=os.environ.get("LEADGEN_WEB_SEARCH") == "1",
            )
        except Exception:
            logger.exception("Claude enricher unavailable; using free fallback")
    return FreeRegexEnricher()


def engine_label() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key and _HAVE_PYDANTIC:
        model = os.environ.get("LEADGEN_MODEL", "claude-opus-4-8")
        return f"AI enrichment ({model})"
    return "Free enrichment (no AI key set)"
