from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time

import redis

from routers.auth import get_market_data_access_token
from schwab.streaming_client import SchwabWebSocketClient
from schwab.orders import SchwabOrderClient
from schwab.user_preferences import UserPreferences
from services.account_activity import normalize_account_activity
from services.order_reconciliation import (
    ambiguous_order_query_window,
    match_ambiguous_order,
    normalize_schwab_order,
)
from services import persistent_store
from services.market_stream import (
    BOOK_FIELDS,
    CHART_FIELDS,
    LEVELONE_FIELDS,
    normalize_chart_equity,
    normalize_equity_book,
    normalize_levelone_equity,
    merge_levelone_equity,
    response_errors,
)
from services.market_stream_store import select_listing_book
from services.live_research_experiment import symbols_requiring_outcomes
from services.trade_journal import (
    attach_broker_order_identity,
    get_order_intent_by_broker_id,
    list_reconcilable_order_intents,
    list_unidentified_order_intents,
    record_account_activity,
    record_order_snapshot,
    list_active_position_symbols,
)


logger = logging.getLogger(__name__)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
HEARTBEAT_KEY = os.getenv("SCHWAB_ACCOUNT_STREAM_HEARTBEAT_KEY", "schwab:account_stream:heartbeat")
ACTIVITY_CHANNEL = os.getenv("SCHWAB_ACCOUNT_ACTIVITY_CHANNEL", "schwab:account_activity")
MARKET_CHANNEL = os.getenv("SCHWAB_MARKET_STREAM_CHANNEL", "schwab:market_stream")
LEVELONE_KEY = os.getenv("SCHWAB_LEVELONE_HASH_KEY", "schwab:stream:levelone")
NASDAQ_BOOK_KEY = os.getenv("SCHWAB_NASDAQ_BOOK_HASH_KEY", "schwab:stream:nasdaq_book")
NYSE_BOOK_KEY = os.getenv("SCHWAB_NYSE_BOOK_HASH_KEY", "schwab:stream:nyse_book")
CHART_KEY_PREFIX = os.getenv("SCHWAB_CHART_KEY_PREFIX", "schwab:stream:chart:")
SYMBOL_REFRESH_SECONDS = max(5, int(os.getenv("SCHWAB_STREAM_SYMBOL_REFRESH_SECONDS", "15")))
MAX_SYMBOLS = max(1, min(int(os.getenv("SCHWAB_STREAM_MAX_SYMBOLS", "25")), 100))
CHART_RETENTION_SECONDS = max(3600, int(os.getenv("SCHWAB_CHART_RETENTION_SECONDS", "172800")))
LEVEL2_ENABLED = os.getenv("SCHWAB_LEVEL2_ENABLED", "true").lower() == "true"
QUOTE_OBSERVATION_INTERVAL_MS = max(
    1000,
    int(float(os.getenv("SCHWAB_QUOTE_OBSERVATION_INTERVAL_SECONDS", "5")) * 1000),
)
BOOK_OBSERVATION_INTERVAL_MS = max(
    1000,
    int(float(os.getenv("SCHWAB_BOOK_OBSERVATION_INTERVAL_SECONDS", "10")) * 1000),
)
PRIORITY_OBSERVATION_INTERVAL_MS = max(
    1000, int(float(os.getenv("SCHWAB_PRIORITY_OBSERVATION_INTERVAL_SECONDS", "1")) * 1000)
)
OBSERVATION_BATCH_SIZE = max(
    1, min(int(os.getenv("SCHWAB_OBSERVATION_BATCH_SIZE", "250")), 1000)
)
OBSERVATION_FLUSH_SECONDS = max(
    0.05, float(os.getenv("SCHWAB_OBSERVATION_FLUSH_SECONDS", "0.25"))
)
OBSERVATION_QUEUE_SIZE = max(
    OBSERVATION_BATCH_SIZE,
    int(os.getenv("SCHWAB_OBSERVATION_QUEUE_SIZE", "5000")),
)
IDLE_SECONDS = max(2, int(os.getenv("SCHWAB_ACCOUNT_STREAM_IDLE_SECONDS", "5")))
ORDER_RECONCILE_SECONDS = max(
    2, int(os.getenv("SCHWAB_ORDER_RECONCILE_SECONDS", "5"))
)
AUTH_KEEPALIVE_SECONDS = max(
    60, int(os.getenv("SCHWAB_AUTH_KEEPALIVE_SECONDS", "300"))
)
REQUIRED_SYMBOLS = [
    value.strip().upper()
    for value in os.getenv("SCHWAB_STREAM_REQUIRED_SYMBOLS", "SPY").split(",")
    if value.strip()
]
r = redis.from_url(REDIS_URL, decode_responses=True)


