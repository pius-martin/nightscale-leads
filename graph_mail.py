import os
import atexit
import threading
import msal
import requests

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.Send"]
CACHE_PATH = os.path.join(os.path.dirname(__file__), "token_cache.bin")

_lock = threading.Lock()
_pending_flow = None
_app_cache = None  # (app, cache, client_id, tenant_id)


def _load_cache():
    cache = msal.SerializableTokenCache()
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, "r") as f:
            cache.deserialize(f.read())
    atexit.register(lambda: _save_cache(cache))
    return cache


def _save_cache(cache):
    if cache.has_state_changed:
        with open(CACHE_PATH, "w") as f:
            f.write(cache.serialize())


def _build_app():
    global _app_cache
    client_id = os.environ.get("AZURE_CLIENT_ID")
    tenant_id = os.environ.get("AZURE_TENANT_ID")
    if not client_id or not tenant_id:
        raise RuntimeError("AZURE_CLIENT_ID and AZURE_TENANT_ID must be set in .env")
    if _app_cache and _app_cache[2] == client_id and _app_cache[3] == tenant_id:
        return _app_cache[0], _app_cache[1]
    cache = _load_cache()
    app = msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        token_cache=cache,
    )
    _app_cache = (app, cache, client_id, tenant_id)
    return app, cache


def get_token_silent():
    app, cache = _build_app()
    accounts = app.get_accounts()
    if not accounts:
        return None
    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    _save_cache(cache)
    if result and "access_token" in result:
        return result["access_token"]
    return None


def start_device_flow():
    """Initiate device code flow. Returns dict with user_code, verification_uri, message."""
    global _pending_flow
    with _lock:
        app, _ = _build_app()
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Failed to start device flow: {flow}")
        _pending_flow = (app, flow)
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
        app, flow = _pending_flow
    _, cache = _build_app()
    result = app.acquire_token_by_device_flow(flow)
    _save_cache(cache)
    with _lock:
        _pending_flow = None
    return "access_token" in result


def is_signed_in():
    try:
        return get_token_silent() is not None
    except Exception:
        return False


def signed_in_account():
    try:
        app, _ = _build_app()
        accounts = app.get_accounts()
        return accounts[0]["username"] if accounts else None
    except Exception:
        return None


def sign_out():
    app, cache = _build_app()
    for acc in app.get_accounts():
        app.remove_account(acc)
    _save_cache(cache)
    if os.path.exists(CACHE_PATH):
        try:
            os.remove(CACHE_PATH)
        except OSError:
            pass


def send_mail(
    to_email: str,
    subject: str,
    body_html: str,
    sender_name: str | None = None,
    sender_email: str | None = None,
):
    token = get_token_silent()
    if not token:
        raise RuntimeError("Not signed in. Start device flow first.")
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
