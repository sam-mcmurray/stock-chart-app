# backend/drl_agent/agent.py
import torch
import torch.nn as nn
from torch.distributions import Categorical

class ActorCritic(nn.Module):
    def __init__(self, state_dim, action_dim):
        super(ActorCritic, self).__init__()

        # Actor
        self.actor = nn.Sequential(
            nn.Linear(state_dim, 64),
            nn.Tanh(),
            nn.Linear(64, 64),
            nn.Tanh(),
            nn.Linear(64, action_dim),
            nn.Softmax(dim=-1)
        )

        # Critic
        self.critic = nn.Sequential(
            nn.Linear(state_dim, 64),
            nn.Tanh(),
            nn.Linear(64, 64),
            nn.Tanh(),
            nn.Linear(64, 1)
        )

    def forward(self):
        raise NotImplementedError

    def distribution(self, state, action_mask=None):
        action_probs = self.actor(state)
        if action_mask is not None:
            mask = torch.as_tensor(action_mask, dtype=torch.bool, device=state.device)
            if mask.shape != action_probs.shape or not mask.any(dim=-1).all():
                raise ValueError("Every observation needs a nonempty action mask")
            action_probs = action_probs.masked_fill(~mask, 0)
        return Categorical(action_probs)

    def act(self, state, action_mask=None):
        dist = self.distribution(state, action_mask)
        action = dist.sample()
        return action.item(), dist.log_prob(action)

    def evaluate(self, state, action, action_mask=None):
        dist = self.distribution(state, action_mask)
        action_logprobs = dist.log_prob(action)
        dist_entropy = dist.entropy()
        state_value = self.critic(state)
        return action_logprobs, state_value.squeeze(-1), dist_entropy

class PPOAgent:
    def __init__(self, state_dim, action_dim, lr, betas, gamma, K_epochs, eps_clip):
        self.lr = lr
        self.betas = betas
        self.gamma = gamma
        self.eps_clip = eps_clip
        self.K_epochs = K_epochs

        self.policy = ActorCritic(state_dim, action_dim)
        self.optimizer = torch.optim.Adam(self.policy.parameters(), lr=lr, betas=betas)
        self.policy_old = ActorCritic(state_dim, action_dim)
        self.policy_old.load_state_dict(self.policy.state_dict())
        
        self.MseLoss = nn.MSELoss()

    def select_action(self, state, memory, action_mask=None):
        state = torch.FloatTensor(state.reshape(1, -1))
        mask = None if action_mask is None else torch.as_tensor(action_mask, dtype=torch.bool).reshape(1, -1)
        with torch.no_grad():
            action, action_logprob = self.policy_old.act(state, mask)
        
        memory.states.append(state)
        memory.actions.append(action)
        memory.logprobs.append(action_logprob)
        if mask is not None:
            memory.action_masks.append(mask)
        
        return action

    def update(self, memory):
        if not memory.rewards or not (len(memory.states) == len(memory.actions) ==
                len(memory.logprobs) == len(memory.rewards) == len(memory.is_terminals)):
            raise ValueError("Rollout fields must have matching nonzero lengths")
        # Monte Carlo estimate of rewards:
        rewards = []
        discounted_reward = 0
        for reward, is_terminal in zip(reversed(memory.rewards), reversed(memory.is_terminals)):
            if is_terminal:
                discounted_reward = 0
            discounted_reward = reward + (self.gamma * discounted_reward)
            rewards.insert(0, discounted_reward)
        
        # Normalizing the rewards:
        rewards = torch.tensor(rewards, dtype=torch.float32)
        if not torch.isfinite(rewards).all():
            raise ValueError("Rollout rewards must be finite")
        
        # convert list to tensor
        old_states = torch.squeeze(torch.stack(memory.states).detach(), 1)
        old_actions = torch.as_tensor(memory.actions, dtype=torch.long).reshape(-1)
        old_logprobs = torch.cat(memory.logprobs).detach().reshape(-1)
        masks = getattr(memory, "action_masks", [])
        if masks and len(masks) != len(memory.states):
            raise ValueError("Masks must cover every rollout observation")
        old_masks = torch.cat(masks) if masks else None
        with torch.no_grad():
            advantages = rewards - self.policy_old.critic(old_states).squeeze(-1)
            if len(advantages) > 1:
                advantages = (advantages - advantages.mean()) / (advantages.std(unbiased=False) + 1e-5)
        
        # Optimize policy for K epochs:
        for _ in range(self.K_epochs):
            # Evaluating old actions and values :
            logprobs, state_values, dist_entropy = self.policy.evaluate(old_states, old_actions, old_masks)
            
            # Finding the ratio (pi_theta / pi_theta__old):
            ratios = torch.exp(logprobs - old_logprobs.detach())

            # Finding Surrogate Loss:
            surr1 = ratios * advantages
            surr2 = torch.clamp(ratios, 1-self.eps_clip, 1+self.eps_clip) * advantages
            loss = -torch.min(surr1, surr2) + 0.5*self.MseLoss(state_values, rewards) - 0.01*dist_entropy
            
            # take gradient step
            self.optimizer.zero_grad()
            loss.mean().backward()
            nn.utils.clip_grad_norm_(self.policy.parameters(), 0.5, error_if_nonfinite=True)
            self.optimizer.step()
            
        # Copy new weights into old policy:
        self.policy_old.load_state_dict(self.policy.state_dict())
