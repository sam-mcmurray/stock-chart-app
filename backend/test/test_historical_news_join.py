import hashlib

import pandas as pd
import pytest

from drl_agent.dataset import encode_dataset
from drl_agent.hf_replay_export import build_export
from drl_agent.historical_news import build_universe
from drl_agent.historical_news_join import join_news
from drl_agent.synthetic_depth import DepthConfig
from test.test_hf_replay_export import META, bars

START = int(pd.Timestamp("2018-07-16T13:30Z").timestamp())


def inputs():
    prices = build_export(bars(), META, start="2018-07-16T13:30Z", end="2018-07-16T13:33Z",
                          depth_config=DepthConfig())
    def article(identity, symbol, publication):
        return {"record_type": "news", "record_id": identity, "version_key": identity,
                "published_ts": publication, "observed_ts": START + 100000,
                "result": {"relevance_label": "finance", "article_verification_status": "publisher",
                           "entity_link_version": 2, "related_tickers": [symbol]}}
    news = build_universe([article("match", "ABC", START + 30), article("future", "ABC", START + 150),
                           article("other", "XYZ", START)], start_ts=START, end_ts=START + 86400,
                          enrichment_cutoff_ts=START + 200000)
    return prices, news


def resign(data, key):
    data.pop(key, None)
    data[key] = hashlib.sha256(encode_dataset(data).encode()).hexdigest()


def test_published_news_only_and_no_other_company_or_future_news():
    prices, news = inputs()
    result = join_news(prices, news)
    assert [s["news_watch_active"] for s in result["states"]] == [False, True, True]
    assert [a["record_id"] for a in result["articles"]] == ["match"]
    assert result["articles"][0]["enriched_observed_ts"] > result["states"][-1]["decision_ts"]
    assert all(s["synthetic_depth_candle_id"] == s["candle_id"] for s in result["states"])
    assert all(not s["entry_eligible"] and s["scanner_eligibility"] == "unknown" for s in result["states"])
    assert result == join_news(prices, news)


def test_watch_expiry_not_extended_by_repeated_candles():
    prices, news = inputs()
    news["watch_days"] = 1
    for row in news["candidates"]:
        row["published_ts"] = START - 86400
        row["replay_available_ts"] = START - 86400 + 90
    resign(news, "manifest_sha256")
    result = join_news(prices, news)
    assert [s["news_watch_active"] for s in result["states"]] == [True, False, False]


def test_hash_tampering_rejected():
    prices, news = inputs()
    prices["candles"][0]["close"] = 999
    with pytest.raises(ValueError, match="digest"):
        join_news(prices, news)


def test_unmatched_depth_inputs_rejected_even_with_resigned_parent():
    prices, news = inputs()
    prices["candles"][0]["close"] = 11
    resign(prices, "export_sha256")
    with pytest.raises(ValueError, match="does not match"):
        join_news(prices, news)
