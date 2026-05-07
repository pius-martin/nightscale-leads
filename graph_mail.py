import os
import threading
import msal
import requests

import db as _db

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.Send"]
TOKEN_CACHE_KEY = "msal_token_cache"

_lock = threading.Lock()
_pending_flow = None  # (app, cache, flow)


def _load_cache_from_db() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    try:
        serialized = _db.get_setting(TOKEN_CACHE_KEY, "")
    except Exception:
        serialized = ""
    if serialized:
        try:
            cache.deserialize(serialized)
        except Exception:
            pass
    return cache


def _save_cache(cache: msal.SerializableTokenCache):
    if cache.has_state_changed:
        try:
            _db.set_setting(TOKEN_CACHE_KEY, cache.serialize())
        except Exception:
            pass


def _build_app():
    """Build a fresh MSAL app + cache loaded from DB. The same cache holds all
    signed-in user accounts; MSAL handles multi-account internally."""
    client_id = os.environ.get("AZURE_CLIENT_ID")
    tenant_id = os.environ.get("AZURE_TENANT_ID")
    if not client_id or not tenant_id:
        raise RuntimeError("AZURE_CLIENT_ID and AZURE_TENANT_ID must be set")
    cache = _load_cache_from_db()
    app = msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        token_cache=cache,
    )
    return app, cache


def list_accounts() -> list:
    """Return all accounts currently in the token cache, as plain dicts."""
    try:
        app, _ = _build_app()
        return [
            {"username": a["username"], "home_account_id": a["home_account_id"]}
            for a in app.get_accounts()
        ]
    except Exception:
        return []


def is_signed_in() -> bool:
    return len(list_accounts()) > 0


def signed_in_account() -> str | None:
    accs = list_accounts()
    return accs[0]["username"] if accs else None


def _find_account(app, username: str | None):
    accounts = app.get_accounts()
    if not accounts:
        return None
    if username:
        for a in accounts:
            if a["username"].lower() == username.lower():
                return a
        return None
    return accounts[0]


def get_token_for(username: str | None = None) -> str | None:
    app, cache = _build_app()
    acc = _find_account(app, username)
    if acc is None:
        return None
    result = app.acquire_token_silent(SCOPES, account=acc)
    _save_cache(cache)
    if result and "access_token" in result:
        return result["access_token"]
    return None


def start_device_flow():
    """Initiate device code flow. Returns dict with user_code, verification_uri, message.
    If users are already signed in, the new login is added to the same cache."""
    global _pending_flow
    with _lock:
        app, cache = _build_app()
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Failed to start device flow: {flow}")
        _pending_flow = (app, cache, flow)
        return {
            "user_code": flow["user_code"],
            "verification_uri": flow["verification_uri"],
            "message": flow["message"],
            "expires_in": flow.get("expires_in", 900),
        }


def complete_device_flow():
    """Block until the user completes the device flow. Returns True on success."""
    global _pending_flow
    with _lock:
        if _pending_flow is None:
            return False
        app, cache, flow = _pending_flow
    result = app.acquire_token_by_device_flow(flow)
    _save_cache(cache)
    with _lock:
        _pending_flow = None
    return "access_token" in result


def sign_out(username: str | None = None):
    """Remove the given account, or all accounts if username is None."""
    try:
        app, cache = _build_app()
        accounts = app.get_accounts()
        if username:
            accounts = [a for a in accounts if a["username"].lower() == username.lower()]
        for a in accounts:
            app.remove_account(a)
        _save_cache(cache)
        # If no accounts remain, clear the cache row entirely
        if not app.get_accounts():
            _db.delete_setting(TOKEN_CACHE_KEY)
    except Exception:
        pass


def send_mail(
    to_email: str,
    subject: str,
    body_html: str,
    sender_name: str | None = None,
    sender_email: str | None = None,
    account: str | None = None,
):
    token = get_token_for(account)
    if not token:
        raise RuntimeError(
            f"Not signed in{' for ' + account if account else ''}. "
            "Connect this Microsoft account first."
        )
    message = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": body_html},
        "toRecipients": [{"emailAddress": {"address": to_email}}],
    }
    if sender_name or sender_email:
        from_addr = {}
        if sender_email:
            from_addr["address"] = sender_email
        if sender_name:
            from_addr["name"] = sender_name
        if from_addr:
            message["from"] = {"emailAddress": from_addr}
    payload = {"message": message, "saveToSentItems": True}
    url = f"{GRAPH_BASE}/me/sendMail"
    r = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=30,
    )
    if r.status_code not in (200, 202):
        raise RuntimeError(f"Graph error {r.status_code}: {r.text}")
    return True
