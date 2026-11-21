from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from workers import gdelt_duplicate_cleanup


def _client(*names):
    client = MagicMock()
    client.list_collections.return_value = [SimpleNamespace(name=name) for name in names]
    return client


def test_cleanup_dry_run_reports_without_deleting():
    client = _client("news", "gdelt_events", "gdelt_events_2")
    news = client.get_collection.return_value
    news.get.return_value = {"ids": ["one", "two"]}

    result = gdelt_duplicate_cleanup.cleanup_duplicates(client)

    assert result["news_gdelt_export_records"] == 2
    assert result["legacy_collection_present"] is True
    news.delete.assert_not_called()
    client.delete_collection.assert_not_called()


def test_cleanup_apply_removes_only_legacy_duplicates():
    client = _client("news", "gdelt_events", "gdelt_events_2")
    news = client.get_collection.return_value
    news.get.return_value = {"ids": ["one"]}

    result = gdelt_duplicate_cleanup.cleanup_duplicates(client, apply=True)

    assert result["applied"] is True
    news.delete.assert_called_once_with(where=gdelt_duplicate_cleanup.GDELT_EXPORT_FILTER)
    client.delete_collection.assert_called_once_with("gdelt_events")


def test_cleanup_refuses_to_run_without_canonical_collection():
    client = _client("news", "gdelt_events")

    with pytest.raises(RuntimeError, match="canonical collection"):
        gdelt_duplicate_cleanup.cleanup_duplicates(client, apply=True)

    client.delete_collection.assert_not_called()
