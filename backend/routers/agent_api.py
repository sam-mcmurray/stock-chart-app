import os
import time
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pandas as pd
import redis
from fastapi import APIRouter, Query, HTTPException, Depends, Header
from fastapi.responses import JSONResponse

from drl_agent.environment import StockTradingEnv
from drl_agent.committee import TradingCommittee
from drl_agent.risk import evaluate_stop_loss
from .scanners_api import _load_agent, _Memory
from schwab.orders import SchwabOrderClient
from schwab.user_preferences import UserPreferences
from schwab.accounts import SchwabAccountClient
from schwab.market_data import SchwabMarketDataClient
from routers.auth import get_trading_authorization_status, require_access_token
from services import persistent_store
from services.agent_price_history import validated_agent_daily_frame
from services.feature_store import list_feature_snapshots, record_feature_snapshot
from services.market_signal import market_signal
from services.semantic_signal import semantic_signal
from services.decision_engine import DecisionConfig, news_sentiment, trade_plan
from services.trade_risk import RiskLimits, calculate_long_position_size
from services.account_risk_ledger import get_account_risk_summary
from services.entry_market_gate import (
    EntryMarketLimits,
    evaluate_entry_market_gate,
    normalize_schwab_equity_hours,
    normalize_schwab_equity_quote,
)
from services.market_stream_store import load_market_stream_snapshot
from services.order_flow_confirmation import (
    OrderFlowLimits,
    evaluate_order_flow_confirmation,
)
from services.local_exit_readiness import evaluate_local_exit_readiness
from services.trade_journal import (
    IdempotencyConflict,
    ensure_entry_position,
    list_order_intents,
    reserve_order_intent,
    update_order_intent,
)
from workers.stop_loss_worker import H_STOP_LOSS_HEARTBEAT, K_STOP_LOSS_EVENTS
from workers.scanner_worker import _current_session_rvol, _get_daily, _schwab_market_client, _macd_open, _recent_articles

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
r = redis.from_url(REDIS_URL, decode_responses=True)
ACTIONS_KEY = os.getenv("AGENT_ACTIONS_KEY", "agent:actions")
ACTIONS_MAX_LEN = int(os.getenv("AGENT_ACTIONS_MAX_LEN", "200"))
EXECUTION_LOG_KEY = os.getenv("AGENT_EXECUTION_LOG_KEY", "agent:exec_log")
ACCOUNT_STREAM_HEARTBEAT_KEY = os.getenv(
    "SCHWAB_ACCOUNT_STREAM_HEARTBEAT_KEY",
    "schwab:account_stream:heartbeat",
)
LEVELONE_STREAM_KEY = os.getenv("SCHWAB_LEVELONE_HASH_KEY", "schwab:stream:levelone")
NASDAQ_BOOK_STREAM_KEY = os.getenv("SCHWAB_NASDAQ_BOOK_HASH_KEY", "schwab:stream:nasdaq_book")
NYSE_BOOK_STREAM_KEY = os.getenv("SCHWAB_NYSE_BOOK_HASH_KEY", "schwab:stream:nyse_book")
CHART_STREAM_KEY_PREFIX = os.getenv("SCHWAB_CHART_KEY_PREFIX", "schwab:stream:chart:")
PROTECTIVE_STOP_HEARTBEAT_KEY = os.getenv(
    "PROTECTIVE_STOP_HEARTBEAT_KEY",
    "agent:protective_stop:heartbeat",
)
EXIT_CONTROLLER_HEARTBEAT_KEY = os.getenv(
    "AGENT_EXIT_HEARTBEAT_KEY",
    "agent:exit_controller:heartbeat",
)
EXIT_CONTROLLER_BACKUP_HEARTBEAT_KEY = os.getenv(
    "AGENT_EXIT_BACKUP_HEARTBEAT_KEY",
    "agent:exit_controller_backup:heartbeat",
)
EXIT_CONTROLLER_EVENTS_KEY = os.getenv(
    "AGENT_EXIT_EVENTS_KEY",
    "agent:exit_controller:events",
)
COMMITTEE_ACTIONS = {"buy": 0, "sell": 1, "hold": 2}

router = APIRouter(prefix="/agent", tags=["agent"])


def _attach_gdelt_mentions(articles: list[dict]) -> list[dict]:
    def _article_event_ids(article: dict) -> list:
        values = article.get("event_ids") or []
        if not isinstance(values, list):
            values = [values]
        return values + ([article.get("event_id")] if article.get("event_id") else [])

    event_ids = sorted({
        str(event_id)
        for article in articles
        for event_id in _article_event_ids(article)
        if str(event_id).isdigit()
    })
    if not event_ids:
        return articles
    try:
        mentions = persistent_store.list_gdelt_mentions_for_events(event_ids)
    except Exception:
        return articles
    by_event: dict[str, list[dict]] = {}
    for mention in mentions:
        by_event.setdefault(str(mention.get("event_id") or ""), []).append(mention)
    output = []
    for article in articles:
        event_id_set = {
            str(event_id)
            for event_id in _article_event_ids(article)
        }
        output.append({
            **article,
            "gdelt_mentions": [
                mention
                for event_id in event_id_set
                for mention in by_event.get(event_id, [])
            ],
        })
    return output


def _event_time_market_observations(symbol: str, as_of_ts: int) -> list[dict]:
    try:
        return persistent_store.list_market_observations_as_of(
            symbol,
            as_of_ts=as_of_ts,
            start_ts=as_of_ts - 86_400,
            observation_type="minute_candle",
            limit=2_000,
        )
    except Exception:
        return []


