import json
import os

import redis.asyncio as async_redis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from auth.session_store import load_tokens
from routers.auth import SESSION_COOKIE_NAME, r


router = APIRouter()
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
ACCOUNT_ACTIVITY_CHANNEL = os.getenv("SCHWAB_ACCOUNT_ACTIVITY_CHANNEL", "schwab:account_activity")


@router.websocket("/ws/acct")
async def ws_acct_activity(websocket: WebSocket):
    """Relay the shared background Schwab account stream to an authenticated browser."""
    sid = websocket.cookies.get(SESSION_COOKIE_NAME)
    if not sid or not load_tokens(r, sid):
        await websocket.close(code=4401)
        return
    await websocket.accept()

    redis_client = async_redis.from_url(REDIS_URL, decode_responses=True)
    pubsub = redis_client.pubsub()
    try:
        await pubsub.subscribe(ACCOUNT_ACTIVITY_CHANNEL)
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            try:
                payload = json.loads(message.get("data") or "{}")
            except json.JSONDecodeError:
                continue
            await websocket.send_json(payload)
    except WebSocketDisconnect:
        pass
    finally:
        await pubsub.unsubscribe(ACCOUNT_ACTIVITY_CHANNEL)
        await pubsub.close()
        await redis_client.close()
