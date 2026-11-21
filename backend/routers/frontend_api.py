import json
import os
import time
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from routers.auth import require_access_token
from routers.scanners_api import agent_recommendation
from routers.rag_api import rag_query, RagQuery
from routers.portfolio import summary as portfolio_summary, price_history as portfolio_price_history
from routers.agent_api import recent_actions

router = APIRouter(prefix="/frontend", tags=["frontend"])
_DASH_CACHE = {}
DASH_CACHE_TTL = int(os.getenv("FRONTEND_DASH_CACHE_TTL", "30"))

def _unwrap_response(resp):
    if isinstance(resp, JSONResponse):
        try:
            return json.loads(resp.body.decode())
        except Exception:
            return resp.body.decode()
    return resp

def _portfolio_contract(portfolio_payload):
    portfolio = portfolio_payload if isinstance(portfolio_payload, dict) else {}
    return {
        "portfolio": portfolio,
        "account": portfolio.get("account") or {},
        "account_meta": portfolio.get("account_meta") or {},
        "capabilities": portfolio.get("capabilities") or {},
        "positions": portfolio.get("positions") or [],
        "fills": portfolio.get("fills") or [],
        "price_buckets": portfolio.get("price_buckets") or [],
        "recent_trades": portfolio.get("recent_trades") or [],
    }

@router.get("/dashboard")
async def dashboard(
    symbol: str = "AAPL",
    rag_limit: int = 5,
    access_token: str = Depends(require_access_token),
):
    """
    Aggregate agent recommendation, RAG hits, account summary, positions, and candles for a symbol.
    Uses authenticated portfolio endpoints for account/positions/candles.
    """
    now = time.time()
    cache_entry = _DASH_CACHE.get(symbol)
    if cache_entry:
        ts, cached_payload = cache_entry
        if (now - ts) <= DASH_CACHE_TTL:
            # always refresh account since it's token-bound and fast
            portfolio = _unwrap_response(await portfolio_summary(access_token=access_token, request=None))
            cached_payload.update(_portfolio_contract(portfolio))
            return JSONResponse(cached_payload)

    rec_resp = agent_recommendation(symbol)
    rec = _unwrap_response(rec_resp)

    rag = await rag_query(RagQuery(question=f"Latest news on {symbol}?", tickers=[symbol], top_k=rag_limit))

    portfolio = _unwrap_response(await portfolio_summary(access_token=access_token, request=None))  # request unused in summary
    candles = await portfolio_price_history(
        request=None,
        symbol=symbol,
        period_type="day",
        period=10,
        frequency_type="minute",
        frequency=5,
        access_token=access_token,
    )
    candles = _unwrap_response(candles)

    actions = _unwrap_response(recent_actions())

    payload = {
        "symbol": symbol,
        "recommendation": rec,
        "rag": rag,
        "candles": candles,
        "recent_actions": actions,
        **_portfolio_contract(portfolio),
    }
    _DASH_CACHE[symbol] = (now, payload.copy())
    return JSONResponse(payload)
