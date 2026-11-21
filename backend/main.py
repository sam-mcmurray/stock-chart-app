import asyncio
import logging
import os
import time
import uuid
from threading import Event

_IMPORT_STARTED_AT = time.perf_counter()

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from services import persistent_store
from services.gdelt_event_queue import consume_forever as consume_gdelt_events
from services.news_index_queue import consume_forever as consume_news_index
from services.news_cleanup_queue import consume_forever as consume_news_cleanup
from services.queue_supervisor import run_resilient_consumer
from workers.news_worker import NewsWorker
from workers.scanner_worker import ScannerWorker
from workers.stop_loss_worker import StopLossWorker
from routers.scanners_api import router as scanner_router
from routers import auth as auth_router
from routers import rag_api as rag_router
from routers import agent_api as agent_router
from routers import calendar_api as calendar_router
from routers import frontend_api as frontend_router
from routers import portfolio as portfolio_router
from routers import ws_stream as ws_router
from routers import news_llm_api as news_llm_router
from routers import earnings_api as earnings_router
from routers import sentiment_api as sentiment_router
from routers import evaluation_api as evaluation_router

logger = logging.getLogger(__name__)
logger.info("API startup: module imports completed in %.3fs", time.perf_counter() - _IMPORT_STARTED_AT)

def _request_id(request: Request) -> str:
    existing = getattr(request.state, "request_id", None)
    if existing:
        return existing
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    return request_id

def _cors_origins() -> list[str]:
    raw = os.getenv("CORS_ALLOW_ORIGINS")
    if raw:
        return [origin.strip() for origin in raw.split(",") if origin.strip()]
    frontend_url = os.getenv("FRONTEND_URL")
    defaults = ["https://localhost:3000", "http://localhost:3000"]
    if frontend_url:
        normalized = frontend_url.rstrip("/")
        if normalized not in defaults:
            defaults.append(normalized)
    return defaults

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(scanner_router)
app.include_router(rag_router.router)
app.include_router(agent_router.router)
app.include_router(calendar_router.router)
app.include_router(frontend_router.router)
app.include_router(portfolio_router.router)
app.include_router(ws_router.router)
app.include_router(news_llm_router.router)
app.include_router(earnings_router.router)
app.include_router(sentiment_router.router)
app.include_router(evaluation_router.router)

@app.middleware("http")
async def request_observability(request: Request, call_next):
    request_id = _request_id(request)
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        auth_router.touch_session(request, response)
        response.headers["X-Request-ID"] = request_id
        return response
    except Exception:
        logger.exception(
            "request failed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
            },
        )
        raise
    finally:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "request completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": status_code,
                "duration_ms": duration_ms,
            },
        )

@app.middleware("http")
async def csrf_protect(request: Request, call_next):
    request_id = _request_id(request)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.cookies.get(auth_router.SESSION_COOKIE_NAME):
        try:
            auth_router.require_csrf(request)
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        else:
            response = await call_next(request)
    else:
        response = await call_next(request)
    if "X-Request-ID" not in response.headers:
        response.headers["X-Request-ID"] = request_id
    return response

# --- background tasks
_news_task = None
_scan_task = None
_stop_loss_task = None
_news_worker = None
_scan_worker = None
_stop_loss_worker = None
_gdelt_event_task = None
_gdelt_event_stop = Event()
_news_index_task = None
_news_index_stop = Event()
_news_cleanup_task = None
_news_cleanup_stop = Event()

async def _ensure_durable_schema():
    if not persistent_store.enabled():
        return
    attempts = max(1, int(os.getenv("POSTGRES_SCHEMA_INIT_ATTEMPTS", "10")))
    delay = max(0.1, float(os.getenv("POSTGRES_SCHEMA_INIT_DELAY_SECONDS", "2")))
    for attempt in range(1, attempts + 1):
        try:
            persistent_store.ensure_schema()
            return
        except Exception:
            if attempt >= attempts:
                logger.exception("durable PostgreSQL schema initialization failed")
                raise
            logger.warning(
                "durable PostgreSQL schema initialization failed; retrying",
                extra={"attempt": attempt, "attempts": attempts},
            )
            await asyncio.sleep(delay)

@app.on_event("startup")
async def start_workers():
    global _news_task, _scan_task, _stop_loss_task, _news_worker, _scan_worker, _stop_loss_worker, _gdelt_event_task, _news_index_task, _news_cleanup_task
    phase_started = time.perf_counter()
    logger.info("API startup: checking database schema")
    await _ensure_durable_schema()
    logger.info("API startup: database schema ready in %.3fs", time.perf_counter() - phase_started)

    gdelt_consumer_enabled = os.getenv(
        "GDELT_EVENT_QUEUE_CONSUMER_ENABLED", "true"
    ).lower() == "true"
    news_index_consumer_enabled = os.getenv(
        "NEWS_INDEX_QUEUE_CONSUMER_ENABLED", "true"
    ).lower() == "true"
    if gdelt_consumer_enabled or news_index_consumer_enabled:
        phase_started = time.perf_counter()
        logger.info("API startup: checking Qdrant collections")
        rag_router.initialize_vector_store()
        logger.info("API startup: Qdrant collections ready in %.3fs", time.perf_counter() - phase_started)

    loop = asyncio.get_running_loop()

    if gdelt_consumer_enabled:
        _gdelt_event_stop.clear()
        _gdelt_event_task = loop.run_in_executor(
            None,
            run_resilient_consumer,
            consume_gdelt_events,
            rag_router.index_gdelt_events,
            _gdelt_event_stop,
            "GDELT Event index",
        )

    if news_index_consumer_enabled:
        _news_index_stop.clear()
        _news_index_task = loop.run_in_executor(
            None,
            run_resilient_consumer,
            consume_news_index,
            rag_router.index_news_articles,
            _news_index_stop,
            "news index",
        )

    if os.getenv("NEWS_CLEANUP_QUEUE_CONSUMER_ENABLED", "true").lower() == "true":
        _news_cleanup_stop.clear()
        _news_cleanup_task = loop.run_in_executor(
            None,
            run_resilient_consumer,
            consume_news_cleanup,
            rag_router.delete_news_articles,
            _news_cleanup_stop,
            "news cleanup",
        )

    if os.getenv("RUN_API_EMBEDDED_WORKERS", "false").lower() != "true":
        return
    _news_worker = NewsWorker()
    _scan_worker = ScannerWorker()
    if os.getenv("AGENT_STOP_LOSS_MONITOR_ENABLED", "false").lower() == "true":
        _stop_loss_worker = StopLossWorker()

    loop = asyncio.get_running_loop()
    _news_task = loop.run_in_executor(None, _news_worker.run_forever)
    _scan_task = loop.run_in_executor(None, _scan_worker.run_forever)
    if _stop_loss_worker:
        _stop_loss_task = loop.run_in_executor(None, _stop_loss_worker.run_forever)

@app.on_event("shutdown")
async def stop_workers():
    _gdelt_event_stop.set()
    _news_index_stop.set()
    _news_cleanup_stop.set()
    if os.getenv("RUN_API_EMBEDDED_WORKERS", "false").lower() != "true":
        return
    if _news_worker: _news_worker.stop()
    if _scan_worker: _scan_worker.stop()
    if _stop_loss_worker: _stop_loss_worker.stop()
    # allow a moment to exit
    await asyncio.sleep(0.2)

@app.get("/healthz")
def healthz():
    return {"ok": True}
