from unittest.mock import MagicMock

import pytest

from services import article_graph


@pytest.mark.parametrize("before", [0, 100])
def test_indexed_page_bounds_and_event_projection(monkeypatch, before):
    driver = MagicMock()
    driver.__enter__.return_value = driver
    driver.execute_query.return_value = ([{"url": "b", "published_ts": 90, "event_ids": ["event"]}], None, None)
    monkeypatch.setattr(article_graph, "_driver", lambda: driver)
    monkeypatch.setattr(article_graph, "_list_historical_articles_newest_legacy", lambda *a: pytest.fail("unexpected fallback"))
    assert article_graph.list_historical_articles_newest(before, "c", 1, 50)[0]["event_ids"] == ["event"]
    query = driver.execute_query.call_args.args[0]
    params = driver.execute_query.call_args.kwargs
    assert params == {"before": before, "url": "c", "minimum": 50, "limit": 1}
    assert "coalesce" not in query
    assert ("UNION ALL" in query) == bool(before)
    assert query.index("LIMIT $limit") < query.index("OPTIONAL MATCH")


def test_undated_tail_preserves_legacy_cursor_semantics(monkeypatch):
    driver = MagicMock()
    driver.__enter__.return_value = driver
    driver.execute_query.return_value = ([], None, None)
    monkeypatch.setattr(article_graph, "_driver", lambda: driver)
    legacy = MagicMock(return_value=[{"url": "undated", "published_ts": 0}])
    monkeypatch.setattr(article_graph, "_list_historical_articles_newest_legacy", legacy)
    assert article_graph.list_historical_articles_newest(1, "cursor", 8)[0]["published_ts"] == 0
    legacy.assert_called_once_with(1, "cursor", 8, 0)


def test_dated_partition_exhaustion_does_not_scan_undated_tail(monkeypatch):
    driver = MagicMock()
    driver.__enter__.return_value = driver
    driver.execute_query.return_value = ([], None, None)
    monkeypatch.setattr(article_graph, "_driver", lambda: driver)
    monkeypatch.setattr(article_graph, "_list_historical_articles_newest_legacy", lambda *a: pytest.fail("unexpected fallback"))
    assert article_graph.list_historical_articles_newest(100, "cursor", 8, 99) == []
