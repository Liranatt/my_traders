import torch
import torch.nn as nn
from torch.distributions import Categorical

from rl.config import ACTION_DIM

class MaskedCategorical:
    def __init__(self, logits: torch.Tensor, mask: torch.Tensor):
        self.logits = logits
        self.mask = mask
        # Apply mask: set invalid logits to -inf
        masked_logits = logits.masked_fill(~mask, -float('inf'))
        self.dist = Categorical(logits=masked_logits)
        
    def sample(self):
        return self.dist.sample()
        
    def log_prob(self, action):
        return self.dist.log_prob(action)
        
    def entropy(self):
        return self.dist.entropy()

class PolicyNetwork(nn.Module):
    def __init__(self, obs_dim: int, hidden_dims: list[int] = [64, 64], action_dim: int = ACTION_DIM):
        super().__init__()
        
        # Actor
        actor_layers = []
        last_dim = obs_dim
        for dim in hidden_dims:
            actor_layers.append(nn.Linear(last_dim, dim))
            actor_layers.append(nn.Tanh())
            last_dim = dim
        actor_layers.append(nn.Linear(last_dim, action_dim))  # HOLD, ENTER sizes, EXIT
        self.actor = nn.Sequential(*actor_layers)

        # Critic
        critic_layers = []
        last_dim = obs_dim
        for dim in hidden_dims:
            critic_layers.append(nn.Linear(last_dim, dim))
            critic_layers.append(nn.Tanh())
            last_dim = dim
        critic_layers.append(nn.Linear(last_dim, 1))
        self.critic = nn.Sequential(*critic_layers)
        
    def forward(self, x):
        logits = self.actor(x)
        value = self.critic(x)
        return logits, value

    def get_action(self, obs, mask):
        logits, value = self.forward(obs)
        dist = MaskedCategorical(logits, mask)
        action = dist.sample()
        return action, dist.log_prob(action), dist.entropy(), value

    def get_value(self, obs):
        _, value = self.forward(obs)
        return value

    def evaluate_actions(self, obs, actions, masks):
        logits, values = self.forward(obs)
        dist = MaskedCategorical(logits, masks)
        log_probs = dist.log_prob(actions)
        dist_entropy = dist.entropy()
        return values, log_probs, dist_entropy