def _heartbeat(status: str, **extra) -> None:
    try:
        r.set(HEARTBEAT_KEY, json.dumps({"status": status, "ts": int(time.time()), **extra}))
    except Exception:
        pass


def _stream_symbols() -> list[str]:
    candidates = list(list_active_position_symbols(limit=MAX_SYMBOLS))
    candidates.extend(REQUIRED_SYMBOLS)
    if persistent_store.enabled():
        focus_rows = persistent_store.list_scanner_results(focus_only=True)
        scanner_rows = persistent_store.list_scanner_results()
        candidates.extend(row.get("symbol") for row in focus_rows)
        if os.getenv('LEGACY_RESEARCH_ENABLED', 'true').lower() == 'true':
            try:
                candidates.extend(symbols_requiring_outcomes(limit=MAX_SYMBOLS))
            except Exception:
                logger.exception("Unable to load live research stream symbols")
        candidates.extend(row.get("symbol") for row in scanner_rows)
    candidates.extend(os.getenv("SCHWAB_STREAM_SYMBOLS", "").split(","))
    output = []
    for raw in candidates:
        symbol = str(raw or "").strip().upper()
        if symbol and re.fullmatch(r"[A-Z0-9.$/-]{1,15}", symbol) and symbol not in output:
            output.append(symbol)
        if len(output) >= MAX_SYMBOLS:
            break
    return output


def _capture_symbols(symbols: list[str]) -> set[str]:
    selected = set(list_active_position_symbols(limit=MAX_SYMBOLS))
    if persistent_store.enabled():
        now = time.time()
        for row in persistent_store.list_scanner_results(focus_only=True):
            try:
                low, high = row["price_range"]
                limits = row["decision"]["guardrails"]
                values = [row["ts"], row["last"], row["rvol"], row["company"]["float_shares"],
                          low, high, limits["min_rvol"], limits["max_float"]]
                if any(isinstance(v, bool) for v in values):
                    continue
                ts, price, rvol, shares, low, high, minimum_rvol, maximum_float = map(float, values)
                if not all(math.isfinite(float(v)) for v in values):
                    continue
                if (0 <= now - ts <= 900 and 0 < low <= price <= high
                        and 0 <= minimum_rvol <= rvol and 0 < shares <= maximum_float):
                    selected.add(str(row["symbol"]).upper())
            except (KeyError, TypeError, ValueError):
                continue
    return selected.intersection(symbols)


def _observation_interval(kind: str, symbol: str, priority: set[str]) -> int:
    baseline = QUOTE_OBSERVATION_INTERVAL_MS if kind == "levelone_quote" else BOOK_OBSERVATION_INTERVAL_MS
    return min(baseline, PRIORITY_OBSERVATION_INTERVAL_MS) if symbol in priority else baseline


async def _refresh_stream_symbols(stream_state: dict, subscribe_market) -> None:
    while True:
        await asyncio.sleep(SYMBOL_REFRESH_SECONDS)
        try:
            symbols = await asyncio.to_thread(_stream_symbols)
            if symbols != stream_state["symbols"]:
                await subscribe_market(symbols)
            if "capture_symbols" in stream_state:
                stream_state["capture_symbols"] = await asyncio.to_thread(_capture_symbols, stream_state["symbols"])
            stream_state["last_symbol_refresh_error"] = None
        except Exception as exc:
            if "capture_symbols" in stream_state:
                stream_state["capture_symbols"] = set()
            stream_state["symbol_refresh_errors"] += 1
            stream_state["last_symbol_refresh_error"] = str(exc)
            logger.exception("Schwab market symbol refresh failed; retaining prior subscriptions")
        finally:
            stream_state["last_symbol_refresh_ms"] = int(time.time() * 1000)


