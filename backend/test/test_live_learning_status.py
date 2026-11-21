import json
from unittest.mock import Mock

from services.live_learning_status import status


def test_capture_is_not_learning_and_private_fields_not_exposed():
    client = Mock()
    client.get.return_value = json.dumps(dict(ts=100, status='connected', symbol_count=25,
        market_observations=90, auth_error='private', priority_capture_symbols=['ABC'],
        last_market_ms={'NASDAQ_BOOK': 99000, 'CHART_EQUITY': 90000}))
    result = status(client, now=101)
    assert result['capture_status'] == 'connected' and result['priority_symbols'] == 1
    assert result['learner_status'] == 'not_deployed' and not result['training_started']
    assert result['book_age_seconds'] == {'NASDAQ_BOOK': 2}
    assert 'auth_error' not in result
    assert status(client, now=200)['capture_status'] == 'stale'


def test_missing_or_corrupt_capture():
    for value in (None, 'broken', '[]', '{}'):
        client = Mock()
        client.get.return_value = value
        assert status(client, now=100)['capture_status'] == 'unavailable'


def test_readiness_is_separate_from_active_learning():
    client = Mock()
    client.get.side_effect = lambda key: json.dumps(dict(ts=100, mode='readiness_only',
        status='market_closed', selected_count=0, usable_count=0)) if key == 'drl:live:readiness' else None
    result = status(client, now=101)
    assert result['readiness']['status'] == 'market_closed'
    assert result['learner_status'] == 'not_deployed' and not result['training_started']
    assert status(client, now=150)['readiness']['status'] == 'unavailable'


def test_deployed_learner_does_not_claim_training_until_update():
    client = Mock()
    heartbeat = dict(ts=100, mode='shadow_learning', status='market_closed', policy_version=0)
    client.get.side_effect = lambda key: json.dumps(heartbeat) if key == 'drl:live:learner' else None
    result = status(client, now=101)
    assert result['learner_status'] == 'market_closed' and not result['training_started']
    heartbeat.update(policy_version=1, status='updated')
    assert status(client, now=101)['training_started']
