# app/routers/auth.py
import logging
import os
import time
import json
import secrets
import redis
from urllib.parse import urljoin, unquote

from fastapi import APIRouter, Body, Request, HTTPException, Query
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse

from auth.schwab_oauth import (
    build_auth_url,
    exchange_code_for_tokens,
    refresh_access_token,
)
from auth.session_store import (
    clear_active_market_session,
    delete_trading_tokens,
    get_auto_trader_enabled,
    load_active_market_session,
    load_tokens,
    load_trading_tokens,
    need_refresh,
    save_tokens,
    save_trading_tokens,
    set_active_market_session,
    set_auto_trader_enabled,
)

router = APIRouter(tags=["auth"])
logger = logging.getLogger(__name__)

# --- Config ---
r = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000/")
DEFAULT_AFTER_LOGIN_PATH = os.getenv("DEFAULT_AFTER_LOGIN_PATH", "/dashboard")
SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "scanner_sess")
CSRF_COOKIE_NAME = os.getenv("CSRF_COOKIE_NAME", "csrf")
CSRF_HEADER_NAME = os.getenv("CSRF_HEADER_NAME", "X-CSRF-Token")
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true"
SESSION_COOKIE_DOMAIN = os.getenv("SESSION_COOKIE_DOMAIN") or None
SESSION_COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE", "None").capitalize()  # "None" for cross-origin dev
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", str(60 * 60 * 24 * 30)))
SCHWAB_REFRESH_TOKEN_LIFETIME_SECONDS = int(os.getenv("SCHWAB_REFRESH_TOKEN_LIFETIME_SECONDS", str(60 * 60 * 24 * 7)))
SCHWAB_REAUTH_WARNING_SECONDS = int(os.getenv("SCHWAB_REAUTH_WARNING_SECONDS", str(60 * 60 * 24)))

if SESSION_COOKIE_SAMESITE not in {"Lax", "Strict", "None"}:
    raise RuntimeError("SESSION_COOKIE_SAMESITE must be one of: Lax, Strict, None")
if SESSION_COOKIE_SAMESITE == "None" and not SESSION_COOKIE_SECURE:
    raise RuntimeError("SESSION_COOKIE_SECURE must be true when SESSION_COOKIE_SAMESITE=None")

def _now() -> int: return int(time.time())
def _sid_key(sid: str) -> str: return f"sess:{sid}"
def _tok_key(sid: str) -> str: return f"tok:{sid}"
def _oauth_state_key(state: str) -> str: return f"oauth:state:{state}"
def _csrf_key(sid: str) -> str: return f"csrf:{sid}"
def _refresh_lock_key(sid: str) -> str: return f"lock:token_refresh:{sid}"
def _trading_refresh_lock_key() -> str: return "lock:token_refresh:trading"

def _sanitize_next(next_path: str | None) -> str:
    """Allow only same-site relative paths; default to root."""
    if not next_path:
        return "/"
    if next_path.startswith("/") and not next_path.startswith("//"):
        return next_path
    return "/"

def _new_session(resp: RedirectResponse) -> str:
    sid = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    r.set(_sid_key(sid), json.dumps({"created_at": _now()}), ex=SESSION_TTL_SECONDS)
    r.set(_csrf_key(sid), csrf, ex=SESSION_TTL_SECONDS)
    resp.set_cookie(
        SESSION_COOKIE_NAME,
        sid,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite=SESSION_COOKIE_SAMESITE,
        domain=SESSION_COOKIE_DOMAIN,
        path="/",
        max_age=SESSION_TTL_SECONDS,
    )
    resp.set_cookie(
        CSRF_COOKIE_NAME,
        csrf,
        httponly=False,
        secure=SESSION_COOKIE_SECURE,
        samesite=SESSION_COOKIE_SAMESITE,
        domain=SESSION_COOKIE_DOMAIN,
        path="/",
        max_age=SESSION_TTL_SECONDS,
    )
    return sid

def touch_session(request: Request, response) -> None:
    """Slide an authenticated browser session without changing Schwab consent."""
    if request.url.path in {"/auth/logout", "/auth/disconnect"}:
        return
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if not sid or not r.get(_sid_key(sid)) or not r.get(_tok_key(sid)):
        return
    csrf = r.get(_csrf_key(sid))
    if not csrf:
        csrf = secrets.token_urlsafe(32)
    r.expire(_sid_key(sid), SESSION_TTL_SECONDS)
    r.set(_csrf_key(sid), csrf, ex=SESSION_TTL_SECONDS)
    set_active_market_session(r, sid, SESSION_TTL_SECONDS)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        sid,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite=SESSION_COOKIE_SAMESITE,
        domain=SESSION_COOKIE_DOMAIN,
        path="/",
        max_age=SESSION_TTL_SECONDS,
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf,
        httponly=False,
        secure=SESSION_COOKIE_SECURE,
        samesite=SESSION_COOKIE_SAMESITE,
        domain=SESSION_COOKIE_DOMAIN,
        path="/",
        max_age=SESSION_TTL_SECONDS,
    )

