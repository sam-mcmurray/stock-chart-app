import json

from services.training_data_status import status


def save(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def test_missing_is_unknown_not_zero(tmp_path):
    result = status(tmp_path)
    assert result['status'] == 'unavailable'
    assert result['export']['days'] is None
    assert result['assembly']['completed'] is None


def test_current_month_progress_and_no_false_training(tmp_path):
    save(tmp_path, 'history-expansion-v1/progress.json', dict(current_month='2026-06', stage='price_acquisition',
        start='2023-08-01', end='2026-07-01', corpus='/sensitive/location', status='running'))
    save(tmp_path, 'history-expansion-v1/supervisor.json', dict(status='blocked', error_type='ValueError'))
    save(tmp_path, 'news-corpus-june-2026-v1/progress.json', dict(status='complete', selected_articles=20,
        unique_symbols=4, completed=[{'date': '2026-06-01'}, {'date': '2026-06-02'}]))
    save(tmp_path, 'news-corpus-june-2026-v1/acquisition/progress.json', dict(status='running',
        completed={'a': {'status': 'cached', 'file': '/secret'}, 'b': {'status': 'no_data'}}, remaining=5))
    result = status(tmp_path)
    assert result['status'] == 'blocked' and result['export']['days'] == 2
    assert result['acquisition']['completed'] == 2 and result['acquisition']['no_data'] == 1
    assert result['assembly']['completed'] is None
    assert not result['training_started'] and not result['split_certified']
    assert '/secret' not in json.dumps(result) and '/sensitive' not in json.dumps(result)


def test_corrupt_snapshot_is_visible(tmp_path):
    path = tmp_path / 'history-expansion-v1'
    path.mkdir()
    (path / 'progress.json').write_text('broken')
    assert status(tmp_path)['status'] == 'snapshot_error'


def test_month_cannot_escape_root(tmp_path):
    save(tmp_path, 'history-expansion-v1/progress.json', dict(current_month='../../private'))
    assert status(tmp_path)['export']['days'] is None
