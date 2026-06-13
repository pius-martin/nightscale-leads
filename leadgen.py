"""Free lead generation: find local businesses via OpenStreetMap (Overpass),
then enrich from their website (email + point-of-sale system detection).

No API keys, no paid services. Network calls go through thin wrappers
(geocode_region / fetch_overpass / fetch_site) so they can be mocked in tests;
the parsing/detection logic is pure and unit-testable.
"""
import logging
import re
from html import unescape
from urllib.parse import urljoin

import requests

logger = logging.getLogger("nightscale.leadgen")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Identify ourselves per OSM usage policy.
USER_AGENT = "NightscaleLeads/1.0 (B2B outreach lead tool)"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# OSM category -> Overpass tag filter. Keep the set focused on the kinds of
# local businesses this tool targets (gastronomy, hospitality, retail).
CATEGORY_FILTERS = {
    "restaurant": '["amenity"="restaurant"]',
    "cafe": '["amenity"="cafe"]',
    "bar": '["amenity"="bar"]',
    "fast_food": '["amenity"="fast_food"]',
    "hotel": '["tourism"="hotel"]',
    "bakery": '["shop"="bakery"]',
    "retail": '["shop"]',
}

# Substring fingerprint in page HTML -> human label for the POS/booking/shop
# system. Lets us auto-fill the contact's pos_system from their website.
POS_FINGERPRINTS = {
    "gastrofix": "Gastrofix", "lightspeed": "Lightspeed", "vectron": "Vectron",
    "ready2order": "ready2order", "orderbird": "orderbird", "tillhub": "Tillhub",
    "gastronovi": "gastronovi", "hellotess": "Hello Tess", "hello-tess": "Hello Tess",
    "resmio": "resmio", "quandoo": "Quandoo", "opentable": "OpenTable",
    "thefork": "TheFork", "bookatable": "Bookatable", "formitable": "Formitable",
    "shopify": "Shopify", "woocommerce": "WooCommerce", "shopware": "Shopware",
    "simphony": "Oracle Simphony", "micros": "Oracle Micros",
    "square": "Square", "sumup": "SumUp", "izettle": "Zettle", "zettle": "Zettle",
}


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable, no network)
# ---------------------------------------------------------------------------
def build_overpass_query(bbox, categories, limit) -> str:
    """bbox = (south, west, north, east)."""
    s, w, n, e = bbox
    parts = []
    for cat in categories:
        filt = CATEGORY_FILTERS.get(cat)
        if not filt:
            continue
        parts.append(f"node{filt}({s},{w},{n},{e});")
        parts.append(f"way{filt}({s},{w},{n},{e});")
    body = "".join(parts)
    return f"[out:json][timeout:25];({body});out center {int(limit)};"


def parse_overpass(data: dict) -> list[dict]:
    out = []
    for el in (data or {}).get("elements", []):
        tags = el.get("tags", {}) or {}
        name = (tags.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "website": (tags.get("website") or tags.get("contact:website") or "").strip(),
            "email": (tags.get("email") or tags.get("contact:email") or "").strip(),
            "phone": (tags.get("phone") or tags.get("contact:phone") or "").strip(),
            "city": (tags.get("addr:city") or "").strip(),
            "pos_system": "",
        })
    return out


def extract_emails(html: str) -> list[str]:
    found = _EMAIL_RE.findall(html or "")
    seen, result = set(), []
    for e in found:
        el = e.lower()
        if el.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
            continue
        if "example.com" in el or "@sentry" in el or ".wixpress" in el or "@2x" in el:
            continue
        if el in seen:
            continue
        seen.add(el)
        result.append(e)
    return result


def detect_pos(html: str) -> str:
    low = (html or "").lower()
    for fingerprint, label in POS_FINGERPRINTS.items():
        if fingerprint in low:
            return label
    return ""


# ---------------------------------------------------------------------------
# Network wrappers (mocked in tests)
# ---------------------------------------------------------------------------
def geocode_region(region: str):
    """Return (south, west, north, east) for a place name, or None."""
    r = requests.get(
        NOMINATIM_URL,
        params={"q": region, "format": "json", "limit": 1},
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    arr = r.json()
    if not arr:
        return None
    bb = arr[0]["boundingbox"]  # [south, north, west, east] as strings
    return (float(bb[0]), float(bb[2]), float(bb[1]), float(bb[3]))


def fetch_overpass(query: str) -> dict:
    r = requests.post(
        OVERPASS_URL, data={"data": query},
        headers={"User-Agent": USER_AGENT}, timeout=60,
    )
    r.raise_for_status()
    return r.json()


def fetch_site(url: str, timeout: int = 8) -> str:
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    return r.text[:500000]


# --- Deep scraping for enrichment ---
_SUBPAGE_HINTS = ("impressum", "kontakt", "contact", "team", "ueber-uns",
                  "ueber_uns", "about", "ueberuns", "standorte", "filialen")
_HREF_RE = re.compile(r'href=["\']([^"\'#]+)["\']', re.I)
_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANKLINES_RE = re.compile(r"\n\s*\n+")


def find_subpages(home_html: str, base_url: str, limit: int = 4) -> list[str]:
    """Pull links that look like Impressum/Kontakt/Team/About/locations pages."""
    if not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url
    found, seen = [], set()
    for href in _HREF_RE.findall(home_html or ""):
        low = href.lower()
        if not any(h in low for h in _SUBPAGE_HINTS):
            continue
        url = urljoin(base_url, href)
        if not url.startswith("http"):
            continue
        if url in seen:
            continue
        seen.add(url)
        found.append(url)
        if len(found) >= limit:
            break
    return found


def html_to_text(html: str) -> str:
    text = _TAG_RE.sub(" ", html or "")
    text = _HTML_TAG_RE.sub(" ", text)
    text = unescape(text)
    text = _WS_RE.sub(" ", text)
    text = _BLANKLINES_RE.sub("\n", text)
    return text.strip()


def collect_site_text(website: str, max_chars: int = 30000) -> str:
    """Fetch homepage + a few key subpages, return cleaned, length-capped text.
    Each page is labelled so the enricher knows where text came from (the
    Impressum block is where the managing director legally must appear)."""
    if not website:
        return ""
    home = fetch_site(website)
    parts = [f"# PAGE: {website}\n{html_to_text(home)}"]
    for sub in find_subpages(home, website):
        try:
            parts.append(f"# PAGE: {sub}\n{html_to_text(fetch_site(sub))}")
        except Exception:
            logger.warning("subpage fetch failed: %s", sub)
    return "\n\n".join(parts)[:max_chars]