def _get_or_create_sid(resp: RedirectResponse, request: Request) -> str:
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if sid and r.get(_sid_key(sid)):
        r.expire(_sid_key(sid), SESSION_TTL_SECONDS)  # slide expiration
        r.expire(_csrf_key(sid), SESSION_TTL_SECONDS)
        return sid
    return _new_session(resp)

def _persist_tokens_for_session(sid: str, tokens: dict):
    save_tokens(r, sid, tokens)
    set_active_market_session(r, sid, SESSION_TTL_SECONDS)
    if get_auto_trader_enabled(r):
        save_trading_tokens(r, tokens)

def _session_tokens(request: Request) -> tuple[str, dict]:
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if not sid or not r.get(_sid_key(sid)):
        raise HTTPException(status_code=401, detail="No session")
    tok = load_tokens(r, sid)
    if not tok:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return sid, tok

def _refresh_session_tokens(sid: str, tok: dict) -> tuple[dict, bool]:
    """Refresh one UI session safely when concurrent requests reach expiry."""
    if not need_refresh(tok):
        return tok, False

    with r.lock(_refresh_lock_key(sid), timeout=20, blocking_timeout=10):
        latest = load_tokens(r, sid)
        if latest and not need_refresh(latest):
            return latest, False

        current = latest or tok
        refresh_token = current.get("refresh_token")
        if not refresh_token:
            raise RuntimeError("Session refresh token is missing")

        refreshed = refresh_access_token(refresh_token)
        refreshed["expires_at"] = int(
            refreshed.get("expires_at") or (_now() + int(refreshed.get("expires_in", 1800)))
        )
        refreshed["authorized_at"] = int(current.get("authorized_at") or _now())
        refreshed["refresh_expires_at"] = int(
            current.get("refresh_expires_at")
            or (refreshed["authorized_at"] + SCHWAB_REFRESH_TOKEN_LIFETIME_SECONDS)
        )
        _persist_tokens_for_session(sid, refreshed)
        return refreshed, True

def _finish_oauth(request: Request, code: str | None, state: str | None):
    # Unified code path used by / and /auth/callback
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    raw = r.get(_oauth_state_key(state))
    if not raw:
        raise HTTPException(status_code=400, detail="Invalid/expired state")
    r.delete(_oauth_state_key(state))
    stored_next = json.loads(raw).get("next")
    next_path = _sanitize_next(stored_next) if stored_next else DEFAULT_AFTER_LOGIN_PATH

    # Schwab note: ensure code is URL-decoded
    code = unquote(code)

    try:
        tokens = exchange_code_for_tokens(code)
    except Exception as e:
        logger.exception("Schwab token exchange failed")
        raise HTTPException(status_code=502, detail=f"Token exchange failed: {e}")

    tokens["authorized_at"] = _now()
    tokens["refresh_expires_at"] = tokens["authorized_at"] + int(
        tokens.get("refresh_token_expires_in") or SCHWAB_REFRESH_TOKEN_LIFETIME_SECONDS
    )

    # Redirect back to frontend with a server-side session
    resp = RedirectResponse(urljoin(FRONTEND_URL, next_path), status_code=302)
    sid = _get_or_create_sid(resp, request)
    _persist_tokens_for_session(sid, tokens)
    return resp

# -----------------------
# Routes
# -----------------------

@router.get("/auth/login")
def auth_login(next: str | None = None):
    """Redirect to Schwab LMS with a CSRF state carrying the 'next' path."""
    state = secrets.token_urlsafe(24)
    target_next = _sanitize_next(next) if next else DEFAULT_AFTER_LOGIN_PATH
    r.setex(_oauth_state_key(state), 600, json.dumps({"next": target_next}))
    return RedirectResponse(build_auth_url(state=state), status_code=302)

@router.get("/auth/callback")
def auth_callback(request: Request, code: str | None = None, state: str | None = None):
    # Your Schwab portal currently has redirect_uri=https://127.0.0.1/auth/callback
    return _finish_oauth(request, code, state)

