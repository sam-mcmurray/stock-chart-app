# backend/test/test_scanner_worker.py
import pytest
from unittest.mock import patch, MagicMock
import json
import os
import pandas as pd
from datetime import datetime, timedelta, timezone

# Adjust path for imports if running pytest from project root
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from workers import scanner_worker as scanner_worker_module
from workers.scanner_worker import ScannerWorker, _now_ts, H_SCAN_RESULTS, _get_recent_tickers, _recent_articles, _get_daily, _get_intraday, _avg_volume_ratio, _macd_open, _price_ok, _score, _select_tickers, _monitor_news_universe_quotes, _moving_symbols, _price_eligible_symbols, _automatic_candidate_universe, _calendar_catalyst_tickers, _schwab_market_mover_tickers, _watch_source_details


@pytest.mark.parametrize("reported_float", [None, 123456])
def test_outstanding_shares_never_substitute_for_float(monkeypatch, reported_float, mock_redis):
    monkeypatch.setattr(scanner_worker_module, "security_company_identity", lambda _: {})
    client = MagicMock()
    client.company_profile2.return_value = {}
    monkeypatch.setattr(scanner_worker_module, "_finnhub_client", lambda: client)
    ticker = MagicMock()
    ticker.fast_info = {"shares": 900000}
    ticker.info = {"sharesOutstanding": 1000000, "floatShares": reported_float}
    monkeypatch.setattr(scanner_worker_module.yf, "Ticker", lambda _: ticker)
    company = scanner_worker_module._load_company_info("TEST")
    assert company["shares_outstanding"] == 1000000
    assert company["float_shares"] == reported_float
    assert company["float_source"] == ("yfinance.floatShares" if reported_float else None)

@pytest.fixture
def mock_redis():
    mock_redis_instance = MagicMock()
    mock_redis_instance.pipeline.return_value = MagicMock() # Mock pipeline
    scanner_worker_module._RECENT_ARTICLE_INDEX_CACHE.update({
        "loaded_at": 0.0, "redis_id": None, "by_ticker": {},
    })
    scanner_worker_module._CALENDAR_CANDIDATE_CACHE.update({
        "loaded_at": 0.0, "symbols": [],
    })
    scanner_worker_module._GRAPH_CANDIDATE_CACHE.update({
        "loaded_at": 0.0, "seed_key": (), "relationships": {},
    })
    with (
        patch.object(scanner_worker_module, "r", mock_redis_instance),
        patch.object(scanner_worker_module.persistent_store, "enabled", return_value=False),
    ):
        yield mock_redis_instance

@pytest.fixture
def scanner_worker(mock_redis):
    return ScannerWorker()

@pytest.fixture
def mock_market_client():
    with patch('workers.scanner_worker._schwab_market_client') as mock_client_factory:
        mock_client_instance = MagicMock()
        mock_client_factory.return_value = mock_client_instance
        yield mock_client_instance

@pytest.fixture
def sample_dataframe():
    # Create a sample DataFrame that would pass checks
    data = {
        't': [datetime.now(timezone.utc) - timedelta(days=i) for i in range(30, 0, -1)],
        'o': [10 + i for i in range(30)],
        'h': [12 + i for i in range(30)],
        'l': [9 + i for i in range(30)],
        'c': [11 + i for i in range(30)],
        'v': [1000 + i * 100 for i in range(30)],
    }
    df = pd.DataFrame(data)
    # Ensure enough data for _avg_volume_ratio and _macd_open
    df = pd.concat([df, pd.DataFrame(data, index=range(30,60))]).reset_index(drop=True)
    df['c'] = df['c'].astype(float) # Ensure 'c' column is float for MACD
    df['v'] = df['v'].astype(float) # Ensure 'v' column is float for avg volume
    return df

def test_get_recent_tickers(mock_redis):
    mock_redis.zrangebyscore.return_value = ["AAPL", "IPO", "SOME", "GOOG"]
    tickers = _get_recent_tickers()
    assert tickers == ["AAPL", "GOOG"]
    mock_redis.zrangebyscore.assert_called_once()


def test_recent_articles_use_only_accepted_validated_tickers(mock_redis):
    now = scanner_worker_module._now_ts()
    mock_redis.zrevrangebyscore.return_value = ["accepted", "raw-only", "unverified"]
    articles = {
        "accepted": {
            "title": "Raw title", "summary": "Raw", "tickers": ["WRONG"],
            "url": "https://example.com/nvidia", "published_ts": now - 86_400,
        },
        "raw-only": {"title": "NVDA appears in raw text", "tickers": ["NVDA"]},
        "unverified": {"title": "NVDA rumor", "tickers": ["NVDA"]},
    }
    enrichments = {
        "accepted": {
            "display_headline": "NVIDIA announces verified partnership",
            "llm_summary": "NVIDIA announced a partnership.",
            "related_tickers": ["NVDA"],
            "relevance_label": "business",
            "relevance_confidence": 0.94,
            "entity_link_version": 2,
            "article_verification_status": "canonical",
        },
        "unverified": {
            "related_tickers": ["NVDA"],
            "relevance_label": "business",
            "relevance_confidence": 0.99,
            "entity_link_version": 2,
            "article_verification_status": "unverified",
        },
    }

    def hget(key, field=None):
        if key.startswith("news:article:"):
            return json.dumps(articles[key.removeprefix("news:article:")])
        return None

    mock_redis.hget.side_effect = hget
    mock_redis.hmget.side_effect = lambda key, ids: [
        json.dumps(enrichments[article_id]) if article_id in enrichments else None
        for article_id in ids
    ]

    result = _recent_articles("NVDA", limit=10)

    assert len(result) == 1
    assert result[0]["title"] == "NVIDIA announces verified partnership"
    assert result[0]["tickers"] == ["NVDA"]
    assert result[0]["ticker_source"] == "accepted_llm_company_validated"
    assert result[0]["article_url"] == "https://example.com/nvidia"
    assert result[0]["published_ts"] == now - 86_400
    assert result[0]["coverage_sequence"] == "earliest_in_lookback"
    assert result[0]["coverage_count"] == 1


