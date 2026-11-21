"""Provider-reported float with bounded, source-preserving fallback."""
import json
import math
import os
import time


def share_count(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return int(number) if math.isfinite(number) and number > 0 and number.is_integer() else None
    except (ValueError, TypeError, OverflowError):
        return None


def _usable(record, symbol, outstanding, now, max_age):
    try:
        observed = float(record["observed_ts"])
        value = share_count(record.get("value"))
        previous_outstanding = share_count(record.get("shares_outstanding"))
        return (record.get("symbol") == symbol and value is not None
                and record.get("source") == "yfinance.floatShares"
                and 0 <= now - observed <= max_age
                and (not outstanding or value <= outstanding)
                and (not outstanding or not previous_outstanding or outstanding == previous_outstanding))
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def resolve_float(symbol, primary, outstanding, redis_client, *, now=None):
    now = time.time() if now is None else now
    symbol = symbol.upper()
    outstanding = share_count(outstanding)
    primary = share_count(primary)
    max_age = max(0, min(7 * 86400, int(os.getenv("SCANNER_FLOAT_MAX_CACHE_SECONDS", "86400"))))
    cache_key = f"scanner:float:last_good:{symbol}"
    record, status, fallback_reason = None, "unknown", None
    if primary and (not outstanding or primary <= outstanding):
        record = {"symbol": symbol, "value": primary, "source": "yfinance.floatShares",
                  "observed_ts": now, "as_of": None, "shares_outstanding": outstanding}
        status = "reported"
    else:
        try:
            cached = json.loads(redis_client.get(cache_key) or "null")
        except Exception:
            cached = None
        if isinstance(cached, dict) and _usable(cached, symbol, outstanding, now, max_age):
            record, status = cached, "cached"
        else:
            fallback_reason = "no_usable_cached_float"
    if record and status != "cached":
        try:
            redis_client.set(cache_key, json.dumps(record), ex=30 * 86400)
        except Exception:
            pass
    return {
        "float_shares": record["value"] if record else None,
        "float_source": record["source"] if record else None,
        "float_status": status,
        "float_observed_ts": record["observed_ts"] if record else None,
        "float_as_of": record.get("as_of") if record else None,
        "float_fallback_reason": fallback_reason,
        "float_basis": "provider_reported" if record else None,
    }