def _store_market_update(service: str, row: dict, *, received_at_ms: int) -> dict:
    if service == "LEVELONE_EQUITIES":
        update = normalize_levelone_equity(row, received_at_ms=received_at_ms)
        previous = None
        if update["symbol"]:
            raw_previous = r.hget(LEVELONE_KEY, update["symbol"])
            try:
                previous = json.loads(raw_previous) if raw_previous else None
            except (TypeError, json.JSONDecodeError):
                previous = None
        normalized = merge_levelone_equity(previous, update)
        if normalized["symbol"]:
            r.hset(LEVELONE_KEY, normalized["symbol"], json.dumps(normalized))
    elif service == "CHART_EQUITY":
        normalized = normalize_chart_equity(row, received_at_ms=received_at_ms)
        if normalized["symbol"] and normalized["chart_time_ms"] > 0:
            key = f"{CHART_KEY_PREFIX}{normalized['symbol']}"
            r.hset(key, str(normalized["chart_time_ms"]), json.dumps(normalized))
            r.expire(key, CHART_RETENTION_SECONDS)
    elif service in {"NASDAQ_BOOK", "NYSE_BOOK"}:
        normalized = normalize_equity_book(
            row, service=service, received_at_ms=received_at_ms
        )
        if normalized["symbol"]:
            target = NASDAQ_BOOK_KEY if service == "NASDAQ_BOOK" else NYSE_BOOK_KEY
            r.hset(target, normalized["symbol"], json.dumps(normalized))
    else:
        return {}
    if normalized.get("symbol"):
        r.publish(MARKET_CHANNEL, json.dumps({"service": service, "data": normalized}))
    return normalized


def _durable_chart_observation(normalized: dict) -> dict:
    return {
        "symbol": normalized["symbol"],
        "observation_type": "minute_candle",
        "source": "schwab_stream",
        "event_ts": int(normalized["chart_time_ms"]) // 1000,
        "received_ts": int(normalized["received_at_ms"]) // 1000,
        "price": normalized.get("close"),
        "open": normalized.get("open"),
        "high": normalized.get("high"),
        "low": normalized.get("low"),
        "close": normalized.get("close"),
        "volume": normalized.get("volume"),
        "payload": normalized,
    }


def _durable_levelone_observation(normalized: dict) -> dict:
    event_ms = max(
        int(normalized.get(key) or 0)
        for key in ("quote_time_ms", "trade_time_ms", "bid_time_ms", "ask_time_ms")
    )
    received_ms = int(normalized.get("received_at_ms") or 0)
    bid = normalized.get("bid")
    ask = normalized.get("ask")
    price = normalized.get("mark") or normalized.get("last")
    if bid and ask and float(ask) >= float(bid):
        price = (float(bid) + float(ask)) / 2
    return {
        "symbol": normalized["symbol"],
        "observation_type": "levelone_quote",
        "source": "schwab_stream",
        "event_ts": (event_ms or received_ms) // 1000,
        "received_ts": received_ms // 1000,
        "price": price,
        "bid": bid,
        "ask": ask,
        "volume": normalized.get("total_volume"),
        "payload": normalized,
    }


def _durable_book_observation(normalized: dict) -> dict:
    bids = normalized.get("bids") or []
    asks = normalized.get("asks") or []
    bid = bids[0].get("price") if bids else None
    ask = asks[0].get("price") if asks else None
    price = None
    if bid and ask and float(ask) >= float(bid):
        price = (float(bid) + float(ask)) / 2
    event_ms = int(normalized.get("market_time_ms") or 0)
    received_ms = int(normalized.get("received_at_ms") or 0)
    return {
        "symbol": normalized["symbol"],
        "observation_type": "level2_book",
        "source": "schwab_stream",
        "event_ts": (event_ms or received_ms) // 1000,
        "received_ts": received_ms // 1000,
        "price": price,
        "bid": bid,
        "ask": ask,
        "payload": normalized,
    }


def _sample_due(
    last_sample_ms: dict[tuple[str, str], int],
    observation_type: str,
    symbol: str,
    received_at_ms: int,
    interval_ms: int,
) -> bool:
    key = (observation_type, symbol)
    previous = last_sample_ms.get(key, 0)
    if received_at_ms - previous < interval_ms:
        return False
    last_sample_ms[key] = received_at_ms
    return True


def _reconcile_order_intent(
    intent: dict, *, access_token: str, preferences: UserPreferences
) -> dict:
    order_client = SchwabOrderClient(preferences, access_token=access_token)
    raw_order = order_client.get_order(
        intent["account_id"], int(intent["broker_order_id"])
    )
    return record_order_snapshot(normalize_schwab_order(raw_order))