def test_recent_articles_label_earliest_and_follow_up_coverage(mock_redis):
    now = scanner_worker_module._now_ts()
    mock_redis.zrevrangebyscore.return_value = ["newer", "earlier"]
    rows = {
        "newer": {"title": "Follow-up report", "published_ts": now - 3_600},
        "earlier": {"title": "Initial report", "published_ts": now - 3 * 86_400},
    }
    enrichment = {
        "related_tickers": ["NVDA"],
        "relevance_label": "business",
        "relevance_confidence": 0.95,
        "entity_link_version": 2,
        "article_verification_status": "canonical",
    }
    mock_redis.hget.side_effect = lambda key, field=None: json.dumps(
        rows[key.removeprefix("news:article:")]
    )
    mock_redis.hmget.side_effect = lambda key, ids: [json.dumps(enrichment) for _ in ids]

    result = _recent_articles("NVDA", limit=10)

    assert [row["coverage_sequence"] for row in result] == ["follow_up", "earliest_in_lookback"]
    assert all(row["coverage_count"] == 2 for row in result)
    assert result[0]["coverage_first_published_ts"] == now - 3 * 86_400


def test_select_tickers_keeps_hot_signals_and_rotates_least_recent(mock_redis):
    mock_redis.zmscore.return_value = [300, None, 100, 200]

    selected = _select_tickers(
        ["AAAA", "BBBB", "CCCC", "DDDD"],
        3,
        hot_symbols=["AAAA"],
    )

    assert selected == ["AAAA", "BBBB", "CCCC"]


def test_select_tickers_prioritizes_detected_movers_before_rotation(mock_redis):
    mock_redis.zmscore.return_value = [400, None, 100, 200]

    selected = _select_tickers(
        ["AAAA", "BBBB", "CCCC", "DDDD"],
        4,
        hot_symbols=["AAAA"],
        mover_symbols=["DDDD"],
    )

    assert selected == ["AAAA", "DDDD", "BBBB", "CCCC"]


def test_volume_triggers_precede_hot_names_without_starving_rotation(mock_redis):
    symbols = ["AAAA", "BBBB", "CCCC", "DDDD", "EEEE", "FFFF", "GGGG", "HHHH"]
    last = {s: 0 for s in symbols}
    mock_redis.zmscore.side_effect = lambda key, names: [last[s] for s in names]
    observed = set()
    for cycle in range(1, 5):
        selected = _select_tickers(symbols, 4, trigger_symbols=["AAAA", "BBBB"],
                                   hot_symbols=["CCCC", "DDDD"], mover_symbols=["EEEE"])
        assert selected[:2] == ["AAAA", "BBBB"]
        observed.update(selected)
        last.update({s: cycle for s in selected})
    assert observed == set(symbols)


def test_quote_volume_threshold_crossing_is_priority_not_trade_permission(monkeypatch):
    now = int(datetime(2026, 9, 23, 15, tzinfo=timezone.utc).timestamp())
    monkeypatch.setattr(scanner_worker_module, "_now_ts", lambda: now)
    day = datetime(2026, 9, 23).date()
    monkeypatch.setattr(scanner_worker_module, "_RVOL_BASELINE_CACHE", {
        symbol: {"date": day, "window": 90, "average": 1000} for symbol in ("NEW", "HOT", "NEAR")})
    quotes = {s: dict(price=10, market_ts=now, previous_market_ts=now - 60,
                      total_volume=vol, previous_total_volume=prior)
              for s, vol, prior in (("NEW", 3100, 2800), ("HOT", 6000, 5900), ("NEAR", 2500, 2400))}
    settings = dict(price_min=1, price_max=20, rvol_threshold=3, rvol_window_days=90)
    assert scanner_worker_module._volume_trigger_symbols(quotes, settings) == ["NEW", "HOT", "NEAR"]
    assert quotes["NEW"]["rvol_crossed_threshold"]
    assert not quotes["NEW"]["rvol_proxy_entry_permission"]
    assert quotes["NEW"]["rvol_proxy"] == pytest.approx(3.1)
    quotes["NEW"]["market_ts"] = now - 121
    assert "NEW" not in scanner_worker_module._volume_trigger_symbols(quotes, settings)
    settings["rvol_window_days"] = 20
    assert scanner_worker_module._volume_trigger_symbols(quotes, settings) == []


