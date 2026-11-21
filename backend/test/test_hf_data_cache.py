from io import BytesIO
import json

import pandas as pd
import pytest

from drl_agent.hf_data_cache import cache_raw


class Response:
    status_code = 200

    def __init__(self, content):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, chunk_size):
        yield self.content


def parquet_bytes():
    frame = pd.DataFrame({"datetime": pd.to_datetime(["2020-01-02 09:30", "2020-01-02 09:31"]),
                          "Open": [10., 10.], "High": [11., 11.], "Low": [9., 9.],
                          "Close": [10.5, 10.5], "Volume": [100, 0], "source": ["pitrading"] * 2})
    stream = BytesIO()
    frame.to_parquet(stream, index=False)
    return stream.getvalue()


def test_download_once_reuse_offline_and_no_key_in_metadata(tmp_path):
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response(parquet_bytes())
    first = cache_raw("aapl", tmp_path, api_key="private-test-key", get=get)
    second = cache_raw("AAPL", tmp_path, get=get)
    assert len(calls) == 1 and calls[0][1]["allow_redirects"] is False
    assert first["sha256"] == second["sha256"] and second["reused_local_file"]
    assert first["rows"] == 2 and first["zero_volume_rows"] == 1
    assert first["invalid_ohlcv_rows"] == 0
    metadata = (tmp_path / "AAPL_raw.provenance.json").read_text()
    assert "private-test-key" not in metadata
    assert json.loads(metadata)["version"] == "raw"


def test_http_and_invalid_file_fail_without_publishing(tmp_path):
    response = Response(b"unauthorized")
    response.status_code = 401
    with pytest.raises(RuntimeError, match="401"):
        cache_raw("AAPL", tmp_path, api_key="secret", get=lambda *a, **k: response)
    with pytest.raises(Exception):
        cache_raw("AAPL", tmp_path, api_key="secret", get=lambda *a, **k: Response(b"not parquet"))
    assert not list(tmp_path.iterdir())


def test_checksum_mismatch_retains_file_and_never_downloads(tmp_path):
    cache_raw("AAPL", tmp_path, api_key="secret", get=lambda *a, **k: Response(parquet_bytes()))
    path = tmp_path / "AAPL_raw.provenance.json"
    metadata = json.loads(path.read_text())
    metadata["sha256"] = "bad"
    path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="checksum"):
        cache_raw("AAPL", tmp_path, get=lambda *a, **k: pytest.fail("network called"))
    assert (tmp_path / "AAPL_raw.parquet").exists()


def test_path_traversal_and_missing_key(tmp_path):
    with pytest.raises(ValueError, match="ticker"):
        cache_raw("../../AAPL", tmp_path)
    with pytest.raises(ValueError, match="HF_DATA"):
        cache_raw("AAPL", tmp_path)
    assert not list(tmp_path.iterdir())
