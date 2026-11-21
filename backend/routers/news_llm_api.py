import hashlib
import json
import logging
import os
import time
import uuid

import redis
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from routers.auth import require_access_token
from services import persistent_store
from services.calendar_relevance import classify_articles
from services.ollama_manager import (
    delete_model,
    delete_unselected_models,
    get_settings,
    list_models,
    pull_model,
    search_catalog,
    server_status,
    update_settings,
)
from services.calendar_relevance_queue import (
    FAILED_KEY,
    HEARTBEAT_KEY,
    REDIS_URL,
    WATCH_KEY,
    pending_counts,
    replay_failed,
)


router = APIRouter(prefix="/news/llm", tags=["news-llm"])
logger = logging.getLogger(__name__)
r = redis.from_url(REDIS_URL, decode_responses=True)
HISTORICAL_HEARTBEAT_KEY = "news:historical:llm:heartbeat"
HISTORICAL_HELPER_HEARTBEAT_KEY = "news:historical:llm:helper:heartbeat"
ENTITY_RELINK_HEARTBEAT_KEY = "news:entity-relink:heartbeat"
QUALITY_CACHE_KEY = "news:llm:quality:cache:v2"
QUALITY_REPORT_VERSION = "single-pass-v1"
QUALITY_REFRESH_LOCK_KEY = "news:llm:quality:refresh-lock:v1"
QUALITY_CACHE_FRESH_SECONDS = max(
    300, int(os.getenv("NEWS_LLM_QUALITY_CACHE_FRESH_SECONDS", "21600"))
)
QUALITY_CACHE_STALE_SECONDS = max(
    QUALITY_CACHE_FRESH_SECONDS,
    int(os.getenv("NEWS_LLM_QUALITY_CACHE_STALE_SECONDS", "86400")),
)
QUALITY_REFRESH_LOCK_SECONDS = max(
    60, int(os.getenv("NEWS_LLM_QUALITY_REFRESH_LOCK_SECONDS", "900"))
)
HISTORICAL_PARTITION_BOUNDARY_TS = max(
    0, int(os.getenv("NEWS_HISTORICAL_PARTITION_BOUNDARY_TS", "1541721600"))
)


def _nonnegative_number(value, *, integer: bool = False):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0
    number = max(0.0, number)
    return int(number) if integer else number