def test_volume_reset_or_previous_day_is_not_a_crossing(monkeypatch):
    now = int(datetime(2026, 9, 23, 15, tzinfo=timezone.utc).timestamp())
    monkeypatch.setattr(scanner_worker_module, "_now_ts", lambda: now)
    monkeypatch.setattr(scanner_worker_module, "_RVOL_BASELINE_CACHE", {
        "TEST": {"date": datetime(2026, 9, 23).date(), "window": 90, "average": 1000}})
    q = {"TEST": dict(price=10, market_ts=now, previous_market_ts=now - 86400,
                      total_volume=3100, previous_total_volume=100)}
    settings = dict(price_min=1, price_max=20, rvol_threshold=3, rvol_window_days=90)
    scanner_worker_module._volume_trigger_symbols(q, settings)
    assert not q["TEST"]["rvol_crossed_threshold"]
    q["TEST"].update(previous_market_ts=now - 60, previous_total_volume=4000)
    scanner_worker_module._volume_trigger_symbols(q, settings)
    assert not q["TEST"]["rvol_crossed_threshold"]


def test_crossing_audit_retains_evaluated_deferred_and_failed_candidates(mock_redis):
    quotes = {s: {"rvol_crossed_threshold": True, "rvol_crossing_detected_ts": 100,
                  "market_ts": 99, "rvol_proxy": 3.1} for s in ["DONE", "WAIT", "FAIL"]}
    quotes["HOT"] = {"rvol_crossed_threshold": False}
    scanner_worker_module._audit_volume_crossings(quotes, ["DONE", "FAIL"], {
        "lower_volume": {"DONE": {"evaluation_completed_ts": 107, "rvol": None,
                                   "rvol_valid": False, "reason": "rvol_unavailable"}}})
    calls = mock_redis.xadd.call_args_list
    assert len(calls) == 3
    events = {json.loads(c.args[1]["event"])["symbol"]: json.loads(c.args[1]["event"]) for c in calls}
    assert events["DONE"]["detection_to_evaluation_seconds"] == 7
    assert events["DONE"]["status"] == "evaluated"
    assert events["DONE"]["rvol_valid"] is False
    assert events["WAIT"]["status"] == "deferred"
    assert events["FAIL"]["status"] == "no_result"
    assert all(not event["entry_permission"] for event in events.values())
    assert all(c.kwargs == {"maxlen": 1000, "approximate": False} for c in calls)


def test_crossing_audit_failure_does_not_interrupt_scanning(mock_redis):
    mock_redis.xadd.side_effect = RuntimeError("Redis unavailable")
    scanner_worker_module._audit_volume_crossings(
        {"TEST": {"rvol_crossed_threshold": True}}, [], {})
    mock_redis.xadd.assert_called_once()


def test_baseline_uses_only_prior_days_with_or_without_current_daily_bar(monkeypatch):
    now = int(datetime(2026, 9, 23, 15, tzinfo=timezone.utc).timestamp())
    monkeypatch.setattr(scanner_worker_module, "_now_ts", lambda: now)
    monkeypatch.setattr(scanner_worker_module, "_RVOL_BASELINE_CACHE", {})
    dates = pd.date_range(end="2026-09-23T04:00:00Z", periods=23, freq="D")
    df = pd.DataFrame({"t": dates, "v": [1000] * 22 + [999999]})
    scanner_worker_module._cache_rvol_baseline("TEST", df, {"rvol_window_days": 20})
    assert scanner_worker_module._RVOL_BASELINE_CACHE["TEST"]["average"] == 1000
    scanner_worker_module._cache_rvol_baseline("TEST", df.iloc[:-1], {"rvol_window_days": 20})
    assert scanner_worker_module._RVOL_BASELINE_CACHE["TEST"]["average"] == 1000
    scanner_worker_module._cache_rvol_baseline("TEST", df.iloc[:-5], {"rvol_window_days": 20})
    assert "TEST" not in scanner_worker_module._RVOL_BASELINE_CACHE


@pytest.mark.parametrize("end,accepted", [
    ("2026-09-18", True),  # Friday baseline for Monday.
    ("2026-09-16", False),  # Old daily history must not trigger priority.
    ("2026-09-22", False),  # Future bars must not enter the denominator.
])
def test_baseline_rejects_stale_or_future_history(monkeypatch, end, accepted):
    now = int(datetime(2026, 9, 21, 15, tzinfo=timezone.utc).timestamp())
    monkeypatch.setattr(scanner_worker_module, "_now_ts", lambda: now)
    monkeypatch.setattr(scanner_worker_module, "_RVOL_BASELINE_CACHE", {})
    df = pd.DataFrame({"t": pd.date_range(end=end + "T05:00:00Z", periods=25), "v": 1000})
    scanner_worker_module._cache_rvol_baseline("TEST", df, {"rvol_window_days": 20})
    assert ("TEST" in scanner_worker_module._RVOL_BASELINE_CACHE) is accepted


