import random
from typing import Callable, Union
from reconstrain.models.base import *
from reconstrain.rl import ReplayBuffer


@auto_args
class MotionPlanningImitation(MotionPlanningActorCritic):
    def __init__(
        self,
        buffer_size: int = 1000000,
        target_policy: str = "c",
        expert_probability: float = 0.5,
        expert_probability_decay: float = 0.99,
        render: bool = False,
        **kwargs,
    ):
        """
        Args:
            buffer_size: size of the replay buffer
            target_policy: the target policy to use for the expert
            expert_probability: probability of sampling from the expert
            render: whether to render the environment
        """
        super().__init__(**kwargs)
        self.save_hyperparameters()
        self.target_policy = target_policy
        self.render = render > 0
        self.buffer = ReplayBuffer[BaseData](buffer_size)
        self.expert_probability = expert_probability
        self.expert_probability_decay = expert_probability_decay

    def training_step(self, data, batch_idx, optimizer_idx):
        if optimizer_idx == 0:
            mu, _ = self.actor.forward(data.state, data)
            mu = F.mse_loss(mu, data.expert)
            self.log("train/mu_loss", mu, prog_bar=True)
            return mu
        elif optimizer_idx == 1:
            q = self.critic.forward(data.state, data.action, data)
            with torch.no_grad():
                muprime, _ = self.actor.forward(data.next_state, data)
                qprime = self.critic(data.next_state, muprime, data)
            loss = self.critic_loss(q, qprime, data.reward, data.done)
            self.log("train/critic_loss", loss, prog_bar=True)
            return loss

    def validation_step(self, data, batch_idx):
        yhat, _ = self.actor.forward(data.state, data)
        loss = F.mse_loss(yhat, data.expert)
        self.log("val/loss", loss, prog_bar=True)
        self.log("val/reward", data.reward.mean(), prog_bar=True)
        self.log("val/metric", -data.reward.mean())
        return loss

    def test_step(self, data, batch_idx):
        yhat, _ = self.actor.forward(data.state, data)
        loss = F.mse_loss(yhat, data.expert)
        self.log("test/loss", loss)
        self.log("test/reward", data.reward.mean())
        return loss

    def rollout_start(self):
        # decide if expert will be used for rollout
        if self.training:
            expert_probability = (
                self.expert_probability
                * self.expert_probability_decay ** self.current_epoch
            )
            self.use_expert = random.random() < expert_probability
        else:
            self.use_expert = False

    def rollout_step(
        self,
        data: BaseData,
    ):
        if self.target_policy == "c":
            expert = self.env.centralized_policy()
        elif self.target_policy == "d0":
            expert = self.env.decentralized_policy(0)
        elif self.target_policy == "d1":
            expert = self.env.decentralized_policy(1)
        else:
            raise ValueError(f"Unknown target policy {self.target_policy}")

        data.expert = torch.as_tensor(expert, dtype=self.dtype, device=self.device)  # type: ignore
        assert isinstance(data.mu, torch.Tensor)

        action = data.expert if self.use_expert else data.mu
        next_state, reward, done, _ = self.env.step(action.detach().cpu().numpy())
        return data, next_state, reward, done

    def batch_generator(self, n_episodes=1, render=False, use_buffer=True):
        data = []
        for _ in range(n_episodes):
            data.extend(self.rollout(render=render))
        if use_buffer:
            self.buffer.extend(data)
            data = self.buffer.collect(shuffle=True)
        return iter(data)

    def train_dataloader(self):
        return self._dataloader(n_episodes=1, render=False, use_buffer=True)

    def val_dataloader(self):
        return self._dataloader(n_episodes=10, render=self.render, use_buffer=False)

    def test_dataloader(self):
        return self._dataloader(n_episodes=100, render=self.render, use_buffer=False)
