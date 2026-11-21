from copy import deepcopy
from datetime import datetime, timezone

import pytest

from drl_agent.dataset import build_dataset


def market(key="q1", event=100, received=100, kind="levelone_quote", **payload):
    return {"observation_key": key, "symbol": "TEST", "source": "schwab_stream",
            "observation_type": kind, "event_ts": event, "received_ts": received,
            "payload": {"received_at_ms": received * 1000, **payload}}


def build(rows, scanner=None, start=100, end=110):
    return build_dataset("TEST", start_ts=start, end_ts=end, scanner_rows=scanner or [],
                         market_rows=rows, step_seconds=1, history_limit=5)


def test_future_data_and_late_revisions_cannot_change_earlier_states():
    original = market(bid=10)
    revision = market("q2", event=100, received=105, bid=20)
    future = market("future", event=200, received=200, bid=1000)
    baseline = build([original])
    result = build([future, revision, original])
    assert result["states"][:5] == baseline["states"][:5]
    quote = result["states"][5]["inputs"]["levelone_quote"]
    assert len(quote) == 1
    assert quote[0]["id"] == "q2"
    assert build([original, revision, future]) == result


def test_scanner_requires_creation_time_and_embedded_news_cutoff():
    row = {"id": "s1", "symbol": "TEST", "source": "scanner_shadow", "ts": 100,
           "created_at": datetime.fromtimestamp(102, tz=timezone.utc),
           "payload": {"semantic_signal": {"hype": {"as_of_ts": 104}}}}
    states = build([], [row])["states"]
    assert all(not s["inputs"]["scanner"] for s in states[:4])
    assert states[4]["inputs"]["scanner"][0]["id"] == "s1"
    missing_time = deepcopy(row)
    del missing_time["created_at"]
    assert build([], [missing_time])["manifest"]["excluded"] == {"invalid_record": 1}


def test_candle_must_be_closed_and_received():
    candle = market("c1", event=100, received=130, kind="minute_candle")
    states = build([candle], start=159, end=160)["states"]
    assert not states[0]["inputs"]["minute_candle"]
    assert states[1]["inputs"]["minute_candle"]
    late = market("c1", event=100, received=165, kind="minute_candle")
    assert not build([late], start=160, end=160)["states"][0]["inputs"]["minute_candle"]


def test_subsecond_receipt_and_field_time_do_not_leak_at_second_boundary():
    quote = market(received_at_ms=100_500)
    assert not build([quote])["states"][0]["inputs"]["levelone_quote"]
    assert build([quote])["states"][1]["inputs"]["levelone_quote"]
    quote = market(quote_time_ms=102_001)
    assert not build([quote])["states"][2]["inputs"]["levelone_quote"]
    assert build([quote])["states"][3]["inputs"]["levelone_quote"]


def test_book_is_preserved_with_missingness_age_and_input_isolation():
    row = market("b1", kind="level2_book", service="NASDAQ_BOOK",
                 bids=[{"price": 10, "size": 200}], asks=[{"price": 11, "size": 100}])
    result = build([row])
    state = result["states"][-1]
    assert state["coverage"]["level2_book"]["event_age_ms"] == 10_000
    assert not state["coverage"]["levelone_quote"]["present"]
    assert state["coverage"]["levelone_quote"]["event_age_ms"] is None
    row["payload"]["bids"][0]["size"] = 999
    assert state["inputs"]["level2_book"][0]["record"]["payload"]["bids"][0]["size"] == 200
    assert result["manifest"]["training_ready"] is False


def test_excludes_other_symbols_backfilled_sources_and_old_history():
    other = {**market(), "symbol": "OTHER"}
    backfill = {**market(), "source": "yahoo"}
    result = build([other, backfill, market(event=1, received=1)], start=1000, end=1000)
    assert result["manifest"]["excluded"] == {"other_symbol": 1, "unsupported_source": 1}
    assert not result["states"][0]["inputs"]["levelone_quote"]


def test_rejects_unbounded_output():
    with pytest.raises(ValueError):
        build_dataset("TEST", start_ts=100, end_ts=86_500, step_seconds=1,
                      scanner_rows=[], market_rows=[])
