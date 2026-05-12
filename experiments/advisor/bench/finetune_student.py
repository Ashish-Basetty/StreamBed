"""PPO fine-tune the BC-distilled student.

Takes a BC checkpoint (single-env, no rollout history), swaps in a
SubprocVecEnv with parallel envs, and runs PPO for N steps. The BC
warm start means the policy already roughly imitates the teacher; PPO
covers the state-distribution shift (states the BC student visits when
acting greedily aren't quite the teacher's state distribution) and
firms up the value head.

Usage:
  python -m experiments.advisor.bench.finetune_student \
      --checkpoint checkpoints/ppo_student_bc.zip \
      --steps 1000000

Output: ppo_student_final.zip in --out-dir.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import torch
from stable_baselines3 import PPO
from stable_baselines3.common.callbacks import CheckpointCallback
from stable_baselines3.common.vec_env import SubprocVecEnv

from experiments.advisor.advisorlib.crafter_gym import CrafterGymEnv
from experiments.advisor.bench.train_ppo import PRESETS, CrafterScoreCallback


def make_env(rank: int, seed: int):
    def _init():
        return CrafterGymEnv(seed=seed + rank)
    return _init


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--checkpoint", type=Path, required=True,
                   help="BC student to fine-tune.")
    p.add_argument("--steps", type=int, default=1_000_000,
                   help="PPO env-step budget for fine-tuning.")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--lr", type=float, default=1e-4,
                   help="Lower than from-scratch PPO (3e-4) so we don't "
                        "trash the BC priors with aggressive updates.")
    p.add_argument("--n-envs", type=int, default=PRESETS["student"]["n_envs"])
    p.add_argument("--out-dir", type=Path,
                   default=Path(__file__).resolve().parents[1] / "checkpoints")
    args = p.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(2)

    print(f"[finetune] loading BC student from {args.checkpoint}")
    env = SubprocVecEnv([make_env(i, args.seed) for i in range(args.n_envs)])
    model = PPO.load(str(args.checkpoint), env=env, device="cpu")

    # Override LR; PPO.load preserves whatever was set during BC. The BC
    # student has converged on teacher actions — PPO with a hot LR would
    # walk the policy off that prior in a few hundred updates.
    model.lr_schedule = lambda _progress: args.lr
    model.learning_rate = args.lr

    n_params = sum(p.numel() for p in model.policy.parameters())
    print(f"[finetune] student params={n_params:,} lr={args.lr} "
          f"n_envs={args.n_envs} budget={args.steps:,} steps")

    score_cb = CrafterScoreCallback(log_every=50_000)
    ckpt_cb = CheckpointCallback(
        save_freq=max(args.steps // 5, 50_000) // args.n_envs,
        save_path=str(args.out_dir),
        name_prefix="ppo_student_ft",
    )

    t0 = time.time()
    model.learn(total_timesteps=args.steps, callback=[score_cb, ckpt_cb],
                reset_num_timesteps=True)
    elapsed = time.time() - t0

    out_path = args.out_dir / "ppo_student_final.zip"
    model.save(str(out_path))
    print(f"[finetune] done in {elapsed/60:.1f} min -> {out_path}")


if __name__ == "__main__":
    main()
