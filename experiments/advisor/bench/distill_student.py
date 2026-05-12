"""Distill the trained teacher into a small student via behavior cloning.

Usage:
  python -m experiments.advisor.bench.distill_student \
      --teacher checkpoints/ppo_teacher_final.zip \
      --rollouts 100000 \
      --epochs 5

Pipeline:
  1. Load the trained teacher PPO.
  2. Run the teacher in Crafter, collecting (obs, action) pairs until we
     have `--rollouts` samples.
  3. Initialize a fresh PPO with the `student` preset (smaller net), single
     DummyVecEnv (BC doesn't need parallel rollouts).
  4. Train the student's policy via cross-entropy on teacher actions, using
     SB3's policy.evaluate_actions() as the gradient entry point (matches
     PPO's own training preprocessing).
  5. Save as ppo_student_distilled.zip.

The output checkpoint is what runs on the edge in the experiment. The
teacher continues to live on the server and emits CSTM_RELIABLE advice.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import torch
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv
from torch.utils.data import DataLoader, TensorDataset

from experiments.advisor.advisorlib.crafter_gym import CrafterGymEnv
from experiments.advisor.bench.train_ppo import PRESETS


def collect_teacher_rollouts(
    teacher: PPO, n_samples: int, seed: int = 0
) -> tuple[np.ndarray, np.ndarray]:
    """Roll teacher out in Crafter, accumulate (obs, action) until we have
    n_samples. Resets on done; agent doesn't need one continuous game."""
    env = CrafterGymEnv(seed=seed)
    obs, _ = env.reset()
    obs_buf: list[np.ndarray] = []
    act_buf: list[int] = []
    t0 = time.time()
    next_log = 10_000
    while len(obs_buf) < n_samples:
        action, _ = teacher.predict(obs, deterministic=False)
        obs_buf.append(obs.copy())
        act_buf.append(int(action))
        obs, _r, term, trunc, _info = env.step(int(action))
        if term or trunc:
            obs, _ = env.reset()
        if len(obs_buf) >= next_log:
            elapsed = time.time() - t0
            print(f"[distill] collected {len(obs_buf)}/{n_samples} "
                  f"({len(obs_buf)/elapsed:.0f}/s)")
            next_log += 10_000
    return np.stack(obs_buf), np.array(act_buf, dtype=np.int64)


def build_student_for_bc(seed: int) -> PPO:
    """Build a fresh student PPO with the small preset and a single
    DummyVecEnv. We don't roll out during BC, so parallel envs are wasted."""
    cfg = PRESETS["student"]
    env = DummyVecEnv([lambda: CrafterGymEnv(seed=seed)])
    policy_kwargs = dict(
        net_arch=cfg["net_arch"],
        features_extractor_kwargs=dict(features_dim=cfg["features_dim"]),
    )
    model = PPO(
        "CnnPolicy",
        env,
        n_steps=cfg["n_steps"],
        batch_size=cfg["batch_size"],
        learning_rate=cfg["learning_rate"],
        n_epochs=4,
        gamma=0.99,
        seed=seed,
        device="cpu",
        policy_kwargs=policy_kwargs,
    )
    n_params = sum(p.numel() for p in model.policy.parameters())
    print(f"[distill] student params={n_params:,}")
    return model


def train_student_bc(
    student: PPO,
    obs: np.ndarray,           # (N, H, W, C) uint8
    actions: np.ndarray,       # (N,) int64
    epochs: int,
    batch_size: int,
    lr: float,
) -> None:
    """Behavior cloning: minimize -log p(teacher_action | obs) under the
    student's policy. Uses policy.evaluate_actions() so the same
    preprocessing path PPO trains under is exercised here too."""
    # SB3's CnnPolicy with normalize_images=True (default) expects float
    # tensors that it'll then divide by 255 internally. Match that.
    obs_chw = np.transpose(obs, (0, 3, 1, 2))  # (N, C, H, W)
    obs_t = torch.from_numpy(obs_chw).float()
    act_t = torch.from_numpy(actions)

    ds = TensorDataset(obs_t, act_t)
    loader = DataLoader(ds, batch_size=batch_size, shuffle=True, num_workers=0)
    optim = torch.optim.Adam(student.policy.parameters(), lr=lr)

    student.policy.train()
    for ep in range(epochs):
        ep_loss = 0.0
        ep_correct = 0
        ep_count = 0
        for x, y in loader:
            # evaluate_actions returns (values, log_prob_of_action, entropy).
            # log_prob_of_action is exactly what cross-entropy minimizes for
            # the teacher's chosen action.
            _values, log_prob, _entropy = student.policy.evaluate_actions(x, y)
            loss = -log_prob.mean()
            optim.zero_grad()
            loss.backward()
            optim.step()
            ep_loss += loss.item() * x.size(0)
            # Accuracy = how often student's argmax matches teacher.
            with torch.no_grad():
                dist = student.policy.get_distribution(x)
                pred = dist.distribution.logits.argmax(-1)
                ep_correct += int((pred == y).sum())
            ep_count += x.size(0)
        print(f"[distill] epoch {ep+1}/{epochs}: "
              f"nll={ep_loss/ep_count:.4f} "
              f"argmax_acc={ep_correct/ep_count:.3f}")
    student.policy.eval()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--teacher", type=Path, required=True)
    p.add_argument("--rollouts", type=int, default=100_000)
    p.add_argument("--epochs", type=int, default=5)
    p.add_argument("--batch-size", type=int, default=256)
    p.add_argument("--lr", type=float, default=3e-4)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out-dir", type=Path,
                   default=Path(__file__).resolve().parents[1] / "checkpoints")
    args = p.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(2)

    print(f"[distill] loading teacher from {args.teacher}")
    teacher = PPO.load(str(args.teacher), device="cpu")

    print(f"[distill] collecting {args.rollouts} (obs, action) pairs from teacher")
    obs, acts = collect_teacher_rollouts(teacher, args.rollouts, seed=args.seed)
    uniq = np.unique(acts, return_counts=True)
    print(f"[distill] obs={obs.shape} acts: unique={len(uniq[0])} "
          f"top={dict(zip(uniq[0][:5].tolist(), uniq[1][:5].tolist(), strict=False))}")

    student = build_student_for_bc(args.seed)
    print(f"[distill] BC training for {args.epochs} epochs")
    train_student_bc(student, obs, acts, args.epochs, args.batch_size, args.lr)

    out_path = args.out_dir / "ppo_student_bc.zip"
    student.save(str(out_path))
    print(f"[distill] saved -> {out_path}")


if __name__ == "__main__":
    main()