def _historical_operations(primary: dict, helper: dict) -> dict:
    workers = []
    for name, heartbeat in (("recent", primary), ("older", helper)):
        completed = _nonnegative_number(
            heartbeat.get("historical_completed", heartbeat.get("completed")),
            integer=True,
        )
        remaining = _nonnegative_number(
            heartbeat.get("historical_remaining"), integer=True
        )
        total = _nonnegative_number(heartbeat.get("historical_total"), integer=True)
        if total <= 0:
            total = completed + remaining
        rate = _nonnegative_number(
            heartbeat.get("historical_rate_per_minute", heartbeat.get("records_per_minute"))
        )
        eta = heartbeat.get("historical_eta_seconds")
        eta = _nonnegative_number(eta, integer=True) if eta is not None else None
        if eta is None and rate > 0:
            eta = round(remaining * 60 / rate)
        workers.append({
            "name": name,
            "completed": completed,
            "remaining": remaining,
            "total": total,
            "rate_per_minute": round(rate, 2),
            "eta_seconds": eta,
            "cursor_published_ts": _nonnegative_number(
                heartbeat.get("historical_cursor_published_ts"), integer=True
            ),
        })

    completed = sum(worker["completed"] for worker in workers)
    shared = [h for h in (primary, helper) if h.get("queue_mode") == "shared_newest_first"]
    if shared:
        latest = max(shared, key=lambda h: h.get("ts", 0))
        completed = _nonnegative_number(latest.get("shared_completed"), integer=True)
        remaining = _nonnegative_number(latest.get("historical_remaining"), integer=True)
        total = completed + remaining
        rate = sum(w["rate_per_minute"] for w, h in zip(workers, (primary, helper))
                   if h.get("queue_mode") == "shared_newest_first"
                   and h.get("status") not in {"complete", "waiting_for_model", "waiting_after_error"}
                   and int(time.time()) - int(h.get("ts") or 0) < 180)
        started = _nonnegative_number(latest.get("shared_tracking_started_at"), integer=True)
        baseline = latest.get("shared_tracking_baseline")
        elapsed = max(0, int(time.time()) - started) if started else 0
        measured = max(0, completed - int(baseline)) if baseline is not None and started else None
        sustained = measured * 60 / elapsed if measured is not None and elapsed >= 60 else None
        return {"queue_mode": "shared_newest_first", "completed": completed,
                "remaining": remaining, "total": total,
                "progress_percent": round(completed * 100 / total, 2) if total else 100.0,
                "rate_per_minute": round(rate, 2),
                "parallel_eta_seconds": None,
                "tracking_started_at": started or None,
                "tracking_elapsed_seconds": elapsed,
                "measured_completions": measured,
                "sustained_rate_per_minute": round(sustained, 2) if sustained is not None else None,
                "partition_boundary_ts": None, "remaining_is_scan_upper_bound": True,
                "workers": {worker["name"]: worker for worker in workers}}
    remaining = sum(worker["remaining"] for worker in workers)
    total = sum(worker["total"] for worker in workers) or completed + remaining
    pending_etas = [
        worker["eta_seconds"] for worker in workers
        if worker["remaining"] > 0 and worker["eta_seconds"] is not None
    ]
    eta_known = all(
        worker["remaining"] == 0 or worker["eta_seconds"] is not None
        for worker in workers
    )
    return {
        "completed": completed,
        "remaining": remaining,
        "total": total,
        "progress_percent": round(completed * 100 / total, 2) if total else 100.0,
        "rate_per_minute": round(sum(worker["rate_per_minute"] for worker in workers), 2),
        # Both partitions run concurrently, so the slower partition determines completion.
        "parallel_eta_seconds": max(pending_etas, default=0) if eta_known else None,
        "partition_boundary_ts": HISTORICAL_PARTITION_BOUNDARY_TS,
        "workers": {worker["name"]: worker for worker in workers},
    }


def _quality_cache_payload() -> dict | None:
    cached = r.get(QUALITY_CACHE_KEY)
    if not cached:
        return None
    try:
        payload = json.loads(cached)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _quality_cache_is_fresh(payload: dict, now: int) -> bool:
    generated_at = int((payload.get("cache") or {}).get("generated_at") or 0)
    # Accept the prior cache format until its short Redis TTL expires.
    return generated_at == 0 or now - generated_at <= QUALITY_CACHE_FRESH_SECONDS


def _quality_cache_response(payload: dict, **cache_updates) -> dict:
    return {
        **payload,
        "cache": {**(payload.get("cache") or {}), **cache_updates},
    }


def _release_quality_refresh_lock(token: str) -> None:
    try:
        r.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end",
            1,
            QUALITY_REFRESH_LOCK_KEY,
            token,
        )
    except redis.RedisError:
        pass


class ModelAction(BaseModel):
    model: str = Field(min_length=1, max_length=200)


class LlmSettingsUpdate(BaseModel):
    enabled: bool | None = None
    active_model: str | None = Field(default=None, min_length=1, max_length=200)


class HeadlineReviewFlag(BaseModel):
    article_id: str = Field(default="", max_length=200)
    url: str = Field(min_length=1, max_length=4000)
    original_title: str = Field(default="", max_length=1000)
    display_title: str = Field(default="", max_length=1000)
    reason: str = Field(default="Headline looks incorrect or malformed.", max_length=1000)


class LlmPreviewRequest(BaseModel):
    title: str = Field(min_length=1, max_length=1000)
    summary: str = Field(default="", max_length=5000)
    source: str = Field(default="manual preview", max_length=200)