@router.get("/")  # Optional: only used if redirect_uri is host root (https://127.0.0.1)
def root_callback(
    request: Request,
    code: str | None = Query(None),
    state: str | None = Query(None),
    session: str | None = Query(None),  # Schwab may include this; unused here
):
    if code:
        return _finish_oauth(request, code, state)
    return {"ok": True}

@router.get("/auth/status")
def auth_status(request: Request):
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    tok = load_tokens(r, sid) if sid and r.get(_sid_key(sid)) else None
    if not tok:
        return {"authenticated": False}
    try:
        tok, refreshed = _refresh_session_tokens(sid, tok)
    except Exception:
        logger.warning("Schwab session refresh failed during auth status", exc_info=True)
        if sid:
            clear_active_market_session(r, sid)
        return {"authenticated": False, "refresh_failed": True}
    if not tok.get("refresh_expires_at"):
        tok["authorized_at"] = int(tok.get("authorized_at") or _now())
        tok["refresh_expires_at"] = tok["authorized_at"] + SCHWAB_REFRESH_TOKEN_LIFETIME_SECONDS
        _persist_tokens_for_session(sid, tok)
    secs = int(tok.get("expires_at", 0)) - _now()
    refresh_secs = int(tok["refresh_expires_at"]) - _now()
    if secs > 0 and sid:
        set_active_market_session(r, sid, SESSION_TTL_SECONDS)
    return {
        "authenticated": secs > 0,
        "expires_at": tok.get("expires_at"),
        "seconds_to_expiry": max(0, secs),
        "refresh_expires_at": tok["refresh_expires_at"],
        "seconds_to_reauthorization": max(0, refresh_secs),
        "reauthorization_required": refresh_secs <= 0,
        "reauthorization_soon": refresh_secs <= SCHWAB_REAUTH_WARNING_SECONDS,
        "refreshed": refreshed,
    }

@router.get("/auth/trading")
def trading_status(request: Request):
    _session_tokens(request)
    trading_tokens = load_trading_tokens(r)
    enabled = get_auto_trader_enabled(r)
    if enabled and not trading_tokens:
        set_auto_trader_enabled(r, False)
        enabled = False
    return {
        "auto_trader_enabled": enabled,
        "has_trading_credentials": bool(trading_tokens),
    }

@router.put("/auth/trading")
def update_trading_status(request: Request, body: dict = Body(...)):
    _, tok = _session_tokens(request)
    enabled = bool(body.get("auto_trader_enabled"))
    set_auto_trader_enabled(r, enabled)
    if enabled:
        save_trading_tokens(r, tok)
    else:
        delete_trading_tokens(r)
    return {
        "auto_trader_enabled": enabled,
        "has_trading_credentials": enabled,
    }

@router.post("/auth/logout")
def auth_logout(request: Request):
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    auto_trader_enabled = get_auto_trader_enabled(r)
    if sid:
        r.delete(_tok_key(sid))
        r.delete(_sid_key(sid))
        r.delete(_csrf_key(sid))
        clear_active_market_session(r, sid)
    if not auto_trader_enabled:
        delete_trading_tokens(r)
    out = JSONResponse({
        "ok": True,
        "auto_trader_enabled": auto_trader_enabled,
        "trading_credentials_retained": auto_trader_enabled,
    })
    out.delete_cookie(SESSION_COOKIE_NAME, domain=SESSION_COOKIE_DOMAIN, path="/")
    out.delete_cookie(CSRF_COOKIE_NAME, domain=SESSION_COOKIE_DOMAIN, path="/")
    return out

@router.post("/auth/disconnect")
def auth_disconnect(request: Request):
    """Remove all app-held Schwab credentials. This does not call a provider revocation API."""
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if sid:
        r.delete(_tok_key(sid))
        r.delete(_sid_key(sid))
        r.delete(_csrf_key(sid))
        clear_active_market_session(r, sid)
    set_auto_trader_enabled(r, False)
    delete_trading_tokens(r)
    out = JSONResponse({
        "ok": True,
        "auto_trader_enabled": False,
        "trading_credentials_retained": False,
        "provider_access_revoked": False,
    })
    out.delete_cookie(SESSION_COOKIE_NAME, domain=SESSION_COOKIE_DOMAIN, path="/")
    out.delete_cookie(CSRF_COOKIE_NAME, domain=SESSION_COOKIE_DOMAIN, path="/")
    return out

