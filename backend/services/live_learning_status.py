"""Expose capture health without claiming the capture worker is an RL learner."""
import json
import os
import time

import redis


def status(client=None, now=None):
    now = time.time() if now is None else now
    result = dict(learner_status='not_deployed', execution='shadow_only', training_started=False,
                  capture_status='unavailable', heartbeat_age_seconds=None,
                  readiness={'status': 'unavailable', 'mode': 'readiness_only'})
    try:
        if client is None:
            client = redis.from_url(os.getenv('REDIS_URL', 'redis://redis:6379/0'),
                                    decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
        try:
            learner = json.loads(client.get('drl:live:learner') or '{}')
            stamp = learner.get('ts')
            if (learner.get('mode') == 'shadow_learning' and isinstance(stamp, (int, float))
                    and not isinstance(stamp, bool) and 0 <= now - stamp <= 45):
                result['learner_status'] = learner.get('status', 'unavailable')
                version = learner.get('policy_version')
                result['training_started'] = type(version) is int and version > 0
                result['learner'] = {k: learner.get(k) for k in ('policy_version',
                    'process_training_transitions', 'active_symbols', 'account_scope', 'error_type', 'quarantined_symbols', 'errors')}
                result['learner']['heartbeat_age_seconds'] = int(now - stamp)
        except (redis.RedisError, ValueError, TypeError, AttributeError):
            pass
        try:
            readiness = json.loads(client.get('drl:live:readiness') or '{}')
            stamp = readiness.get('ts')
            if (readiness.get('mode') == 'readiness_only' and isinstance(stamp, (int, float))
                    and not isinstance(stamp, bool) and 0 <= now - stamp <= 45):
                result['readiness'] = {k: readiness.get(k) for k in
                    ('status', 'mode', 'selected_count', 'usable_count', 'error_type')}
                result['readiness']['heartbeat_age_seconds'] = int(now - stamp)
        except (redis.RedisError, ValueError, TypeError, AttributeError):
            pass
        raw = client.get(os.getenv('SCHWAB_ACCOUNT_STREAM_HEARTBEAT_KEY', 'schwab:account_stream:heartbeat'))
        heartbeat = json.loads(raw) if raw else {}
        stamp = heartbeat.get('ts')
        if not isinstance(stamp, (int, float)) or isinstance(stamp, bool) or stamp <= 0 or stamp > now + 5:
            return result
        age = max(0, int(now - stamp))
        result.update(capture_status=heartbeat.get('status') if age <= 30 else 'stale',
                      heartbeat_age_seconds=age)
        for key in ('symbol_count', 'market_observations', 'market_observation_errors',
                    'market_observation_dropped', 'market_observation_queue_depth',
                    'book_observation_interval_ms', 'priority_observation_interval_ms'):
            result[key] = heartbeat.get(key)
        result['priority_symbols'] = len(heartbeat.get('priority_capture_symbols') or [])
        result['book_age_seconds'] = {
            service: max(0, int(now - stamp / 1000))
            for service, stamp in (heartbeat.get('last_market_ms') or {}).items()
            if service in {'NASDAQ_BOOK', 'NYSE_BOOK'} and isinstance(stamp, (int, float)) and stamp > 0
        }
    except (redis.RedisError, ValueError, TypeError, AttributeError):
        result['capture_status'] = 'unavailable'
    return result
