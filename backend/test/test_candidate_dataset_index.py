import json

import pytest

from drl_agent.candidate_dataset_index import build_index
from drl_agent.candidate_sessions import assemble
from test.test_candidate_sessions import inputs


def prepared(tmp_path):
    plan, receipts, config = inputs(tmp_path)
    root = tmp_path / "baseline"
    args = (plan, tmp_path, tmp_path, tmp_path, tmp_path, receipts)
    assemble(*args, root, config, max_sessions=1)
    assemble(*args, root / "repairs/combined", config, symbols=["ABC"], max_sessions=10)
    return root, plan


def test_merge_deduplicates_and_retains_earlier_no_entry_sessions(tmp_path):
    root, plan = prepared(tmp_path)
    result = build_index(root, plan, ["repairs/combined"])
    assert result["totals"]["sessions"] == 2 and result["replacements"] == 1
    assert result["missing_sessions"] == []
    assert result["totals"]["entry_ready_states"] == 0
    assert not result["training_ready"] and result["network_calls"] == 0
    assert all(r["directory"].startswith("repairs/combined/") for r in result["sessions"].values())
    assert build_index(root, plan, ["repairs/combined"])["index_sha256"] == result["index_sha256"]
    assert build_index(root, plan)["missing_sessions"] == ["ABC:2026-09-21"]


@pytest.mark.parametrize("repairs", [["../outside"], ["/tmp"], ["."], ["repairs/combined", "repairs/combined"]])
def test_unsafe_or_duplicate_source_rejected(tmp_path, repairs):
    root, plan = prepared(tmp_path)
    with pytest.raises(ValueError, match="escapes root|Duplicate"):
        build_index(root, plan, repairs)


def test_corrupt_feature_rejected(tmp_path):
    root, plan = prepared(tmp_path)
    path = root / "repairs/combined/ABC_2026-09-22/features.json"
    value = json.loads(path.read_text())
    value["states"][0]["rvol"] = 999
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="digest"):
        build_index(root, plan, ["repairs/combined"])


@pytest.mark.parametrize("change", ["policy", "evidence", "running"])
def test_incompatible_or_active_source_rejected(tmp_path, change):
    root, plan = prepared(tmp_path)
    if change == "running":
        path = root / "repairs/combined/progress.json"
        value = json.loads(path.read_text())
        value["status"] = "running"
    else:
        path = root / ("config.json" if change == "evidence" else "repairs/combined/config.json")
        value = json.loads(path.read_text())
        if change == "policy":
            value["watch_config"]["minimum_rvol"] = 4
        else:
            value["trade_hashes"] = ["evidence-not-in-repair"]
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="policy changed|Unstable"):
        build_index(root, plan, ["repairs/combined"])


@pytest.mark.parametrize("with_history", [False, True])
def test_resigned_price_change_is_not_accepted_as_volume_repair(tmp_path, with_history):
    from drl_agent.corpus_gap_audit import signed
    root, plan = prepared(tmp_path)
    source = "repairs/combined"
    if with_history:
        from test.test_candidate_sessions import earlier_archive
        plan, receipts, config = inputs(tmp_path)
        source = "history"
        assemble(plan, tmp_path, tmp_path, tmp_path, tmp_path, receipts, root / source, config,
                 history_archives=[earlier_archive()])
    directory = root / source / "ABC_2026-09-22"
    def update(name, key, change):
        path = directory / (name + ".json")
        value = json.loads(path.read_text())
        value.pop(key)
        change(value)
        signed(value, key)
        path.write_text(json.dumps(value))
        return value[key]
    features_hash = update("features", "features_sha256",
        lambda v: v["states"][0].update(historical_nominal_price=999))
    watch_hash = update("watch", "watch_sha256", lambda v: v["feature_hashes"].update(ABC=features_hash))
    summary_hash = update("summary", "summary_sha256",
        lambda v: v.update(features_sha256=features_hash, watch_sha256=watch_hash))
    path = root / source / "progress.json"
    value = json.loads(path.read_text())
    value["completed"]["ABC:2026-09-22"].update(features_sha256=features_hash,
                                              watch_sha256=watch_hash, summary_sha256=summary_hash)
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="changes prices/rules"):
        build_index(root, plan, [source])


def test_active_assembly_lock_blocks_index(tmp_path):
    import fcntl
    root, plan = prepared(tmp_path)
    with (root / ".assemble.lock").open("r") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            build_index(root, plan)


def test_session_scoped_source_merges_and_rejects_outside_scope(tmp_path):
    plan, receipts, config = inputs(tmp_path)
    root = tmp_path / "baseline"
    args = (plan, tmp_path, tmp_path, tmp_path, tmp_path, receipts)
    assemble(*args, root, config, max_sessions=1)
    assemble(*args, root / "missing", config, sessions=["ABC:2026-09-21"])
    result = build_index(root, plan, ["missing"])
    assert result["totals"]["sessions"] == 2 and result["replacements"] == 0
    path = root / "missing/config.json"
    value = json.loads(path.read_text())
    value["session_scope"] = ["ABC:2026-09-22"]
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="outside source scope"):
        build_index(root, plan, ["missing"])


