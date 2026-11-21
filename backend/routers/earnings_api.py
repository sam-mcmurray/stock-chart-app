import json
import os

import redis
from fastapi import APIRouter, Depends, Query

from routers.auth import require_access_token
from services import persistent_store
from workers.yahoo_earnings_worker import (
    K_COMPLETE,
    K_CURSOR,
    K_HEARTBEAT,
    K_OFFSET,
    REDIS_URL,
)


router = APIRouter(prefix="/earnings", tags=["earnings"])
r = redis.from_url(REDIS_URL, decode_responses=True)


@router.get("/status")
def earnings_status(_access_token: str = Depends(require_access_token)):
    try:
        heartbeat = json.loads(r.get(K_HEARTBEAT) or "{}")
    except (TypeError, ValueError):
        heartbeat = {}
    return {
        "heartbeat": heartbeat,
        "cursor": r.get(K_CURSOR),
        "offset": int(r.get(K_OFFSET) or 0),
        "complete": r.get(K_COMPLETE) == "1",
        "counts": persistent_store.earnings_event_counts(),
    }


@router.get("/events")
def earnings_events(
    symbol: str | None = Query(None, pattern=r"^[A-Za-z0-9.\-]{1,20}$"),
    start_date: str | None = Query(None, pattern=r"^20\d{2}-\d{2}-\d{2}$"),
    end_date: str | None = Query(None, pattern=r"^20\d{2}-\d{2}-\d{2}$"),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    _access_token: str = Depends(require_access_token),
):
    rows = persistent_store.list_earnings_events(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset,
    )
    return {"events": rows, "limit": limit, "offset": offset}