@pytest.mark.parametrize("include_today", [True, False])
def test_current_rvol_uses_quote_not_final_daily_volume(monkeypatch, include_today):
    now = int(datetime(2026, 9, 23, 15, tzinfo=timezone.utc).timestamp())
    monkeypatch.setattr(scanner_worker_module, "_now_ts", lambda: now)
    monkeypatch.setattr(scanner_worker_module, "_RVOL_BASELINE_CACHE", {})
    df = pd.DataFrame({"t": pd.date_range(end="2026-09-23T05:00:00Z", periods=23),
                       "v": [1000] * 22 + [999999]})
    if not include_today:
        df = df.iloc[:-1]
    ratio, details = scanner_worker_module._current_session_rvol(
        "TEST", df, {"rvol_window_days": 20}, {"market_ts": now, "total_volume": 3500})
    assert ratio == 3.5
    assert details["valid"]
    assert details["baseline_sessions"] == 20
    assert details["baseline_last_date"] == "2026-09-22"


@pytest.mark.parametrize("age,volume,valid", [
    (0, 0, True), (120, 3000, True), (121, 3000, False),
    (-6, 3000, False), (86400, 3000, False),
    (0, None, False), (0, -1, False), (0, float("nan"), False),
    (0, float("inf"), False),
])
def test_current_rvol_rejects_invalid_quote_inputs(monkeypatch, age, volume, valid):
    now = int(datetime(2026, 9, 23, 15, tzinfo=timezone.utc).timestamp())
    monkeypatch.setattr(scanner_worker_module, "_now_ts", lambda: now)
    monkeypatch.setattr(scanner_worker_module, "_RVOL_BASELINE_CACHE", {})
    df = pd.DataFrame({"t": pd.date_range(end="2026-09-22T05:00:00Z", periods=25), "v": 1000})
    ratio, details = scanner_worker_module._current_session_rvol(
        "TEST", df, {"rvol_window_days": 20}, {"market_ts": now - age, "total_volume": volume})
    assert details["valid"] is valid
    assert (ratio is not None) is valid


@pytest.mark.parametrize("volume,macd_open,float_shares,scan_delay,bucket,intraday_calls", [
    (500, True, 1000000, 0, "lower_volume", 0),
    (2900, True, 1000000, 0, "lower_volume", 0),
    (3100, False, 1000000, 0, "priority", 0),
    (3100, True, 30000000, 0, "higher_float", 0),
    (3100, True, 1000000, 0, "actionable", 1),
    (3100, True, None, 0, "actionable", 1),
    (3100, True, 1000000, 121, "lower_volume", 0),
])
def test_volume_priority_handoff_rechecks_current_data_and_gates(
    monkeypatch, mock_redis, volume, macd_open, float_shares, scan_delay, bucket, intraday_calls,
):
    now = int(datetime(2026, 9, 23, 15, tzinfo=timezone.utc).timestamp())
    monkeypatch.setattr(scanner_worker_module, "_now_ts", lambda: now)
    monkeypatch.setattr(scanner_worker_module, "_RVOL_BASELINE_CACHE", {})
    df = pd.DataFrame({
        "t": pd.date_range(end="2026-09-22T05:00:00Z", periods=90, freq="B"),
        "o": 9.8, "h": 10.2, "l": 9.6, "c": 10.0, "v": 1000,
    })
    settings = dict(price_min=1, price_max=20, rvol_threshold=3,
                    rvol_window_days=90, float_max=20000000)
    quote = {"price": 10, "market_ts": now, "total_volume": volume,
             "previous_market_ts": now - 60, "previous_total_volume": min(volume, 2800)}
    scanner_worker_module._cache_rvol_baseline("TEST", df, settings)
    triggers = scanner_worker_module._volume_trigger_symbols({"TEST": quote}, settings)
    if volume >= 3000:
        assert triggers == ["TEST"]
        assert quote["rvol_crossed_threshold"] is True
        assert quote["rvol_proxy_entry_permission"] is False
        selected = scanner_worker_module._select_tickers(
            ["OTHER", "TEST", "QUIET"], 2, trigger_symbols=triggers)
        assert selected[0] == "TEST"

    monkeypatch.setattr(scanner_worker_module, "_now_ts", lambda: now + scan_delay)
    monkeypatch.setattr(scanner_worker_module, "_company_info", lambda _: {"float_shares": float_shares})
    monkeypatch.setattr(scanner_worker_module, "_macd_open", lambda _: macd_open)
    monkeypatch.setattr(scanner_worker_module, "_recent_articles", lambda _: [
        {"title": "TEST announces new product", "published_ts": now - 86400}])
    monkeypatch.setattr(scanner_worker_module, "_record_shadow_candidate", MagicMock(return_value={}))
    monkeypatch.setattr(scanner_worker_module, "record_decision", MagicMock())
    monkeypatch.setattr(scanner_worker_module, "should_analyze_candidate", lambda _: False)
    intraday = MagicMock(return_value=df)
    monkeypatch.setattr(scanner_worker_module, "_get_intraday", intraday)
    actual_bucket, row = scanner_worker_module._classify("TEST", df, settings, quote_snapshot=quote)
    assert actual_bucket == bucket
    assert intraday.call_count == intraday_calls
    if scan_delay:
        assert row["rvol"] is None
        assert row["reason"] == "rvol_unavailable"
    else:
        assert row["rvol"] == pytest.approx(volume / 1000)
    if volume < 3000 or not macd_open or float_shares is None or float_shares > 20000000 or scan_delay:
        assert row["decision"]["action"] != "long_candidate"
    assert row["news_watch"]


