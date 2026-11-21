from copy import deepcopy
import math

from drl_agent.dataset import build_dataset
from drl_agent.features import encode_observations, encode_state


def state(quote=None, book=None):
    rows = []
    for kind, payload in (("levelone_quote", quote), ("level2_book", book)):
        if payload is not None:
            rows.append({"symbol": "TEST", "source": "schwab_stream", "observation_type": kind,
                         "observation_key": kind, "event_ts": 100, "received_ts": 100,
                         "payload": {"received_at_ms": 100_000, **payload}})
    return build_dataset("TEST", start_ts=101, end_ts=101, market_rows=rows, scanner_rows=[])["states"][0]


def features(row):
    encoded = encode_state(row)
    return dict(zip(encoded["names"], encoded["values"])), dict(zip(encoded["names"], encoded["valid"]))


def test_fixed_finite_shape_and_missing_is_not_balanced_book():
    missing = state()
    full = state({"bid": 10, "ask": 10.02}, {
        "service": "NASDAQ_BOOK", "bids": [{"price": 10, "size": 100}],
        "asks": [{"price": 10.02, "size": 100}],
    })
    a, b = encode_state(missing), encode_state(full)
    assert a["names"] == b["names"]
    assert len(set(a["names"])) == len(a["names"])
    assert all(math.isfinite(v) and -1 <= v <= 1 for v in b["values"])
    va, ma = features(missing)
    vb, mb = features(full)
    assert va["book.imbalance"] == vb["book.imbalance"] == 0
    assert ma["book.imbalance"] == 0 and mb["book.imbalance"] == 1
    assert mb["book.bids.1.distance"] == 0
    assert vb["book.bids.0.distance"] < 0 < vb["book.asks.0.distance"]


def test_revalidates_future_records_instead_of_trusting_export_wrappers():
    row = state({"bid": 10, "ask": 11})
    future = deepcopy(row["inputs"]["levelone_quote"][0])
    future["record"]["payload"]["received_at_ms"] = 999_000
    future["record"]["payload"]["bid"] = 900
    future["record"]["payload"]["ask"] = 901
    before = encode_state(row)
    row["inputs"]["levelone_quote"].append(future)
    after = encode_state(row)
    assert before["values"] == after["values"]
    assert before["valid"] == after["valid"]
    assert after["rejected_inputs"] == 1


def test_stale_values_keep_age_and_freshness_separate():
    row = state({"bid": 10, "ask": 11, "quote_time_ms": 100_000})
    row["decision_ts"] = 120
    values, valid = features(row)
    assert values["levelone_quote.present"] == 1
    assert values["levelone_quote.fresh"] == 0
    assert values["quote.quote_age"] > 0
    assert valid["quote.spread"] == 1


def test_invalid_prices_and_nonfinite_measurements_are_masked():
    row = state({"bid": 11, "ask": 10}, {
        "bids": [{"price": 12, "size": 10}], "asks": [{"price": 11, "size": 10}],
    })
    row["inputs"]["levelone_quote"][0]["record"]["payload"]["net_percent_change"] = float("nan")
    values, valid = features(row)
    assert values["quote.valid_nbbo"] == values["book.valid"] == 0
    assert valid["quote.spread"] == valid["quote.day_change"] == valid["book.imbalance"] == 0


def test_actions_rewards_and_arbitrary_fields_do_not_enter_features():
    row = state({"bid": 10, "ask": 11})
    baseline = encode_state(row)
    row["reward"] = 100000
    row["action"] = "buy"
    row["inputs"]["levelone_quote"][0]["record"]["payload"].update(
        reward=100000, action="sell", future_return=100)
    assert encode_state(row) == baseline


def test_dataset_encoding_keeps_versions_and_feature_order():
    dataset = build_dataset("TEST", start_ts=100, end_ts=110, market_rows=[], scanner_rows=[])
    result = encode_observations(dataset)
    assert result["manifest"]["source_dataset_sha256"] == dataset["manifest"]["dataset_sha256"]
    assert result["manifest"]["training_ready"] is False
    assert result["manifest"]["state_count"] == 2
    assert result["manifest"]["feature_count"] == len(result["states"][0]["values"])
