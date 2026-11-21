# backend/drl_agent/train.py
import os
import torch
import pandas as pd
from agent import PPOAgent
from environment import StockTradingEnv

# Dummy memory class
class Memory:
    def __init__(self):
        self.actions = []
        self.states = []
        self.logprobs = []
        self.rewards = []
        self.is_terminals = []
    
    def clear_memory(self):
        del self.actions[:]
        del self.states[:]
        del self.logprobs[:]
        del self.rewards[:]
        del self.is_terminals[:]

def load_price_history(symbol: str, period: str = "6mo") -> pd.DataFrame:
    """Load OHLCV history; falls back to a small dummy frame if unavailable."""
    try:
        import yfinance as yf

        df = yf.download(symbol, period=period, interval="1d", progress=False, auto_adjust=False)
        if not df.empty:
            df = df.rename(columns={"Open": "Open", "High": "High", "Low": "Low", "Close": "Close", "Volume": "Volume"})
            return df.reset_index(drop=True)
    except Exception:
        pass
    dummy_data = {
        "Open": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        "High": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        "Low": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        "Close": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        "Volume": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
    }
    return pd.DataFrame(dummy_data)


def main():
    ############## Hyperparameters ##############
    env_name = "StockTrading"
    render = False
    solved_reward = 230         # stop training if avg_reward > solved_reward
    log_interval = 20           # print avg reward in the interval
    max_episodes = 50000        # max training episodes
    max_timesteps = 300         # max timesteps in one episode

    update_timestep = 2000      # update policy every n timesteps
    K_epochs = 80               # update policy for K epochs
    eps_clip = 0.2              # clip parameter for PPO
    gamma = 0.99                # discount factor

    lr = 0.0003                 # parameters for Adam optimizer
    betas = (0.9, 0.999)

    random_seed = None
    #############################################

    # creating environment
    df = load_price_history(symbol="AAPL", period="6mo")
    emb_dim = int(os.getenv("RAG_SIGNAL_EMBED_DIM", "16"))
    env = StockTradingEnv(df, symbol="AAPL", max_episode_steps=max_timesteps, embedding_dim=emb_dim)
    state_dim = env.observation_space.shape[0]
    action_dim = env.action_space.n

    if random_seed:
        torch.manual_seed(random_seed)
        env.seed(random_seed)

    memory = Memory()
    ppo = PPOAgent(state_dim, action_dim, lr, betas, gamma, K_epochs, eps_clip)
    print(lr, betas)

    running_reward = 0
    avg_length = 0
    time_step = 0

    for i_episode in range(1, max_episodes + 1):
        state = env.reset()
        for t in range(max_timesteps):
            time_step += 1
            action = ppo.select_action(state, memory)
            state, reward, done, _ = env.step(action)

            memory.rewards.append(reward)
            memory.is_terminals.append(done)

            if time_step % update_timestep == 0:
                ppo.update(memory)
                memory.clear_memory()
                time_step = 0
            running_reward += reward
            if render:
                env.render()
            if done:
                break

        avg_length += t

        if i_episode % 50 == 0:
            avg_length = int(avg_length / 50)
            running_reward = int((running_reward / 50))

            print(f"Episode {i_episode} \t Avg length: {avg_length} \t Avg reward: {running_reward}")
            running_reward = 0
            avg_length = 0


if __name__ == '__main__':
    main()
