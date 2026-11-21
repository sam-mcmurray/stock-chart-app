"""Single-flight, timestamped research snapshots; never used for trade decisions."""
import hashlib
import json
import os
import threading
import time
import uuid

import redis

r = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)
LEASE_KEY = "research:snapshot:build:v1"
_RELEASE = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0"
_RENEW = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('EXPIRE', KEYS[1], 60) end return 0"
_PUBLISH = "if redis.call('GET', KEYS[1]) == ARGV[1] then redis.call('SET', KEYS[2], ARGV[2], 'EX', 86400); redis.call('DEL', KEYS[2] .. ':error'); return 1 end return 0"


def _build(key, token, builder):
    stopped = threading.Event()
    def renew():
        while not stopped.wait(20):
            try:
                if not r.eval(_RENEW, 1, LEASE_KEY, token):
                    return
            except redis.RedisError:
                return
    threading.Thread(target=renew, daemon=True, name="research-snapshot-lease").start()
    try:
        result = builder()
        r.eval(_PUBLISH, 2, LEASE_KEY, key, token,
               json.dumps({"generated_ts": int(time.time()), "result": result}))
    except Exception as exc:
        # Do not expose database URLs or raw SQL in a user-visible failure.
        if r.get(LEASE_KEY) == token:
            r.set(key + ":error", type(exc).__name__, ex=30)
    finally:
        stopped.set()
        r.eval(_RELEASE, 1, LEASE_KEY, token)


def request(kind, parameters, builder):
    key = "research:snapshot:v1:" + hashlib.sha256(
        json.dumps([kind, parameters], sort_keys=True).encode()).hexdigest()
    raw = r.get(key)
    cached = json.loads(raw) if raw else None
    stale = not cached or time.time() - cached["generated_ts"] >= 300
    failed = r.get(key + ":error") if stale else None
    if stale and not failed:
        token = uuid.uuid4().hex
        # One build across API processes, including both coverage and report work.
        if r.set(LEASE_KEY, token, nx=True, ex=60):
            try:
                threading.Thread(target=_build, args=(key, token, builder),
                                 daemon=True, name="research-snapshot").start()
            except Exception:
                r.eval(_RELEASE, 1, LEASE_KEY, token)
                raise
    if cached:
        return {**cached["result"], "snapshot": {
            "generated_ts": cached["generated_ts"], "stale": stale,
            "refresh_failed": bool(failed),
        }}, False
    return {"status": "retrying" if failed else "building", "retry_after_seconds": 5}, True
