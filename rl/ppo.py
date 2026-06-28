from __future__ import annotations

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim


class RolloutBuffer:
    def __init__(self):
        self.obs = []
        self.actions = []
        self.logprobs = []
        self.rewards = []
        self.values = []
        self.masks = []
        self.dones = []

    def clear(self):
        self.obs.clear()
        self.actions.clear()
        self.logprobs.clear()
        self.rewards.clear()
        self.values.clear()
        self.masks.clear()
        self.dones.clear()


class PPO:
    def __init__(self, policy, config):
        self.policy = policy
        self.config = config
        self.optimizer = optim.AdamW(
            [
                {"params": self.policy.actor.parameters(), "lr": config.actor_lr},
                {"params": self.policy.critic.parameters(), "lr": config.critic_lr},
            ],
            weight_decay=config.weight_decay,
        )
        self.buffer = RolloutBuffer()
        self.mse_loss = nn.MSELoss()
        self.bc_loss = nn.CrossEntropyLoss()



    def update(self, *, entropy_coef: float = 0.02) -> float:
        if not self.buffer.obs:
            return 0.0

        old_obs = torch.tensor(np.array(self.buffer.obs), dtype=torch.float32)
        old_actions = torch.tensor(np.array(self.buffer.actions), dtype=torch.long)
        old_logprobs = torch.tensor(np.array(self.buffer.logprobs), dtype=torch.float32)
        old_masks = torch.tensor(np.array(self.buffer.masks), dtype=torch.bool)
        old_values = torch.tensor(np.array(self.buffer.values), dtype=torch.float32)
        rewards = self.buffer.rewards
        dones = self.buffer.dones

        values_list = [float(v) for v in old_values]
        advantages = []
        gae = 0.0
        for i in reversed(range(len(rewards))):
            next_value = 0.0 if i == len(rewards) - 1 else values_list[i + 1]
            nonterminal = 1.0 - float(dones[i])
            delta = rewards[i] + self.config.gamma * next_value * nonterminal - values_list[i]
            gae = delta + self.config.gamma * self.config.gae_lambda * nonterminal * gae
            advantages.insert(0, gae)

        advantages = torch.tensor(advantages, dtype=torch.float32)
        returns = advantages + old_values
        if advantages.std() > 1e-8:
            advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)

        total_loss_val = 0.0
        updates = 0
        for _ in range(self.config.n_epochs):
            indices = np.arange(len(old_obs))
            np.random.shuffle(indices)
            minibatch_size = max(1, len(old_obs) // self.config.n_minibatches)

            for start_idx in range(0, len(old_obs), minibatch_size):
                batch_idx = indices[start_idx:start_idx + minibatch_size]
                b_obs = old_obs[batch_idx]
                b_actions = old_actions[batch_idx]
                b_logprobs = old_logprobs[batch_idx]
                b_masks = old_masks[batch_idx]
                b_advantages = advantages[batch_idx]
                b_returns = returns[batch_idx].view(-1)

                logits, values = self.policy.forward(b_obs)
                masked_logits = logits.masked_fill(~b_masks, -float("inf"))
                dist = torch.distributions.Categorical(logits=masked_logits)
                logprobs = dist.log_prob(b_actions)
                dist_entropy = dist.entropy()
                values = values.view(-1)

                ratios = torch.exp(logprobs - b_logprobs)
                surr1 = ratios * b_advantages
                surr2 = (
                    torch.clamp(ratios, 1.0 - self.config.clip_epsilon, 1.0 + self.config.clip_epsilon)
                    * b_advantages
                )
                actor_loss = -torch.min(surr1, surr2).mean()
                critic_loss = self.mse_loss(values, b_returns)
                loss = actor_loss + 0.5 * critic_loss - entropy_coef * dist_entropy.mean()

                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()
                total_loss_val += float(loss.item())
                updates += 1

        self.buffer.clear()
        return total_loss_val / max(1, updates)
