# backend/drl_agent/environment.py
import os
import numpy as np
import requests
import random
import math
from typing import Any, Callable

try:
    import gym
    from gym import spaces
except Exception:
    class _Env:
        pass

    class _Discrete:
        def __init__(self, n):
            self.n = n

    class _Box:
        def __init__(self, low, high, shape, dtype):
            self.low = low
            self.high = high
            self.shape = shape
            self.dtype = dtype

    class _Spaces:
        Discrete = _Discrete
        Box = _Box

    class _Gym:
        Env = _Env

    gym = _Gym()
    spaces = _Spaces()

MAX_STEPS = 2000
SIGNAL_FEATURE_DIM = 13


def _safe_column_max(df, column: str, default: float = 1.0) -> float:
    try:
        values = df[column].astype(float).to_numpy()
        finite = values[np.isfinite(values)]
        if finite.size:
            return float(np.max(finite))
    except Exception:
        pass
    return default

class StockTradingEnv(gym.Env):
    """A stock trading environment for reinforcement learning"""
    metadata = {'render.modes': ['human']}

    def __init__(
        self,
        df,
        symbol: str = "AAPL",
        rag_api_url: str = "http://api:8443/rag",
        max_episode_steps: int = 300,
        embedding_dim: int = None,
        signal_provider: Callable[[int, str], dict[str, Any]] | None = None,
        signal_features: dict[str, Any] | None = None,
    ):
        super(StockTradingEnv, self).__init__()

        self.df = df
        self.symbol = symbol
        self.reward_range = (0, np.inf)
        self.rag_api_url = rag_api_url
        self.transaction_cost_pct = 0.001  # 0.1% slippage/fee per trade
        self.drawdown_penalty_coeff = 0.01  # penalize drawdown in reward
        self.max_episode_steps = max_episode_steps
        self.episode_step = 0
        self.signal_provider = signal_provider
        self.signal_features = signal_features or {}
        
        self.max_share_price = _safe_column_max(df, 'High')
        self.max_account_balance = 100000
        self.max_shares = 1000
        self.max_volume = _safe_column_max(df, 'Volume')


        # Actions of the format Buy x%, Sell x%, Hold, etc.
        self.action_space = spaces.Discrete(3) # 3 actions: Buy, Sell, Hold

        # Prices contains the OHCL values for the last five prices, plus RAG data and embedding (flattened)
        # 5 OHLCV rows + 1 account row + 1 RAG scalar row = 7 rows; 6 cols for fixed-length window
        # We will append embedding separately to the flattened observation to avoid reshaping the price tensor.
        self.price_obs_shape = (7, 6)
        self.embedding_dim = embedding_dim or int(os.getenv("RAG_SIGNAL_EMBED_DIM", "16"))
        self.signal_feature_dim = SIGNAL_FEATURE_DIM
        flat_dim = self.price_obs_shape[0] * self.price_obs_shape[1] + self.embedding_dim + self.signal_feature_dim
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(flat_dim,), dtype=np.float32)

    def _get_signal_features(self):
        payload = self.signal_features
        if self.signal_provider:
            try:
                payload = self.signal_provider(self.current_step, self.symbol) or {}
            except Exception:
                payload = {}
        semantic = payload.get("semantic_signal") or payload.get("semantic") or {}
        market = payload.get("market_signal") or payload.get("market") or {}
        stop_loss = payload.get("stop_loss") or {}

        sentiment_map = {"negative": -1.0, "neutral": 0.0, "positive": 1.0}
        direction_map = {"negative": -1.0, "neutral": 0.0, "positive": 1.0, "mixed": 0.0}
        values = np.array(
            [
                sentiment_map.get(str(semantic.get("sentiment") or "neutral").lower(), 0.0),
                direction_map.get(str(semantic.get("direction") or "neutral").lower(), 0.0),
                float(semantic.get("confidence") or 0.0),
                float(semantic.get("sentiment_score") or 0.0),
                min(float(semantic.get("article_count") or 0.0) / 10.0, 1.0),
                float(market.get("trend_score") or 0.0),
                min(float(market.get("rvol") or 0.0) / max(float(market.get("rvol_threshold") or 3.0), 0.01), 1.0),
                float(market.get("momentum_5d_pct") or 0.0) / 100.0,
                (float(market.get("rsi_14") or 50.0) - 50.0) / 50.0,
                float(market.get("volatility_20") or 0.0),
                float(market.get("liquidity_score") or 0.0),
                1.0 if stop_loss.get("triggered") else 0.0,
                float(stop_loss.get("trailing_drawdown_pct") or 0.0),
            ],
            dtype=np.float32,
        )
        return np.clip(values, -1.0, 1.0)

    def _get_rag_observation(self):
        """
        Query the RAG endpoint for the current symbol and return a scaled signal.
        Uses the length of the answer and hit count as a proxy feature; replace
        with an embedding or sentiment score when available.
        """
        try:
            payload = {"question": f"What is the latest news on {self.symbol}?", "tickers": [self.symbol], "top_k": 3}
            response = requests.post(self.rag_api_url, json=payload, timeout=3)
            response.raise_for_status()
            rag_data = response.json()
            answer = rag_data.get("answer") or ""
            hits = rag_data.get("hits") or []
            signal = rag_data.get("signal") or {}
            # Simple heuristic: combine answer length, hit count, and graph_count if present
            hit_count = signal.get("hit_count", len(hits))
            graph_count = signal.get("graph_count", 0)
            emb = signal.get("embedding") or []
            emb_norm = float(np.linalg.norm(np.array(emb, dtype=np.float32))) if emb else 0.0
            confidence = float(signal.get("confidence", 0.0) or 0.0)
            score = math.log(1 + len(answer)) + hit_count + 0.5 * graph_count + 0.1 * emb_norm
            return score, emb, confidence
        except Exception as e:
            print(f"Error querying RAG API: {e}")
            return 0.0, [], 0.0

    def _window_values(self, column: str, scale: float):
        values = self.df.iloc[self.current_step: self.current_step + 6][column].values.astype(np.float32)
        if len(values) == 0:
            values = np.zeros(6, dtype=np.float32)
        if len(values) < 6:
            values = np.pad(values, (0, 6 - len(values)), mode="edge")
        elif len(values) > 6:
            values = values[:6]
        divisor = scale if scale else 1.0
        return values / divisor

    def _next_observation(self):
        # Get the stock data points for the last 5 days and scale to between 0-1
        frame = np.array([
            self._window_values('Open', self.max_share_price),
            self._window_values('High', self.max_share_price),
            self._window_values('Low', self.max_share_price),
            self._window_values('Close', self.max_share_price),
            self._window_values('Volume', self.max_volume),
        ])
        
        # Get RAG observation
        rag_scalar, rag_emb, rag_conf = self._get_rag_observation()

        # Append additional data and scale each value to between 0-1
        obs = np.append(frame, [[
            self.balance / self.max_account_balance,
            (self.realized_pnl if hasattr(self, "realized_pnl") else 0) / self.max_account_balance,
            self.shares_held / self.max_shares,
            self.cost_basis / self.max_share_price,
            self.total_shares_sold / self.max_shares,
            self.total_sales_value / (self.max_share_price * self.max_shares),
        ]], axis=0)

        # Add RAG observation
        rag_obs_scaled = np.array([[rag_scalar + rag_conf] * 6]) / 100 # Scale RAG obs for now
        obs = np.append(obs, rag_obs_scaled, axis=0)

        flat_prices = obs.astype(np.float32).flatten()
        # pad/trim embedding to expected dim
        emb = np.zeros(self.embedding_dim, dtype=np.float32)
        if rag_emb:
            rag_emb = np.array(rag_emb, dtype=np.float32).flatten()
            emb[: min(self.embedding_dim, len(rag_emb))] = rag_emb[: self.embedding_dim]
        return np.concatenate([flat_prices, emb, self._get_signal_features()])

    def step(self, action):
        # Execute one time step within the environment
        prev_net_worth = getattr(self, "net_worth", 0)
        current_price = self._take_action(action)

        self.current_step += 1
        self.episode_step += 1
        end_of_data = self.current_step > len(self.df.loc[:, 'Open'].values) - 6
        max_steps_reached = self.episode_step >= self.max_episode_steps

        # Update net worth after action
        self.net_worth = self.balance + self.shares_held * current_price
        self.max_net_worth = max(self.max_net_worth, self.net_worth)

        drawdown = max(0, (self.max_net_worth - self.net_worth))
        reward = (self.net_worth - prev_net_worth) - self.drawdown_penalty_coeff * drawdown
        done = self.net_worth <= 0 or end_of_data or max_steps_reached

        obs = self._next_observation()

        return obs, reward, done, {}

    def reset(self, *, latest_window=False, account_state=None):
        # Reset the state of the environment to an initial state
        self.balance = 10000
        self.net_worth = 10000
        self.max_net_worth = 10000
        self.realized_pnl = 0
        self.shares_held = 0
        self.cost_basis = 0
        self.total_shares_sold = 0
        self.total_sales_value = 0
        self.episode_step = 0
        if account_state is not None:
            if not latest_window:
                raise ValueError("Account snapshots are for current inference only")
            values = {key: float(account_state[key]) for key in
                      ("balance", "equity", "realized_pnl", "quantity", "average_price")}
            if not all(np.isfinite(value) for value in values.values()):
                raise ValueError("Account snapshot must be finite")
            if any(values[key] < 0 for key in ("balance", "equity", "quantity", "average_price")):
                raise ValueError("Long account snapshot must be nonnegative")
            self.balance = values["balance"]
            self.net_worth = self.max_net_worth = values["equity"]
            self.realized_pnl = values["realized_pnl"]
            self.shares_held = values["quantity"]
            self.cost_basis = values["average_price"]
        
        # Set the current step to a random point within the data frame (leave room for 5-step window)
        max_start = max(0, len(self.df.loc[:, 'Open'].values) - 6)
        self.current_step = max_start if latest_window else (
            random.randint(0, max_start) if max_start > 0 else 0)

        return self._next_observation()

    def render(self, mode='human', close=False):
        # Render the environment to the screen
        profit = self.net_worth - 10000

        print(f'Step: {self.current_step}')
        print(f'Balance: {self.balance}')
        print(f'Shares held: {self.shares_held} (Total sold: {self.total_shares_sold})')
        print(f'Avg cost for held shares: {self.cost_basis} (Total sales value: {self.total_sales_value})')
        print(f'Net worth: {self.net_worth} (Max net worth: {self.max_net_worth})')
        print(f'Profit: {profit}')

    def _take_action(self, action):
        # Set the current price to a random price between open and close
        current_price = random.uniform(
            self.df.loc[self.current_step, "Open"],
            self.df.loc[self.current_step, "Close"])
        
        if action == 0: # Buy
            # Buy shares with available balance (integer shares)
            shares_to_buy = max(0, int(self.balance // current_price))
            if shares_to_buy > 0:
                cost = shares_to_buy * current_price
                fee = cost * self.transaction_cost_pct
                self.balance -= cost + fee
                prev_shares = self.shares_held
                self.shares_held += shares_to_buy
                if self.shares_held > 0:
                    self.cost_basis = (self.cost_basis * prev_shares + shares_to_buy * current_price) / self.shares_held
                else:
                    self.cost_basis = 0

        elif action == 1: # Sell
            # Sell all held shares
            shares_to_sell = self.shares_held
            if shares_to_sell > 0:
                revenue = shares_to_sell * current_price
                fee = revenue * self.transaction_cost_pct
                self.balance += revenue - fee
                self.shares_held -= shares_to_sell
                self.total_shares_sold += shares_to_sell
                self.total_sales_value += revenue
                # Realized PnL tracks profit/loss vs cost basis
                self.realized_pnl += (current_price - self.cost_basis) * shares_to_sell

        elif action == 2: # Hold
            pass

        # Update net worth after action for logging/consistency
        self.net_worth = self.balance + self.shares_held * current_price

        return current_price
