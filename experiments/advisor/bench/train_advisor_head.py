"""Frankenstein experiment: can a small head map student features → teacher actions?

The advisor pipeline assumes the server can run a tiny `advisor_head`
on the student's CNN feature embedding to produce teacher-quality
advice. The student's encoder, though, was trained for the student's
policy — its features may not contain everything the teacher uses.
This script tests that assumption empirically before we commit to
wiring the full pipeline.

Pipeline:
  1. Roll the student in Crafter.
  2. At each step, capture (a) student's encoder feature, (b) teacher's
     argmax action on the same observation.
  3. Train AdvisorHead (128 → 64 → 17, ReLU) supervised on
     (feature, teacher_action) pairs. Held-out 20% for eval.
  4. Report:
       - overall argmax accuracy (advisor agrees with teacher)
       - top-3 accuracy (teacher's action in advisor's top 3)
       - accuracy on disagreement subset (where student ≠ teacher,
         which is where advice would actually move the action)
       - per-action accuracy

Usage:
  python -m experiments.advisor.bench.train_advisor_head \
      --student checkpoints/ppo_student_distilled.zip \
      --teacher checkpoints/ppo_teacher_final.zip \
      --rollouts 20000
"""
from __future__ import annotations

import argparse
import time
from collections import Counter
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from stable_baselines3 import PPO
from torch.utils.data import DataLoader, TensorDataset

from experiments.advisor.advisorlib.crafter_gym import CrafterGymEnv

N_ACTIONS = 17


class AdvisorHead(nn.Module):
    """Tiny head: student feature → teacher action distribution.

    Default 128→64→17 = ~9.5K params. If this can't reach 90% argmax
    accuracy vs teacher, the bigger story (advisor over the wire) is
    in trouble.
    """

    def __init__(self, in_dim: int = 128, hidden: int = 64, n_actions: int = N_ACTIONS):
        super().__init__()
        self.fc1 = nn.Linear(in_dim, hidden)
        self.fc2 = nn.Linear(hidden, n_actions)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.fc2(F.relu(self.fc1(x)))


def _obs_to_chw_tensor(obs: np.ndarray) -> torch.Tensor:
    """SB3's CnnPolicy expects CHW float input (it normalizes /255 internally
    when normalize_images=True). Match that here."""
    chw = np.transpose(obs, (2, 0, 1))  # HWC -> CHW
    return torch.from_numpy(chw).float().unsqueeze(0)  # (1, C, H, W)


def collect_dataset(
    student: PPO, teacher: PPO, n_samples: int, seed: int = 0
) -> dict:
    """Roll the student. At each step capture:
      - student's CNN feature (the thing the wire would carry)
      - student's chosen action (what edge would do solo)
      - teacher's argmax action (what advice should look like)
    Step the env using the student's action so the trajectory matches
    what the edge would actually visit at deploy time.
    """
    env = CrafterGymEnv(seed=seed)
    obs, _ = env.reset()
    feats: list[np.ndarray] = []
    student_acts: list[int] = []
    teacher_acts: list[int] = []
    t0 = time.time()
    next_log = 2_000

    while len(feats) < n_samples:
        obs_chw = _obs_to_chw_tensor(obs)
        with torch.no_grad():
            student_feat = student.policy.extract_features(obs_chw)  # (1, 128)
        s_act, _ = student.predict(obs, deterministic=False)
        t_act, _ = teacher.predict(obs, deterministic=True)

        feats.append(student_feat.squeeze(0).numpy())
        student_acts.append(int(s_act))
        teacher_acts.append(int(t_act))

        obs, _r, term, trunc, _info = env.step(int(s_act))
        if term or trunc:
            obs, _ = env.reset()

        if len(feats) >= next_log:
            elapsed = time.time() - t0
            print(f"[advisor] collected {len(feats)}/{n_samples} "
                  f"({len(feats)/elapsed:.0f}/s)")
            next_log += 2_000

    return {
        "features": np.stack(feats).astype(np.float32),
        "student_actions": np.array(student_acts, dtype=np.int64),
        "teacher_actions": np.array(teacher_acts, dtype=np.int64),
    }


def train_head(
    feats: np.ndarray,
    targets: np.ndarray,
    epochs: int,
    batch_size: int,
    lr: float,
) -> AdvisorHead:
    in_dim = feats.shape[1]
    head = AdvisorHead(in_dim=in_dim)
    optim = torch.optim.Adam(head.parameters(), lr=lr)

    x = torch.from_numpy(feats)
    y = torch.from_numpy(targets)
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
        print(f"[advisor] epoch {ep+1}/{epochs}: "
              f"train_nll={ep_loss/ep_count:.4f} train_acc={ep_correct/ep_count:.3f}")
    head.eval()
    return head