class LlmSampleReview(BaseModel):
    article_url: str = Field(min_length=1, max_length=4000)
    category: str = Field(min_length=1, max_length=50)
    judgment: str = Field(min_length=1, max_length=20)
    note: str = Field(default="", max_length=1000)


@router.get("/status")
def llm_status(_access_token: str = Depends(require_access_token)):
    status = server_status(r)
    raw = r.get(HISTORICAL_HEARTBEAT_KEY)
    try:
        historical = json.loads(raw) if raw else {"status": "not_started", "completed": 0}
    except (TypeError, ValueError):
        historical = {"status": "unavailable", "completed": 0}
    status["historical_backfill"] = historical if isinstance(historical, dict) else {
        "status": "unavailable",
        "completed": 0,
    }
    raw_helper = r.get(HISTORICAL_HELPER_HEARTBEAT_KEY)
    try:
        helper = json.loads(raw_helper) if raw_helper else {"status": "not_started", "completed": 0}
    except (TypeError, ValueError):
        helper = {"status": "unavailable", "completed": 0}
    status["historical_helper"] = helper if isinstance(helper, dict) else {
        "status": "unavailable",
        "completed": 0,
    }
    status["historical_operations"] = _historical_operations(
        status["historical_backfill"], status["historical_helper"]
    )
    raw_relink = r.get(ENTITY_RELINK_HEARTBEAT_KEY)
    try:
        relink = json.loads(raw_relink) if raw_relink else {"status": "not_started", "completed": 0}
    except (TypeError, ValueError):
        relink = {"status": "unavailable", "completed": 0}
    status["entity_relink"] = relink if isinstance(relink, dict) else {
        "status": "unavailable",
        "completed": 0,
    }
    raw_live = r.get(HEARTBEAT_KEY)
    try:
        live = json.loads(raw_live) if raw_live else {}
    except (TypeError, ValueError):
        live = {}
    counts = pending_counts(r)
    rate = float(live.get("records_per_minute") or 0) if isinstance(live, dict) else 0.0
    status["live_worker"] = {
        **(live if isinstance(live, dict) else {}),
        "live_pending": counts["live"],
        "backlog_pending": counts["backlog"],
        "processing": counts["processing"],
        "pending": counts["total"],
        "failed": int(r.llen(FAILED_KEY) or 0),
        "eta_seconds": round(counts["total"] * 60 / rate) if rate > 0 else None,
    }
    return status


