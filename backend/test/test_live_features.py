from copy import deepcopy
from datetime import datetime, timezone
import math

from drl_agent.live_features import encode_live
from drl_agent.features import encode_state


def observation():
    state = dict(symbol='ABC', decision_ts=2200, inputs={})
    quote = dict(symbol='ABC', source='schwab_stream', observation_type='levelone_quote',
        observation_key='q', event_ts=2199, received_ts=2199,
        payload=dict(received_at_ms=2199000, bid=10, ask=10.02, last=10.01))
    scanner = dict(symbol='ABC', source='scanner_shadow', id='s', ts=2100,
        created_at=datetime.fromtimestamp(2100, timezone.utc),
        payload=dict(watch_inputs=dict(rvol=4, float_shares=1000000),
                     market_signal=dict(price=5, intraday=dict(vwap=10))))
    state['inputs']['scanner'] = [{'record': scanner}]
    state['inputs']['levelone_quote'] = [{'record': quote}]
    state['inputs']['minute_candle'] = [{'record': dict(symbol='ABC', source='schwab_stream',
        observation_type='minute_candle', observation_key=str(i), event_ts=i*60, received_ts=i*60,
        open=10+i*.01, high=11+i*.01, low=9+i*.01, close=10+i*.02, volume=100,
        payload=dict(received_at_ms=i*60000))} for i in range(1, 36)]
    return state


def maps(state, account=None):
    encoded = encode_live(state, account or dict(observed_ts=2200, exposure_fraction=.25))
    return dict(zip(encoded['names'], encoded['values'])), dict(zip(encoded['names'], encoded['valid']))


def test_numeric_macd_current_price_vwap_and_missing_masks():
    state = observation()
    before = encode_state(state)
    values, masks = maps(state)
    assert values['live.current_vwap_distance'] > 0  # current quote, not scanner's $5
    assert masks['live.macd'] == masks['live.macd_signal'] == masks['live.macd_histogram_change'] == 1
    assert values['live.macd'] > 0 and masks['live.rvol_log'] == 1
    assert masks['live.remaining_risk_fraction'] == 0
    assert before == encode_state(state)  # immutable historical feature version


def test_future_inputs_and_rewards_cannot_change_observation():
    state = observation()
    before = maps(state)
    future = deepcopy(state['inputs']['minute_candle'][-1])
    future['record'].update(event_ts=2220, received_ts=2220, close=999)
    future['record']['payload']['received_at_ms'] = 2220000
    state['inputs']['minute_candle'].append(future)
    state.update(reward=999, future_return=999, action='buy')
    assert maps(state) == before
    _, masks = maps(state, dict(observed_ts=2300, exposure_fraction=1))
    assert masks['live.exposure_fraction'] == 0


def test_missing_candles_do_not_fabricate_macd():
    state = observation()
    state['inputs']['minute_candle'].pop(20)
    values, masks = maps(state)
    assert masks['live.macd'] == 0 and values['live.macd'] == 0
    assert all(math.isfinite(v) for v in values.values())


def test_revision_is_one_candle_and_no_shape_changes():
    state = observation()
    baseline = encode_live(state, {})
    state['inputs']['minute_candle'].append(deepcopy(state['inputs']['minute_candle'][-1]))
    assert encode_live(state, {})['values'] == baseline['values']
    assert encode_live(dict(symbol='ABC', decision_ts=2200, inputs={}), {})['names'] == baseline['names']