# -----------------------
# Dependency for other routers
# -----------------------
def require_access_token(request: Request) -> str:
    """Use inside endpoints that need a valid Access Token. Auto-refresh if needed."""
    sid, tok = _session_tokens(request)

    try:
        tok, _ = _refresh_session_tokens(sid, tok)
    except Exception:
        logger.warning("Schwab session refresh failed for protected request", exc_info=True)
        clear_active_market_session(r, sid)
        raise HTTPException(status_code=401, detail="Session refresh failed") from None

    return tok["access_token"]

def get_trading_access_token() -> str:
    """Use from background trading workers that must run without a UI session."""
    tok = load_trading_tokens(r)
    if not tok or not get_auto_trader_enabled(r):
        raise RuntimeError("Auto trader is off or trading credentials are missing")

    if need_refresh(tok):
        with r.lock(_trading_refresh_lock_key(), timeout=20, blocking_timeout=10):
            latest = load_trading_tokens(r)
            if not latest or not get_auto_trader_enabled(r):
                raise RuntimeError("Auto trader is off or trading credentials are missing")
            if not need_refresh(latest):
                tok = latest
            else:
                now = _now()
                rt = latest.get("refresh_token")
                if not rt:
                    raise RuntimeError("Trading refresh token is missing")
                refresh_expires_at = int(
                    latest.get("refresh_expires_at")
                    or (
                        int(latest.get("authorized_at") or now)
                        + SCHWAB_REFRESH_TOKEN_LIFETIME_SECONDS
                    )
                )
                if refresh_expires_at <= now:
                    raise RuntimeError("Schwab reauthorization is required")
                new_tok = refresh_access_token(rt)
                new_tok["expires_at"] = int(
                    new_tok.get("expires_at")
                    or (now + int(new_tok.get("expires_in", 1800)))
                )
                new_tok["authorized_at"] = int(latest.get("authorized_at") or now)
                new_tok["refresh_expires_at"] = refresh_expires_at
                save_trading_tokens(r, new_tok)

                # Keep an active UI session on the same refreshed credential. This
                # matters if Schwab rotates the refresh token in its response.
                sid = load_active_market_session(r)
                session_tok = load_tokens(r, sid) if sid else None
                if sid and session_tok and (
                    session_tok.get("refresh_token") == rt
                    or session_tok.get("authorized_at") == new_tok["authorized_at"]
                ):
                    save_tokens(r, sid, new_tok)
                tok = new_tok

    return tok["access_token"]


def get_trading_authorization_status() -> dict:
    """Return local credential readiness without making a Schwab network call."""
    enabled = get_auto_trader_enabled(r)
    tok = load_trading_tokens(r) if enabled else None
    now = _now()
    refresh_expires_at = int((tok or {}).get("refresh_expires_at") or 0)
    return {
        "auto_trader_enabled": enabled,
        "has_trading_credentials": bool(tok),
        "has_refresh_token": bool((tok or {}).get("refresh_token")),
        "access_expires_at": int((tok or {}).get("expires_at") or 0),
        "refresh_expires_at": refresh_expires_at,
        "seconds_to_reauthorization": max(0, refresh_expires_at - now),
        "reauthorization_required": not tok or refresh_expires_at <= now,
        "reauthorization_soon": bool(tok) and refresh_expires_at - now <= SCHWAB_REAUTH_WARNING_SECONDS,
    }

def get_market_data_access_token() -> str:
    """Return a refreshable token for background Schwab market-data workers.

    Prefer the active browser session. When Auto Trader is enabled, its isolated
    credential keeps the scanner pipeline alive after the browser logs out.
    """
    sid = load_active_market_session(r)
    if not sid:
        if get_auto_trader_enabled(r):
            return get_trading_access_token()
        raise RuntimeError("No authenticated Schwab session is available for market data")

    tok = load_tokens(r, sid)
    if not tok:
        clear_active_market_session(r, sid)
        if get_auto_trader_enabled(r):
            return get_trading_access_token()
        raise RuntimeError("The Schwab market-data session has expired")

    try:
        tok, _ = _refresh_session_tokens(sid, tok)
    except Exception:
        clear_active_market_session(r, sid)
        if get_auto_trader_enabled(r):
            return get_trading_access_token()
        raise RuntimeError("The Schwab market-data session could not be refreshed") from None

    set_active_market_session(r, sid, SESSION_TTL_SECONDS)
    return tok["access_token"]

def require_csrf(request: Request):
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if not sid:
        return
    expected = r.get(_csrf_key(sid))
    if not expected:
        raise HTTPException(status_code=403, detail="Missing CSRF token")
    supplied = request.headers.get(CSRF_HEADER_NAME)
    cookie_value = request.cookies.get(CSRF_COOKIE_NAME)
    if not supplied or not cookie_value or supplied != expected or cookie_value != expected:
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
