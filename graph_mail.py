import json
import logging
import os
import threading
import time
import msal
import requests

import db as _db

logger = logging.getLogger("nightscale.graph")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.Send"]
LEGACY_CACHE_KEY = "msal_token_cache"

_lock = threading.Lock()
_pending_flow = None  # (app, cache, flow)
# Tracks the most recent device-code flow so the UI can report why a sign-in
# failed instead of polling forever. Guarded by _lock.
_flow_state = {"status": "idle", "error": None, "username": None, "expires_at": 0.0}


# ---------------------------------------------------------------------------
# Per-account cache
# ---------------------------------------------------------------------------

def _migrate_legacy_cache_if_present():
    """If a legacy single multi-account cache exists, split it into one row
    per account and remove the legacy entry."""
    legacy = _db.get_setting(LEGACY_CACHE_KEY, "")
    if not legacy:
        return
    try:
        data = json.loads(legacy)
    except Exception:
        _db.delete_setting(LEGACY_CACHE_KEY)
        return
    accounts = data.get("Account", {}) or {}
    for acct_key, acct_info in accounts.items():
        username = (acct_info or {}).get("username")
        home_id = (acct_info or {}).get("home_account_id")
        if not username or not home_id:
            continue
        sub = {
            "Account": {acct_key: acct_info},
            "AccessToken": {
                k: v for k, v in (data.get("AccessToken") or {}).items()
                if home_id in k
            },
            "RefreshToken": {
                k: v for k, v in (data.get("RefreshToken") or {}).items()
                if home_id in k
            },
            "IdToken": {
                k: v for k, v in (data.get("IdToken") or {}).items()
                if home_id in k
            },
            "AppMetadata": data.get("AppMetadata", {}),
        }
        _db.upsert_ms_account(username, home_id, json.dumps(sub))
    _db.delete_setting(LEGACY_CACHE_KEY)


def _load_account_cache(username: str) -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    blob = ""
    try:
        blob = _db.get_ms_account_cache(username)
    except Exception:
        logger.exception("Could not load token cache for %s", username)
        blob = ""
    if blob:
        try:
            cache.deserialize(blob)
        except Exception:
            logger.exception("Corrupt token cache for %s; starting empty", username)
    return cache


def _save_account_cache(username: str, cache: msal.SerializableTokenCache, home_account_id: str | None = None):
    if not cache.has_state_changed:
        return
    try:
        hid = home_account_id
        if not hid:
            data = json.loads(cache.serialize() or "{}")
            for v in (data.get("Account") or {}).values():
                if v.get("username", "").lower() == username.lower():
                    hid = v.get("home_account_id")
                    break
        _db.upsert_ms_account(username, hid or "", cache.serialize())
    except Exception:
        logger.exception("Could not persist token cache for %s", username)


def _build_app_with_cache(cache: msal.SerializableTokenCache):
    client_id = os.environ.get("AZURE_CLIENT_ID")
    tenant_id = os.environ.get("AZURE_TENANT_ID")
    if not client_id or not tenant_id:
        raise RuntimeError("AZURE_CLIENT_ID and AZURE_TENANT_ID must be set")
    return msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        token_cache=cache,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_accounts() -> list:
    """All connected accounts. Backed by the ms_accounts DB table; does not
    talk to Microsoft and never modifies any cache."""
    try:
        _migrate_legacy_cache_if_present()
        return _db.list_ms_accounts()
    except Exception:
        return []


def is_signed_in() -> bool:
    return len(list_accounts()) > 0


def signed_in_account() -> str | None:
    accs = list_accounts()
    return accs[0]["username"] if accs else None


def get_token_for(username: str | None = None) -> str | None:
    accounts = list_accounts()
    if not accounts:
        return None
    target = username or accounts[0]["username"]
    cache = _load_account_cache(target)
    app = _build_app_with_cache(cache)
    msal_accounts = app.get_accounts()
    if not msal_accounts:
        return None
    result = app.acquire_token_silent(SCOPES, account=msal_accounts[0])
    _save_account_cache(target, cache, msal_accounts[0].get("home_account_id"))
    if result and "access_token" in result:
        return result["access_token"]
    return None


def get_flow_state() -> dict:
    with _lock:
        state = dict(_flow_state)
    state["expired"] = bool(
        state["status"] == "pending" and state["expires_at"] and time.time() > state["expires_at"]
    )
    return state


def start_device_flow():
    """Begin a device-code flow with a FRESH empty cache so the new account
    is added independently of any existing accounts. If a flow is already
    pending and not expired, return it again instead of starting a new one —
    this dedupes double-clicks and stops /auth/start spam."""
    global _pending_flow
    with _lock:
        if (
            _pending_flow is not None
            and _flow_state["status"] == "pending"
            and time.time() < _flow_state["expires_at"]
        ):
            _, _, flow = _pending_flow
            return {
                "user_code": flow["user_code"],
                "verification_uri": flow["verification_uri"],
                "message": flow["message"],
                "expires_in": max(1, int(_flow_state["expires_at"] - time.time())),
            }
        cache = msal.SerializableTokenCache()
        app = _build_app_with_cache(cache)
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            logger.error("Device flow start failed: %s", flow)
            raise RuntimeError(f"Failed to start device flow: {flow}")
        expires_in = int(flow.get("expires_in", 900))
        _pending_flow = (app, cache, flow)
        _flow_state.update(
            status="pending", error=None, username=None,
            expires_at=time.time() + expires_in,
        )
        return {
            "user_code": flow["user_code"],
            "verification_uri": flow["verification_uri"],
            "message": flow["message"],
            "expires_in": expires_in,
        }


def complete_device_flow():
    global _pending_flow
    with _lock:
        if _pending_flow is None:
            return False
        app, cache, flow = _pending_flow
    try:
        result = app.acquire_token_by_device_flow(flow)
    except Exception as e:
        logger.exception("Device flow blew up")
        result = {"error": "exception", "error_description": str(e)}
    success = "access_token" in result
    username = None
    if success:
        msal_accounts = app.get_accounts()
        if msal_accounts:
            new_acc = msal_accounts[0]
            username = new_acc["username"]
            home_id = new_acc.get("home_account_id", "")
            try:
                _db.upsert_ms_account(username, home_id, cache.serialize())
            except Exception:
                logger.exception("Could not persist new account %s after sign-in", username)
        logger.info("Device flow completed for %s", username)
    else:
        logger.warning(
            "Device flow failed: %s — %s",
            result.get("error"), result.get("error_description"),
        )
    with _lock:
        _pending_flow = None
        if success:
            _flow_state.update(status="success", error=None, username=username, expires_at=0.0)
        else:
            _flow_state.update(
                status="error",
                error=result.get("error_description") or result.get("error") or "Sign-in failed.",
                username=None,
                expires_at=0.0,
            )
    return success


def sign_out(username: str | None = None):
    """Disconnect a specific account, or every account when username is None."""
    try:
        if username:
            _db.delete_ms_account(username)
        else:
            _db.delete_all_ms_accounts()
    except Exception:
        logger.exception("Sign-out failed for %s", username or "<all accounts>")


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
