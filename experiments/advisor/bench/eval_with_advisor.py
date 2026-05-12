"""Simulated cadence sweep: shared-encoder student + teacher advisor, in-process.

This bypasses the docker/sidecar pipeline and tests the *policy* question
directly: given a small student head and teacher's full head as the
advisor, how does Crafter score depend on advisor cadence?

Both heads operate on the same teacher.encoder(frame) feature, so the
"wire" is implicit — no actual networking. This validates the
algorithmic assumption (advisor closes the gap) before we commit to
the docker plumbing. Once the wiring is built, we re-run with the
real sidecar and verify the score curve matches.

Cadence semantics:
  - At step t, the "server" recomputes advice every `cadence` steps.
  - The edge always applies the *most recent* advice it has, with
    staleness up to `cadence-1` steps. (Realistic — a real edge
    wouldn't sit blocking on the server.)
  - cadence=1 means advice is fresh every step → ≈ teacher behavior.
  - cadence=inf means no advice ever → pure student baseline.

Blending modes:
  - replacement (default): use advice as the action distribution when
    available. Cleanest; exposes pure cadence effect.
  - additive: final_logits = student + alpha * advice. Tests soft
    blending as an ablation.

Usage:
  python -m experiments.advisor.bench.eval_with_advisor \\
      --student experiments/advisor/checkpoints/students/shared_h4.pt \\
      --teacher experiments/advisor/checkpoints/teacher.zip \\
      --episodes 30 \\
      --cadences 1 5 10 20 inf \\
      --mode replacement
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np
import torch
from stable_baselines3 import PPO

from experiments.advisor.advisorlib.crafter_gym import CrafterGymEnv
from experiments.advisor.bench.train_ppo import CRAFTER_ACHIEVEMENTS
from experiments.advisor.bench.train_shared_head import SmallHead


def _parse_cadence(s: str) -> float:
    return math.inf if s.lower() in {"inf", "infinity", "none"} else float(int(s))


def load_student_head(path: Path) -> tuple[SmallHead, dict]:
    ckpt = torch.load(str(path), map_location="cpu", weights_only=False)
    head = SmallHead(in_dim=ckpt["in_dim"], hidden=ckpt["hidden"],
                     n_actions=ckpt["n_actions"])
    head.load_state_dict(ckpt["state_dict"])
    head.eval()
    return head, ckpt


def teacher_advice_logits(teacher: PPO, feat: torch.Tensor) -> torch.Tensor:
    """Run teacher's policy MLP + action_net on a feature vector. This is
    what the server-side advisor would compute — purely on top of the
    same shared encoder output."""
    with torch.no_grad():
        latent_pi, _ = teacher.policy.mlp_extractor(feat)
        return teacher.policy.action_net(latent_pi).squeeze(0)


def run_episode(
    teacher: PPO,
    student_head: SmallHead,
    cadence: float,
    mode: str,
    alpha: float,
    seed: int,
) -> tuple[set[str], float, dict]:
    """One Crafter episode. Returns (unlocks, return, telemetry)."""
    env = CrafterGymEnv(seed=seed)
    obs, _ = env.reset()
    ep_unlocks: set[str] = set()
    ep_ret = 0.0
    step = 0
    advice_steps = 0
    advice_recomputes = 0
    latest_advice: torch.Tensor | None = None
    advice_age = math.inf

    done = False
    while not done:
        chw = np.transpose(obs, (2, 0, 1)).astype(np.float32)
        x = torch.from_numpy(chw).unsqueeze(0)
        with torch.no_grad():
            feat = teacher.policy.extract_features(x)
            student_logits = student_head(feat).squeeze(0)

        # Server recomputes advice every `cadence` steps.
        if cadence != math.inf and step % int(cadence) == 0:
            latest_advice = teacher_advice_logits(teacher, feat)
            advice_age = 0
            advice_recomputes += 1
        else:
            if latest_advice is not None:
                advice_age += 1

        if latest_advice is None:
            final_logits = student_logits
            used_advice = False
        elif mode == "replacement":
            final_logits = latest_advice
            used_advice = True
        elif mode == "additive":
            final_logits = student_logits + alpha * latest_advice
            used_advice = True
        else:
            raise ValueError(f"unknown blending mode: {mode}")

        a = int(final_logits.argmax(-1).item())
        if used_advice:
            advice_steps += 1

        obs, r, term, trunc, info = env.step(a)
        ep_ret += float(r)
        for k, v in info.get("achievements", {}).items():
            if v:
                ep_unlocks.add(k)
        step += 1
        done = term or trunc

    telemetry = {
        "steps": step,
        "advice_steps": advice_steps,
        "advice_recomputes": advice_recomputes,
    }
    return ep_unlocks, ep_ret, telemetry


def evaluate_at_cadence(
    teacher: PPO,
    student_head: SmallHead,
    cadence: float,
    mode: str,
    alpha: float,
    n_episodes: int,
    seed: int,
) -> dict:
    unlocks_all: list[set[str]] = []
    returns_all: list[float] = []
    tel_acc = {"steps": 0, "advice_steps": 0, "advice_recomputes": 0}
    t0 = time.time()
    for ep in range(n_episodes):
        unlocks, ep_ret, tel = run_episode(
            teacher, student_head, cadence, mode, alpha, seed=seed + ep
        )
        unlocks_all.append(unlocks)
        returns_all.append(ep_ret)
        for k, v in tel.items():
            tel_acc[k] += v
    rates = np.array([
        sum(1 for s in unlocks_all if a in s) / max(len(unlocks_all), 1)
        for a in CRAFTER_ACHIEVEMENTS
    ])
    score = float(np.exp(np.mean(np.log(1 + 100 * rates))) - 1)
    elapsed = time.time() - t0
    return {
        "cadence": "inf" if cadence == math.inf else int(cadence),
        "episodes": n_episodes,
        "crafter_score": score,
        "avg_unlocks_per_ep": float(np.mean([len(s) for s in unlocks_all])),
        "avg_return": float(np.mean(returns_all)),
        "advice_apply_pct": tel_acc["advice_steps"] / max(tel_acc["steps"], 1),
        "advice_recomputes": tel_acc["advice_recomputes"],
        "wallclock_s": elapsed,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--student", type=Path, required=True,
                   help="shared_h{N}.pt checkpoint.")
    p.add_argument("--teacher", type=Path, required=True)
    p.add_argument("--episodes", type=int, default=30)
    p.add_argument("--cadences", nargs="+", default=["1", "5", "10", "20", "inf"],
                   help="Cadences to sweep. 'inf' = no advice (student baseline).")
    p.add_argument("--mode", choices=["replacement", "additive"],
                   default="replacement")
    p.add_argument("--alpha", type=float, default=1.0,
                   help="Blending alpha (additive mode only).")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out-dir", type=Path,
                   default=Path(__file__).resolve().parents[1] / "checkpoints" / "eval")
    args = p.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(2)

    print(f"[advisor-eval] loading teacher={args.teacher.name}")
    teacher = PPO.load(str(args.teacher), device="cpu")
    print(f"[advisor-eval] loading student head={args.student.name}")
    student_head, ckpt_meta = load_student_head(args.student)

    print(f"[advisor-eval] head hidden={ckpt_meta['hidden']} "
          f"in_dim={ckpt_meta['in_dim']} mode={args.mode} "
          f"alpha={args.alpha if args.mode == 'additive' else 'n/a'}")
    cadences = [_parse_cadence(c) for c in args.cadences]

    results = []
    for c in cadences:
        label = "inf" if c == math.inf else int(c)
        print(f"\n[advisor-eval] cadence={label}, {args.episodes} eps")
        r = evaluate_at_cadence(
            teacher, student_head, c, args.mode, args.alpha,
            args.episodes, args.seed,
        )
        print(f"  score={r['crafter_score']:.2f}  "
              f"unlocks/ep={r['avg_unlocks_per_ep']:.2f}  "
              f"advice%={r['advice_apply_pct']*100:.1f}  "
              f"({r['wallclock_s']:.1f}s)")
        results.append(r)

    # Pretty summary table.
    print()
    print("== cadence sweep ==")
    print(f"  student:     {args.student.name} (hidden={ckpt_meta['hidden']})")
    print(f"  mode:        {args.mode}"
          + (f"  alpha={args.alpha}" if args.mode == "additive" else ""))
    print(f"  episodes:    {args.episodes}")
    print()
    print(f"  {'cadence':>8s}  {'score':>6s}  {'unlocks/ep':>10s}  {'advice%':>8s}")
    for r in results:
        print(f"  {str(r['cadence']):>8s}  {r['crafter_score']:6.2f}  "
              f"{r['avg_unlocks_per_ep']:10.2f}  "
              f"{r['advice_apply_pct']*100:7.1f}%")

    # JSON dump.
    out = {
        "config": {
            "student": args.student.name,
            "teacher": args.teacher.name,
            "hidden": ckpt_meta["hidden"],
            "in_dim": ckpt_meta["in_dim"],
            "mode": args.mode,
            "alpha": args.alpha if args.mode == "additive" else None,
            "episodes": args.episodes,
            "seed": args.seed,
        },
        "results": results,
    }
    summary_path = (args.out_dir /
                    f"cadence_sweep_shared_h{ckpt_meta['hidden']}_"
                    f"{args.mode}_{args.episodes}ep.json")
    summary_path.write_text(json.dumps(out, indent=2))
    print(f"\n[advisor-eval] saved -> {summary_path}")


if __name__ == "__main__":
    main()