def test_quote_monitor_detects_interval_move_and_batches_snapshot(mock_redis):
    client = MagicMock()
    client.get_quotes.return_value = {
        "NVDA": {"quote": {"lastPrice": 12.5, "netPercentChange": 4.2, "totalVolume": 5000}},
        "AMD": {"quote": {"lastPrice": 8.0, "netPercentChange": -1.0, "totalVolume": 3000}},
    }
    mock_redis.hmget.return_value = [
        json.dumps({"price": 10.0}), json.dumps({"price": 8.5}),
    ]

    quotes, errors = _monitor_news_universe_quotes(client, ["NVDA", "AMD"])

    assert errors == []
    assert quotes["NVDA"]["interval_change_pct"] == pytest.approx(25.0)
    assert quotes["AMD"]["interval_change_pct"] == pytest.approx(-5.8823529)
    assert _moving_symbols(quotes, {"price_min": 1, "price_max": 20})[0] == "NVDA"
    client.get_quotes.assert_called_once_with(["NVDA", "AMD"], fields="quote")
    mock_redis.hset.assert_called_once()


def test_price_eligible_symbols_requires_current_in_range_quote():
    symbols = ["CHEAP", "GOOD", "HIGH", "MISSING"]
    quotes = {
        "CHEAP": {"price": 0.5},
        "GOOD": {"price": 12.5},
        "HIGH": {"price": 75.0},
    }

    assert _price_eligible_symbols(
        symbols,
        quotes,
        {"price_min": 1.1, "price_max": 20.0},
    ) == ["GOOD"]


def test_schwab_market_movers_merge_supported_exchanges():
    client = MagicMock()
    client.get_market_movers.side_effect = [
        [{"symbol": "ABCD"}, {"symbol": "LOW"}],
        [{"symbol": "WXYZ"}, {"symbol": "ABCD"}],
    ]

    symbols, errors = _schwab_market_mover_tickers(client)

    assert symbols == ["ABCD", "LOW", "WXYZ"]
    assert errors == []
    assert client.get_market_movers.call_count == 2