def _read_json_list(key: str, limit: int) -> list[dict]:
    rows = r.lrange(key, 0, max(0, limit) - 1)
    out = []
    for row in rows:
        try:
            parsed = json.loads(row)
        except Exception:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def _read_heartbeat(key: str) -> dict | None:
    try:
        raw = r.get(key)
        parsed = json.loads(raw) if raw else None
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _local_exit_readiness() -> dict:
    try:
        database_available = persistent_store.healthcheck()
    except Exception:
        database_available = False
    try:
        authorization = get_trading_authorization_status()
    except Exception:
        authorization = {}
    result = evaluate_local_exit_readiness(
        now=int(time.time()),
        account_stream=_read_heartbeat(ACCOUNT_STREAM_HEARTBEAT_KEY),
        exit_worker=_read_heartbeat(EXIT_CONTROLLER_HEARTBEAT_KEY),
        backup_exit_worker=_read_heartbeat(EXIT_CONTROLLER_BACKUP_HEARTBEAT_KEY),
        backup_required=os.getenv(
            "AGENT_EXIT_BACKUP_REQUIRED", "true"
        ).lower() == "true",
        database_available=database_available,
        trading_authorization=authorization,
        execute_trades=os.getenv("AGENT_EXECUTE_TRADES", "false").lower() == "true",
        broker_stops_enabled=os.getenv(
            "AGENT_BROKER_PROTECTIVE_STOPS_ENABLED", "false"
        ).lower() == "true",
        local_stop_apply_enabled=os.getenv(
            "AGENT_EXIT_APPLY_STOPS", "false"
        ).lower() == "true",
        exit_submit_enabled=os.getenv(
            "AGENT_EXIT_SUBMIT_ENABLED", "false"
        ).lower() == "true",
        kill_switch=os.getenv("AGENT_TRADING_KILL_SWITCH", "false").lower() == "true",
        max_stream_age_seconds=int(os.getenv("AGENT_ENTRY_MAX_STREAM_HEARTBEAT_AGE_SECONDS", "10")),
        max_exit_worker_age_seconds=int(os.getenv("AGENT_ENTRY_MAX_EXIT_HEARTBEAT_AGE_SECONDS", "3")),
        minimum_reauthorization_seconds=int(
            os.getenv("AGENT_ENTRY_MIN_REAUTHORIZATION_SECONDS", "600")
        ),
    ).to_dict()
    result.update(
        {
            "protection_mode": "local_trigger",
            "database_available": database_available,
            "trading_authorization": authorization,
        }
    )
    return result


@router.get("/signals/{symbol}")
def signals(symbol: str):
    sym = symbol.upper()
    as_of_ts = int(time.time())
    try:
        client = _schwab_market_client()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="schwab_history_unavailable") from exc
    df = _load_agent_daily_history(sym, client)
    current_price = 0.0
    try:
        current_price = float(df["Close"].iloc[-1])
    except Exception:
        pass
    try:
        quote = normalize_schwab_equity_quote(
            client.get_quote(sym, fields="quote"), symbol=sym, fetched_at_ms=int(time.time() * 1000))
    except Exception:
        quote = {}
    current_rvol, rvol_details = _agent_current_rvol(sym, df, quote)
    rvol = current_rvol if current_rvol is not None else 0.0
    macd_open = _macd_open(df)
    articles = _attach_gdelt_mentions(_recent_articles(sym, limit=10))
    market_observations = _event_time_market_observations(sym, as_of_ts)
    return JSONResponse(
        {
            "symbol": sym,
            "rvol": current_rvol,
            "rvol_details": rvol_details,
            "history_source": "schwab_daily",
            "history_last_bar": df["t"].iloc[-1].isoformat(),
            "semantic_signal": semantic_signal(
                sym,
                articles=articles,
                as_of_ts=as_of_ts,
                market_observations=market_observations,
            ),
            "market_signal": market_signal(sym, df, price=current_price, rvol=rvol, macd_open=macd_open),
        }
    )


@router.get("/features/recent")
def recent_features(symbol: str | None = None, limit: int = 100, offset: int = 0):
    return JSONResponse(list_feature_snapshots(symbol=symbol, limit=limit, offset=offset))


@router.get("/stop-loss/status")
def stop_loss_status():
    heartbeat = None
    heartbeat_age_seconds = None
    events = []
    try:
        raw = r.get(PROTECTIVE_STOP_HEARTBEAT_KEY)
        heartbeat = json.loads(raw) if raw else None
        if isinstance(heartbeat, dict) and heartbeat.get("ts"):
            heartbeat_age_seconds = max(0, int(time.time()) - int(heartbeat["ts"]))
    except Exception:
        heartbeat = None
        heartbeat_age_seconds = None

    try:
        for row in r.lrange(K_STOP_LOSS_EVENTS, 0, 19):
            try:
                events.append(json.loads(row))
            except Exception:
                continue
    except Exception:
        events = []

    return JSONResponse(
        {
            "heartbeat": heartbeat,
            "heartbeat_age_seconds": heartbeat_age_seconds,
            "recent_events": events,
            "recent_execution_count": len(_read_json_list(EXECUTION_LOG_KEY, 20)),
            "execute_trades": os.getenv("AGENT_EXECUTE_TRADES", "false").lower() == "true",
            "monitor_enabled": True,
            "protection_mode": (
                "broker_held_stop"
                if os.getenv("AGENT_BROKER_PROTECTIVE_STOPS_ENABLED", "false").lower() == "true"
                else "local_trigger"
            ),
            "broker_stops_enabled": os.getenv(
                "AGENT_BROKER_PROTECTIVE_STOPS_ENABLED", "false"
            ).lower() == "true",
            "legacy_monitor_enabled": os.getenv(
                "AGENT_LEGACY_STOP_LOSS_ENABLED", "false"
            ).lower() == "true",
        }
    )


