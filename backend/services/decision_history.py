import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional

import redis
try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None


REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DATABASE_URL = os.getenv("DATABASE_URL")
DECISION_HISTORY_ZSET = os.getenv("DECISION_HISTORY_ZSET", "decisions:history")
DECISION_HISTORY_HASH_PREFIX = os.getenv("DECISION_HISTORY_HASH_PREFIX", "decisions:item:")
DECISION_HISTORY_MAX = int(os.getenv("DECISION_HISTORY_MAX", "10000"))

r = redis.from_url(REDIS_URL, decode_responses=True)


DDL = """
CREATE TABLE IF NOT EXISTS decision_history (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    ts BIGINT NOT NULL,
    source TEXT NOT NULL,
    watchlist TEXT,
    action TEXT,
    confidence DOUBLE PRECISION,
    score DOUBLE PRECISION,
    catalyst JSONB,
    sentiment JSONB,
    technicals JSONB,
    intraday JSONB,
    trade_plan JSONB,
    reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_decision_history_symbol_ts
    ON decision_history (symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_decision_history_ts
    ON decision_history (ts DESC);
"""


def _now_ts() -> int:
    return int(time.time())


def _decision_id(symbol: str, ts: int, source: str, payload: Dict[str, Any]) -> str:
    raw = json.dumps(
        {
            "symbol": symbol.upper(),
            "ts": ts,
            "source": source,
            "action": payload.get("action"),
            "price": (payload.get("technicals") or {}).get("last"),
        },
        sort_keys=True,
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


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


def _record_pg(item: Dict[str, Any]) -> None:
    _ensure_pg_schema()
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            """
            INSERT INTO decision_history (
                id, symbol, ts, source, watchlist, action, confidence, score,
                catalyst, sentiment, technicals, intraday, trade_plan, reasons, payload
            )
            VALUES (
                %(id)s, %(symbol)s, %(ts)s, %(source)s, %(watchlist)s, %(action)s,
                %(confidence)s, %(score)s, %(catalyst)s, %(sentiment)s, %(technicals)s,
                %(intraday)s, %(trade_plan)s, %(reasons)s, %(payload)s
            )
            ON CONFLICT (id) DO UPDATE SET
                confidence = EXCLUDED.confidence,
                score = EXCLUDED.score,
                catalyst = EXCLUDED.catalyst,
                sentiment = EXCLUDED.sentiment,
                technicals = EXCLUDED.technicals,
                intraday = EXCLUDED.intraday,
                trade_plan = EXCLUDED.trade_plan,
                reasons = EXCLUDED.reasons,
                payload = EXCLUDED.payload
            """,
            {
                **item,
                "catalyst": _pg_json(item.get("catalyst")),
                "sentiment": _pg_json(item.get("sentiment")),
                "technicals": _pg_json(item.get("technicals")),
                "intraday": _pg_json(item.get("intraday")),
                "trade_plan": _pg_json(item.get("trade_plan")),
                "reasons": _pg_json(item.get("reasons") or []),
                "payload": _pg_json(item.get("payload") or {}),
            },
        )


def _list_pg(symbol: Optional[str], limit: int, offset: int) -> List[Dict[str, Any]]:
    _ensure_pg_schema()
    params: Dict[str, Any] = {"limit": limit, "offset": offset}
    where = ""
    if symbol:
        where = "WHERE symbol = %(symbol)s"
        params["symbol"] = symbol.upper()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            f"""
            SELECT id, symbol, ts, source, watchlist, action, confidence, score,
                   catalyst, sentiment, technicals, intraday, trade_plan, reasons, payload, created_at
            FROM decision_history
            {where}
            ORDER BY ts DESC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def record_decision(
    symbol: str,
    payload: Dict[str, Any],
    *,
    source: str = "scanner",
    watchlist: Optional[str] = None,
    ts: Optional[int] = None,
) -> Dict[str, Any]:
    ts = int(ts or payload.get("ts") or _now_ts())
    decision_id = _decision_id(symbol, ts, source, payload)
    item = {
        "id": decision_id,
        "symbol": symbol.upper(),
        "ts": ts,
        "source": source,
        "watchlist": watchlist,
        "action": payload.get("action"),
        "confidence": payload.get("confidence"),
        "score": payload.get("score"),
        "catalyst": payload.get("catalyst"),
        "sentiment": payload.get("sentiment"),
        "technicals": payload.get("technicals"),
        "intraday": payload.get("intraday"),
        "trade_plan": payload.get("trade_plan"),
        "reasons": payload.get("reasons") or [],
        "payload": payload,
    }

    try:
        if _pg_enabled():
            _record_pg(item)
            return item
    except Exception as exc:
        print("[decision_history] postgres write failed:", exc)

    pipe = r.pipeline()
    pipe.hset(f"{DECISION_HISTORY_HASH_PREFIX}{decision_id}", mapping={"json": json.dumps(item)})
    pipe.zadd(DECISION_HISTORY_ZSET, {decision_id: ts})
    pipe.zremrangebyrank(DECISION_HISTORY_ZSET, 0, -(DECISION_HISTORY_MAX + 1))
    pipe.execute()
    return item


def list_decisions(
    *,
    symbol: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    try:
        if _pg_enabled():
            return _list_pg(symbol, limit, offset)
    except Exception as exc:
        print("[decision_history] postgres read failed:", exc)

    ids = r.zrevrange(DECISION_HISTORY_ZSET, offset, offset + limit * 3 - 1)
    out: List[Dict[str, Any]] = []
    normalized = symbol.upper() if symbol else None
    for decision_id in ids:
        raw = r.hget(f"{DECISION_HISTORY_HASH_PREFIX}{decision_id}", "json")
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