def test_calendar_candidates_include_earnings_and_exclude_market_news(monkeypatch, mock_redis):
    monkeypatch.setattr(scanner_worker_module.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(
        scanner_worker_module.persistent_store,
        "list_calendar_events",
        lambda **_kwargs: [
            {"symbols": ["ABCD"], "details": {"calendar_kind": "catalyst"}},
            {"symbols": ["NEWS"], "details": {"calendar_kind": "market_news"}},
        ],
    )
    monkeypatch.setattr(
        scanner_worker_module.persistent_store,
        "list_earnings_events",
        lambda **_kwargs: [{"symbol": "WXYZ"}],
    )

    assert _calendar_catalyst_tickers() == ["ABCD", "WXYZ"]


def test_automatic_universe_merges_sources_and_graph_relations(monkeypatch, mock_redis):
    monkeypatch.setattr(scanner_worker_module, "_get_recent_tickers", lambda: ["NWSA"])
    monkeypatch.setattr(scanner_worker_module, "_calendar_catalyst_tickers", lambda: ["CALS"])
    monkeypatch.setattr(
        scanner_worker_module,
        "_schwab_market_mover_tickers",
        lambda _client: (["MOVE"], []),
    )
    monkeypatch.setattr(
        scanner_worker_module,
        "_graph_related_candidates",
        lambda _symbols: {
            "NWSA": [{"symbol": "PEER", "kind": "supplier"}],
            "CALS": [{"symbol": "MOVE", "kind": "partnership"}],
        },
    )

    symbols, sources, movers, errors = _automatic_candidate_universe(MagicMock())

    assert symbols == ["NWSA", "CALS", "MOVE", "PEER"]
    assert sources["MOVE"] == ["graph_partnership", "schwab_mover"]
    assert sources["PEER"] == ["graph_supplier"]
    assert movers == ["MOVE"]
    assert errors == []


def test_watch_source_details_distinguish_discovery_from_company_relation():
    assert _watch_source_details(["schwab_mover", "graph_supplier"]) == [
        {
            "type": "schwab_mover",
            "category": "discovery",
            "label": "Schwab market movers",
        },
        {
            "type": "graph_supplier",
            "category": "company_relation",
            "label": "Company relationship: supplier",
        },
    ]


def test_article_coverage_metadata_is_isolated_per_ticker(mock_redis):
    now = scanner_worker_module._now_ts()
    mock_redis.zrevrangebyscore.return_value = ["shared", "nvda-only"]
    rows = {
        "shared": {"title": "NVIDIA and AMD partnership", "published_ts": now - 3_600},
        "nvda-only": {"title": "Earlier NVIDIA report", "published_ts": now - 2 * 86_400},
    }
    enrichments = {
        "shared": {
            "related_tickers": ["NVDA", "AMD"], "relevance_label": "business",
            "relevance_confidence": 0.95, "entity_link_version": 2,
            "article_verification_status": "canonical",
        },
        "nvda-only": {
            "related_tickers": ["NVDA"], "relevance_label": "business",
            "relevance_confidence": 0.95, "entity_link_version": 2,
            "article_verification_status": "canonical",
        },
    }
    mock_redis.hget.side_effect = lambda key, field=None: json.dumps(
        rows[key.removeprefix("news:article:")]
    )
    mock_redis.hmget.side_effect = lambda key, ids: [json.dumps(enrichments[item]) for item in ids]

    nvda = _recent_articles("NVDA", limit=10)
    amd = _recent_articles("AMD", limit=10)

    assert [row["coverage_count"] for row in nvda] == [2, 2]
    assert amd[0]["coverage_count"] == 1
    assert amd[0]["coverage_sequence"] == "earliest_in_lookback"


def test_rl_only_keeps_features_without_legacy_writes(monkeypatch, sample_dataframe):
    monkeypatch.setenv('LEGACY_RESEARCH_ENABLED', 'false')
    monkeypatch.setattr(scanner_worker_module, 'semantic_signal', MagicMock(return_value={}))
    monkeypatch.setattr(scanner_worker_module, 'market_signal', MagicMock(return_value={}))
    record = MagicMock(return_value={'id': 'rl-feature'})
    shadow, legacy = MagicMock(), MagicMock()
    monkeypatch.setattr(scanner_worker_module, 'record_feature_snapshot', record)
    monkeypatch.setattr(scanner_worker_module, 'upsert_shadow_opportunity', shadow)
    monkeypatch.setattr(scanner_worker_module, 'record_scanner_candidate', legacy)
    monkeypatch.setattr(scanner_worker_module, '_attach_gdelt_mentions', lambda rows: rows)
    monkeypatch.setattr(scanner_worker_module.persistent_store, 'list_market_observations_as_of', MagicMock(return_value=[]))
    result = scanner_worker_module._record_shadow_candidate('ABC', ts=1000, df=sample_dataframe,
        intraday=None, price=10, rvol=4, macd_open=True, articles=[],
        decision={'action': 'buy'}, float_shares=1000000, scanner_settings={'price_min': 1, 'price_max': 20})
    assert result['id'] == 'rl-feature'
    record.assert_called_once()
    shadow.assert_not_called()
    legacy.assert_not_called()


def test_shadow_candidate_snapshot_is_ranking_only(monkeypatch, sample_dataframe):
    semantic = MagicMock(return_value={"hype": {
        "feature_version": "news-hype-v1",
        "evidence_article_ids": ["article-2", "article-1"],
    }})
    market = MagicMock(return_value={"price": 10.0})
    record = MagicMock(return_value={"id": "snapshot-1"})
    opportunity = MagicMock(return_value={"opportunity_key": "opportunity-1"})
    monkeypatch.setattr(scanner_worker_module, "semantic_signal", semantic)
    monkeypatch.setattr(scanner_worker_module, "market_signal", market)
    monkeypatch.setattr(scanner_worker_module, "record_feature_snapshot", record)
    monkeypatch.setattr(scanner_worker_module, "upsert_shadow_opportunity", opportunity)
    monkeypatch.setattr(scanner_worker_module, "_attach_gdelt_mentions", lambda rows: rows)
    monkeypatch.setattr(
        scanner_worker_module.persistent_store,
        "list_market_observations_as_of",
        MagicMock(return_value=[]),
    )

    result = scanner_worker_module._record_shadow_candidate(
        "NVDA",
        ts=1_000,
        df=sample_dataframe,
        intraday=None,
        price=10,
        rvol=4,
        macd_open=True,
        articles=[],
        decision={"action": "buy", "trade_plan": {"stop_loss": 9.8}},
        float_shares=1_000_000,
        scanner_settings={"price_min": 1, "price_max": 20},
    )

    assert result["id"] == "snapshot-1"
    payload = record.call_args.args[1]
    assert payload["shadow_candidate"] is True
    assert payload["ranking_only"] is True
    assert payload["live_activation_eligible"] is False
    assert payload["entry_eligible"] is False
    assert payload["watch_inputs"] == {
        "version": "scanner-watch-inputs-v1", "observed_at": 1_000,
        "price": 10, "rvol": 4, "float_shares": 1_000_000,
        "scanner_settings": {"price_min": 1, "price_max": 20},
    }
    assert payload["action"] == "buy"
    assert payload["shadow_opportunity_key"].startswith("shadow-opportunity:")
    assert len(payload["shadow_opportunity_key"]) == len("shadow-opportunity:") + 64
    record.assert_called_once_with(
        "NVDA", payload, source="scanner_shadow", ts=1_000
    )
    opportunity.assert_called_once_with(result)


def test_low_rvol_news_candidate_is_captured_before_rejection(monkeypatch, sample_dataframe):
    record = MagicMock(return_value={"id": "low-rvol-snapshot"})
    monkeypatch.setattr(scanner_worker_module, "_latest_quote", lambda *args, **kwargs: 10.0)
    monkeypatch.setattr(scanner_worker_module, "_current_session_rvol", lambda *args: (1.0, {"valid": True}))
    monkeypatch.setattr(scanner_worker_module, "_recent_articles", lambda _symbol: [{"title": "News"}])
    monkeypatch.setattr(
        scanner_worker_module,
        "_company_info",
        lambda _symbol: {"name": "NVIDIA Corporation", "float_shares": 10_000_000},
    )
    monkeypatch.setattr(scanner_worker_module, "_record_shadow_candidate", record)

    bucket, row = scanner_worker_module._classify(
        "NVDA",
        sample_dataframe,
        {
            "price_min": 1,
            "price_max": 50,
            "rvol_threshold": 3,
            "rvol_window_days": 60,
            "float_max": 20_000_000,
        },
        market_client=MagicMock(),
    )

    assert bucket == "lower_volume"
    assert row["shadow_feature_id"] == "low-rvol-snapshot"
    assert record.call_args.kwargs["decision"]["shadow_stage"] == "low_rvol"
    assert row["company"]["name"] == "NVIDIA Corporation"
    assert row["decision"]["catalyst"]
    assert row["macd_open"] in {True, False}


def test_price_rejection_retains_complete_available_context(monkeypatch, sample_dataframe):
    monkeypatch.setattr(scanner_worker_module, "_latest_quote", lambda *args, **kwargs: 75.0)
    monkeypatch.setattr(scanner_worker_module, "_current_session_rvol", lambda *args: (2.5, {"valid": True}))
    monkeypatch.setattr(scanner_worker_module, "_macd_open", lambda _df: True)
    monkeypatch.setattr(
        scanner_worker_module,
        "_company_info",
        lambda _symbol: {
            "name": "NVIDIA Corporation", "float_shares": 2_000_000_000,
            "market_cap": 4_000_000_000_000,
        },
    )
    monkeypatch.setattr(
        scanner_worker_module,
        "_recent_articles",
        lambda _symbol: [{"title": "NVIDIA product launch", "published_ts": 1_800_000_000}],
    )

    bucket, row = scanner_worker_module._classify(
        "NVDA",
        sample_dataframe,
        {
            "price_min": 1, "price_max": 50, "rvol_threshold": 3,
            "rvol_window_days": 20, "float_max": 20_000_000,
        },
        market_client=MagicMock(),
    )

    assert bucket == "too_expensive"
    assert row["reason"] == "price_out_of_range"
    assert row["company"]["name"] == "NVIDIA Corporation"
    assert row["rvol"] == 2.5
    assert row["macd_open"] is True
    assert row["articles"][0]["title"] == "NVIDIA product launch"
    assert row["decision"]["trade_plan"]


@pytest.mark.parametrize("threshold", [0, 3])
def test_unavailable_rvol_stays_watched_without_recording_fake_zero(monkeypatch, sample_dataframe, threshold):
    monkeypatch.setattr(scanner_worker_module, "_latest_quote", lambda *args, **kwargs: 10.0)
    monkeypatch.setattr(scanner_worker_module, "_current_session_rvol", lambda *args: (
        None, {"valid": False, "reason": "quote_volume_stale_or_missing"}))
    monkeypatch.setattr(scanner_worker_module, "_company_info", lambda _: {"float_shares": 1000000})
    monkeypatch.setattr(scanner_worker_module, "_recent_articles", lambda _: [{"title": "News"}])
    monkeypatch.setattr(scanner_worker_module, "_macd_open", lambda _: True)
    record = MagicMock()
    monkeypatch.setattr(scanner_worker_module, "_record_shadow_candidate", record)
    bucket, row = scanner_worker_module._classify("TEST", sample_dataframe, {
        "price_min": 1, "price_max": 20, "rvol_threshold": threshold,
        "rvol_window_days": 20, "float_max": 20000000,
    })
    assert bucket == "lower_volume"
    assert row["rvol"] is None
    assert row["decision"]["technicals"]["rvol"] is None
    assert row["reason"] == "rvol_unavailable"
    assert "rvol_unavailable" in row["decision"]["reasons"]
    assert row["decision"]["action"] != "long_candidate"
    record.assert_not_called()

def test_get_daily_success(mock_market_client, sample_dataframe):
    mock_market_client.get_daily_frame.return_value = sample_dataframe
    df = _get_daily("AAPL")
    assert df is not None
    assert isinstance(df, pd.DataFrame)
    assert len(df) == len(sample_dataframe)

def test_get_daily_failure(mock_market_client):
    mock_market_client.get_daily_frame.return_value = pd.DataFrame()
    df = _get_daily("UNKNOWN")
    assert df is None

def test_get_intraday_success(mock_market_client):
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=1)
    mock_market_client.get_intraday_frame.return_value = pd.DataFrame({
        "t": [old, now - timedelta(minutes=5), now],
        "o": [1.0, 2.0, 3.0],
        "h": [1.1, 2.1, 3.1],
        "l": [0.9, 1.9, 2.9],
        "c": [1.05, 2.05, 3.05],
        "v": [100, 200, 300],
    })
    df = _get_intraday("AAPL")
    assert df is not None
    assert len(df) == 3
    mock_market_client.get_intraday_frame.assert_called_once_with("AAPL", days=2, frequency=5)
    assert set(["t", "o", "h", "l", "c", "v"]).issubset(df.columns)