@router.get("/exit-controller/status")
def exit_controller_status():
    heartbeat = None
    heartbeat_age_seconds = None
    try:
        raw = r.get(EXIT_CONTROLLER_HEARTBEAT_KEY)
        heartbeat = json.loads(raw) if raw else None
        if isinstance(heartbeat, dict) and heartbeat.get("ts"):
            heartbeat_age_seconds = max(0, int(time.time()) - int(heartbeat["ts"]))
    except Exception:
        heartbeat = None
    try:
        events = _read_json_list(EXIT_CONTROLLER_EVENTS_KEY, 20)
    except Exception:
        events = []
    return JSONResponse(
        {
            "heartbeat": heartbeat,
            "heartbeat_age_seconds": heartbeat_age_seconds,
            "recent_events": events,
            "execution_mode": (heartbeat or {}).get("execution_mode", "shadow"),
            "execute_trades": bool((heartbeat or {}).get("exit_submit_enabled", False)),
        }
    )


@router.get("/account-stream/status")
def account_stream_status():
    heartbeat = None
    heartbeat_age_seconds = None
    try:
        raw = r.get(ACCOUNT_STREAM_HEARTBEAT_KEY)
        heartbeat = json.loads(raw) if raw else None
        if isinstance(heartbeat, dict) and heartbeat.get("ts"):
            heartbeat_age_seconds = max(0, int(time.time()) - int(heartbeat["ts"]))
    except Exception:
        heartbeat = None
    return JSONResponse({
        "heartbeat": heartbeat,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "shared_connection": True,
        "browser_mode": "redis_relay",
        "execute_trades": os.getenv("AGENT_EXECUTE_TRADES", "false").lower() == "true",
    })


@router.get("/local-exit-readiness")
def local_exit_readiness(access_token: str = Depends(require_access_token)):
    """Report whether a new live position can be protected locally."""
    return JSONResponse(_local_exit_readiness())


@router.get("/market-stream/{symbol}")
def market_stream_snapshot(
    symbol: str,
    candle_limit: int = Query(390, ge=1, le=1000),
    access_token: str = Depends(require_access_token),
):
    """Return the authenticated user's shared Schwab stream snapshot."""
    wanted = str(symbol or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9.$/-]{1,15}", wanted):
        raise HTTPException(status_code=400, detail="Invalid symbol")
    snapshot = _market_stream_snapshot(wanted, candle_limit=candle_limit)
    snapshot["order_flow_confirmation"] = evaluate_order_flow_confirmation(
        snapshot, limits=_order_flow_limits()
    ).to_dict()
    snapshot["subscribed"] = bool(snapshot.get("levelone"))
    return JSONResponse(snapshot)


def _resolve_account_number(access_token: str, preferred: str | None = None) -> str:
    """
    Resolve Schwab's encrypted account ID required by trading endpoints.
    """
    try:
        prefs = UserPreferences(access_token)
        client = SchwabAccountClient(user_prefs=prefs, access_token=access_token, eager_load=False)
        rows = client.get_account_numbers() or []
        if isinstance(rows, dict):
            rows = rows.get("accounts") or rows.get("securitiesAccounts") or rows.get("items") or []
        if not rows:
            raise RuntimeError("No Schwab accounts found")
        selected = None
        for row in rows:
            if selected is None:
                selected = row or {}
            if preferred and str((row or {}).get("accountNumber")) == str(preferred):
                selected = row or {}
                break
        if preferred and str((selected or {}).get("accountNumber")) != str(preferred):
            raise RuntimeError("Requested Schwab account was not found")
        encrypted = (
            (selected or {}).get("hashValue")
            or (selected or {}).get("accountHash")
            or (selected or {}).get("encryptedAccountId")
        )
        if not encrypted:
            raise RuntimeError("Schwab did not return an encrypted account ID")
        return str(encrypted)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Unable to resolve Schwab account number: {exc}")


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _position_for_symbol(access_token: str, symbol: str) -> dict | None:
    try:
        prefs = UserPreferences(access_token)
        client = SchwabAccountClient(user_prefs=prefs)
        accounts = client.get_all_accounts(include_positions=True) or []
    except Exception:
        return None

    wanted = symbol.upper()
    for account in accounts:
        sa = (account or {}).get("securitiesAccount") or {}
        for position in sa.get("positions") or []:
            instrument = position.get("instrument") or {}
            if str(instrument.get("symbol") or "").upper() == wanted:
                return position
    return None


def _long_position_snapshot(position: dict | None) -> dict:
    position = position or {}
    qty = _num(position.get("longQuantity"))
    avg = _num(
        position.get("averageLongPrice"),
        default=position.get("averagePrice") or 0.0,
    )
    return {"quantity": qty, "average_price": avg}


def _first_positive(mapping: dict, *keys: str) -> float:
    for key in keys:
        value = _num(mapping.get(key))
        if value > 0:
            return value
    return 0.0


def _load_agent_daily_history(symbol, client):
    try:
        return validated_agent_daily_frame(_get_daily(symbol, market_client=client))
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="schwab_history_unavailable") from exc


