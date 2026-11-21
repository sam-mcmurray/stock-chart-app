import hashlib
import json

import pandas as pd
import pytest

from drl_agent.hf_replay_export import build_export, export_archive
from drl_agent.synthetic_depth import DepthConfig

META = {"provider": "HF Data Library", "version": "raw", "symbol": "ABC", "sha256": "fixture"}


def bars(day="2018-07-16"):
    return pd.DataFrame({"datetime": pd.to_datetime([f"{day} 09:30", f"{day} 09:31", f"{day} 09:32"]),
                         "Open": [10.] * 3, "High": [11.] * 3, "Low": [9.] * 3,
                         "Close": [10.5] * 3, "Volume": [100] * 3, "source": ["pitrading"] * 3})


def run(frame=None, **kwargs):
    return build_export(bars() if frame is None else frame, META,
                        start="2018-07-16T13:30:00Z", end=kwargs.pop("end", "2018-07-16T13:33:00Z"), **kwargs)


def test_completion_clock_and_no_future_depth_leak():
    full = run(depth_config=DepthConfig())
    prefix = run(end="2018-07-16T13:32:30Z", depth_config=DepthConfig())
    assert len(prefix["candles"]) == 2
    assert prefix["candles"] == full["candles"][:2]
    assert prefix["synthetic_depth"]["snapshots"] == full["synthetic_depth"]["snapshots"][:2]
    assert all(c["available_ts"] == c["end_ts"] == c["start_ts"] + 60 for c in full["candles"])
    assert full["network_calls"] == 0 and not full["execution_supported"]


def test_winter_timezone_offset():
    result = build_export(bars("2018-01-16"), META, start="2018-01-16T14:30:00Z", end="2018-01-16T14:33:00Z")
    assert len(result["candles"]) == 3
    assert result["candles"][0]["start_ts"] == int(pd.Timestamp("2018-01-16T14:30:00Z").timestamp())


def test_bad_values_excluded_without_changing_raw_frame():
    frame = bars()
    frame.loc[1, "High"] = float("inf")
    result = run(frame)
    assert len(result["candles"]) == 2
    assert result["rejected_rows"] == {"invalid_ohlcv": 1}
    assert len(frame) == 3


def test_duplicates_and_source_splice_rejected():
    with pytest.raises(ValueError, match="Duplicate"):
        run(pd.concat([bars(), bars()]))
    frame = bars()
    frame.loc[1, "source"] = "iex"
    with pytest.raises(ValueError, match="source boundary"):
        run(frame)


def test_clean_data_and_timezone_naive_window_rejected():
    with pytest.raises(ValueError, match="raw"):
        build_export(bars(), {**META, "version": "clean"}, start="2018-07-16T13:30Z", end="2018-07-16T13:33Z")
    with pytest.raises(ValueError, match="timezone"):
        build_export(bars(), META, start="2018-07-16", end="2018-07-17")


def test_archive_checksum_and_local_slice(tmp_path):
    path = tmp_path / "ABC_raw.parquet"
    bars().to_parquet(path, index=False)
    metadata = {**META, "file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    path.with_suffix(".provenance.json").write_text(json.dumps(metadata))
    result = export_archive(path, start="2018-07-16T13:31Z", end="2018-07-16T13:33Z")
    assert len(result["candles"]) == 2
    metadata["sha256"] = "wrong"
    path.with_suffix(".provenance.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="checksum"):
        export_archive(path, start="2018-07-16T13:31Z", end="2018-07-16T13:33Z")
