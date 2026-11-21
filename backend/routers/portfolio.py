# app/routers/portfolio.py
import logging
import os, anyio
from typing import List, Optional
from datetime import datetime, timezone, timedelta, time as dtime

from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import JSONResponse

from routers.auth import require_access_token
from schwab.accounts import SchwabAccountClient
from schwab.user_preferences import UserPreferences
from schwab.transactions import SchwabTransactionClient
from schwab.market_data import SchwabMarketDataClient


router = APIRouter(prefix="/portfolio", tags=["portfolio"])
logger = logging.getLogger(__name__)

SCHWAB_ACCOUNT_NUMBER = os.getenv("SCHWAB_ACCOUNT_NUMBER")  # optional
DASHBOARD_FILLS_DAYS = int(os.getenv("DASHBOARD_FILLS_DAYS", "60"))
MARKET_OVERVIEW_SYMBOLS = (
    ("$SPX", "S&P 500"),
    ("$DJI", "Dow Jones"),
    ("$COMPX", "Nasdaq Composite"),
    ("$RUT", "Russell 2000"),
)

def _schwab_error_status(exc: Exception) -> int:
    status_code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    try:
        status_code = int(status_code)
    except (TypeError, ValueError):
        status_code = None
    if status_code in {401, 403, 429}:
        return status_code

    message = str(exc).lower()
    if "unauthorized" in message or "expired" in message or "invalid token" in message:
        return 401
    if "forbidden" in message:
        return 403
    if "rate limit" in message or "too many requests" in message:
        return 429
    return 502

def _schwab_error_payload(exc: Exception, resource: str) -> dict:
    return {
        "error": "schwab_api_error",
        "resource": resource,
        "message": str(exc) or f"Unable to load {resource}",
    }