def _agent_current_rvol(symbol, frame, quote):
    return _current_session_rvol(symbol, frame, {
        "rvol_window_days": max(20, min(252, int(os.getenv("AGENT_RVOL_WINDOW_DAYS", "60")))),
    }, {
        "market_ts": _num(quote.get("quote_time_ms")) / 1000,
        "total_volume": quote.get("total_volume") if quote.get("total_volume_valid") is True else None,
    })


def _rvol_entry_reason(rvol, minimum):
    if rvol is None:
        return "rvol_unavailable"
    if rvol < minimum:
        return f"rvol_below_threshold({rvol:.2f}<{minimum:.2f})"
    return None


def _live_entry_market_snapshot(access_token: str, symbol: str) -> dict:
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    client = SchwabMarketDataClient(UserPreferences(access_token), access_token=access_token)
    raw_quote = client.get_quote(symbol.upper(), fields="quote")
    market_date = now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    raw_hours = client.get_market_hours_for("equity", date=market_date)
    return {
        "quote": normalize_schwab_equity_quote(
            raw_quote, symbol=symbol, fetched_at_ms=now_ms
        ),
        "market_hours": normalize_schwab_equity_hours(raw_hours, now=now),
        "now_ms": now_ms,
    }


def _entry_market_limits() -> EntryMarketLimits:
    return EntryMarketLimits(
        max_quote_age_seconds=float(os.getenv("AGENT_MAX_QUOTE_AGE_SECONDS", "3")),
        max_spread_bps=float(os.getenv("AGENT_MAX_SPREAD_BPS", "75")),
        min_bid_size=int(os.getenv("AGENT_MIN_BID_SIZE", "1")),
        min_ask_size=int(os.getenv("AGENT_MIN_ASK_SIZE", "1")),
        min_bid_notional=float(os.getenv("AGENT_MIN_BID_NOTIONAL", "1000")),
        min_ask_notional=float(os.getenv("AGENT_MIN_ASK_NOTIONAL", "1000")),
        min_total_volume=int(os.getenv("AGENT_MIN_DAY_VOLUME", "25000")),
        min_price=float(os.getenv("AGENT_MIN_ENTRY_PRICE", "1")),
        max_price=float(os.getenv("AGENT_MAX_ENTRY_PRICE", "50")),
    )


def _order_flow_limits() -> OrderFlowLimits:
    return OrderFlowLimits(
        max_book_age_seconds=float(os.getenv("AGENT_MAX_LEVEL2_AGE_SECONDS", "2")),
        max_receive_age_seconds=float(os.getenv("AGENT_MAX_LEVEL2_RECEIVE_AGE_SECONDS", "2")),
        min_combined_depth=int(os.getenv("AGENT_MIN_LEVEL2_DEPTH", "1000")),
        min_imbalance=float(os.getenv("AGENT_MIN_LEVEL2_IMBALANCE", "-0.35")),
    )


def _market_stream_snapshot(symbol: str, *, candle_limit: int = 390) -> dict:
    try:
        return load_market_stream_snapshot(
            r,
            symbol=symbol,
            levelone_key=LEVELONE_STREAM_KEY,
            nasdaq_book_key=NASDAQ_BOOK_STREAM_KEY,
            nyse_book_key=NYSE_BOOK_STREAM_KEY,
            chart_key_prefix=CHART_STREAM_KEY_PREFIX,
            chart_limit=candle_limit,
        )
    except Exception as exc:
        return {
            "symbol": symbol.upper(),
            "levelone": None,
            "book": None,
            "book_metrics": {},
            "candles": [],
            "candle_count": 0,
            "stream_error": str(exc),
        }


def _schwab_account_risk_snapshot(
    access_token: str,
    preferred_account: str | None = None,
) -> dict:
    """Load the conservative account inputs required for risk sizing."""
    prefs = UserPreferences(access_token)
    client = SchwabAccountClient(user_prefs=prefs, access_token=access_token, eager_load=False)
    accounts = client.get_all_accounts(include_positions=True) or []
    selected = None
    for row in accounts:
        account = (row or {}).get("securitiesAccount") or {}
        if selected is None and not preferred_account:
            selected = account
        if preferred_account and str(account.get("accountNumber")) == str(preferred_account):
            selected = account
            break
    if not selected:
        raise RuntimeError("Selected Schwab account unavailable")
    current = selected.get("currentBalances") or {}
    initial = selected.get("initialBalances") or {}
    positions = selected.get("positions") or []

    equity = _first_positive(
        current,
        "liquidationValue",
        "equity",
    ) or _first_positive(initial, "liquidationValue", "equity")
    buying_power = _first_positive(
        current,
        "availableFundsNonMarginableTrade",
        "cashAvailableForTrading",
        "availableFunds",
        "buyingPowerNonMarginableTrade",
        "stockBuyingPower",
        "buyingPower",
    )
    open_positions = sum(
        1
        for position in positions
        if _num(position.get("longQuantity")) > 0 or _num(position.get("shortQuantity")) > 0
    )
    encrypted_account_id = None
    try:
        account_numbers = client.get_account_numbers() or []
        for row in account_numbers:
            if str((row or {}).get("accountNumber")) == str(selected.get("accountNumber")):
                encrypted_account_id = (
                    (row or {}).get("hashValue")
                    or (row or {}).get("accountHash")
                    or (row or {}).get("encryptedAccountId")
                )
                break
        if not encrypted_account_id:
            raise RuntimeError("encrypted account ID unavailable")
        journal_risk = get_account_risk_summary(account_id=str(encrypted_account_id))
    except Exception as exc:
        journal_risk = {
            "daily_realized_pnl": 0.0,
            "aggregate_open_risk": 0.0,
            "risk_state_complete": False,
            "incomplete_reasons": [f"risk_ledger_unavailable:{type(exc).__name__}"],
        }
    return {
        "account_number": selected.get("accountNumber"),
        "equity": equity,
        "buying_power": buying_power,
        "closing_only": bool(selected.get("isClosingOnlyRestricted")),
        "open_position_count": open_positions,
        "positions": positions,
        "risk_account_id": encrypted_account_id,
        **journal_risk,
    }


