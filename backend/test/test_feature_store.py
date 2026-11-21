import os
import sys
from unittest.mock import MagicMock

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

import services.feature_store as feature_store


def test_optional_snapshot_identity_prevents_same_second_provenance_collision():
    base = {
        "action": "research_candidate",
        "market_signal": {"price": None},
    }

    first = feature_store._feature_id(
        "NVDA", 100, "historical_news_hype", {**base, "snapshot_identity": "v1:NVDA"}
    )
    second = feature_store._feature_id(
        "NVDA", 100, "historical_news_hype", {**base, "snapshot_identity": "v2:NVDA"}
    )
    legacy = feature_store._feature_id("NVDA", 100, "historical_news_hype", base)

    assert first != second
    assert legacy not in {first, second}


def test_record_feature_snapshot_writes_redis(monkeypatch):
    mock_pipe = MagicMock()
    mock_redis = MagicMock()
    mock_redis.pipeline.return_value = mock_pipe
    monkeypatch.setattr(feature_store, "r", mock_redis)
    monkeypatch.setattr(feature_store, "DATABASE_URL", None)

    item = feature_store.record_feature_snapshot(
        "AAPL",
        {
            "ts": 123,
            "action": "buy",
            "reward": 1.5,
            "semantic_signal": {"sentiment": "positive"},
            "market_signal": {"price": 10.5},
            "stop_loss": {"triggered": False},
        },
    )

    assert item["symbol"] == "AAPL"
    assert item["price"] == 10.5
    mock_pipe.hset.assert_called_once()
    mock_pipe.zadd.assert_called_once()
    mock_pipe.execute.assert_called_once()


def test_record_feature_snapshot_replaces_non_finite_training_values(monkeypatch):
    mock_pipe = MagicMock()
    mock_redis = MagicMock()
    mock_redis.pipeline.return_value = mock_pipe
    monkeypatch.setattr(feature_store, "r", mock_redis)
    monkeypatch.setattr(feature_store, "DATABASE_URL", None)

    item = feature_store.record_feature_snapshot(
        "NVDA",
        {
            "market_signal": {"price": 10.0, "volatility_20": float("nan")},
            "semantic_signal": {"score": float("inf")},
        },
        ts=123,
    )

    assert item["market_signal"]["volatility_20"] is None
    assert item["semantic_signal"]["score"] is None
    stored = feature_store.json.loads(mock_pipe.hset.call_args.kwargs["mapping"]["json"])
    assert stored["payload"]["market_signal"]["volatility_20"] is None


def test_list_feature_snapshots_filters_symbol(monkeypatch):
    row_a = {
        "id": "a",
        "symbol": "AAPL",
        "ts": 2,
        "semantic_signal": {},
        "market_signal": {},
        "stop_loss": {},
    }
    row_b = {
        "id": "b",
        "symbol": "MSFT",
        "ts": 1,
        "semantic_signal": {},
        "market_signal": {},
        "stop_loss": {},
    }
    mock_redis = MagicMock()
    mock_redis.zrevrange.return_value = ["a", "b"]
    mock_redis.hget.side_effect = [
        feature_store.json.dumps(row_a),
        feature_store.json.dumps(row_b),
    ]
    monkeypatch.setattr(feature_store, "r", mock_redis)
    monkeypatch.setattr(feature_store, "DATABASE_URL", None)

    rows = feature_store.list_feature_snapshots(symbol="AAPL")

    assert rows == [row_a]
