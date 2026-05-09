"""Record demo videos of the student+advisor at different cadences.

Runs one episode per cadence in-process (no wire), collects obs frames,
overlays a HUD, and saves one MP4 per cadence.

Usage:
  python bench/record_demos.py \
      --student checkpoints/students/shared_h16.pt \
      --teacher checkpoints/teacher.zip \
      --cadences 1 5 10 20 inf \
      --blend-mode decaying \
      --out-dir videos/
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Optional

import imageio
import numpy as np
import torch
from PIL import Image, ImageDraw, ImageFont
from stable_baselines3 import PPO

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from advisorlib.crafter_gym import CrafterGymEnv  # noqa: E402
from bench.train_shared_head import SmallHead  # noqa: E402

_FONT: ImageFont.ImageFont | None = None


def _font(size: int = 10) -> ImageFont.ImageFont:
    global _FONT
    if _FONT is None:
        try:
            _FONT = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", size)
        except Exception:
            _FONT = ImageFont.load_default()
    return _FONT


def _overlay(frame: np.ndarray, cadence: str, step: int, unlocks: int,
             advice_used: bool, advice_age_s: float, scale: int) -> np.ndarray:
    h, w = frame.shape[:2]
    img = Image.fromarray(frame).resize(
        (w * scale, h * scale), Image.NEAREST
    )
    draw = ImageDraw.Draw(img)
    font = _font(max(10, scale * 3))

    cad_str = f"cadence={cadence}"
    adv_str = f"advice: {'ON  age={:.0f}ms'.format(advice_age_s * 1000) if advice_used else 'OFF'}"
    step_str = f"step={step}  unlocks={unlocks}"

    bg = (0, 0, 0, 160)
    line_h = max(12, scale * 4)
    for i, text in enumerate([cad_str, adv_str, step_str]):
        y = 2 + i * line_h
        draw.rectangle([0, y, img.width, y + line_h], fill=bg)
        color = (0, 255, 128) if advice_used else (200, 200, 200)
        draw.text((3, y + 1), text, font=font, fill=color)

    return np.array(img)


def _teacher_advice(teacher: PPO, feat: torch.Tensor) -> torch.Tensor:
    with torch.no_grad():
        latent_pi, _ = teacher.policy.mlp_extractor(feat)
        return teacher.policy.action_net(latent_pi).squeeze(0)


def run_episode(
    teacher: PPO,
    student_head: SmallHead,
    cadence: float,
    blend_mode: str,
    decay_tau_s: float,
    fps: float,
    seed: int,
    scale: int,
) -> list[np.ndarray]:
    env = CrafterGymEnv(seed=seed)
    obs, _ = env.reset()

    frames: list[np.ndarray] = []
    step = 0
    ep_unlocks: set[str] = set()
    latest_advice: Optional[torch.Tensor] = None
    advice_ts: float = -math.inf
    advice_used = False
    advice_age_s = 0.0
    done = False

    while not done:
        chw = np.transpose(obs, (2, 0, 1)).astype(np.float32)
        x = torch.from_numpy(chw).unsqueeze(0)
        with torch.no_grad():
            feat = teacher.policy.extract_features(x)
            student_logits = student_head(feat).squeeze(0)

        now_s = step / fps
        if cadence != math.inf and step % int(cadence) == 0:
            latest_advice = _teacher_advice(teacher, feat)
            advice_ts = now_s

        advice_age_s = now_s - advice_ts if latest_advice is not None else 0.0

        if latest_advice is None or cadence == math.inf:
            final_logits = student_logits
            advice_used = False
        elif blend_mode == "replacement":
            final_logits = latest_advice
            advice_used = True
        elif blend_mode == "additive":
            final_logits = student_logits + latest_advice
            advice_used = True
        elif blend_mode == "decaying":
            alpha = math.exp(-advice_age_s / max(decay_tau_s, 1e-6))
            final_logits = student_logits + alpha * latest_advice
            advice_used = alpha > 0.01
        else:
            raise ValueError(f"unknown blend_mode: {blend_mode}")

        a = int(final_logits.argmax(-1).item())
        obs_next, _, term, trunc, info = env.step(a)
        for k, v in info.get("achievements", {}).items():
            if v:
                ep_unlocks.add(k)

        cad_label = "inf" if cadence == math.inf else str(int(cadence))
        rendered = _overlay(obs, cad_label, step, len(ep_unlocks),
                            advice_used, advice_age_s, scale)
        frames.append(rendered)

        obs = obs_next
        step += 1
        done = term or trunc

    return frames


def save_video(frames: list[np.ndarray], path: Path, fps: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = imageio.get_writer(str(path), fps=fps, codec="libx264",
                                pixelformat="yuv420p", quality=8)
    for f in frames:
        writer.append_data(f)
    writer.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--student", type=Path, required=True)
    p.add_argument("--teacher", type=Path, required=True)
    p.add_argument("--cadences", nargs="+", default=["1", "5", "10", "20", "inf"])
    p.add_argument("--blend-mode",
                   choices=["replacement", "additive", "decaying"],
                   default="decaying")
    p.add_argument("--decay-tau-s", type=float, default=0.05)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--fps", type=float, default=20.0,
                   help="Playback FPS of output video (also used for advice-age HUD).")
    p.add_argument("--scale", type=int, default=4,
                   help="Pixel upscale factor (64→256 at scale=4).")
    p.add_argument("--out-dir", type=Path,
                   default=Path(__file__).resolve().parents[1] / "videos")
    args = p.parse_args()

    torch.set_num_threads(2)
    print(f"[record_demos] loading teacher={args.teacher.name}")
    teacher = PPO.load(str(args.teacher), device="cpu")

    ckpt = torch.load(str(args.student), map_location="cpu", weights_only=False)
    student_head = SmallHead(in_dim=ckpt["in_dim"], hidden=ckpt["hidden"],
                             n_actions=ckpt["n_actions"])
    student_head.load_state_dict(ckpt["state_dict"])
    student_head.eval()
    print(f"[record_demos] loaded student hidden={ckpt['hidden']}")

    cadences = [
        math.inf if c.lower() in {"inf", "infinity", "none"} else int(c)
        for c in args.cadences
    ]

    for cad in cadences:
        label = "inf" if cad == math.inf else str(int(cad))
        print(f"\n[record_demos] recording cadence={label} ...", flush=True)
        frames = run_episode(
            teacher, student_head, cad, args.blend_mode,
            args.decay_tau_s, args.fps, args.seed, args.scale,
        )
        out_path = args.out_dir / f"demo_cadence_{label}.mp4"
        save_video(frames, out_path, args.fps)
        print(f"  {len(frames)} frames → {out_path}")

    print(f"\n[record_demos] done. videos in {args.out_dir}/")


if __name__ == "__main__":
    main()
