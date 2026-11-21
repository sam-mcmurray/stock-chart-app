from copy import deepcopy
from datetime import datetime, timezone

import pytest

from drl_agent.alpaca_data_cache import URL
from drl_agent.alpaca_replay_export import build_export
from drl_agent.historical_features import build_features
from drl_agent.historical_news import build_universe
from drl_agent.historical_news_join import join_news
from drl_agent.historical_watch import build_watch
from drl_agent.synthetic_depth import DepthConfig
from drl_agent.synthetic_execution import SyntheticExecution
from drl_agent.watch_replay import WatchConfig
from test.test_historical_features import fixture
from test.test_synthetic_execution import resign


def archive():
    candles = fixture()["candles"]
    bars = [{"t": datetime.fromtimestamp(c["start_ts"], timezone.utc).isoformat(),
             "o": c["close"], "h": c["high"], "l": c["low"], "c": c["close"], "v": c["volume"]}
            for c in candles]
    result = {"version": "alpaca-sip-raw-cache-v1", "url": URL, "archived_at": "2026-09-23T00:00:00Z",
              "request": {"symbols": "ABC", "feed": "sip", "adjustment": "raw", "asof": "-",
                          "timeframe": "1Min", "start": "2026-09-21T13:30:00Z", "end": "2026-09-22T19:59:59Z"},
              "responses": [{"bars": {"ABC": bars}}], "summary": {"rows": len(bars)}}
    resign(result, "archive_sha256")
    return result


def joined(data):
    start = data["candles"][0]["start_ts"]
    news = build_universe([], start_ts=start, end_ts=start + 86400 * 2, enrichment_cutoff_ts=start + 86400 * 3)
    return join_news(data, news)


def test_nominal_sip_survives_news_features_watch_and_execution():
    source = archive()
    before = deepcopy(source)
    exported = build_export(source, depth_config=DepthConfig())
    data = joined(exported)
    features = build_features(data, minimum_sessions=1)
    last = features["states"][-1]
    assert last["historical_nominal_price"] == 10
    assert last["observed_adjusted_close"] is None
    assert last["volume_source"] == "alpaca_sip"
    assert last["rvol"] == 1 and last["complete_prior_session_count"] == 1
    watch = build_watch([data], [features], [], config=WatchConfig(1, 20, 0, 20_000_000, 10))
    reasons = watch["frames"][-1]["rejected"]["ABC"]
    assert reasons == ["news_watch_inactive"]
    sim = SyntheticExecution(data, session_date="2026-09-22")
    while not sim.done:
        sim.step()
    assert sim.report()["cash"] == 500 and sim.report()["broker_calls"] == 0
    assert source == before
    assert exported["candles"][0]["available_ts"] == exported["candles"][0]["start_ts"] + 60


@pytest.mark.parametrize("field,value", [("feed", "iex"), ("adjustment", "all"), ("asof", "2026-09-23")])
def test_rejects_wrong_source_even_with_valid_digest(field, value):
    data = archive()
    data["request"][field] = value
    resign(data, "archive_sha256")
    with pytest.raises(ValueError, match="SIP/raw"):
        build_export(data)


def test_rejects_corruption_duplicate_and_unknown_export_version():
    data = archive()
    data["summary"]["rows"] += 1
    with pytest.raises(ValueError, match="digest"):
        build_export(data)
    data = archive()
    data["responses"][0]["bars"]["ABC"].append(data["responses"][0]["bars"]["ABC"][-1])
    resign(data, "archive_sha256")
    with pytest.raises(ValueError, match="nonchronological"):
        build_export(data)
    exported = build_export(archive())
    exported["version"] = "unknown-source"
    resign(exported, "export_sha256")
    with pytest.raises(ValueError, match="Unsupported"):
        joined(exported)


def test_partial_consolidated_history_can_be_used_only_as_labeled_proxy():
    source = archive()
    del source["responses"][0]["bars"]["ABC"][100]
    source["summary"]["rows"] -= 1
    resign(source, "archive_sha256")
    data = joined(build_export(source))
    features = build_features(data, minimum_sessions=1)
    assert features["states"][-1]["rvol"] is None
    assert features["states"][-1]["observed_volume_rvol"] == pytest.approx(390 / 389)
    config = WatchConfig(1, 20, 0, 20_000_000, 10)
    strict = build_watch([data], [features], [], config=config)
    assert not strict["frames"][-1]["watching"]
    partial = build_watch([data], [features], [], config=config, allow_partial_volume_rvol=True)
    assert not partial["frames"][-1]["watching"]
    assert partial["partial_volume_rvol_states"] == 390
    assert not partial["training_ready"]
