"""Train a PPO agent on Crafter.

Two presets:
  --preset student  ~1M params, fast policy meant to run on the edge.
  --preset teacher  ~10M params, the advisor model that runs server-side.

The same script trains both — only width/depth differ. Logs Crafter
score (log mean unlock rate over the 22 achievements) periodically.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import torch
from stable_baselines3 import PPO
from stable_baselines3.common.callbacks import BaseCallback, CheckpointCallback
from stable_baselines3.common.vec_env import SubprocVecEnv

# Make `shared.*` importable when running this file directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from advisorlib.crafter_gym import CrafterGymEnv  # noqa: E402

CRAFTER_ACHIEVEMENTS = [
    "collect_coal", "collect_diamond", "collect_drink", "collect_iron",
    "collect_sapling", "collect_stone", "collect_wood", "defeat_skeleton",
    "defeat_zombie", "eat_cow", "eat_plant", "make_iron_pickaxe",
    "make_iron_sword", "make_stone_pickaxe", "make_stone_sword",
    "make_wood_pickaxe", "make_wood_sword", "place_furnace", "place_plant",
    "place_stone", "place_table", "wake_up",
]


PRESETS = {
    "student": dict(
        features_dim=128,
        net_arch=[64, 64],
        n_steps=128,
        batch_size=256,
        n_envs=8,
        learning_rate=3e-4,
        ent_coef=0.01,
    ),
    "teacher": dict(
        features_dim=512,
        net_arch=[512, 512],
        n_steps=256,
        batch_size=512,
        n_envs=8,
        learning_rate=2.5e-4,
        ent_coef=0.005,
    ),
}


class CrafterScoreCallback(BaseCallback):
    """Tracks per-episode achievement unlocks across the vec env, logs the
    Crafter score (geometric-ish mean unlock rate) every `log_every` steps.
    """

    def __init__(self, log_every: int = 50_000, verbose: int = 0):
        super().__init__(verbose)
        self.log_every = log_every
        self._next_log_at = log_every
        self._episode_unlocks: list[set[str]] = []
        self._open: dict[int, set[str]] = {}

    def _on_step(self) -> bool:
        infos = self.locals.get("infos", [])
        dones = self.locals.get("dones", [])
        for i, info in enumerate(infos):
            ach = info.get("achievements", {})
            if ach:
                cur = self._open.setdefault(i, set())
                for k, v in ach.items():
                    if v:
                        cur.add(k)
            if dones[i]:
                self._episode_unlocks.append(self._open.pop(i, set()))

        if self.num_timesteps >= self._next_log_at:
            self._next_log_at += self.log_every
            if self._episode_unlocks:
                rates = []
                for a in CRAFTER_ACHIEVEMENTS:
                    n = sum(1 for s in self._episode_unlocks if a in s)
                    rates.append(n / len(self._episode_unlocks))
                # Crafter score: exp(mean(log(1+100*rate))) - 1, in %.
                score = float(np.exp(np.mean(np.log(1 + 100 * np.array(rates)))) - 1)
                self.logger.record("crafter/score", score)
                self.logger.record("crafter/episodes", len(self._episode_unlocks))
                self.logger.record(
                    "crafter/avg_unlocks_per_ep",
                    float(np.mean([len(s) for s in self._episode_unlocks])),
                )
                # Roll the window so the score reflects recent perf.
                self._episode_unlocks = self._episode_unlocks[-200:]
        return True


def make_env(rank: int, seed: int):
    def _init():
        return CrafterGymEnv(seed=seed + rank)
    return _init


def build_model(preset: str, seed: int, log_dir: Path) -> PPO:
    cfg = PRESETS[preset]
    env_fns = [make_env(i, seed) for i in range(cfg["n_envs"])]
    vec = SubprocVecEnv(env_fns)
    policy_kwargs = dict(
        net_arch=cfg["net_arch"],
        features_extractor_kwargs=dict(features_dim=cfg["features_dim"]),
    )
    model = PPO(
        "CnnPolicy",
        vec,
        n_steps=cfg["n_steps"],
        batch_size=cfg["batch_size"],
        learning_rate=cfg["learning_rate"],
        ent_coef=cfg["ent_coef"],
        n_epochs=4,
        gamma=0.99,
        gae_lambda=0.95,
        clip_range=0.2,
        verbose=1,
        seed=seed,
        device="cpu",
        policy_kwargs=policy_kwargs,
    )
    n_params = sum(p.numel() for p in model.policy.parameters())
    print(f"[train_ppo] preset={preset} total_params={n_params:,}")
    return model


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--preset", choices=list(PRESETS), required=True)
    p.add_argument("--total-steps", type=int, default=5_000_000)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out-dir", type=Path,
                   default=Path(__file__).resolve().parents[1] / "checkpoints")
    p.add_argument("--log-dir", type=Path,
                   default=Path(__file__).resolve().parents[1] / "logs")
    args = p.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    args.log_dir.mkdir(parents=True, exist_ok=True)

    torch.set_num_threads(2)  # leave room for the 8 env subprocs

    model = build_model(args.preset, args.seed, args.log_dir)
    ckpt_cb = CheckpointCallback(
        save_freq=max(args.total_steps // 10, 50_000) // PRESETS[args.preset]["n_envs"],
        save_path=str(args.out_dir),
        name_prefix=f"ppo_{args.preset}",
    )
    score_cb = CrafterScoreCallback(log_every=50_000)

    t0 = time.time()
    model.learn(total_timesteps=args.total_steps, callback=[ckpt_cb, score_cb])
    elapsed = time.time() - t0

    final_path = args.out_dir / f"ppo_{args.preset}_final.zip"
    model.save(str(final_path))
    print(f"[train_ppo] {args.preset} done in {elapsed/60:.1f} min -> {final_path}")


if __name__ == "__main__":
    main()