def test_avg_volume_ratio(sample_dataframe):
    # Manually calculate expected AVR for the sample_dataframe
    window = 90
    cur_vol = float(sample_dataframe["v"].iloc[-1])
    avg = float(sample_dataframe["v"].iloc[-window-1:-1].mean())
    expected_avr = cur_vol / avg if avg > 0 else 0.0

    avr = _avg_volume_ratio(sample_dataframe)
    assert avr == pytest.approx(expected_avr)


def test_indicator_helpers_accept_single_symbol_yahoo_multiindex():
    closes = list(range(10, 50))
    frame = pd.DataFrame({
        ("Close", "NVDA"): closes,
        ("Volume", "NVDA"): [100] * 39 + [300],
    })
    frame.columns = pd.MultiIndex.from_tuples(frame.columns)

    assert _avg_volume_ratio(frame, 20) == pytest.approx(3.0)
    assert _price_ok(
        frame,
        settings={"price_min": 1, "price_max": 100},
    ) is True

def test_macd_open(sample_dataframe):
    # Mock pandas_ta.macd to control its output
    with patch('pandas_ta.macd') as mock_macd:
        mock_macd_df = pd.DataFrame({
            "MACD_12_26_9": [0.1, 0.2, 0.3],
            "MACDs_12_26_9": [0.05, 0.15, 0.2],
            "MACDh_12_26_9": [0.05, 0.05, 0.1]
        })
        mock_macd.return_value = mock_macd_df
        assert _macd_open(sample_dataframe) is True # Based on mock_macd_df last row

        mock_macd_df_false = pd.DataFrame({
            "MACD_12_26_9": [0.1, 0.2, 0.1],
            "MACDs_12_26_9": [0.05, 0.15, 0.2],
            "MACDh_12_26_9": [0.05, 0.05, -0.1]
        })
        mock_macd.return_value = mock_macd_df_false
        assert _macd_open(sample_dataframe) is False


