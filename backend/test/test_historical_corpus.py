import fcntl
import json
import os

import pytest

from drl_agent.historical_corpus import export_corpus
from test.test_drl_historical_news import article

OPTIONS = {"start": "2026-07-01", "end": "2026-07-04", "enrichment_cutoff_ts": 1800000000}


def loader(**kwargs):
    return [article(published_ts=kwargs["start_ts"] + 100)]


def test_resume_freezes_cutoff_and_reuses_completed_days(tmp_path):
    calls = []
    def load(**kwargs):
        calls.append(kwargs["start_ts"])
        return loader(**kwargs)
    first = export_corpus(tmp_path, **OPTIONS, max_days=1, loader=load)
    assert first["status"] == "paused_at_batch_limit"
    assert first["next_day"] == "2026-07-02"
    assert first["selected_articles"] == 1 and first["unique_symbols"] == 2
    second = export_corpus(tmp_path, **OPTIONS, max_days=2, loader=load)
    assert second["status"] == "complete" and len(calls) == 3
    assert second["selected_articles"] == 3
    assert second["exported_this_run"] == 2
    assert not second["training_started"] and second["broker_calls"] == 0
    with pytest.raises(ValueError, match="configuration changed"):
        export_corpus(tmp_path, **{**OPTIONS, "enrichment_cutoff_ts": 1800000001}, loader=load)


def test_failed_day_is_retried_without_losing_earlier_shards(tmp_path):
    count = 0
    def failure(**kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise TimeoutError("private connection detail")
        return loader(**kwargs)
    with pytest.raises(TimeoutError):
        export_corpus(tmp_path, **OPTIONS, loader=failure)
    progress = json.loads((tmp_path / "progress.json").read_text())
    assert progress["status"] == "failed" and progress["current_day"] == "2026-07-02"
    assert len(progress["completed"]) == 1
    assert "private" not in (tmp_path / "progress.json").read_text()
    result = export_corpus(tmp_path, **OPTIONS, loader=loader)
    assert result["status"] == "complete" and result["exported_this_run"] == 2


def test_corruption_and_competing_writer_fail_closed(tmp_path):
    export_corpus(tmp_path, **OPTIONS, max_days=1, loader=loader)
    target = tmp_path / "2026-07-01.json"
    payload = json.loads(target.read_text())
    payload["selected_article_count"] = 999
    target.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="digest"):
        export_corpus(tmp_path, **OPTIONS, loader=loader)
    assert json.loads(target.read_text())["selected_article_count"] == 999
    with (tmp_path / ".export.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="Another exporter"):
            export_corpus(tmp_path, **OPTIONS, loader=loader)


def test_empty_days_retained(tmp_path):
    report = export_corpus(tmp_path, **OPTIONS, loader=lambda **kwargs: [])
    assert len(report["completed"]) == 3
    assert report["selected_articles"] == 0 and report["unique_symbols"] == 0


def test_new_buffer_and_legacy_resume_preserve_frozen_windows(tmp_path):
    fresh = tmp_path / "fresh"
    report = export_corpus(fresh, **OPTIONS, max_days=1, loader=loader)
    shard = json.loads((fresh / "2026-07-01.json").read_text())
    assert report["price_lookback_days"] == shard["price_lookback_days"] == 45
    legacy = tmp_path / "legacy"
    export_corpus(legacy, **OPTIONS, max_days=1, loader=loader, price_lookback_days=30)
    original = (legacy / "2026-07-01.json").read_bytes()
    old_shard = json.loads(original)
    symbols = {r["symbol"] for r in shard["price_requests"]}
    assert symbols == {r["symbol"] for r in old_shard["price_requests"]} and symbols
    for symbol in symbols:
        newer = [r for r in shard["price_requests"] if r["symbol"] == symbol]
        older = [r for r in old_shard["price_requests"] if r["symbol"] == symbol]
        assert min(r["start_ts"] for r in older) - min(r["start_ts"] for r in newer) == 15 * 86400
        assert max(r["end_ts"] for r in newer) == max(r["end_ts"] for r in older)
    resumed = export_corpus(legacy, **OPTIONS, loader=loader)
    assert resumed["price_lookback_days"] == 30 and resumed["status"] == "complete"
    assert (legacy / "2026-07-01.json").read_bytes() == original
    assert json.loads((legacy / "2026-07-02.json").read_text())["price_lookback_days"] == 30
    with pytest.raises(ValueError, match="configuration changed"):
        export_corpus(legacy, **OPTIONS, loader=loader, price_lookback_days=45)


@pytest.mark.parametrize("lookback", [0, 366, True, 1.5])
def test_invalid_history_buffer_rejected(tmp_path, lookback):
    with pytest.raises(ValueError, match="price_lookback_days"):
        export_corpus(tmp_path, **OPTIONS, loader=loader, price_lookback_days=lookback)


@pytest.mark.skipif(os.getenv("DRL_SQL_INTEGRATION") != "1", reason="Explicit temporary-table PostgreSQL test")
def test_indexed_latest_version_query_preserves_corrections_and_limit(monkeypatch):
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    from drl_agent import historical_news
    conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
    try:
        conn.execute("""CREATE TEMP TABLE news_enrichment_versions (
            version_key text, record_type text, record_id text, article_url text,
            published_ts bigint, observed_ts bigint, pipeline_version text, result jsonb)""")
        for key, identity, published, observed in [
            ("old", "moved", 1000, 1200), ("new", "moved", 2000, 1400),
            ("early", "fixed", 1000, 1100), ("tie-a", "fixed", 1000, 1300),
            ("tie-z", "fixed", 1000, 1300), ("future", "fixed", 1000, 1600)]:
            conn.execute("INSERT INTO news_enrichment_versions VALUES (%s,'news',%s,'url',%s,%s,'v',%s)",
                         (key, identity, published, observed, Jsonb({})))
        conn.commit()
        class Borrowed:
            def __enter__(self):
                return conn
            def __exit__(self, *args):
                conn.rollback()
        monkeypatch.setattr(historical_news.psycopg, "connect", lambda *args, **kwargs: Borrowed())
        rows = historical_news.load_versions(start_ts=900, end_ts=1100, enrichment_cutoff_ts=1500)
        assert {row["version_key"] for row in rows} == {"new", "tie-z"}
        assert next(row for row in rows if row["version_key"] == "new")["published_ts"] == 2000
        with pytest.raises(ValueError, match="limit exceeded"):
            historical_news.load_versions(start_ts=900, end_ts=1100, enrichment_cutoff_ts=1500, limit=1)
    finally:
        conn.close()
