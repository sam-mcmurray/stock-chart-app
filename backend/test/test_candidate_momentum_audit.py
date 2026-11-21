import hashlib
import json

import pytest

from drl_agent.candidate_momentum_audit import audit
from drl_agent.dataset import encode_dataset


def sign(value, key):
    value.pop(key, None)
    value[key] = hashlib.sha256(encode_dataset(value).encode()).hexdigest()
    return value


def fixture(tmp_path):
    states = [
        (True, ["historical_float_unknown"]),
        (True, ["historical_float_unknown", "macd_not_bullish"]),
        (False, ["historical_float_unknown", "rvol_below_threshold"]),
    ]
    watch = sign({
        "version": "historical-watch-v2", "config": {"maximum_float": 20_000_000},
        "source_hashes": {"ABC": "join"}, "feature_hashes": {"ABC": "features"},
        "frames": [{"decision_ts": 100 + i, "watching": [{
            "symbol": "ABC", "momentum_triggered": momentum, "research_entry_ready": False,
            "entry_block_reasons": reasons,
        }]} for i, (momentum, reasons) in enumerate(states)],
    }, "watch_sha256")
    path = tmp_path / "repair"
    path.mkdir()
    (path / "watch.json").write_text(json.dumps(watch))
    index = sign({
        "version": "candidate-dataset-index-v1", "totals": {"momentum_states": 2},
        "sessions": {"ABC:2026-07-01": {
            "symbol": "ABC", "date": "2026-07-01", "directory": "repair",
            "watch_sha256": watch["watch_sha256"], "join_sha256": "join",
            "features_sha256": "features",
            "counts": {"watched_states": 3, "momentum_states": 2, "entry_ready_states": 0},
        }},
    }, "index_sha256")
    return index


def test_audit_counts_only_momentum_blockers(tmp_path):
    result = audit(tmp_path, fixture(tmp_path))
    assert result["totals"]["float_only_states"] == 1
    assert result["momentum_blockers"] == {"historical_float_unknown": 2, "macd_not_bullish": 1}
    assert result["sessions"][0]["float_only_decision_ts"] == [100]
    assert result["broker_calls"] == result["network_calls"] == 0
    assert result["training_started"] is False


@pytest.mark.parametrize("change", ["digest", "count", "lineage", "escape", "watch"])
def test_audit_rejects_changed_or_inconsistent_artifacts(tmp_path, change):
    index = fixture(tmp_path)
    row = index["sessions"]["ABC:2026-07-01"]
    if change == "digest":
        index["totals"]["momentum_states"] = 99
    elif change == "count":
        row["counts"]["momentum_states"] = 99
    elif change == "lineage":
        row["features_sha256"] = "wrong"
    elif change == "escape":
        row["directory"] = "../outside"
    else:
        path = tmp_path / "repair" / "watch.json"
        value = json.loads(path.read_text())
        value["config"]["maximum_float"] = 1
        path.write_text(json.dumps(value))
    if change != "digest":
        sign(index, "index_sha256")
    with pytest.raises(ValueError):
        audit(tmp_path, index)


def test_empty_index_is_not_training_readiness(tmp_path):
    index = sign({"version": "candidate-dataset-index-v1", "sessions": {},
                  "totals": {"momentum_states": 0}}, "index_sha256")
    result = audit(tmp_path, index)
    assert result["audited_sessions"] == 0
    assert result["sessions"] == []
    assert result["training_started"] is False
