from __future__ import annotations

import hashlib
import json
import math
import numbers
import os
import time
from typing import Any

try:
    import redis
except Exception:
    redis = None

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None


REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DATABASE_URL = os.getenv("DATABASE_URL")
FEATURE_STORE_ZSET = os.getenv("FEATURE_STORE_ZSET", "features:history")
FEATURE_STORE_HASH_PREFIX = os.getenv("FEATURE_STORE_HASH_PREFIX", "features:item:")
FEATURE_STORE_MAX = int(os.getenv("FEATURE_STORE_MAX", "50000"))

r = redis.from_url(REDIS_URL, decode_responses=True) if redis is not None else None


DDL = """
CREATE TABLE IF NOT EXISTS feature_store (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    ts BIGINT NOT NULL,
    source TEXT NOT NULL,
    price DOUBLE PRECISION,
    action TEXT,
    reward DOUBLE PRECISION,
    semantic_signal JSONB NOT NULL,
    market_signal JSONB NOT NULL,
    stop_loss JSONB NOT NULL,
    committee JSONB,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_feature_store_symbol_ts
    ON feature_store (symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_feature_store_ts
    ON feature_store (ts DESC);
"""


def _now_ts() -> int:
    return int(time.time())


def _finite_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _finite_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_json(item) for item in value]
    if isinstance(value, numbers.Real) and not math.isfinite(float(value)):
        return None
    return value


def _pg_enabled() -> bool:
    return bool(DATABASE_URL and psycopg is not None)


def _pg_json(value: Any):
    if psycopg is None:
        return value
    return psycopg.types.json.Jsonb(value)


def _ensure_pg_schema() -> None:
    if not _pg_enabled():
        return
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(DDL)


def _feature_id(symbol: str, ts: int, source: str, payload: dict[str, Any]) -> str:
    identity = {
        "symbol": symbol.upper(),
        "ts": ts,
        "source": source,
        "price": (payload.get("market_signal") or {}).get("price"),
        "action": payload.get("action"),
    }
    if payload.get("snapshot_identity"):
        identity["snapshot_identity"] = payload["snapshot_identity"]
    raw = json.dumps(identity, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _record_pg(item: dict[str, Any]) -> None:
    _ensure_pg_schema()
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            """
            INSERT INTO feature_store (
                id, symbol, ts, source, price, action, reward,
                semantic_signal, market_signal, stop_loss, committee, payload
            )
            VALUES (
                %(id)s, %(symbol)s, %(ts)s, %(source)s, %(price)s, %(action)s, %(reward)s,
                %(semantic_signal)s, %(market_signal)s, %(stop_loss)s, %(committee)s, %(payload)s
            )
            ON CONFLICT (id) DO UPDATE SET
                price = EXCLUDED.price,
                action = EXCLUDED.action,
                reward = EXCLUDED.reward,
                semantic_signal = EXCLUDED.semantic_signal,
                market_signal = EXCLUDED.market_signal,
                stop_loss = EXCLUDED.stop_loss,
                committee = EXCLUDED.committee,
                payload = EXCLUDED.payload
            """,
            {
                **item,
                "semantic_signal": _pg_json(item.get("semantic_signal") or {}),
                "market_signal": _pg_json(item.get("market_signal") or {}),
                "stop_loss": _pg_json(item.get("stop_loss") or {}),
                "committee": _pg_json(item.get("committee") or {}),
                "payload": _pg_json(item.get("payload") or {}),
            },
        )


def _list_pg(symbol: str | None, limit: int, offset: int) -> list[dict[str, Any]]:
    _ensure_pg_schema()
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    where = ""
    if symbol:
        where = "WHERE symbol = %(symbol)s"
        params["symbol"] = symbol.upper()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            f"""
            SELECT id, symbol, ts, source, price, action, reward,
                   semantic_signal, market_signal, stop_loss, committee, payload, created_at
            FROM feature_store
            {where}
            ORDER BY ts DESC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def record_feature_snapshot(
    symbol: str,
    payload: dict[str, Any],
    *,
    source: str = "agent",
    ts: int | None = None,
) -> dict[str, Any]:
    payload = _finite_json(payload)
    ts = int(ts or payload.get("ts") or _now_ts())
    item = {
        "id": _feature_id(symbol, ts, source, payload),
        "symbol": symbol.upper(),
        "ts": ts,
        "source": source,
        "price": (payload.get("market_signal") or {}).get("price"),
        "action": payload.get("action"),
        "reward": payload.get("reward"),
        "semantic_signal": payload.get("semantic_signal") or {},
        "market_signal": payload.get("market_signal") or {},
        "stop_loss": payload.get("stop_loss") or {},
        "committee": payload.get("committee") or {},
        "payload": payload,
    }

    try:
        if _pg_enabled():
            _record_pg(item)
            return item
    except Exception as exc:
        print("[feature_store] postgres write failed:", exc)

    if r is None:
        return item
    pipe = r.pipeline()
    pipe.hset(f"{FEATURE_STORE_HASH_PREFIX}{item['id']}", mapping={"json": json.dumps(item)})
    pipe.zadd(FEATURE_STORE_ZSET, {item["id"]: ts})
    pipe.zremrangebyrank(FEATURE_STORE_ZSET, 0, -(FEATURE_STORE_MAX + 1))
    pipe.execute()
    return item


def list_feature_snapshots(
    *,
    symbol: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    try:
        if _pg_enabled():
            return _list_pg(symbol, limit, offset)
    except Exception as exc:
        print("[feature_store] postgres read failed:", exc)

    if r is None:
        return []
    ids = r.zrevrange(FEATURE_STORE_ZSET, offset, offset + limit * 3 - 1)
    normalized = symbol.upper() if symbol else None
    out: list[dict[str, Any]] = []
    for row_id in ids:
        raw = r.hget(f"{FEATURE_STORE_HASH_PREFIX}{row_id}", "json")
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if normalized and item.get("symbol") != normalized:
            continue
        out.append(item)
        if len(out) >= limit:
            break
    return out