def evaluate(
    head: AdvisorHead,
    feats: np.ndarray,
    teacher_acts: np.ndarray,
    student_acts: np.ndarray,
) -> dict:
    """Argmax accuracy, top-3 accuracy, accuracy on disagreement subset,
    per-action accuracy."""
    with torch.no_grad():
        logits = head(torch.from_numpy(feats))
    pred = logits.argmax(-1).numpy()

    correct = pred == teacher_acts
    overall_acc = float(correct.mean())

    top3 = torch.topk(logits, k=3, dim=-1).indices.numpy()
    in_top3 = (top3 == teacher_acts[:, None]).any(axis=1)
    top3_acc = float(in_top3.mean())

    # Disagreement subset: frames where student would have done something
    # other than what teacher chose. This is where the advice actually
    # changes the agent's behavior.
    disagree = student_acts != teacher_acts
    disagree_count = int(disagree.sum())
    if disagree_count > 0:
        disagree_acc = float(correct[disagree].mean())
    else:
        disagree_acc = float("nan")

    # Per-action accuracy.
    per_action_acc = {}
    per_action_n = {}
    for a in range(N_ACTIONS):
        mask = teacher_acts == a
        n = int(mask.sum())
        per_action_n[a] = n
        per_action_acc[a] = float(correct[mask].mean()) if n > 0 else float("nan")

    return {
        "overall_acc": overall_acc,
        "top3_acc": top3_acc,
        "disagree_acc": disagree_acc,
        "disagree_count": disagree_count,
        "n": len(feats),
        "per_action_acc": per_action_acc,
        "per_action_n": per_action_n,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--student", type=Path, required=True)
    p.add_argument("--teacher", type=Path, required=True)
    p.add_argument("--rollouts", type=int, default=20_000)
    p.add_argument("--epochs", type=int, default=10)
    p.add_argument("--batch-size", type=int, default=256)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--holdout-frac", type=float, default=0.2)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out-dir", type=Path,
                   default=Path(__file__).resolve().parents[1] / "checkpoints")
    args = p.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(2)

    print(f"[advisor] loading student={args.student.name} teacher={args.teacher.name}")
    student = PPO.load(str(args.student), device="cpu")
    teacher = PPO.load(str(args.teacher), device="cpu")

    print(f"[advisor] collecting {args.rollouts} (feature, teacher_action) pairs")
    data = collect_dataset(student, teacher, args.rollouts, seed=args.seed)

    feats = data["features"]
    teacher_acts = data["teacher_actions"]
    student_acts = data["student_actions"]
    print(f"[advisor] feature dim: {feats.shape[1]}")
    print(f"[advisor] student-vs-teacher agreement (raw): "
          f"{(student_acts == teacher_acts).mean():.3f}")
    teacher_dist = Counter(teacher_acts.tolist())
    print(f"[advisor] teacher action distribution: "
          f"{dict(sorted(teacher_dist.items()))}")

    # Held-out split.
    n = len(feats)
    rng = np.random.default_rng(args.seed)
    idx = rng.permutation(n)
    n_eval = int(n * args.holdout_frac)
    eval_idx = idx[:n_eval]
    train_idx = idx[n_eval:]

    head = train_head(
        feats[train_idx], teacher_acts[train_idx],
        epochs=args.epochs, batch_size=args.batch_size, lr=args.lr,
    )

    metrics = evaluate(
        head,
        feats[eval_idx],
        teacher_acts[eval_idx],
        student_acts[eval_idx],
    )

    print()
    print("== held-out eval ==")
    print(f"  n samples:           {metrics['n']}")
    print(f"  overall argmax acc:  {metrics['overall_acc']:.3f}")
    print(f"  top-3 acc:           {metrics['top3_acc']:.3f}")
    print(f"  disagree subset:     {metrics['disagree_count']} samples, "
          f"acc={metrics['disagree_acc']:.3f}")
    print()
    print("  per-action accuracy (sorted by frequency):")
    sorted_actions = sorted(metrics["per_action_n"].items(),
                            key=lambda kv: -kv[1])
    for a, n in sorted_actions:
        if n == 0:
            continue
        acc = metrics["per_action_acc"][a]
        print(f"    action {a:2d}  n={n:5d}  acc={acc:.3f}")

    out_path = args.out_dir / "advisor_head.pt"
    torch.save({
        "state_dict": head.state_dict(),
        "in_dim": feats.shape[1],
        "n_actions": N_ACTIONS,
        "metrics": metrics,
    }, str(out_path))
    print(f"\n[advisor] head saved -> {out_path}")


if __name__ == "__main__":
    main()
