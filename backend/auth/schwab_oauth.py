import os
import time
import json
import base64
from urllib.parse import urlencode
import requests

# ------------------------------------------------------------------
# Config: from environment (fallbacks provided) or your config module
# ------------------------------------------------------------------
SCHWAB_CLIENT_ID = os.getenv("SCHWAB_CLIENT_ID", "")
SCHWAB_CLIENT_SECRET = os.getenv("SCHWAB_CLIENT_SECRET", "")
SCHWAB_REDIRECT_URI = os.getenv("SCHWAB_REDIRECT_URI", "https://localhost/auth/callback")
SCHWAB_AUTH_URL = os.getenv("SCHWAB_AUTH_URL", "https://api.schwabapi.com/v1/oauth/authorize")
SCHWAB_TOKEN_URL = os.getenv("SCHWAB_TOKEN_URL", "https://api.schwabapi.com/v1/oauth/token")

TOKEN_CACHE_PATH = os.getenv("SCHWAB_TOKEN_CACHE", "cache/schwab_tokens.json")
TOKEN_DISK_CACHE_ENABLED = os.getenv(
    "SCHWAB_TOKEN_DISK_CACHE_ENABLED", "false"
).lower() == "true"

# Optional: default scope (adjust if your Schwab app uses scopes)
DEFAULT_SCOPE = os.getenv("SCHWAB_SCOPE", "")

# ---------------------------------------
# Public API used by your FastAPI router
# ---------------------------------------
def build_auth_url(state: str | None = None, scope: str | None = None, extra_params: dict | None = None) -> str:
    """
    Build the Schwab authorize URL for the web flow.
    Use this in /auth/login to redirect the browser.
    """
    params = {
        "response_type": "code",
        "client_id": SCHWAB_CLIENT_ID,
        "redirect_uri": SCHWAB_REDIRECT_URI,
    }
    sc = scope if scope is not None else DEFAULT_SCOPE
    if sc:
        params["scope"] = sc
    if state:
        params["state"] = state
    if extra_params:
        params.update(extra_params)
    return f"{SCHWAB_AUTH_URL}?{urlencode(params)}"


def exchange_code_for_tokens(code: str) -> dict:
    """
    Exchange authorization code -> tokens using client secret (server-side).
    Saves tokens to cache and returns the token dict.
    """
    headers = {
        "Authorization": f"Basic { _basic_auth_b64() }",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": SCHWAB_REDIRECT_URI,
    }
    resp = requests.post(SCHWAB_TOKEN_URL, headers=headers, data=data, timeout=20)
    resp.raise_for_status()
    tokens = resp.json()
    tokens["expires_at"] = int(time.time()) + int(tokens.get("expires_in", 1800))
    if TOKEN_DISK_CACHE_ENABLED:
        save_tokens_to_cache(tokens)
    return tokens


def refresh_access_token(refresh_token: str) -> dict:
    """
    Refresh access token using client secret.
    Saves tokens to cache and returns the token dict.
    """
    headers = {
        "Authorization": f"Basic { _basic_auth_b64() }",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    resp = requests.post(SCHWAB_TOKEN_URL, headers=headers, data=data, timeout=20)
    resp.raise_for_status()
    tokens = resp.json()
    tokens["expires_at"] = int(time.time()) + int(tokens.get("expires_in", 1800))
    if "refresh_token" not in tokens:
        tokens["refresh_token"] = refresh_token  # some providers omit it on refresh
    if TOKEN_DISK_CACHE_ENABLED:
        save_tokens_to_cache(tokens)
    return tokens


def load_tokens_from_cache(path: str = TOKEN_CACHE_PATH) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_tokens_to_cache(tokens: dict, path: str = TOKEN_CACHE_PATH) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)
    os.chmod(path, 0o600)


def get_valid_access_token() -> str:
    """
    Web-app behavior: NEVER launches a local HTTPS server.
    - If tokens are present and valid -> return access_token
    - If expired and refresh_token exists -> refresh and return
    - If no tokens -> raise (your /auth/login flow must be used)
    """
    if not TOKEN_DISK_CACHE_ENABLED:
        raise RuntimeError(
            "Legacy Schwab token disk cache is disabled; use the encrypted session store"
        )
    tokens = load_tokens_from_cache()
    if not tokens:
        raise RuntimeError("No cached tokens. User must authenticate via /auth/login.")

    now = int(time.time())
    if now >= int(tokens.get("expires_at", 0)):
        rt = tokens.get("refresh_token")
        if not rt:
            raise RuntimeError("Access token expired and no refresh_token available.")
        tokens = refresh_access_token(rt)

    return tokens["access_token"]

# ------------------------
# Internal helper(s)
# ------------------------
def _basic_auth_b64() -> str:
    return base64.b64encode(f"{SCHWAB_CLIENT_ID}:{SCHWAB_CLIENT_SECRET}".encode()).decode()