def _recover_unidentified_order_intent(
    intent: dict, *, access_token: str, preferences: UserPreferences
) -> dict:
    order_client = SchwabOrderClient(preferences, access_token=access_token)
    from_time, to_time = ambiguous_order_query_window(intent)
    raw_orders = order_client.get_orders(
        intent["account_id"], from_time, to_time, max_results=100
    )
    matched = match_ambiguous_order(intent, raw_orders)
    if matched is None:
        return {"matched": False, "reason": "no_unique_exact_match"}
    snapshot = normalize_schwab_order(matched)
    attached = attach_broker_order_identity(
        intent["idempotency_key"], broker_order_id=snapshot["broker_order_id"]
    )
    if attached is None:
        return {"matched": False, "reason": "broker_identity_already_claimed"}
    return record_order_snapshot(snapshot)


async def _run_authenticated_stream(access_token: str) -> None:
    await asyncio.to_thread(persistent_store.ensure_schema)
    preferences = await asyncio.to_thread(UserPreferences, access_token)
    client = SchwabWebSocketClient(preferences, get_market_data_access_token)
    stream_state = {
        "last_event_key": None,
        "symbols": [],
        "capture_symbols": set(),
        "last_market_ms": {},
        "market_updates": 0,
        "market_observations": 0,
        "market_observation_errors": 0,
        "market_observation_dropped": 0,
        "orders_reconciled": 0,
        "ambiguous_orders_recovered": 0,
        "order_reconcile_errors": 0,
        "last_order_reconcile_ms": None,
        "subscription_errors": {},
        "last_auth_check_ms": None,
        "auth_refresh_ok": True,
        "auth_error": None,
        "symbol_refresh_errors": 0,
        "last_symbol_refresh_ms": None,
        "last_symbol_refresh_error": None,
    }
    observation_queue: asyncio.Queue[dict] = asyncio.Queue(
        maxsize=OBSERVATION_QUEUE_SIZE
    )
    last_sample_ms: dict[tuple[str, str], int] = {}

    async def pulse() -> None:
        while True:
            _heartbeat(
                "degraded" if (
                    stream_state["subscription_errors"]
                    or stream_state["last_symbol_refresh_error"]
                ) else "connected",
                last_event_key=stream_state["last_event_key"],
                symbols=stream_state["symbols"],
                symbol_count=len(stream_state["symbols"]),
                priority_capture_symbols=sorted(stream_state["capture_symbols"]),
                priority_observation_interval_ms=PRIORITY_OBSERVATION_INTERVAL_MS,
                quote_observation_interval_ms=QUOTE_OBSERVATION_INTERVAL_MS,
                book_observation_interval_ms=BOOK_OBSERVATION_INTERVAL_MS,
                market_updates=stream_state["market_updates"],
                market_observations=stream_state["market_observations"],
                market_observation_errors=stream_state["market_observation_errors"],
                market_observation_dropped=stream_state["market_observation_dropped"],
                market_observation_queue_depth=observation_queue.qsize(),
                last_market_ms=stream_state["last_market_ms"],
                orders_reconciled=stream_state["orders_reconciled"],
                ambiguous_orders_recovered=stream_state["ambiguous_orders_recovered"],
                order_reconcile_errors=stream_state["order_reconcile_errors"],
                last_order_reconcile_ms=stream_state["last_order_reconcile_ms"],
                subscription_errors=list(stream_state["subscription_errors"].values()),
                last_auth_check_ms=stream_state["last_auth_check_ms"],
                auth_refresh_ok=stream_state["auth_refresh_ok"],
                auth_error=stream_state["auth_error"],
                symbol_refresh_errors=stream_state["symbol_refresh_errors"],
                last_symbol_refresh_ms=stream_state["last_symbol_refresh_ms"],
                last_symbol_refresh_error=stream_state["last_symbol_refresh_error"],
            )
            await asyncio.sleep(5)

    async def persist_observations() -> None:
        while True:
            first = await observation_queue.get()
            batch = [first]
            deadline = asyncio.get_running_loop().time() + OBSERVATION_FLUSH_SECONDS
            while len(batch) < OBSERVATION_BATCH_SIZE:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    batch.append(
                        await asyncio.wait_for(observation_queue.get(), timeout=remaining)
                    )
                except asyncio.TimeoutError:
                    break
            try:
                stored = await asyncio.to_thread(
                    persistent_store.record_market_observations,
                    batch,
                    ensure=False,
                )
                stream_state["market_observations"] += stored
            except Exception:
                stream_state["market_observation_errors"] += len(batch)
                logger.exception(
                    "durable market observation batch write failed for %s records",
                    len(batch),
                )
            finally:
                for _ in batch:
                    observation_queue.task_done()

    def enqueue_observation(observation: dict) -> None:
        try:
            observation_queue.put_nowait(observation)
        except asyncio.QueueFull:
            stream_state["market_observation_dropped"] += 1
            logger.error(
                "durable market observation queue full; dropped %s for %s",
                observation.get("observation_type"),
                observation.get("symbol"),
            )

    async def maintain_auth() -> None:
        while True:
            await asyncio.sleep(AUTH_KEEPALIVE_SECONDS)
            try:
                await asyncio.to_thread(get_market_data_access_token)
                stream_state["auth_refresh_ok"] = True
                stream_state["auth_error"] = None
            except Exception as exc:
                # A transient outage must not delete locally retained credentials.
                stream_state["auth_refresh_ok"] = False
                stream_state["auth_error"] = str(exc)
                logger.warning("Schwab background authorization check failed: %s", exc)
            finally:
                stream_state["last_auth_check_ms"] = int(time.time() * 1000)

    async def subscribe_market(symbols: list[str]) -> None:
        if not symbols:
            previous = stream_state["symbols"]
            if previous:
                await client.unsubscribe("LEVELONE_EQUITIES", previous)
                await client.unsubscribe("CHART_EQUITY", previous)
                if LEVEL2_ENABLED:
                    await client.unsubscribe("NASDAQ_BOOK", previous)
                    await client.unsubscribe("NYSE_BOOK", previous)
            stream_state["symbols"] = []
            return
        await client.subscribe_levelone_equities(symbols, LEVELONE_FIELDS)
        await client.subscribe_chart_equity(symbols, CHART_FIELDS)
        if LEVEL2_ENABLED:
            await client.subscribe_nasdaq_book(symbols)
            await client.subscribe_nyse_book(symbols)
        stream_state["symbols"] = symbols

    async def reconcile_orders() -> None:
        while True:
            await asyncio.sleep(ORDER_RECONCILE_SECONDS)
            try:
                intents = await asyncio.to_thread(list_reconcilable_order_intents)
                unidentified = await asyncio.to_thread(list_unidentified_order_intents)
                if intents or unidentified:
                    current_token = await asyncio.to_thread(
                        get_market_data_access_token
                    )
                    for intent in unidentified:
                        try:
                            result = await asyncio.to_thread(
                                _recover_unidentified_order_intent,
                                intent,
                                access_token=current_token,
                                preferences=preferences,
                            )
                            if result.get("matched"):
                                stream_state["ambiguous_orders_recovered"] += 1
                        except Exception:
                            stream_state["order_reconcile_errors"] += 1
                            logger.exception(
                                "ambiguous order recovery failed for intent %s",
                                intent.get("idempotency_key"),
                            )
                    for intent in intents:
                        try:
                            await asyncio.to_thread(
                                _reconcile_order_intent,
                                intent,
                                access_token=current_token,
                                preferences=preferences,
                            )
                            stream_state["orders_reconciled"] += 1
                        except Exception:
                            stream_state["order_reconcile_errors"] += 1
                            logger.exception(
                                "periodic order reconciliation failed for broker order %s",
                                intent.get("broker_order_id"),
                            )
                stream_state["last_order_reconcile_ms"] = int(time.time() * 1000)
            except Exception:
                stream_state["order_reconcile_errors"] += 1
                logger.exception("periodic order reconciliation cycle failed")

    async def on_data(service: str, row: dict) -> None:
        if service not in {"ACCT_ACTIVITY", "ACCOUNT_ACTIVITY"}:
            received_at_ms = int(time.time() * 1000)
            normalized = _store_market_update(
                service, row, received_at_ms=received_at_ms
            )
            if normalized:
                stream_state["market_updates"] += 1
                stream_state["last_market_ms"][service] = received_at_ms
                if service == "CHART_EQUITY" and normalized.get("chart_time_ms"):
                    enqueue_observation(_durable_chart_observation(normalized))
                elif service == "LEVELONE_EQUITIES" and _sample_due(
                    last_sample_ms,
                    "levelone_quote",
                    normalized.get("symbol") or "",
                    received_at_ms,
                    _observation_interval("levelone_quote", normalized.get("symbol") or "", stream_state["capture_symbols"]),
                ):
                    enqueue_observation(_durable_levelone_observation(normalized))
                elif service in {"NASDAQ_BOOK", "NYSE_BOOK"}:
                    symbol = normalized.get("symbol") or ""
                    levelone_raw = r.hget(LEVELONE_KEY, symbol)
                    nasdaq_raw = r.hget(NASDAQ_BOOK_KEY, symbol)
                    nyse_raw = r.hget(NYSE_BOOK_KEY, symbol)
                    try:
                        levelone = json.loads(levelone_raw) if levelone_raw else None
                        nasdaq = json.loads(nasdaq_raw) if nasdaq_raw else None
                        nyse = json.loads(nyse_raw) if nyse_raw else None
                    except (TypeError, json.JSONDecodeError):
                        levelone = nasdaq = nyse = None
                    selected = select_listing_book(levelone, nasdaq, nyse)
                    if (
                        selected
                        and selected.get("service") == service
                        and int(selected.get("market_time_ms") or 0)
                        == int(normalized.get("market_time_ms") or 0)
                        and _sample_due(
                            last_sample_ms,
                            "level2_book",
                            symbol,
                            received_at_ms,
                            _observation_interval("level2_book", symbol, stream_state["capture_symbols"]),
                        )
                    ):
                        enqueue_observation(_durable_book_observation(selected))
            return
        activity = normalize_account_activity(row)
        result = await asyncio.to_thread(record_account_activity, activity)
        stream_state["last_event_key"] = result["event_key"]
        if activity.get("broker_order_id"):
            intent = await asyncio.to_thread(
                get_order_intent_by_broker_id,
                activity["broker_order_id"],
            )
            if intent:
                current_token = await asyncio.to_thread(get_market_data_access_token)
                order_client = SchwabOrderClient(preferences, access_token=current_token)
                raw_order = await asyncio.to_thread(
                    order_client.get_order,
                    intent["account_id"],
                    activity["broker_order_id"],
                )
                snapshot = normalize_schwab_order(raw_order)
                await asyncio.to_thread(record_order_snapshot, snapshot)
        r.publish(ACTIVITY_CHANNEL, json.dumps({
            "service": service,
            "data": row,
            "journal": result,
        }))

    async def on_response(response) -> None:
        errors = response_errors(response)
        for item in response if isinstance(response, list) else []:
            service = str(item.get("service") or "unknown")
            stream_state["subscription_errors"].pop(service, None)
        for error in errors:
            stream_state["subscription_errors"][str(error.get("service") or "unknown")] = error
        if errors:
            logger.warning("Schwab subscription errors: %s", errors)

    async def on_error(message: str) -> None:
        logger.warning("Schwab account stream error: %s", message)
        _heartbeat("error", error=message)

    await client.connect()
    await client.subscribe_acct_activity(fields="0,1,2,3")
    await subscribe_market(await asyncio.to_thread(_stream_symbols))
    _heartbeat("connected")
    pulse_task = asyncio.create_task(pulse())
    auth_task = asyncio.create_task(maintain_auth())
    refresh_task = asyncio.create_task(
        _refresh_stream_symbols(stream_state, subscribe_market)
    )
    reconcile_task = asyncio.create_task(reconcile_orders())
    persistence_task = asyncio.create_task(persist_observations())
    try:
        await client.run(on_data, on_response, on_error)
    finally:
        pulse_task.cancel()
        auth_task.cancel()
        refresh_task.cancel()
        reconcile_task.cancel()
        try:
            await asyncio.wait_for(observation_queue.join(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning(
                "market observation queue did not drain before stream shutdown: %s pending",
                observation_queue.qsize(),
            )
        persistence_task.cancel()


async def run_forever() -> None:
    while True:
        try:
            access_token = await asyncio.to_thread(get_market_data_access_token)
        except RuntimeError as exc:
            _heartbeat("idle", reason="schwab_auth_required", detail=str(exc))
            await asyncio.sleep(IDLE_SECONDS)
            continue
        try:
            await _run_authenticated_stream(access_token)
        except Exception as exc:
            logger.exception("Schwab account stream session failed")
            _heartbeat("error", error=str(exc))
            await asyncio.sleep(IDLE_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    asyncio.run(run_forever())