def test_float_additions_require_opt_in_and_cannot_remove_evidence(tmp_path):
    from test.test_candidate_sessions import float_record
    root, plan = prepared(tmp_path)
    plan, receipts, config = inputs(tmp_path)
    assemble(plan, tmp_path, tmp_path, tmp_path, tmp_path, receipts, root / "floats", config,
             max_sessions=1, float_evidence=[float_record()])
    with pytest.raises(ValueError, match="policy changed"):
        build_index(root, plan, ["floats"])
    result = build_index(root, plan, ["floats"], allow_float_additions=True)
    assert result["float_revision_policy"].startswith("explicit_additions_only")
    assert len(result["sessions"]["ABC:2026-09-22"]["float_evidence_hashes"]) == 1
    assert not result["training_ready"]
    with pytest.raises(ValueError, match="Float evidence removed"):
        build_index(root, plan, ["floats", "repairs/combined"], allow_float_additions=True)
    path = root / "floats/config.json"
    value = json.loads(path.read_text())
    value["float_evidence"][0]["value"] += 1
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="Float evidence lineage"):
        build_index(root, plan, ["floats"], allow_float_additions=True)


def test_resigned_float_watch_must_match_recomputed_evidence(tmp_path):
    from test.test_candidate_sessions import float_record
    from test.test_synthetic_execution import resign
    root, plan = prepared(tmp_path)
    plan, receipts, config = inputs(tmp_path)
    assemble(plan, tmp_path, tmp_path, tmp_path, tmp_path, receipts, root / "floats", config,
             max_sessions=1, float_evidence=[float_record()])
    directory = root / "floats/ABC_2026-09-22"
    watch = json.loads((directory / "watch.json").read_text())
    watch["evidence"][0]["value"] += 1
    resign(watch, "watch_sha256")
    (directory / "watch.json").write_text(json.dumps(watch))
    summary = json.loads((directory / "summary.json").read_text())
    summary["watch_sha256"] = watch["watch_sha256"]
    resign(summary, "summary_sha256")
    (directory / "summary.json").write_text(json.dumps(summary))
    path = root / "floats/progress.json"
    progress = json.loads(path.read_text())
    progress["completed"]["ABC:2026-09-22"].update(
        watch_sha256=watch["watch_sha256"], summary_sha256=summary["summary_sha256"])
    path.write_text(json.dumps(progress))
    with pytest.raises(ValueError, match="Watch does not match declared float"):
        build_index(root, plan, ["floats"], allow_float_additions=True)


def test_explicit_earlier_history_can_replace_provenance_not_prices(tmp_path):
    from test.test_candidate_sessions import earlier_archive
    plan, receipts, config = inputs(tmp_path)
    root = tmp_path / "baseline"
    args = (plan, tmp_path, tmp_path, tmp_path, tmp_path, receipts)
    assemble(*args, root, config, max_sessions=1)
    assemble(*args, root / "history", config, max_sessions=1, history_archives=[earlier_archive()])
    result = build_index(root, plan, ["history"])
    assert result["totals"]["sessions"] == result["replacements"] == 1
    row = result["sessions"]["ABC:2026-09-22"]
    original = build_index(root, plan)["sessions"]["ABC:2026-09-22"]
    assert row["join_sha256"] != original["join_sha256"]
    assert row["session_content_sha256"] == original["session_content_sha256"]
    assert len(row["price_archive_hashes"]) == 2
    path = root / "history/config.json"
    value = json.loads(path.read_text())
    value.pop("history_extensions")
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="changes prices/rules"):
        build_index(root, plan, ["history"])


def test_unrelated_session_need_not_copy_history_but_replacement_must_retain_it(tmp_path):
    from test.test_candidate_sessions import earlier_archive
    plan, receipts, config = inputs(tmp_path)
    root = tmp_path / "baseline"
    args = (plan, tmp_path, tmp_path, tmp_path, tmp_path, receipts)
    assemble(*args, root, config, max_sessions=1)
    assemble(*args, root / "history", config, max_sessions=1,
             history_archives=[earlier_archive()])
    assemble(*args, root / "other", config, max_sessions=10)
    path = root / "other/progress.json"
    progress = json.loads(path.read_text())
    original = json.dumps(progress)
    del progress["completed"]["ABC:2026-09-22"]
    path.write_text(json.dumps(progress))
    result = build_index(root, plan, ["history", "other"])
    assert result["totals"]["sessions"] == 2
    assert result["sessions"]["ABC:2026-09-22"]["directory"].startswith("history/")
    path.write_text(original)
    with pytest.raises(ValueError, match="changes prices/rules"):
        build_index(root, plan, ["history", "other"])