@router.get("/quality")
def llm_quality(
    refresh: bool = Query(False),
    _access_token: str = Depends(require_access_token),
):
    now = int(time.time())
    cached = _quality_cache_payload()
    if not refresh and not cached:
        return {"summary": {"available": False}, "samples": [], "cache": {
            "generated_at": None, "refresh_required": True,
            "refresh_in_progress": bool(r.exists(QUALITY_REFRESH_LOCK_KEY)),
        }}
    if cached and not refresh:
        cache_meta = cached.get("cache") or {}
        upgraded = _quality_cache_response(
            cached,
            generated_at=int(cache_meta.get("generated_at") or now),
            fresh_for_seconds=QUALITY_CACHE_FRESH_SECONDS,
            refresh_in_progress=bool(r.exists(QUALITY_REFRESH_LOCK_KEY)),
            refresh_required=not _quality_cache_is_fresh(cached, now),
        )
        if not cache_meta.get("generated_at"):
            r.setex(
                QUALITY_CACHE_KEY,
                QUALITY_CACHE_STALE_SECONDS,
                json.dumps(upgraded, default=str),
            )
        return upgraded

    token = uuid.uuid4().hex
    lock_acquired = bool(
        r.set(
            QUALITY_REFRESH_LOCK_KEY,
            token,
            nx=True,
            ex=QUALITY_REFRESH_LOCK_SECONDS,
        )
    )
    if not lock_acquired:
        if cached:
            return _quality_cache_response(cached, refresh_in_progress=True)
        raise HTTPException(
            status_code=503,
            detail="News quality metrics are being generated",
        )

    started = time.monotonic()
    try:
        summary = persistent_store.news_llm_quality_summary()
        summary_finished = time.monotonic()
        samples = persistent_store.news_llm_review_samples(per_category=5)
        samples_finished = time.monotonic()
        payload = {
            "summary": summary,
            "samples": samples,
            "cache": {
                "generated_at": now,
                "finished_at": int(time.time()),
                "report_version": QUALITY_REPORT_VERSION,
                "summary_seconds": round(summary_finished - started, 3),
                "samples_seconds": round(samples_finished - summary_finished, 3),
                "refresh_seconds": round(samples_finished - started, 3),
                "fresh_for_seconds": QUALITY_CACHE_FRESH_SECONDS,
                "refresh_in_progress": False,
                "refresh_required": False,
            },
        }
        r.setex(
            QUALITY_CACHE_KEY,
            QUALITY_CACHE_STALE_SECONDS,
            json.dumps(payload, default=str),
        )
        logger.info(
            "News quality report %s completed: summary=%.3fs samples=%.3fs total=%.3fs",
            QUALITY_REPORT_VERSION, summary_finished - started,
            samples_finished - summary_finished, samples_finished - started,
        )
        return payload
    except persistent_store.NewsQualityRefreshBusy:
        if cached:
            return _quality_cache_response(
                cached, refresh_in_progress=True, refresh_failed=False,
            )
        raise HTTPException(
            status_code=503,
            detail="News quality metrics are being generated",
        )
    except Exception:
        logger.exception(
            "News quality report %s refresh failed after %.3fs",
            QUALITY_REPORT_VERSION, time.monotonic() - started,
        )
        if cached:
            return _quality_cache_response(
                cached,
                refresh_in_progress=False,
                refresh_failed=True,
            )
        raise
    finally:
        _release_quality_refresh_lock(token)


@router.get("/training-export")
def export_accepted_news_training_records(
    ticker: str | None = Query(default=None, min_length=1, max_length=10, pattern=r"^[A-Za-z][A-Za-z0-9.-]*$"),
    start_ts: int = Query(0, ge=0),
    end_ts: int = Query(0, ge=0),
    limit: int = Query(250_000, ge=1, le=1_000_000),
    _access_token: str = Depends(require_access_token),
):
    if not persistent_store.enabled():
        raise HTTPException(status_code=503, detail="News training storage is unavailable")
    if start_ts and end_ts and start_ts > end_ts:
        raise HTTPException(status_code=400, detail="start_ts must be less than or equal to end_ts")

    def stream():
        for record in persistent_store.iter_accepted_news_training_records(
            ticker=ticker,
            start_ts=start_ts,
            end_ts=end_ts,
            limit=limit,
        ):
            yield json.dumps(record, default=str, ensure_ascii=False, separators=(",", ":")) + "\n"

    filename = f"accepted-news-{ticker.upper() if ticker else 'all'}.jsonl"
    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/retry-failed")
def retry_failed_news_llm_items(
    limit: int = Query(500, ge=1, le=5000),
    _access_token: str = Depends(require_access_token),
):
    replayed = replay_failed(r, limit=limit)
    counts = pending_counts(r)
    return {
        "replayed": replayed,
        "failed_remaining": int(r.llen(FAILED_KEY) or 0),
        "live_pending": counts["live"],
        "backlog_pending": counts["backlog"],
    }