def _num(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return float(default)

def _bucket_for_price(price: float) -> str:
    if price < 5:
        return "$0-5"
    if price < 10:
        return "$5-10"
    if price < 20:
        return "$10-20"
    if price < 50:
        return "$20-50"
    if price < 100:
        return "$50-100"
    return "$100+"

def _position_unrealized_pl(positions: List[dict]) -> float:
    total = 0.0
    for p in positions:
        qty = _num(p.get("longQuantity")) - _num(p.get("shortQuantity"))
        if not qty:
            continue
        total += (_num(p.get("marketPrice")) - _num(p.get("averagePrice"))) * qty
    return total

def _summarize_closed_trades(fills: list[dict], trade_limit: int = 20) -> tuple[list[dict], list[dict], float]:
    open_lots: dict[str, list[dict]] = {}
    recent: list[dict] = []
    buckets: dict[str, dict] = {}
    realized_pl = 0.0

    for fill in sorted(fills, key=lambda item: item.get("ts") or 0):
        symbol = fill.get("symbol") or "UNKNOWN"
        qty = abs(_num(fill.get("qty")))
        price = _num(fill.get("price"))
        if not qty or not price:
            continue
        side = (fill.get("side") or "").upper()
        if side == "BUY":
            open_lots.setdefault(symbol, []).append({"qty": qty, "price": price})
            continue
        if side != "SELL":
            continue

        remaining = qty
        while remaining > 1e-9 and open_lots.get(symbol):
            lot = open_lots[symbol][0]
            matched = min(remaining, lot["qty"])
            pl = (price - lot["price"]) * matched
            realized_pl += pl
            bucket = _bucket_for_price(lot["price"])
            entry = buckets.setdefault(bucket, {"bucket": bucket, "trades": 0, "wins": 0, "pl": 0.0})
            entry["trades"] += 1
            entry["wins"] += 1 if pl >= 0 else 0
            entry["pl"] += pl
            recent.append({
                "symbol": symbol,
                "qty": matched,
                "entry": lot["price"],
                "exit": price,
                "pl": pl,
                "bucket": bucket,
                "ts": fill.get("ts") or 0,
            })
            lot["qty"] -= matched
            remaining -= matched
            if lot["qty"] <= 1e-9:
                open_lots[symbol].pop(0)

    bucket_rows = []
    for item in buckets.values():
        trades = item["trades"] or 1
        bucket_rows.append({
            "bucket": item["bucket"],
            "trades": item["trades"],
            "win_rate": round(item["wins"] / trades * 100, 2),
            "pl": round(item["pl"], 2),
            "avg_pl": round(item["pl"] / trades, 2),
        })
    bucket_rows.sort(key=lambda item: item["bucket"])
    recent.sort(key=lambda item: item.get("ts") or 0, reverse=True)
    return bucket_rows, recent[:trade_limit], round(realized_pl, 2)

def _mk_user_prefs(access_token: str) -> UserPreferences:
    return UserPreferences(access_token)

def _mk_acct_client(access_token: str) -> SchwabAccountClient:
    return SchwabAccountClient(
        user_prefs=_mk_user_prefs(access_token), access_token=access_token
    )

def _mk_market_client(access_token: str) -> SchwabMarketDataClient:
    return SchwabMarketDataClient(
        user_prefs=_mk_user_prefs(access_token), access_token=access_token
    )

def _resolve_encrypted_account_id(acc_client: SchwabAccountClient) -> Optional[str]:
    rows = acc_client.get_account_numbers()
    if isinstance(rows, dict):
        rows = rows.get("accounts") or rows.get("securitiesAccounts") or rows.get("items") or []
    rows = rows or []

    best = None
    for row in rows:
        if best is None:
            best = row
        if SCHWAB_ACCOUNT_NUMBER and str(row.get("accountNumber")) == str(SCHWAB_ACCOUNT_NUMBER):
            best = row
            break
    if not best:
        return None
    return best.get("hashValue") or best.get("accountHash") or best.get("encryptedAccountId") or None

async def _get_account(access_token: str) -> dict:
    def _sync():
        client = _mk_acct_client(access_token)
        # If your client has refresh_summary(), call it to populate capabilities & chosen account
        if hasattr(client, "refresh_summary"):
            client.refresh_summary()
            sa = client.securities_account or {}
            caps = client.capabilities or {}
        else:
            # Fallback: just pick first account
            payload = client.get_all_accounts(include_positions=True) or []
            sa = (payload[0] or {}).get("securitiesAccount") if payload else {}
            caps = {}

        init = sa.get("initialBalances") or {}
        curr = sa.get("currentBalances") or {}
        positions = sa.get("positions") or []

        equity = _num(curr.get("equity") or init.get("equity") or init.get("liquidationValue"))
        cash   = _num(init.get("cashBalance") or init.get("totalCash"))
        day_pl = sum(_num(p.get("currentDayProfitLoss")) for p in positions)

        return {
            "equity": equity,
            "cash": cash,
            "day_pl": day_pl,
            "sa": sa,
            "capabilities": caps,
        }
    return await anyio.to_thread.run_sync(_sync)

async def _get_positions(access_token: str) -> List[dict]:
    client = _mk_acct_client(access_token)
    def _sync_call():
        return client.get_all_accounts(include_positions=True)
    accounts_list = await anyio.to_thread.run_sync(_sync_call)

    def _choose(sa_list):
        if SCHWAB_ACCOUNT_NUMBER:
            for row in sa_list:
                sa = (row or {}).get("securitiesAccount") or {}
                if str(sa.get("accountNumber")) == str(SCHWAB_ACCOUNT_NUMBER):
                    return sa
        return (sa_list[0] or {}).get("securitiesAccount")
    sa = _choose(accounts_list) or {}
    positions = sa.get("positions") or []

    out = []
    for p in positions:
        inst = p.get("instrument") or {}
        itype = (inst.get("type") or "").upper()
        if itype in {"SWEEP_VEHICLE", "CASH_EQUIVALENT"}:
            continue
        qty_long  = _num(p.get("longQuantity"))
        qty_short = _num(p.get("shortQuantity"))
        qty = qty_long if qty_long else -qty_short
        avg = _num(
            p.get("averageLongPrice") if qty >= 0 else p.get("averageShortPrice"),
            default=p.get("averagePrice") or 0.0
        )
        mkt_value = _num(p.get("marketValue"))
        last = (mkt_value / abs(qty)) if abs(qty) > 1e-6 else 0.0

        out.append({
            "symbol": inst.get("symbol") or inst.get("description") or "UNKNOWN",
            "longQuantity": qty if qty >= 0 else 0.0,
            "shortQuantity": -qty if qty < 0 else 0.0,
            "averagePrice": avg,
            "marketPrice": last,
            "marketValue": mkt_value,
            "currentDayProfitLoss": _num(p.get("currentDayProfitLoss")),
        })
    return out

# -------- transaction mapping helpers (for /accounts/{id}/transactions) --------

def _iso_utc(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S.000Z")

def _parse_iso_ts(s: Optional[str]) -> int:
    if not s: return 0
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0

def _is_security_item(item: dict) -> bool:
    itype = ((item.get("instrument") or {}).get("type") or "").upper()
    return itype not in {"SWEEP_VEHICLE", "CASH_EQUIVALENT", ""}

def _infer_side(position_effect: Optional[str]) -> str:
    pe = (position_effect or "").upper()
    if pe == "OPENING": return "BUY"
    if pe == "CLOSING": return "SELL"
    return "BUY"

def _map_tx_to_fills_list(tx: dict) -> list[dict]:
    items = tx.get("transferItems") or []
    sec_legs = [it for it in items if _is_security_item(it)]
    if not sec_legs:
        return []
    total_commission = 0.0
    for it in items:
        if (it.get("feeType") or "").upper() == "COMMISSION":
            total_commission += _num(it.get("amount"))
    total_commission = abs(total_commission)

    notionals = [abs(_num(l.get("amount")) * _num(l.get("price"))) for l in sec_legs]
    notional_sum = sum(notionals) or 1.0
    ts = _parse_iso_ts(tx.get("tradeDate")) or _parse_iso_ts(tx.get("time"))

    fills = []
    for leg, notional in zip(sec_legs, notionals):
        inst = leg.get("instrument") or {}
        symbol = inst.get("symbol") or inst.get("description") or "UNKNOWN"
        qty    = _num(leg.get("amount"))
        price  = _num(leg.get("price"))
        side   = _infer_side(leg.get("positionEffect"))
        commission = (total_commission * (notional / notional_sum)) if total_commission else 0.0
        if not symbol or qty == 0 or price == 0:
            continue
        fills.append({
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "price": price,
            "commission": commission,
            "ts": ts,
        })
    return fills

async def _get_fills(access_token: str, since_epoch: Optional[int] = None) -> list[dict]:
    up = _mk_user_prefs(access_token)
    tx_client = SchwabTransactionClient(user_prefs=up, access_token=access_token)
    acct_client = _mk_acct_client(access_token)

    def _sync_call():
        enc_id = _resolve_encrypted_account_id(acct_client)
        if not enc_id:
            return []
        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=DASHBOARD_FILLS_DAYS)
        if since_epoch:
            since_date = datetime.fromtimestamp(since_epoch, tz=timezone.utc).date()
            if since_date > start:
                start = since_date
        start_iso = _iso_utc(datetime.combine(start, dtime(0, 0)))
        end_iso   = _iso_utc(datetime.combine(today, dtime(23, 59, 59)))
        tx_raw = tx_client.get_transactions(
            account_id=enc_id,
            start_date=start_iso,
            end_date=end_iso,
            types="TRADE"
        )
        tx_list = tx_raw if isinstance(tx_raw, list) else (tx_raw.get("transactions") or tx_raw.get("items") or [])
        fills: list[dict] = []
        for tx in tx_list:
            if (tx.get("type") or "").upper() != "TRADE":
                continue
            fills.extend(_map_tx_to_fills_list(tx))
        return fills

    try:
        return await anyio.to_thread.run_sync(_sync_call)
    except Exception as e:
        status_code = getattr(getattr(e, "response", None), "status_code", None)
        logger.warning(
            "Schwab fills lookup failed; continuing with empty fills",
            extra={"status_code": status_code, "error": str(e)},
        )
        return []

async def _get_price_history(
    access_token: str,
    symbol: str,
    period_type: str,
    period: int,
    frequency_type: str,
    frequency: int,
    lookback_days: int | None = None,
) -> dict:
    mc = _mk_market_client(access_token)
    now = datetime.now(timezone.utc)
    end_date = int(now.timestamp() * 1000)

    def _sync_call():
        params = {
            "frequencyType": frequency_type,
            "frequency": frequency,
            "needExtendedHoursData": True,
            "endDate": end_date,
        }
        if lookback_days is not None:
            params["startDate"] = int((now - timedelta(days=lookback_days)).timestamp() * 1000)
        else:
            params["periodType"] = period_type
            params["period"] = period
        return mc.get_price_history(symbol, **params)

    return await anyio.to_thread.run_sync(_sync_call)

async def _get_market_overview(access_token: str) -> list[dict]:
    mc = _mk_market_client(access_token)

    def _sync_call():
        return mc.get_quotes([symbol for symbol, _ in MARKET_OVERVIEW_SYMBOLS], fields="quote")

    payload = await anyio.to_thread.run_sync(_sync_call)
    rows = []
    for symbol, name in MARKET_OVERVIEW_SYMBOLS:
        item = (payload or {}).get(symbol) or {}
        quote = item.get("quote") or {}
        last = _num(
            quote.get("lastPrice")
            or quote.get("mark")
            or quote.get("regularMarketLastPrice")
            or quote.get("closePrice")
        )
        close = _num(quote.get("closePrice"))
        change = _num(quote.get("netChange"), default=last - close if last and close else 0.0)
        percent_change = _num(quote.get("netPercentChange"))
        if not percent_change and close:
            percent_change = change / close * 100
        rows.append({
            "symbol": symbol,
            "name": name,
            "last": round(last, 4),
            "change": round(change, 4),
            "percent_change": round(percent_change, 4),
            "quote_time": quote.get("quoteTime") or quote.get("tradeTime"),
        })
    return rows

# ------------------------------- Public route -------------------------------

@router.get("/summary")
async def summary(
    request: Request,
    access_token: str = Depends(require_access_token),
    since: Optional[int] = Query(None, description="epoch seconds to limit fills window"),
    trade_limit: int = Query(20, ge=1, le=500, description="maximum closed trades returned in recent_trades"),
):
    try:
        acct = await _get_account(access_token)
        positions = await _get_positions(access_token)
    except Exception as e:
        logger.warning("Schwab portfolio summary lookup failed", exc_info=True)
        return JSONResponse(_schwab_error_payload(e, "portfolio_summary"), status_code=_schwab_error_status(e))

    fills = await _get_fills(access_token, since_epoch=since)
    price_buckets, recent_trades, realized_pl = _summarize_closed_trades(fills, trade_limit=trade_limit)
    unrealized_pl = _position_unrealized_pl(positions)

    sa = acct.get("sa") or {}
    payload = {
        "account": {
            "equity": acct["equity"],
            "cash": acct["cash"],
            "day_pl": acct["day_pl"],
            "unrealized_pl": round(unrealized_pl, 2),
            "realized_pl": realized_pl,
        },
        "account_meta": {
            "accountNumber": sa.get("accountNumber"),
            "isDayTrader": sa.get("isDayTrader"),
            "roundTrips": sa.get("roundTrips"),
            "isClosingOnlyRestricted": sa.get("isClosingOnlyRestricted"),
        },
        "capabilities": acct.get("capabilities") or {},
        "positions": positions,
        "fills": fills,  # raw fills list; your UI/logic can compute realized P/L, buckets, etc.
        "price_buckets": price_buckets,
        "recent_trades": recent_trades,
    }
    return JSONResponse(payload)


@router.get("/market_overview")
async def market_overview(
    access_token: str = Depends(require_access_token),
):
    try:
        rows = await _get_market_overview(access_token)
    except Exception as e:
        logger.warning("Schwab market overview lookup failed", exc_info=True)
        return JSONResponse(_schwab_error_payload(e, "market_overview"), status_code=_schwab_error_status(e))
    return JSONResponse({"indices": rows})


@router.get("/price_history")
async def price_history(
    request: Request,
    symbol: str,
    period_type: str = Query("day", description="day|month|year|ytd|ytd"),
    period: int = Query(10, description="number of periods"),
    frequency_type: str = Query("minute", description="minute|daily|weekly|monthly"),
    frequency: int = Query(5, description="interval within the period_type"),
    lookback_days: int | None = Query(None, ge=1, le=365),
    access_token: str = Depends(require_access_token),
):
    """
    Schwab pricehistory wrapper for charting.
    """
    try:
        res = await _get_price_history(
            access_token,
            symbol,
            period_type,
            period,
            frequency_type,
            frequency,
            lookback_days,
        )
    except Exception as e:
        logger.warning("Schwab price history lookup failed for %s", symbol, exc_info=True)
        return JSONResponse(_schwab_error_payload(e, "price_history"), status_code=_schwab_error_status(e))

    candles = []
    for c in res.get("candles", []):
        candles.append({
            "datetime": c.get("datetime"),
            "open": c.get("open"),
            "high": c.get("high"),
            "low": c.get("low"),
            "close": c.get("close"),
            "volume": c.get("volume"),
        })
    return JSONResponse({"symbol": symbol, "candles": candles})
