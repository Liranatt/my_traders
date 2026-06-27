import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np

from rl.config import ACTION_EXIT, ACTION_HOLD
from rl.config import OBSERVATION_COLS

CEM_RECOMMENDS_EXIT_IDX = OBSERVATION_COLS.index("cem_recommends_exit")
CONVERGENCE_RESIDUAL_IDX = OBSERVATION_COLS.index("convergence_residual")
CEM_TARGET_PROB = 0.95
CEM_OTHER_PROB = 0.05


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
            self.policy.parameters(), 
            lr=config.actor_lr, 
            weight_decay=config.weight_decay
        )
        self.buffer = RolloutBuffer()
        self.mse_loss = nn.MSELoss()
        self.bc_loss = nn.CrossEntropyLoss()
        self._zero_cem_input_weights()

    def _split_cem_feature(self, obs: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        policy_obs = obs.clone()
        target_cem = policy_obs[:, CEM_RECOMMENDS_EXIT_IDX].clone()
        policy_obs[:, CEM_RECOMMENDS_EXIT_IDX] = 0.0
        return policy_obs, target_cem

    def _zero_cem_input_weights(self) -> None:
        # Keep checkpoints insensitive to the CEM feature even if a caller forgets to mask it.
        for tower_name in ("actor", "critic"):
            tower = getattr(self.policy, tower_name, None)
            if tower is None:
                continue
            for module in tower:
                if isinstance(module, nn.Linear) and module.in_features > CEM_RECOMMENDS_EXIT_IDX:
                    with torch.no_grad():
                        module.weight[:, CEM_RECOMMENDS_EXIT_IDX].zero_()
                    break

    def _soft_long_states(self, masks: torch.Tensor) -> torch.Tensor:
        # Only imitate/regularize states where the policy genuinely chooses
        # between HOLD and EXIT. Hard-exit-only states are supervised elsewhere.
        return masks[:, ACTION_HOLD] & masks[:, ACTION_EXIT]

    def update_bc(self):
        if not self.buffer.obs:
            return 0.0

        old_obs = torch.tensor(np.array(self.buffer.obs), dtype=torch.float32)
        old_masks = torch.tensor(np.array(self.buffer.masks), dtype=torch.bool)

        policy_obs, target_cem = self._split_cem_feature(old_obs)
        long_states = self._soft_long_states(old_masks)
        if not long_states.any():
            self.buffer.clear()
            return 0.0

        b_obs = policy_obs[long_states]
        b_cem = target_cem[long_states]
        targets = torch.where(
            b_cem >= 0.5,
            torch.ones_like(b_cem, dtype=torch.long),
            torch.zeros_like(b_cem, dtype=torch.long),
        )

        logits, _ = self.policy.forward(b_obs)
        exit_hold_logits = logits[:, [ACTION_HOLD, ACTION_EXIT]]
        loss = self.bc_loss(exit_hold_logits, targets)

        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()
        self._zero_cem_input_weights()

        loss_val = float(loss.item())
        self.buffer.clear()
        return loss_val

    def update(self, kl_coef=0.0):
        if not self.buffer.obs:
            return 0.0

        # Convert list of arrays/tensors to tensors
        old_obs = torch.tensor(np.array(self.buffer.obs), dtype=torch.float32)
        old_actions = torch.tensor(np.array(self.buffer.actions), dtype=torch.long)
        old_logprobs = torch.tensor(np.array(self.buffer.logprobs), dtype=torch.float32)
        old_masks = torch.tensor(np.array(self.buffer.masks), dtype=torch.bool)
        old_values = torch.tensor(np.array(self.buffer.values), dtype=torch.float32)
        rewards = self.buffer.rewards
        dones = self.buffer.dones

        # Calculate advantages using GAE (work in floats; dones cut the trace).
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
        
        # Normalize advantages
        if advantages.std() > 1e-8:
            advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)

        total_loss_val = 0.0
        
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
                b_returns = returns[batch_idx]

                b_obs, b_cem = self._split_cem_feature(b_obs)
                logits, values = self.policy.forward(b_obs)
                masked_logits = logits.masked_fill(~b_masks, -float('inf'))
                dist = torch.distributions.Categorical(logits=masked_logits)
                logprobs = dist.log_prob(b_actions)
                dist_entropy = dist.entropy()
                values = values.view(-1)
                b_returns = b_returns.view(-1)

                ratios = torch.exp(logprobs - b_logprobs)
                
                surr1 = ratios * b_advantages
                surr2 = torch.clamp(ratios, 1.0 - self.config.clip_epsilon, 1.0 + self.config.clip_epsilon) * b_advantages
                
                actor_loss = -torch.min(surr1, surr2).mean()
                critic_loss = self.mse_loss(values, b_returns)
                kl_divergence_loss = logits.new_tensor(0.0)
                if kl_coef > 0.0:
                    long_states = self._soft_long_states(b_masks)
                    if long_states.any():
                        policy_probs = dist.probs[long_states][:, [ACTION_HOLD, ACTION_EXIT]]
                        policy_probs = policy_probs / policy_probs.sum(dim=1, keepdim=True).clamp_min(1e-8)

                        target_probs = torch.full_like(policy_probs, CEM_OTHER_PROB)
                        cem_exit = b_cem[long_states] >= 0.5
                        target_probs[:, 0] = torch.where(
                            cem_exit,
                            torch.full_like(target_probs[:, 0], CEM_OTHER_PROB),
                            torch.full_like(target_probs[:, 0], CEM_TARGET_PROB),
                        )
                        target_probs[:, 1] = torch.where(
                            cem_exit,
                            torch.full_like(target_probs[:, 1], CEM_TARGET_PROB),
                            torch.full_like(target_probs[:, 1], CEM_OTHER_PROB),
                        )
                        kl_div = (
                                policy_probs
                                * (policy_probs.clamp_min(1e-8).log() - target_probs.log())
                        ).sum(dim=1)

                        # Fade KL if convergence_residual < 0 (thesis broken)
                        # We allow deviation from CEM to cut losers early
                        conv_res = b_obs[long_states, CONVERGENCE_RESIDUAL_IDX]
                        kl_weights = torch.where(conv_res < 0.0, 0.1, 1.0)

                        kl_divergence_loss = (kl_div * kl_weights).mean()

                loss = (
                    actor_loss
                    + 0.5 * critic_loss
                    - self.config.entropy_beta * dist_entropy.mean()
                    + kl_coef * kl_divergence_loss
                )

                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()
                self._zero_cem_input_weights()
                
                total_loss_val += loss.item()

        self.buffer.clear()
        return total_loss_val