@router.post("/quality/review")
def review_llm_quality_sample(
    body: LlmSampleReview,
    _access_token: str = Depends(require_access_token),
):
    try:
        saved = persistent_store.review_news_llm_sample(
            body.article_url,
            body.category,
            body.judgment,
            body.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not saved:
        raise HTTPException(status_code=404, detail="The enrichment sample no longer exists")
    return saved


@router.get("/watch")
def llm_watch(_access_token: str = Depends(require_access_token)):
    raw = r.get(WATCH_KEY)
    if not raw:
        return {"phase": "waiting", "inputs": [], "outputs": {}}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {"phase": "unavailable", "inputs": [], "outputs": {}}
    return payload if isinstance(payload, dict) else {
        "phase": "unavailable", "inputs": [], "outputs": {},
    }


@router.get("/catalog")
def llm_catalog(
    q: str = Query("", max_length=100),
    _access_token: str = Depends(require_access_token),
):
    return {"query": q, "models": search_catalog(q)}


@router.post("/models/pull")
def llm_pull(body: ModelAction, _access_token: str = Depends(require_access_token)):
    update_settings(r, enabled=False)
    try:
        result = pull_model(body.model)
        settings = update_settings(r, active_model=body.model, enabled=False)
        removed_models = delete_unselected_models(body.model)
        return {**result, **settings, "removed_models": removed_models}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:500]) from exc


@router.delete("/models/{model:path}")
def llm_delete(model: str, _access_token: str = Depends(require_access_token)):
    try:
        if get_settings(r)["active_model"] == model:
            raise HTTPException(status_code=409, detail="Select another active model before deleting this one")
        return delete_model(model)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:500]) from exc


@router.put("/settings")
def llm_settings(body: LlmSettingsUpdate, _access_token: str = Depends(require_access_token)):
    try:
        installed = {row.get("name") or row.get("model") for row in list_models()}
        removed_models = []
        if body.active_model is not None:
            if body.active_model not in installed:
                raise HTTPException(status_code=409, detail="Pull the model before making it active")
            update_settings(r, enabled=False)
            settings = update_settings(r, active_model=body.active_model, enabled=False)
            removed_models = delete_unselected_models(body.active_model)
            if body.enabled is True:
                settings = update_settings(r, enabled=True)
        else:
            current = get_settings(r)
            if body.enabled and (
                not current["active_model"] or current["active_model"] not in installed
            ):
                raise HTTPException(status_code=409, detail="Download and select a model before enabling processing")
            settings = update_settings(r, enabled=body.enabled)
        return {**settings, "removed_models": removed_models}
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:500]) from exc


@router.post("/preview")
def llm_preview(body: LlmPreviewRequest, _access_token: str = Depends(require_access_token)):
    settings = get_settings(r)
    installed = {row.get("name") or row.get("model") for row in list_models()}
    if settings["active_model"] not in installed:
        raise HTTPException(status_code=409, detail="Pull the active model before testing it")
    preview_id = hashlib.sha256(f"{body.title}|{body.summary}".encode("utf-8")).hexdigest()[:24]
    try:
        result = classify_articles([{
            "id": preview_id,
            "title": body.title,
            "original_title": body.title,
            "summary": body.summary,
            "source": body.source,
            "headline_resolution_source": "headline_only",
            "content_verified": False,
        }], allow_disabled=True).get(preview_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:500]) from exc
    if not result:
        raise HTTPException(status_code=502, detail="The model did not return a valid result")
    return result


@router.post("/headline-review")
def flag_headline(body: HeadlineReviewFlag, _access_token: str = Depends(require_access_token)):
    if not persistent_store.enabled():
        raise HTTPException(status_code=503, detail="News quality storage is unavailable")
    issue_key = hashlib.sha256(f"headline_review|{body.url}".encode("utf-8")).hexdigest()
    persistent_store.upsert_news_quality_issues([{
        "issue_key": issue_key,
        "issue_type": "headline_review",
        "severity": "review",
        "title": body.display_title or body.original_title or "Headline review",
        "article_url": body.url,
        "details": {
            "article_id": body.article_id,
            "original_title": body.original_title,
            "display_title": body.display_title,
            "reason": body.reason,
        },
    }])
    return {"ok": True, "issue_key": issue_key}
