import json

import pytest

from drl_agent.alpaca_data_cache import _time
from drl_agent.dataset import encode_dataset
from drl_agent.historical_corpus import export_corpus
from drl_agent.historical_corpus_prices import merge, subtract, plan
from test.test_drl_historical_news import article
from test.test_alpaca_replay_export import archive


def test_merge_and_subtract_never_redownload_known_windows():
    assert merge([(10, 20), (15, 25), (25, 30)]) == [(10, 30)]
    assert subtract([(10, 30)], [(5, 12), (15, 17), (20, 40)]) == [(12, 15), (17, 20)]
    assert subtract([(10, 30)], [(0, 40)]) == []
    assert subtract([(10, 30)], []) == [(10, 30)]


def corpus(tmp_path):
    output = tmp_path / "corpus"
    export_corpus(output, start="2026-09-22", end="2026-09-24", enrichment_cutoff_ts=1800000000,
                  loader=lambda **kw: [article(published_ts=kw["start_ts"] + 100)])
    return output


def test_plan_reuses_verified_archives_without_claiming_bar_completeness(tmp_path):
    output = corpus(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    raw = archive()
    (cache / "ABC_sip_raw.json").write_text(encode_dataset(raw))
    result = plan(output, cache)
    assert len(result["reusable_archives"]) == 1
    assert result["symbols"] == 2 and result["merged_windows"] == 2
    assert result["daily_requirements"] > len(result["requests"])
    low = int(_time(raw["request"]["start"]).timestamp())
    high = int(_time(raw["request"]["end"]).timestamp()) + 1
    for request in result["requests"]:
        assert request["start_ts"] % 60 == request["end_ts"] % 60 == 0
        assert request["end_ts"] - request["start_ts"] <= 31 * 86400
        if request["symbol"] == "ABC":
            assert request["end_ts"] <= low or request["start_ts"] >= high
    assert result["network_calls"] == 0 and not result["training_started"]
    raw["summary"]["rows"] += 1
    (cache / "ABC_sip_raw.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="digest"):
        plan(output, cache)


def test_plan_rejects_missing_daily_shard_in_complete_progress(tmp_path):
    output = corpus(tmp_path)
    progress = json.loads((output / "progress.json").read_text())
    progress["completed"].pop()
    (output / "progress.json").write_text(json.dumps(progress))
    with pytest.raises(ValueError, match="missing"):
        plan(output, tmp_path)
