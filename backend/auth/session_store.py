# auth/session_store.py
import os, json, time
from typing import Optional, Dict
from cryptography.fernet import Fernet, InvalidToken

TOKEN_EXP_SKEW = 60             # refresh when <= 60s remaining
SESSION_TOKEN_TTL_SECONDS = int(os.getenv("SESSION_TOKEN_TTL_SECONDS", str(60 * 60 * 24 * 8)))
TRADING_TOKEN_TTL_SECONDS = int(os.getenv("TRADING_TOKEN_TTL_SECONDS", str(60 * 60 * 24 * 30)))
TRADING_TOKEN_KEY = os.getenv("TRADING_TOKEN_KEY", "trading:tokens")
AUTO_TRADER_KEY = os.getenv("AUTO_TRADER_KEY", "trading:auto_enabled")
ACTIVE_MARKET_SESSION_KEY = os.getenv("ACTIVE_MARKET_SESSION_KEY", "market:active_session")

_enc_key = os.getenv("REDIS_TOKEN_ENC_KEY")  # base64 urlsafe 32-byte
fernet = Fernet(_enc_key) if _enc_key else None

def _now() -> int: return int(time.time())

def _enc(obj: dict) -> str:
    raw = json.dumps(obj).encode()
    return fernet.encrypt(raw).decode() if fernet else raw.decode()

def _dec(s: str) -> dict:
    data = s.encode()
    if fernet:
        try:
            data = fernet.decrypt(data)
        except InvalidToken:
            # key rotated or tampered data
            return {}
    try:
        return json.loads(data.decode())
    except Exception:
        return {}

def save_tokens(r, sid: str, tokens: Dict) -> None:
    # Ensure expires_at exists
    expires_at = int(tokens.get("expires_at") or (_now() + int(tokens.get("expires_in", 1800))))
    tokens = {**tokens, "expires_at": expires_at}
    # Keep the refresh credential available after the 30-minute access token
    # expires. Schwab still enforces its own refresh-token lifetime.
    ttl = max(expires_at - _now(), SESSION_TOKEN_TTL_SECONDS)
    r.set(f"tok:{sid}", _enc(tokens), ex=ttl)

def load_tokens(r, sid: str) -> Optional[Dict]:
    raw = r.get(f"tok:{sid}")
    return _dec(raw) if raw else None

def save_trading_tokens(r, tokens: Dict) -> None:
    expires_at = int(tokens.get("expires_at") or (_now() + int(tokens.get("expires_in", 1800))))
    tokens = {**tokens, "expires_at": expires_at}
    r.set(TRADING_TOKEN_KEY, _enc(tokens), ex=TRADING_TOKEN_TTL_SECONDS)

def load_trading_tokens(r) -> Optional[Dict]:
    raw = r.get(TRADING_TOKEN_KEY)
    return _dec(raw) if raw else None

def delete_trading_tokens(r) -> None:
    r.delete(TRADING_TOKEN_KEY)

def set_auto_trader_enabled(r, enabled: bool) -> None:
    r.set(AUTO_TRADER_KEY, "1" if enabled else "0")

def get_auto_trader_enabled(r) -> bool:
    return r.get(AUTO_TRADER_KEY) == "1"

def set_active_market_session(r, sid: str, ttl_seconds: int) -> None:
    r.set(ACTIVE_MARKET_SESSION_KEY, sid, ex=ttl_seconds)

def load_active_market_session(r) -> Optional[str]:
    return r.get(ACTIVE_MARKET_SESSION_KEY)

def clear_active_market_session(r, sid: str | None = None) -> None:
    if sid is None or load_active_market_session(r) == sid:
        r.delete(ACTIVE_MARKET_SESSION_KEY)

def need_refresh(tok: Dict) -> bool:
    return _now() >= int(tok.get("expires_at", 0)) - TOKEN_EXP_SKEW