@router.post("/act")
def act(
    symbol: str = "AAPL",
    qty: int = Query(None, description="Override quantity; defaults to sizing logic"),
    order_type: str = Query("LIMIT", description="LIMIT; MARKET entries require explicit enablement"),
    limit_slippage_pct: float = Query(0.2, description="If LIMIT, set price = last*(1+/-slippage_pct depending on side)"),
    simulate: bool = Query(False, description="Force dry-run even if AGENT_EXECUTE_TRADES=true"),
    account_id: str = Query(None, description="Override Schwab account number; otherwise resolves for the user"),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    access_token: str = Depends(require_access_token),
):
    """
    Returns the policy-selected action (or heuristic fallback) plus features used.
    Can submit an equity order if AGENT_EXECUTE_TRADES=true and guardrails pass.
    """
    decision_ts = int(time.time())
    execute = os.getenv("AGENT_EXECUTE_TRADES", "false").lower() == "true"
    dry_run = (not execute) or simulate
    local_exit_readiness = _local_exit_readiness()
    order_type = str(order_type or "").strip().upper()
    if order_type not in {"LIMIT", "MARKET"}:
        raise HTTPException(status_code=400, detail="order_type must be LIMIT or MARKET")
    max_position_pct = float(os.getenv("AGENT_MAX_POSITION_PCT", "0.10"))
    risk_per_trade_pct = float(os.getenv("AGENT_RISK_PER_TRADE_PCT", "0.005"))
    daily_loss_limit_pct = float(os.getenv("AGENT_DAILY_LOSS_LIMIT_PCT", "0.02"))
    aggregate_open_risk_pct = float(os.getenv("AGENT_MAX_AGGREGATE_OPEN_RISK_PCT", "0.015"))
    max_volume_participation_pct = float(os.getenv("AGENT_MAX_VOLUME_PARTICIPATION_PCT", "0.005"))
    risk_slippage_bps = float(os.getenv("AGENT_RISK_SLIPPAGE_BPS", "20"))
    min_rag_conf = float(os.getenv("AGENT_MIN_RAG_CONF", "0.2"))
    min_rvol = float(os.getenv("AGENT_MIN_RVOL", os.getenv("RVOL_THRESHOLD", "3.0")))
    default_shares = int(os.getenv("AGENT_DEFAULT_SHARES", "1"))
    max_loss_pct = float(os.getenv("AGENT_MAX_LOSS_PCT", "0.03"))
    trailing_loss_pct = float(os.getenv("AGENT_TRAILING_LOSS_PCT", str(max_loss_pct)))
    max_loss_amount_env = os.getenv("AGENT_MAX_LOSS_DOLLARS")
    trailing_loss_amount_env = os.getenv("AGENT_TRAILING_LOSS_DOLLARS")
    max_loss_amount = float(max_loss_amount_env) if max_loss_amount_env else None
    trailing_loss_amount = float(trailing_loss_amount_env) if trailing_loss_amount_env else None

    history_client = SchwabMarketDataClient(UserPreferences(access_token), access_token=access_token)
    df = _load_agent_daily_history(symbol, history_client)
    emb_dim = int(os.getenv("RAG_SIGNAL_EMBED_DIM", "16"))
    probe_env = StockTradingEnv(df, symbol=symbol, embedding_dim=emb_dim, max_episode_steps=50)
    rag_score, _, rag_conf = probe_env._get_rag_observation()
    articles = _attach_gdelt_mentions(_recent_articles(symbol.upper(), limit=10))
    current_price = 0.0
    try:
        current_price = float(df["Close"].iloc[-1])
    except Exception:
        pass
    market_data_error = None
    try:
        live_market = _live_entry_market_snapshot(access_token, symbol)
    except Exception as exc:
        market_data_error = str(exc)
        now_ms = int(time.time() * 1000)
        live_market = {
            "quote": normalize_schwab_equity_quote(
                {}, symbol=symbol, fetched_at_ms=now_ms
            ),
            "market_hours": {
                "market_open": False,
                "is_regular": False,
                "session": "unknown",
            },
            "now_ms": now_ms,
        }
    live_quote = live_market["quote"]
    live_bid = _num(live_quote.get("bid"))
    live_ask = _num(live_quote.get("ask"))
    live_price = _num(live_quote.get("mark"))
    if live_price <= 0 and live_bid > 0 and live_ask > 0:
        live_price = (live_bid + live_ask) / 2
    if live_price <= 0:
        live_price = _num(live_quote.get("last"))
    if live_price > 0:
        current_price = live_price
    rvol_frame = df
    current_rvol, rvol_details = _agent_current_rvol(symbol, rvol_frame, live_quote)
    rvol = current_rvol if current_rvol is not None else 0.0
    macd_open = _macd_open(df)
    try:
        account_risk = _schwab_account_risk_snapshot(access_token, preferred_account=account_id)
    except Exception as exc:
        account_risk = {
            "account_number": account_id,
            "equity": 0.0,
            "buying_power": 0.0,
            "closing_only": False,
            "open_position_count": 0,
            "daily_realized_pnl": 0.0,
            "aggregate_open_risk": 0.0,
            "risk_state_complete": False,
            "error": str(exc),
        }
    account_positions = account_risk.pop("positions", [])
    position = _long_position_snapshot(next((row for row in account_positions
        if str((row.get("instrument") or {}).get("symbol") or "").upper() == symbol.upper()), None))
    proposed_plan = trade_plan(
        current_price,
        df,
        news_sentiment(articles),
        DecisionConfig(max_stop_loss_pct=max_loss_pct),
    )
    position_sizing = calculate_long_position_size(
        account_equity=account_risk["equity"],
        buying_power=account_risk["buying_power"],
        entry_price=current_price,
        stop_price=proposed_plan["stop_loss"],
        daily_realized_pnl=account_risk["daily_realized_pnl"],
        aggregate_open_risk=account_risk["aggregate_open_risk"],
        limits=RiskLimits(
            risk_per_trade_pct=risk_per_trade_pct,
            daily_loss_limit_pct=daily_loss_limit_pct,
            max_aggregate_open_risk_pct=aggregate_open_risk_pct,
            max_position_pct=max_position_pct,
            max_volume_participation_pct=max_volume_participation_pct,
            slippage_bps=risk_slippage_bps,
        ),
    )
    semantic = semantic_signal(
        symbol,
        articles=articles,
        rag_signal={"confidence": rag_conf, "hit_count": 0, "graph_count": 0},
        as_of_ts=decision_ts,
        market_observations=_event_time_market_observations(symbol, decision_ts),
    )
    market = market_signal(
        symbol,
        df,
        price=current_price,
        rvol=rvol,
        macd_open=macd_open,
    )
    entry_market_gate = evaluate_entry_market_gate(
        quote=live_quote,
        market_hours=live_market["market_hours"],
        now_ms=live_market["now_ms"],
        limits=_entry_market_limits(),
        heuristic_halt_risk=bool((market.get("intraday") or {}).get("halt_risk")),
    )
    stream_market = _market_stream_snapshot(symbol, candle_limit=30)
    order_flow_confirmation = evaluate_order_flow_confirmation(
        stream_market,
        limits=_order_flow_limits(),
    )
    env = StockTradingEnv(
        df,
        symbol=symbol,
        embedding_dim=emb_dim,
        max_episode_steps=50,
        signal_features={"semantic_signal": semantic, "market_signal": market},
    )
    state = env.reset(latest_window=True, account_state={
        "balance": account_risk["buying_power"],
        "equity": account_risk["equity"],
        "realized_pnl": account_risk["daily_realized_pnl"],
        "quantity": position["quantity"],
        "average_price": position["average_price"],
    })

    action_probs = [0.0] * env.action_space.n
    agent = _load_agent(env.observation_space.shape[0], env.action_space.n)
    policy_used = "ppo" if agent else "heuristic"
    action = 2  # hold default
    ppo_action = action
    guardrail = None

    if agent:
        import torch

        mem = _Memory()
        action = agent.select_action(state, mem)
        ppo_action = action
        with torch.no_grad():
            tstate = torch.tensor(state, dtype=torch.float32)
            probs = agent.policy_old.actor(tstate)
            action_probs = probs.detach().cpu().numpy().tolist()

    stop_loss = evaluate_stop_loss(
        quantity=position["quantity"],
        average_price=position["average_price"],
        current_price=current_price,
        max_loss_pct=max_loss_pct,
        max_loss_amount=max_loss_amount,
        peak_price=max(position["average_price"], current_price),
        trailing_loss_pct=trailing_loss_pct,
        trailing_loss_amount=trailing_loss_amount,
    )

    committee = TradingCommittee().deliberate(
        df,
        {
            "ppo_action": ppo_action,
            "rag_score": rag_score,
            "rag_confidence": rag_conf,
            "min_rag_confidence": min_rag_conf,
            "current_price": current_price,
            "equity": account_risk["equity"],
            "quantity": qty if qty is not None else default_shares,
            "max_position_pct": max_position_pct,
        },
    )
    committee_action = COMMITTEE_ACTIONS.get(committee["final_action"], 2)
    if committee_action != action:
        guardrail = f"committee_override({action}->{committee_action})"
        action = committee_action

    if stop_loss.triggered:
        guardrail = stop_loss.reason
        action = 1
        qty = max(1, int(position["quantity"]))

    if action == 0 and not entry_market_gate.allowed:
        guardrail = f"entry_market_gate:{entry_market_gate.reasons[0]}"
        action = 2
        qty = 0

    if action == 0 and not order_flow_confirmation.allowed:
        guardrail = f"order_flow:{order_flow_confirmation.reasons[0]}"
        action = 2
        qty = 0

    if (
        action == 0
        and execute
        and os.getenv("AGENT_LIVE_ENTRY_APPROVED", "false").lower() != "true"
    ):
        guardrail = "live_entry_not_approved"
        action = 2
        qty = 0
    if action == 0 and execute and not dry_run and not local_exit_readiness["ready"]:
        guardrail = f"local_exit_not_ready:{local_exit_readiness['reasons'][0]}"
        action = 2
        qty = 0
    if (
        action == 0
        and order_type.upper() == "MARKET"
        and os.getenv("AGENT_ALLOW_MARKET_ENTRIES", "false").lower() != "true"
    ):
        guardrail = "market_entries_disabled"
        action = 2
        qty = 0

    rvol_entry_reason = _rvol_entry_reason(current_rvol, min_rvol)
    if action == 0 and rvol_entry_reason:
        guardrail = rvol_entry_reason
        action = 2
        qty = 0

    if rag_conf < min_rag_conf:
        if not stop_loss.triggered:
            guardrail = guardrail or f"rag_conf_too_low({rag_conf:.2f}<{min_rag_conf})"
            action = 2
    else:
        try:
            if action == 0:  # buy
                if account_risk.get("closing_only"):
                    guardrail = "account_closing_only"
                    action = 2
                    qty = 0
                elif not position_sizing.allowed:
                    guardrail = position_sizing.reason or "risk_sizing_blocked"
                    action = 2
                    qty = 0
                elif execute and not dry_run and not account_risk.get("risk_state_complete"):
                    guardrail = "risk_journal_not_authoritative"
                    action = 2
                    qty = 0
                else:
                    calculated_qty = position_sizing.quantity
                    qty = min(max(int(qty), 0), calculated_qty) if qty is not None else calculated_qty
                    if qty < 1:
                        guardrail = "requested_quantity_below_one_share"
                        action = 2
                        qty = 0
            elif action == 1:  # sell
                qty = qty if qty is not None else max(1, int(position["quantity"] or env.shares_held))
            else:
                qty = 0
        except Exception:
            qty = qty if qty is not None else default_shares

    action_name = "buy" if action == 0 else "sell" if action == 1 else "hold"

    payload = {
        "symbol": symbol,
        "action": action_name,
        "reward": None,
        "reward_status": "not_measured_at_inference",
        "observation_window": "latest_six_daily_bars",
        "policy": policy_used,
        "obs_len": len(state),
        "rag_api_url": env.rag_api_url,
        "action_probs": action_probs,
        "guardrail": guardrail,
        "rag_confidence": rag_conf,
        "semantic_signal": semantic,
        "market_signal": market,
        "rvol": current_rvol,
        "rvol_details": rvol_details,
        "history_source": "schwab_daily",
        "history_last_bar": df["t"].iloc[-1].isoformat(),
        "committee": committee,
        "stop_loss": stop_loss.to_dict(),
        "position": position,
        "account_risk": account_risk,
        "trade_plan": proposed_plan,
        "position_sizing": position_sizing.to_dict(),
        "entry_market_gate": entry_market_gate.to_dict(),
        "order_flow_confirmation": order_flow_confirmation.to_dict(),
        "local_exit_readiness": local_exit_readiness,
        "execution_quote": live_quote,
        "market_data_error": market_data_error,
        "ts": decision_ts,
        "quantity": qty if qty is not None else 0,
        "order_type": order_type,
        "dry_run": dry_run,
        "account_id": account_id,
    }

    try:
        r.lpush(ACTIONS_KEY, json.dumps(payload))
        r.ltrim(ACTIONS_KEY, 0, ACTIONS_MAX_LEN - 1)
    except Exception:
        pass
    try:
        record_feature_snapshot(symbol, payload, source="agent_api", ts=payload["ts"])
    except Exception:
        pass

    if execute and not dry_run and action_name in ("buy", "sell") and qty > 0 and current_price > 0:
        exec_payload = {
            "symbol": symbol,
            "action": action_name,
            "qty": qty,
            "order_type": order_type,
            "ts": payload["ts"],
            "note": "",
            "status": "pending",
            "account_id": account_id,
            "idempotency_key": idempotency_key,
        }
        try:
            r.lpush(EXECUTION_LOG_KEY, json.dumps(exec_payload))
            r.ltrim(EXECUTION_LOG_KEY, 0, ACTIONS_MAX_LEN - 1)
            try:
                acct_num = _resolve_account_number(access_token, preferred=account_id)
                prefs = UserPreferences(access_token)
                order_client = SchwabOrderClient(prefs, access_token=access_token)
                instruction = "BUY" if action_name == "buy" else "SELL"
                if instruction == "BUY":
                    final_exit_readiness = _local_exit_readiness()
                    exec_payload["final_local_exit_readiness"] = final_exit_readiness
                    if not final_exit_readiness["ready"]:
                        raise ValueError(
                            "local_exit_not_ready:"
                            f"{final_exit_readiness['reasons'][0]}"
                        )
                    final_market = _live_entry_market_snapshot(access_token, symbol)
                    final_rvol, final_rvol_details = _agent_current_rvol(
                        symbol, rvol_frame, final_market["quote"])
                    exec_payload["final_rvol"] = final_rvol
                    exec_payload["final_rvol_details"] = final_rvol_details
                    final_rvol_reason = _rvol_entry_reason(final_rvol, min_rvol)
                    if final_rvol_reason:
                        raise ValueError(final_rvol_reason)
                    final_gate = evaluate_entry_market_gate(
                        quote=final_market["quote"],
                        market_hours=final_market["market_hours"],
                        now_ms=final_market["now_ms"],
                        limits=_entry_market_limits(),
                        heuristic_halt_risk=bool(
                            (market.get("intraday") or {}).get("halt_risk")
                        ),
                    )
                    exec_payload["final_entry_market_gate"] = final_gate.to_dict()
                    if not final_gate.allowed:
                        raise ValueError(
                            f"entry_market_gate:{final_gate.reasons[0]}"
                        )
                    final_order_flow = evaluate_order_flow_confirmation(
                        _market_stream_snapshot(symbol, candle_limit=1),
                        limits=_order_flow_limits(),
                    )
                    exec_payload["final_order_flow_confirmation"] = (
                        final_order_flow.to_dict()
                    )
                    if not final_order_flow.allowed:
                        raise ValueError(
                            f"order_flow:{final_order_flow.reasons[0]}"
                        )
                    final_sizing = calculate_long_position_size(
                        account_equity=account_risk["equity"],
                        buying_power=account_risk["buying_power"],
                        entry_price=final_gate.price,
                        stop_price=proposed_plan["stop_loss"],
                        daily_realized_pnl=account_risk["daily_realized_pnl"],
                        aggregate_open_risk=account_risk["aggregate_open_risk"],
                        limits=RiskLimits(
                            risk_per_trade_pct=risk_per_trade_pct,
                            daily_loss_limit_pct=daily_loss_limit_pct,
                            max_aggregate_open_risk_pct=aggregate_open_risk_pct,
                            max_position_pct=max_position_pct,
                            max_volume_participation_pct=max_volume_participation_pct,
                            slippage_bps=risk_slippage_bps,
                        ),
                    )
                    if not final_sizing.allowed:
                        raise ValueError(
                            f"final_risk_sizing:{final_sizing.reason or 'blocked'}"
                        )
                    qty = min(qty, final_sizing.quantity)
                    top_book_cap = max(
                        1,
                        int(
                            _num(final_market["quote"].get("ask_size"))
                            * float(os.getenv("AGENT_MAX_TOP_BOOK_PARTICIPATION_PCT", "0.25"))
                        ),
                    )
                    qty = min(qty, top_book_cap)
                    if qty < 1:
                        raise ValueError("final_risk_sizing:quantity_below_one")
                    current_price = final_gate.price
                    exec_payload["qty"] = qty
                    exec_payload["final_position_sizing"] = final_sizing.to_dict()
                    exec_payload["top_book_quantity_cap"] = top_book_cap
                order_payload = {
                    "orderType": order_type,
                    "session": "NORMAL",
                    "duration": "DAY",
                    "orderStrategyType": "SINGLE",
                    "orderLegCollection": [
                        {
                            "instruction": instruction,
                            "quantity": qty,
                            "instrument": {
                                "symbol": symbol,
                                "assetType": "EQUITY"
                            }
                        }
                    ],
                }
                if order_type == "LIMIT":
                    if instruction == "BUY":
                        price = _num(final_market["quote"].get("ask"))
                    else:
                        price = current_price * (1 - abs(limit_slippage_pct) / 100.0)
                    order_payload["price"] = round(price, 2)
                if not idempotency_key:
                    raise ValueError("Idempotency-Key header is required for live orders")
                reservation = reserve_order_intent(
                    idempotency_key=idempotency_key,
                    account_id=acct_num,
                    symbol=symbol,
                    side=instruction,
                    quantity=qty,
                    order_type=order_type,
                    order_payload=order_payload,
                    intent_role="entry" if instruction == "BUY" else "exit",
                )
                if instruction == "BUY":
                    exec_payload["position_id"] = ensure_entry_position(
                        idempotency_key=idempotency_key,
                        account_id=acct_num,
                        symbol=symbol,
                        intended_quantity=qty,
                        hard_stop_price=proposed_plan["stop_loss"],
                        strategy_version=os.getenv("AGENT_STRATEGY_VERSION", "news-momentum-v1"),
                    )
                if not reservation.created:
                    existing = reservation.intent
                    exec_payload["note"] = "Existing order intent returned; Schwab was not called again"
                    exec_payload["status"] = existing.get("status") or "unknown"
                    exec_payload["idempotent_replay"] = True
                    exec_payload["order_response"] = {
                        "order_id": existing.get("broker_order_id"),
                        "location": existing.get("broker_location"),
                    }
                else:
                    try:
                        order_resp = order_client.place_order(account_id=acct_num, order_payload=order_payload)
                    except Exception as exc:
                        update_order_intent(idempotency_key, status="unknown", error=str(exc))
                        raise
                    update_order_intent(
                        idempotency_key,
                        status="submitted",
                        broker_order_id=(order_resp or {}).get("order_id"),
                        broker_location=(order_resp or {}).get("location"),
                    )
                    exec_payload["note"] = f"Submitted {instruction} {qty} {symbol} ({order_type})"
                    exec_payload["status"] = "submitted"
                    exec_payload["order_response"] = order_resp
            except Exception as exc:
                exec_payload["note"] = f"Order placement failed: {exc}"
                exec_payload["status"] = "blocked" if isinstance(exc, (ValueError, IdempotencyConflict)) else "error"
                payload["execution_error"] = exec_payload["note"]
            r.lpush(EXECUTION_LOG_KEY, json.dumps(exec_payload))
            r.ltrim(EXECUTION_LOG_KEY, 0, ACTIONS_MAX_LEN - 1)
            payload["execution"] = exec_payload
        except Exception:
            pass
    elif execute and not dry_run and action_name in ("buy", "sell"):
        payload["execution_error"] = "Execution skipped (missing price/qty/guardrail)"

    return JSONResponse(payload)


@router.get("/actions/recent")
def recent_actions(limit: int = 50):
    """
    Return recent logged agent actions (cached in Redis).
    """
    try:
        return JSONResponse(_read_json_list(ACTIONS_KEY, limit))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/executions/recent")
def recent_executions(limit: int = 50):
    """
    Return recent order execution attempts and results from Redis.
    """
    try:
        return JSONResponse(_read_json_list(EXECUTION_LOG_KEY, limit))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/orders/recent")
def recent_order_intents(
    limit: int = Query(50, ge=1, le=200),
    account_id: str | None = Query(None),
    access_token: str = Depends(require_access_token),
):
    """Return durable order intents for the selected authenticated Schwab account."""
    encrypted_account_id = _resolve_account_number(access_token, preferred=account_id)
    try:
        return JSONResponse({"orders": list_order_intents(account_id=encrypted_account_id, limit=limit)})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
