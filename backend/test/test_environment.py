import numpy as np
import os
import pandas as pd
import pytest
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from drl_agent.environment import StockTradingEnv


class DummyResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture
def dummy_df():
    data = {
        "Open": [1, 1, 1, 1, 1, 1, 1, 1],
        "High": [1, 1, 1, 1, 1, 1, 1, 1],
        "Low": [1, 1, 1, 1, 1, 1, 1, 1],
        "Close": [1, 1, 1, 1, 1, 1, 1, 1],
        "Volume": [1, 1, 1, 1, 1, 1, 1, 1],
    }
    return data


def test_env_observation_and_step(monkeypatch, dummy_df):
    # deterministic price and rag response
    monkeypatch.setattr("random.uniform", lambda a, b: 1.0)
    monkeypatch.setattr("random.randint", lambda a, b: 0)
    monkeypatch.setattr(
        "requests.post",
        lambda url, json, timeout: DummyResponse(
            {"answer": "ok", "hits": [{}], "signal": {"hit_count": 1, "graph_count": 0, "embedding": [0.1, 0.2, 0.3, 0.4]}}
        ),
    )

    env = StockTradingEnv(df=pd.DataFrame(dummy_df), symbol="TEST", max_episode_steps=10, embedding_dim=4)
    obs = env.reset()
    assert obs.shape == (env.observation_space.shape[0],)

    state, reward, done, _ = env.step(0)  # buy
    assert env.shares_held > 0
    assert reward < 0  # fee applied
    assert not done

    state, reward, done, _ = env.step(1)  # sell
    assert env.shares_held == 0
    assert isinstance(reward, float)
    assert state.shape == (env.observation_space.shape[0],)


def test_env_includes_signal_features(monkeypatch, dummy_df):
    monkeypatch.setattr("random.uniform", lambda a, b: 1.0)
    monkeypatch.setattr(
        "requests.post",
        lambda url, json, timeout: DummyResponse({"answer": "", "hits": [], "signal": {}}),
    )

    env = StockTradingEnv(
        df=pd.DataFrame(dummy_df),
        symbol="TEST",
        max_episode_steps=10,
        embedding_dim=4,
        signal_features={
            "semantic_signal": {
                "sentiment": "positive",
                "direction": "positive",
                "confidence": 0.8,
                "sentiment_score": 0.5,
                "article_count": 3,
            },
            "market_signal": {
                "trend_score": 0.6,
                "rvol": 3.0,
                "rvol_threshold": 3.0,
                "momentum_5d_pct": 10,
                "rsi_14": 60,
                "volatility_20": 0.02,
                "liquidity_score": 0.9,
            },
            "stop_loss": {
                "triggered": True,
                "trailing_drawdown_pct": 0.04,
            },
        },
    )

    obs = env.reset()
    signal = obs[-13:]
    np.testing.assert_allclose(
        signal,
        np.array([1.0, 1.0, 0.8, 0.5, 0.3, 0.6, 1.0, 0.1, 0.2, 0.02, 0.9, 1.0, 0.04], dtype=np.float32),
    )


def test_env_signal_provider_overrides_static_features(monkeypatch, dummy_df):
    monkeypatch.setattr("requests.post", lambda url, json, timeout: DummyResponse({"answer": "", "hits": [], "signal": {}}))

    env = StockTradingEnv(
        df=pd.DataFrame(dummy_df),
        symbol="TEST",
        embedding_dim=4,
        signal_features={"semantic_signal": {"sentiment": "positive"}},
        signal_provider=lambda step, symbol: {"semantic_signal": {"sentiment": "negative", "confidence": 0.7}},
    )

    obs = env.reset()
    signal = obs[-13:]
    assert signal[0] == -1.0
    assert signal[2] == pytest.approx(0.7)


@pytest.mark.parametrize("index", [range(10), range(100, 110), pd.date_range("2026-01-01", periods=10)])
def test_latest_window_uses_final_six_rows_without_random_start(monkeypatch, index):
    monkeypatch.setattr(StockTradingEnv, "_get_rag_observation", lambda _: (0, [], 0))
    def no_random(*args):
        raise AssertionError("Inference must not sample a historical start")
    monkeypatch.setattr("random.randint", no_random)
    df = pd.DataFrame({column: np.arange(1, 11) for column in
                       ("Open", "High", "Low", "Close", "Volume")}, index=index)
    env = StockTradingEnv(df, embedding_dim=4)
    obs = env.reset(latest_window=True)
    assert env.current_step == 4
    assert obs.shape == env.observation_space.shape
    np.testing.assert_allclose(obs[:30].reshape(5, 6),
                               np.tile(np.arange(5, 11) / 10, (5, 1)))
    np.testing.assert_array_equal(obs, env.reset(latest_window=True))


def test_default_reset_keeps_training_random_start(monkeypatch):
    monkeypatch.setattr(StockTradingEnv, "_get_rag_observation", lambda _: (0, [], 0))
    calls = []
    def choose(low, high):
        calls.append((low, high))
        return 2
    monkeypatch.setattr("random.randint", choose)
    df = pd.DataFrame({column: np.arange(1, 11) for column in
                       ("Open", "High", "Low", "Close", "Volume")})
    env = StockTradingEnv(df)
    env.reset()
    assert env.current_step == 2
    assert calls == [(0, 4)]


def test_inference_account_snapshot_and_training_reset(monkeypatch):
    monkeypatch.setattr(StockTradingEnv, "_get_rag_observation", lambda _: (0, [], 0))
    df = pd.DataFrame({column: np.arange(1, 11) for column in
                       ("Open", "High", "Low", "Close", "Volume")})
    env = StockTradingEnv(df)
    snapshot = {"balance": 400, "equity": 500, "realized_pnl": -5,
                "quantity": 20, "average_price": 5}
    obs = env.reset(latest_window=True, account_state=snapshot)
    assert env.balance == 400
    assert env.net_worth == 500
    assert env.shares_held == 20
    assert env.cost_basis == 5
    assert env.realized_pnl == -5
    assert np.isfinite(obs).all()
    with pytest.raises(ValueError):
        env.reset(account_state=snapshot)
    for key in snapshot:
        with pytest.raises(ValueError):
            env.reset(latest_window=True, account_state={**snapshot, key: float("nan")})
    env.reset()
    assert env.balance == 10000
    assert env.shares_held == 0
