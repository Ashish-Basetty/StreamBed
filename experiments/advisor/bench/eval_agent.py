"""Evaluate a trained PPO checkpoint on Crafter.

Runs N episodes, prints per-achievement unlock rate and the canonical
Crafter score: S = exp(mean(log(1 + 100*rate))) - 1, in %.

Usage:
  python -m experiments.advisor.bench.eval_agent --checkpoint experiments/advisor/checkpoints/ppo_teacher_final.zip
  python -m experiments.advisor.bench.eval_agent --random  # baseline
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
from stable_baselines3 import PPO

from experiments.advisor.advisorlib.crafter_gym import CrafterGymEnv
from experiments.advisor.bench.train_ppo import CRAFTER_ACHIEVEMENTS


def evaluate(
    model: PPO | None,
    n_episodes: int,
    deterministic: bool,
    seed: int,
) -> tuple[list[set[str]], list[float]]:
    """Returns (per_episode_unlocks, per_episode_return)."""
    env = CrafterGymEnv(seed=seed)
    rng = np.random.default_rng(seed)
    unlocks: list[set[str]] = []
    returns: list[float] = []
    for ep in range(n_episodes):
        obs, _ = env.reset()
        ep_unlocked: set[str] = set()
        ep_return = 0.0
        done = False
        while not done:
            if model is None:
                a = int(rng.integers(0, env.action_space.n))
            else:
                a, _ = model.predict(obs, deterministic=deterministic)
                a = int(a)
            obs, r, term, trunc, info = env.step(a)
            ep_return += float(r)
            ach = info.get("achievements", {})
            for k, v in ach.items():
                if v:
                    ep_unlocked.add(k)
            done = term or trunc
        unlocks.append(ep_unlocked)
        returns.append(ep_return)
        if (ep + 1) % 10 == 0:
            print(f"  episode {ep+1}/{n_episodes}: "
                  f"unlocks={len(ep_unlocked)} return={ep_return:.2f}")
    return unlocks, returns


def crafter_score(unlocks: list[set[str]]) -> tuple[float, dict[str, float]]:
    """Canonical Crafter score and per-achievement unlock rate."""
    n = len(unlocks)
    rates = {}
    for a in CRAFTER_ACHIEVEMENTS:
        rates[a] = sum(1 for s in unlocks if a in s) / max(n, 1)
    arr = np.array(list(rates.values()))
    score = float(np.exp(np.mean(np.log(1 + 100 * arr))) - 1)
    return score, rates


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--checkpoint", type=Path, default=None)
    p.add_argument("--random", action="store_true",
                   help="Evaluate a random policy as baseline.")
    p.add_argument("--episodes", type=int, default=50)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--stochastic", action="store_true",
                   help="Sample from policy (default: argmax for reproducibility).")
    args = p.parse_args()

    if not args.random and args.checkpoint is None:
        p.error("provide --checkpoint or --random")

    label = "RANDOM" if args.random else f"CHECKPOINT={args.checkpoint.name}"
    print(f"[eval] {label} over {args.episodes} episodes, seed={args.seed}")

    model = None
    if not args.random:
        model = PPO.load(str(args.checkpoint), device="cpu")

    t0 = time.time()
    unlocks, returns = evaluate(
        model, args.episodes, deterministic=not args.stochastic, seed=args.seed
    )
    elapsed = time.time() - t0

    score, rates = crafter_score(unlocks)
    avg_unlocks = float(np.mean([len(s) for s in unlocks]))
    avg_ret = float(np.mean(returns))
    print()
    print(f"== {label} ==")
    print(f"  episodes:        {len(unlocks)}")
    print(f"  Crafter score:   {score:.2f}")
    print(f"  avg unlocks/ep:  {avg_unlocks:.2f}")
    print(f"  avg return:      {avg_ret:.2f}")
    print(f"  wallclock:       {elapsed:.1f}s")
    print()
    print("  per-achievement unlock rate (sorted):")
    for k, v in sorted(rates.items(), key=lambda kv: -kv[1]):
        bar = "#" * int(v * 40)
        print(f"    {k:24s} {v*100:5.1f}%  {bar}")


if __name__ == "__main__":
    main()