def test_price_ok(sample_dataframe):
    settings = {
        "price_min": 1,
        "price_max": 100,
        "rvol_threshold": 3,
        "rvol_window_days": 90,
        "float_max": 20_000_000,
        "scan_interval": 60,
    }
    assert _price_ok(sample_dataframe, settings=settings) is True

    # Test with price out of bounds
    sample_dataframe.loc[sample_dataframe.index[-1], 'c'] = 0.5 # Below min
    assert _price_ok(sample_dataframe, settings=settings) is False
    sample_dataframe.loc[sample_dataframe.index[-1], 'c'] = 150.0 # Above max
    assert _price_ok(sample_dataframe, settings=settings) is False

def test_score(sample_dataframe):
    # Default _score just returns avr
    avr = 5.5
    score = _score("SYM", sample_dataframe, avr)
    assert score == avr

def test_scanner_worker_run_forever(scanner_worker, mock_redis, mock_market_client, sample_dataframe):
    # Mock dependencies
    with (
        patch('workers.scanner_worker._get_recent_tickers', return_value=["AAPL", "GOOG"]),
        patch(
            'workers.scanner_worker._automatic_candidate_universe',
            return_value=(
                ["AAPL", "GOOG"],
                {"AAPL": ["recent_news"], "GOOG": ["recent_news"]},
                [],
                [],
            ),
        ),
        patch(
            'workers.scanner_worker._monitor_news_universe_quotes',
            return_value=(
                {"AAPL": {"price": 9.0}, "GOOG": {"price": 8.0}},
                [],
            ),
        ),
        patch('workers.scanner_worker._get_daily', return_value=sample_dataframe),
        patch(
            'workers.scanner_worker._classify',
            side_effect=lambda sym, df, settings, market_client=None, quote_snapshot=None: (
                "actionable",
                {"symbol": sym, "avr": 10.0, "rvol": 10.0, "macd_open": True, "score": 10.0, "ts": 123},
            ),
        ),
        patch('time.sleep', side_effect=lambda _: scanner_worker.stop()),
    ): # Prevent actual sleep

        # Run once
        scanner_worker.run_forever()

        # Assertions
        pipe = mock_redis.pipeline.return_value
        assert mock_redis.pipeline.call_count > 0
        assert pipe.delete.call_count == 6 # focus plus each watchlist
        assert pipe.hset.call_count >= 2 # watchlist rows plus focus rows
        pipe.execute.assert_called_once()

        # Verify hset arguments for a sample result
        hset_calls = pipe.hset.call_args_list
        assert any(args[1] == "AAPL" for args, kwargs in hset_calls)
        assert any(args[1] == "GOOG" for args, kwargs in hset_calls)

        # Check content of a stored result
        # Find the call for AAPL
        aapl_call_args = next(args for args, kwargs in hset_calls if args[1] == "AAPL")
        aapl_result = json.loads(aapl_call_args[2])
        assert aapl_result["symbol"] == "AAPL"
        assert aapl_result["avr"] == 10.0
        assert aapl_result["macd_open"] is True
        assert aapl_result["score"] == 10.0
