import os
import sys
from unittest.mock import MagicMock

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from services import company_relationship_graph as graph


def test_normalize_company_relationships_keeps_only_verified_scanner_relations():
    rows = graph.normalize_company_relationships([
        {
            "company_a": "Luminar",
            "validated_tickers_a": ["LAZR"],
            "relationship": "partnership",
            "company_b": "NVIDIA",
            "validated_tickers_b": ["NVDA"],
            "evidence": "The companies announced a partnership.",
            "materiality": "high",
        },
        {
            "company_a": "Fund",
            "validated_tickers_a": ["FUND"],
            "relationship": "investment",
            "company_b": "NVIDIA",
            "validated_tickers_b": ["NVDA"],
        },
    ], article_url="https://example.com/story", published_ts=123)

    assert rows == [{
        "ticker_a": "LAZR",
        "ticker_b": "NVDA",
        "kind": "partnership",
        "company_a": "Luminar",
        "company_b": "NVIDIA",
        "evidence": "The companies announced a partnership.",
        "materiality": "high",
        "article_url": "https://example.com/story",
        "published_ts": 123,
    }]


def test_related_security_symbols_ranks_and_caps_per_seed(monkeypatch):
    driver = MagicMock()
    driver.__enter__.return_value = driver
    driver.execute_query.return_value = ([
        {"seed": "LAZR", "symbol": "AAA", "kind": "supplier", "materiality": "low", "last_seen_ts": 20},
        {"seed": "LAZR", "symbol": "BBB", "kind": "partnership", "materiality": "high", "last_seen_ts": 10},
        {"seed": "LAZR", "symbol": "AAA", "kind": "supplier", "materiality": "high", "last_seen_ts": 30},
    ], None, None)
    monkeypatch.setattr(graph, "_driver", lambda: driver)

    result = graph.related_security_symbols(["LAZR"], max_per_seed=2, limit=2)

    assert [row["symbol"] for row in result["LAZR"]] == ["AAA", "BBB"]


def test_upsert_company_relationships_is_idempotent_graph_merge(monkeypatch):
    driver = MagicMock()
    driver.__enter__.return_value = driver
    driver.execute_query.return_value = ([{"count": 1}], None, None)
    monkeypatch.setattr(graph, "_driver", lambda: driver)

    count = graph.upsert_company_relationships([{
        "validated_tickers_a": ["LAZR"],
        "relationship": "supplier",
        "validated_tickers_b": ["NVDA"],
    }])

    assert count == 1
    assert "MERGE (a)-[relation:COMPANY_RELATION" in driver.execute_query.call_args.args[0]
