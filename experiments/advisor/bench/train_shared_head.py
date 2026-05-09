"""Train a small head on top of teacher's frozen encoder.

Shared-encoder student: edge runs teacher.encoder (transferred at deploy)
plus a small trainable head; server runs teacher's own mlp_extractor +
action_net on the wire-carried features. No "advisor head" to train —
the server already has the perfect head inside the teacher checkpoint.

Pipeline:
  1. Roll the teacher in Crafter, collect (frame, teacher_action).
  2. Encode all frames once via teacher's frozen CNN → (feature, action).
  3. Train SmallHead (512 → 64 → 17) supervised on teacher's actions.
  4. Eval on held-out split: head should hit >>90% argmax accuracy
     (it's literally fitting teacher logits on teacher features).
  5. Optionally roll the shared student in Crafter and report Crafter
     score — this is the new "edge solo" baseline.

Outputs:
  - shared_head.pt: head state_dict + meta. Loaded by the edge
    inference container alongside the existing teacher checkpoint.
  - Optional shared_student_eval.json with Crafter score.

Usage:
  python bench/train_shared_head.py \\
      --teacher checkpoints/ppo_teacher_final.zip \\
      --rollouts 50000 --epochs 8 --eval-episodes 30
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from stable_baselines3 import PPO
from torch.utils.data import DataLoader, TensorDataset

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from advisorlib.crafter_gym import CrafterGymEnv  # noqa: E402
from bench.train_ppo import CRAFTER_ACHIEVEMENTS  # noqa: E402

N_ACTIONS = 17


class SmallHead(nn.Module):
    """Edge head: teacher_feature (512) → action logits (17).

    Default 512→64→17 = ~37K params. Cheap to evaluate; the heavy
    work is the (frozen) teacher encoder.
    """

    def __init__(self, in_dim: int, hidden: int = 64, n_actions: int = N_ACTIONS):
        super().__init__()
        self.fc1 = nn.Linear(in_dim, hidden)
        self.fc2 = nn.Linear(hidden, n_actions)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.fc2(F.relu(self.fc1(x)))


def collect_teacher_data(
    teacher: PPO, n_samples: int, seed: int = 0
) -> tuple[np.ndarray, np.ndarray]:
    """Roll teacher, capture (frame, teacher_argmax_action). Single env."""
    env = CrafterGymEnv(seed=seed)
    obs, _ = env.reset()
    frames: list[np.ndarray] = []
    acts: list[int] = []
    t0 = time.time()
    next_log = 5_000
    while len(frames) < n_samples:
        a, _ = teacher.predict(obs, deterministic=False)
        frames.append(obs.copy())
        acts.append(int(a))
        obs, _r, term, trunc, _info = env.step(int(a))
        if term or trunc:
            obs, _ = env.reset()
        if len(frames) >= next_log:
            print(f"  collected {len(frames)}/{n_samples} "
                  f"({len(frames)/(time.time()-t0):.0f}/s)")
            next_log += 5_000
    return np.stack(frames), np.array(acts, dtype=np.int64)


def encode_frames(teacher: PPO, frames_hwc_uint8: np.ndarray) -> np.ndarray:
    """Run teacher.encoder on all frames once. Returns (N, features_dim)."""
    obs_chw = np.transpose(frames_hwc_uint8, (0, 3, 1, 2)).astype(np.float32)
    x = torch.from_numpy(obs_chw)
    out: list[np.ndarray] = []
    with torch.no_grad():
        for i in range(0, len(x), 1024):
            feats = teacher.policy.extract_features(x[i:i + 1024])
            out.append(feats.cpu().numpy())
    return np.concatenate(out, axis=0)


def train_head(
    head: SmallHead,
    feats: np.ndarray,
    acts: np.ndarray,
    epochs: int,
    batch_size: int,
    lr: float,
) -> None:
    optim = torch.optim.Adam(head.parameters(), lr=lr)
    x = torch.from_numpy(feats)
    y = torch.from_numpy(acts)
    loader = DataLoader(TensorDataset(x, y), batch_size=batch_size, shuffle=True)

    head.train()
    for ep in range(epochs):
        ep_loss = 0.0
        ep_correct = 0
        ep_count = 0
        for xb, yb in loader:
            logits = head(xb)
            loss = F.cross_entropy(logits, yb)
            optim.zero_grad()
            loss.backward()
            optim.step()
            ep_loss += loss.item() * xb.size(0)
            ep_correct += int((logits.argmax(-1) == yb).sum())
            ep_count += xb.size(0)
        print(f"  epoch {ep+1}/{epochs}: "
              f"train_nll={ep_loss/ep_count:.4f} "
              f"train_acc={ep_correct/ep_count:.3f}")
    head.eval()


def evaluate_offline(head: SmallHead, feats: np.ndarray, acts: np.ndarray) -> dict:
    """Held-out argmax + top-3 accuracy."""
    with torch.no_grad():
        logits = head(torch.from_numpy(feats))
    pred = logits.argmax(-1).numpy()
    top3 = torch.topk(logits, k=3, dim=-1).indices.numpy()
    return {
        "n": int(len(feats)),
        "argmax_acc": float((pred == acts).mean()),
        "top3_acc": float((top3 == acts[:, None]).any(axis=1).mean()),
    }


def evaluate_in_crafter(
    teacher: PPO, head: SmallHead, n_episodes: int, seed: int = 42
) -> dict:
    """Run shared-encoder student in Crafter, deterministic argmax actions.
    The forward path is: frame → teacher.encoder → head → action."""
    env = CrafterGymEnv(seed=seed)
    unlocks: list[set[str]] = []
    returns: list[float] = []

    for ep in range(n_episodes):
        obs, _ = env.reset()
        ep_unlocks: set[str] = set()
        ep_ret = 0.0
        done = False
        while not done:
            chw = np.transpose(obs, (2, 0, 1)).astype(np.float32)
            x = torch.from_numpy(chw).unsqueeze(0)
            with torch.no_grad():
                feat = teacher.policy.extract_features(x)
                logits = head(feat)
                a = int(logits.argmax(-1).item())
            obs, r, term, trunc, info = env.step(a)
            ep_ret += float(r)
            for k, v in info.get("achievements", {}).items():
                if v:
                    ep_unlocks.add(k)
            done = term or trunc
        unlocks.append(ep_unlocks)
        returns.append(ep_ret)
        if (ep + 1) % 5 == 0:
            print(f"  crafter ep {ep+1}/{n_episodes}: "
                  f"unlocks={len(ep_unlocks)} return={ep_ret:.2f}")

    rates = np.array([
        sum(1 for s in unlocks if a in s) / len(unlocks)
        for a in CRAFTER_ACHIEVEMENTS
    ])
    score = float(np.exp(np.mean(np.log(1 + 100 * rates))) - 1)
    avg_unlocks = float(np.mean([len(s) for s in unlocks]))
    return {
        "episodes": len(unlocks),
        "crafter_score": score,
        "avg_unlocks_per_ep": avg_unlocks,
        "avg_return": float(np.mean(returns)),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--teacher", type=Path, required=True)
    p.add_argument("--rollouts", type=int, default=50_000)
    p.add_argument("--epochs", type=int, default=8)
    p.add_argument("--batch-size", type=int, default=512)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--hidden", type=int, default=64,
                   help="Edge head hidden dim. Total params ≈ "
                        "512*hidden + hidden*17.")
    p.add_argument("--holdout-frac", type=float, default=0.2)
    p.add_argument("--eval-episodes", type=int, default=30,
                   help="Crafter episodes for end-to-end eval. 0 to skip.")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out-dir", type=Path,
                   default=Path(__file__).resolve().parents[1] / "checkpoints")
    args = p.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(2)

    print(f"[shared] loading teacher={args.teacher.name}")
    teacher = PPO.load(str(args.teacher), device="cpu")

    # Probe encoder output dim with a dummy frame.
    dummy = torch.zeros(1, 3, 64, 64)
    with torch.no_grad():
        feat_dim = teacher.policy.extract_features(dummy).shape[1]
    print(f"[shared] teacher features_dim = {feat_dim}")

    print(f"[shared] collecting {args.rollouts} (frame, teacher_action) pairs")
    frames, acts = collect_teacher_data(teacher, args.rollouts, seed=args.seed)

    print(f"[shared] encoding all frames once via teacher.encoder")
    feats = encode_frames(teacher, frames)
    print(f"[shared] feats={feats.shape}")

    # Held-out split.
    n = len(feats)
    rng = np.random.default_rng(args.seed)
    idx = rng.permutation(n)
    n_eval = int(n * args.holdout_frac)
    eval_idx, train_idx = idx[:n_eval], idx[n_eval:]

    head = SmallHead(in_dim=feat_dim, hidden=args.hidden)
    n_params = sum(p.numel() for p in head.parameters())
    print(f"[shared] head params = {n_params:,}")

    print(f"[shared] training head ({args.epochs} epochs)")
    train_head(head, feats[train_idx], acts[train_idx],
               args.epochs, args.batch_size, args.lr)

    metrics = evaluate_offline(head, feats[eval_idx], acts[eval_idx])
    print()
    print(f"== held-out offline eval ==")
    print(f"  n samples:    {metrics['n']}")
    print(f"  argmax acc:   {metrics['argmax_acc']:.3f}")
    print(f"  top-3 acc:    {metrics['top3_acc']:.3f}")

    crafter_metrics: dict = {}
    if args.eval_episodes > 0:
        print()
        print(f"[shared] rolling shared-encoder student in Crafter "
              f"({args.eval_episodes} episodes)")
        crafter_metrics = evaluate_in_crafter(teacher, head, args.eval_episodes,
                                              seed=42)
        print()
        print(f"== Crafter end-to-end ==")
        print(f"  episodes:        {crafter_metrics['episodes']}")
        print(f"  Crafter score:   {crafter_metrics['crafter_score']:.2f}")
        print(f"  avg unlocks/ep:  {crafter_metrics['avg_unlocks_per_ep']:.2f}")
        print(f"  avg return:      {crafter_metrics['avg_return']:.2f}")

    students_dir = args.out_dir / "students"
    eval_dir = args.out_dir / "eval"
    students_dir.mkdir(parents=True, exist_ok=True)
    eval_dir.mkdir(parents=True, exist_ok=True)

    # File names encode hyperparams so different runs don't clobber each other.
    out_path = students_dir / f"shared_h{args.hidden}.pt"
    torch.save({
        "state_dict": head.state_dict(),
        "in_dim": feat_dim,
        "hidden": args.hidden,
        "n_actions": N_ACTIONS,
        "offline_metrics": metrics,
        "crafter_metrics": crafter_metrics,
    }, str(out_path))
    print(f"\n[shared] head saved -> {out_path}")

    summary_path = eval_dir / f"shared_h{args.hidden}_{args.eval_episodes}ep.json"
    summary_path.write_text(json.dumps({
        "hidden": args.hidden,
        "n_params": n_params,
        "feat_dim": feat_dim,
        "eval_episodes": args.eval_episodes,
        "offline": metrics,
        "crafter": crafter_metrics,
    }, indent=2))
    print(f"[shared] eval summary -> {summary_path}")


if __name__ == "__main__":
    main()
