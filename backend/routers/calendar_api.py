import json
import os
import time
import redis
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator

from routers.auth import SESSION_COOKIE_NAME
from services import persistent_store

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
CALENDAR_STATE_TTL_SECONDS = int(os.getenv("CALENDAR_STATE_TTL_SECONDS", str(60 * 60 * 24 * 365)))

r = redis.from_url(REDIS_URL, decode_responses=True)
router = APIRouter(prefix="/calendar", tags=["calendar"])


class CalendarReview(BaseModel):
    notes: str = ""
    tags: list[str] = Field(default_factory=list)


class CalendarPlanningEvent(BaseModel):
    id: str
    type: str = "Personal"
    title: str
    note: str = ""


class CalendarReviewArchive(BaseModel):
    month: str
    title: str
    text: str
    created_at: int


class CalendarState(BaseModel):
    reviews: dict[str, CalendarReview] = Field(default_factory=dict)
    planning_events: dict[str, list[CalendarPlanningEvent]] = Field(default_factory=dict)
    review_archives: dict[str, CalendarReviewArchive] = Field(default_factory=dict)
    updated_at: int | None = None

    @validator("reviews", "planning_events", "review_archives", pre=True)
    def _dict_or_empty(cls, value):
        return value if isinstance(value, dict) else {}


def _calendar_key(request: Request) -> str:
    return f"calendar:state:{_session_id(request)}"


def _session_id(request: Request) -> str:
    return request.cookies.get(SESSION_COOKIE_NAME) or "local"


def _empty_state() -> dict:
    return {"reviews": {}, "planning_events": {}, "review_archives": {}, "updated_at": None}


def _state_payload(payload: dict) -> dict:
    try:
        state = CalendarState(**payload)
    except Exception:
        return _empty_state()
    return state.dict()


@router.get("/state")
def get_calendar_state(request: Request):
    if persistent_store.enabled():
        payload = persistent_store.load_calendar_state(_session_id(request))
        if payload:
            return JSONResponse(_state_payload(payload))
        return JSONResponse(_empty_state())

    raw = r.get(_calendar_key(request))
    if not raw:
        return JSONResponse(_empty_state())
    try:
        payload = json.loads(raw)
    except Exception:
        return JSONResponse(_empty_state())
    if not isinstance(payload, dict):
        return JSONResponse(_empty_state())
    return JSONResponse(_state_payload(payload))


@router.put("/state")
def put_calendar_state(state: CalendarState, request: Request):
    payload = state.dict()
    payload["updated_at"] = int(os.getenv("CALENDAR_STATE_TEST_NOW", "0")) or time.time_ns() // 1_000_000
    if persistent_store.enabled():
        payload = persistent_store.save_calendar_state(_session_id(request), payload)
    else:
        r.set(_calendar_key(request), json.dumps(payload), ex=CALENDAR_STATE_TTL_SECONDS)
    return JSONResponse(payload)
