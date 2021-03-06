import argparse
import os
import sys
from typing import Union

import numpy as np
import pytorch_lightning as pl
import torch
from pytorch_lightning.callbacks import EarlyStopping, ModelCheckpoint
from pytorch_lightning.loggers import TensorBoardLogger
from reconstrain.envs.motion_planning import MotionPlanning
from reconstrain.models import (
    MotionPlanningActorCritic,
    MotionPlanningGPG,
    MotionPlanningImitation,
    MotionPlanningTD3,
)
from reconstrain.utils import TensorboardHistogramLogger
from tqdm import tqdm


def make_trainer(params):
    logger = (
        TensorBoardLogger(
            save_dir=f"./{params.operation}/", name="tensorboard", version=""
        )
        if params.log
        else None
    )
    callbacks = [
        EarlyStopping(
            monitor="val/metric",
            patience=params.patience,
        ),
        (
            ModelCheckpoint(
                monitor="val/metric",
                dirpath=f"./{params.operation}/checkpoints/",
                filename="best",
                auto_insert_metric_name=False,
                mode="min",
                save_last=True,
                save_top_k=1,
            )
            if params.log
            else None
        ),
        (
            TensorboardHistogramLogger(every_n_steps=1000)
            if params.log and params.histograms
            else None
        ),
    ]
    callbacks = [cb for cb in callbacks if cb is not None]

    print("starting training")
    trainer = pl.Trainer(
        logger=logger,
        callbacks=callbacks,
        enable_checkpointing=params.log,
        precision=32,
        gpus=params.gpus,
        max_epochs=params.max_epochs,
        default_root_dir=".",
        check_val_every_n_epoch=10,
    )
    return trainer


def find_checkpoint(operation):
    candidates = [
        f"./{operation}/checkpoints/best.ckpt",
        f"./{operation}/checkpoints/last.ckpt",
    ]
    for ckpt in candidates:
        if os.path.exists(ckpt):
            return ckpt


def imitation(params):
    trainer = make_trainer(params)
    model = MotionPlanningImitation(**vars(params))
    ckpt_path = "./imitation/checkpoints/last.ckpt"
    ckpt_path = ckpt_path if os.path.exists(ckpt_path) else None
    trainer.fit(model, ckpt_path=ckpt_path)
    if params.test:
        trainer.test(model)


def train(params):
    trainer = make_trainer(params)
    imitation_checkpoint = find_checkpoint("imitation")
    if imitation_checkpoint is not None:
        print("Resuming from pretraining.")
        imitation = MotionPlanningImitation.load_from_checkpoint(imitation_checkpoint)
        merged = {
            **vars(params),
            **{
                "F": imitation.F,
                "K": imitation.K,
                "n_layers": imitation.n_layers,
            },
        }
        model = get_model(params.operation)(**merged)
        model.actor = imitation.actor
    else:
        print("Did not find a pretrain checkpoint.")
        model = get_model(params.operation)(**vars(params))

    # check if checkpoint exists
    ckpt_path = "./train/checkpoints/last.ckpt"
    ckpt_path = ckpt_path if os.path.exists(ckpt_path) else None
    trainer.fit(model, ckpt_path=ckpt_path)
    if params.test:
        trainer.test(model)


def _test(
    policy: Union[MotionPlanningActorCritic, str],
    max_steps=200,
    n_trials=10,
    render=False,
):
    env = MotionPlanning()
    rewards = []

    reference_policy = {
        "c": env.centralized_policy,
        "d0": lambda: env.decentralized_policy(0),
        "d1": lambda: env.decentralized_policy(1),
    }

    for _ in tqdm(range(n_trials)):
        trial_rewards = []
        observation = env.reset()
        for _ in range(max_steps):
            if render:
                env.render()
            if isinstance(policy, str):
                action = reference_policy[policy]()
            else:
                with torch.no_grad():
                    data = policy.to_data(observation, env.adjacency())
                    action = (
                        policy.policy(
                            *policy.forward(data.x, data),
                            deterministic=True,
                        )[0]
                        .detach()
                        .cpu()
                        .numpy()
                    )
            observation, reward, done, _ = env.step(action)
            trial_rewards.append(reward)
            if done:
                break
        trial_reward = np.mean(trial_rewards)
        rewards.append(trial_reward)

    rewards = np.asarray(rewards)
    print(
        f"""
    MEAN: {rewards.mean():.2f}
    STD: {rewards.std():.2f}
    """
    )


def test(params):
    checkpoint_path = find_checkpoint("pretrain")
    if checkpoint_path is not None:
        model = MotionPlanningImitation.load_from_checkpoint(checkpoint_path)
    else:
        checkpoint_path = find_checkpoint("train")
        model = MotionPlanningGPG.load_from_checkpoint(checkpoint_path)
    _test(model, n_trials=params.n_trials, render=params.render)


def get_model(model_str) -> MotionPlanningActorCritic:
    return {
        "imitation": MotionPlanningImitation,
        "gpg": MotionPlanningGPG,
        "td3": MotionPlanningTD3,
    }[model_str]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    # program arguments
    parser.add_argument(
        "operation",
        metavar="OP",
        type=str,
        default="td3",
        choices=["imitation", "gpg", "td3", "test", "baseline"],
    )
    operation = sys.argv[1]

    parser.add_argument("--log", type=int, default=1)
    parser.add_argument("--histograms", type=int, default=0)
    parser.add_argument("--patience", type=int, default=10)
    parser.add_argument("--test", type=int, default=1)

    # trainer arguments
    group = parser.add_argument_group("Trainer")
    group.add_argument("--max_epochs", type=int, default=100)
    group.add_argument("--gpus", type=int, default=1)

    # operation specific arguments arguments
    group = parser.add_argument_group("Operation")
    if operation in ("imitation", "gpg", "td3"):
        get_model(operation).add_args(group)
    elif operation in ("test", "baseline"):
        group.add_argument("--render", type=int, default=0)
        group.add_argument("--n_trials", type=int, default=10)
        if operation == "baseline":
            group.add_argument("--policy", type=str, default="c")

    params = parser.parse_args()
    if params.operation in ("gpg", "td3"):
        train(params)
    elif params.operation == "test":
        test(params)
    elif params.operation == "imitation":
        imitation(params)
    elif params.operation == "baseline":
        _test(params.policy, n_trials=params.n_trials, render=params.render)
    else:
        raise ValueError(f"Invalid operation {params.operation}.")
