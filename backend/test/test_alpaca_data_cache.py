from copy import deepcopy
import fcntl
import json

import pytest

from drl_agent.alpaca_data_cache import cache_bars, URL

START, END = "2018-07-16T13:30:00Z", "2018-07-16T13:32:00Z"
BAR = dict(t=START, o=10, h=11, l=9, c=10.5, v=100, n=2, vw=10.1)


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status
        self.content = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def json(self):
        return deepcopy(self.payload)


def test_pagination_raw_sip_local_reuse_and_no_credentials_in_archive(tmp_path):
    calls = []
    def get(url, **kwargs):
        calls.append(kwargs)
        assert url == URL and not kwargs["allow_redirects"]
        assert kwargs["params"]["feed"] == "sip"
        assert kwargs["params"]["adjustment"] == "raw"
        assert kwargs["params"]["asof"] == "-"
        return Response({"bars": {"ABC": [BAR if len(calls) == 1 else {**BAR, "t": "2018-07-16T13:31:00Z"}]},
                         "next_page_token": "page2" if len(calls) == 1 else None})
    result = cache_bars("ABC", START, END, tmp_path, key="secret-key", secret="secret-value", get=get)
    assert result["rows"] == 2 and result["volume_sum"] == 200
    assert calls[1]["params"]["page_token"] == "page2"
    assert calls[0]["params"]["end"] == "2018-07-16T13:31:59+00:00"
    assert "secret-key" not in (tmp_path / result["file"]).read_text()
    assert "secret-value" not in (tmp_path / result["file"]).read_text()
    reused = cache_bars("ABC", START, END, tmp_path, get=lambda *a, **kw: pytest.fail("Network used"))
    assert reused["reused"] and reused["archive_sha256"] == result["archive_sha256"]


@pytest.mark.parametrize("payload,status", [({"bars": {}}, 200), ({}, 403),
    ({"bars": {"ABC": [BAR, BAR]}}, 200),
    ({"bars": {"ABC": [{**BAR, "h": 1}]}}, 200),
    ({"bars": {"ABC": [{**BAR, "t": END}]}}, 200)])
def test_failure_does_not_publish_and_releases_lock(tmp_path, payload, status):
    with pytest.raises((ValueError, RuntimeError)):
        cache_bars("ABC", START, END, tmp_path, key="k", secret="s",
                   get=lambda *a, **kw: Response(payload, status))
    assert all(path.name.endswith(".lock") for path in tmp_path.iterdir())
    result = cache_bars("ABC", START, END, tmp_path, key="k", secret="s",
                        get=lambda *a, **kw: Response({"bars": {"ABC": [BAR]}}))
    assert result["rows"] == 1


def test_corrupt_cache_never_overwritten_or_redownloaded(tmp_path):
    result = cache_bars("ABC", START, END, tmp_path, key="k", secret="s",
                       get=lambda *a, **kw: Response({"bars": {"ABC": [BAR]}}))
    path = tmp_path / result["file"]
    contents = json.loads(path.read_text())
    contents["summary"]["rows"] = 100
    path.write_text(json.dumps(contents))
    with pytest.raises(ValueError, match="verification"):
        cache_bars("ABC", START, END, tmp_path, get=lambda *a, **kw: pytest.fail("Network used"))
    assert json.loads(path.read_text())["summary"]["rows"] == 100


def test_bounds_and_credentials(tmp_path):
    with pytest.raises(ValueError, match="31 days"):
        cache_bars("ABC", START, "2019-07-16T13:32:00Z", tmp_path)
    with pytest.raises(ValueError, match="timezone"):
        cache_bars("ABC", "2018-07-16T13:30:00", END, tmp_path)
    with pytest.raises(ValueError, match="APCA_API"):
        cache_bars("ABC", START, END, tmp_path)


def test_competing_cache_writer_blocked_and_abandoned_lock_reusable(tmp_path):
    path = tmp_path / ".ABC_20180716T1330Z_20180716T1332Z_sip_raw.json.lock"
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            cache_bars("ABC", START, END, tmp_path, get=lambda *a, **kw: pytest.fail("Network used"))
    result = cache_bars("ABC", START, END, tmp_path, key="k", secret="s",
                        get=lambda *a, **kw: Response({"bars": {"ABC": [BAR]}}))
    assert result["rows"] == 1
