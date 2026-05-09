"""Local frame source: drive Crafter, talk to the edge inference container
over TCP, log Crafter score.

This is the "host process" in the advisor architecture — the thing
that would be a robot, a phone, or a user's machine in a real
deployment. It doesn't import StreamBed sidecar code; it talks to
the edge container over a TCP socket using StreamBed-style framing
([4-byte length][4-byte tag][JSON payload]) so any sidecar-aware tool
could sniff or fake either side.

Phase B usage (no advisor yet):
  python host/frame_gen.py --episodes 30
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from advisorlib.crafter_gym import CrafterGymEnv  # noqa: E402
from advisorlib.tcp_framing import (  # noqa: E402
    ACTN_MAGIC, CHUNK_MAGIC, read_message, write_message,
)
from bench.train_ppo import CRAFTER_ACHIEVEMENTS  # noqa: E402

log = logging.getLogger("frame_gen")


def encode_frame_msg(frame_idx: int, frame_hwc_uint8: np.ndarray) -> bytes:
    return json.dumps({
        "frame_idx": frame_idx,
        "timestamp": time.time(),
        "frame_b64": base64.b64encode(frame_hwc_uint8.tobytes()).decode("ascii"),
    }).encode("utf-8")


async def play_episode(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    env: CrafterGymEnv,
    seed: int,
    starting_idx: int,
) -> tuple[set[str], float, int, dict]:
    """Run one Crafter episode, ferrying frames/actions over TCP.
    Returns (unlocks, return, last_frame_idx, telemetry)."""
    obs, _ = env.reset(seed=seed)
    ep_unlocks: set[str] = set()
    ep_ret = 0.0
    advice_used_count = 0
    advice_age_sum = 0.0
    advice_age_count = 0
    rt_ms_sum = 0.0
    rt_ms_count = 0
    frame_idx = starting_idx
    done = False

    while not done:
        msg = encode_frame_msg(frame_idx, obs)
        t0 = time.monotonic()
        await write_message(writer, CHUNK_MAGIC, msg)

        tag, payload = await read_message(reader)
        rt_ms = (time.monotonic() - t0) * 1000.0
        rt_ms_sum += rt_ms
        rt_ms_count += 1

        if tag != ACTN_MAGIC:
            raise RuntimeError(f"unexpected response tag: {tag!r}")
        resp = json.loads(payload)
        if int(resp["frame_idx"]) != frame_idx:
            log.warning("frame_idx skew: sent %d, got %d",
                        frame_idx, int(resp["frame_idx"]))
        action = int(resp["action"])
        if resp.get("advice_used"):
            advice_used_count += 1
            age = float(resp.get("advice_age_s", -1.0))
            if age >= 0:
                advice_age_sum += age
                advice_age_count += 1

        obs, r, term, trunc, info = env.step(action)
        ep_ret += float(r)
        for k, v in info.get("achievements", {}).items():
            if v:
                ep_unlocks.add(k)
        frame_idx += 1
        done = term or trunc

    telemetry = {
        "steps": rt_ms_count,
        "advice_used_steps": advice_used_count,
        "advice_age_sum": advice_age_sum,
        "advice_age_count": advice_age_count,
        "avg_rt_ms": rt_ms_sum / max(rt_ms_count, 1),
    }
    return ep_unlocks, ep_ret, frame_idx, telemetry


def crafter_score(unlocks_per_ep: list[set[str]]) -> tuple[float, float]:
    n = len(unlocks_per_ep)
    rates = np.array([
        sum(1 for s in unlocks_per_ep if a in s) / max(n, 1)
        for a in CRAFTER_ACHIEVEMENTS
    ])
    score = float(np.exp(np.mean(np.log(1 + 100 * rates))) - 1)
    avg_unlocks = float(np.mean([len(s) for s in unlocks_per_ep])) if n else 0.0
    return score, avg_unlocks


async def main_async(args: argparse.Namespace) -> dict:
    log.info("connecting to edge at %s:%d", args.edge_host, args.edge_port)
    reader, writer = await asyncio.open_connection(args.edge_host, args.edge_port)
    log.info("connected; running %d episodes", args.episodes)

    env = CrafterGymEnv(seed=args.seed)
    unlocks_all: list[set[str]] = []
    returns_all: list[float] = []
    advice_pct_all: list[float] = []
    advice_age_means: list[float] = []
    rt_ms_all: list[float] = []
    frame_idx = 0
    t0 = time.time()

    try:
        for ep in range(args.episodes):
            unlocks, ep_ret, frame_idx, tel = await play_episode(
                reader, writer, env, seed=args.seed + ep, starting_idx=frame_idx,
            )
            unlocks_all.append(unlocks)
            returns_all.append(ep_ret)
            advice_pct_all.append(tel["advice_used_steps"] / max(tel["steps"], 1))
            if tel["advice_age_count"] > 0:
                advice_age_means.append(
                    tel["advice_age_sum"] / tel["advice_age_count"])
            rt_ms_all.append(tel["avg_rt_ms"])
            if (ep + 1) % 5 == 0 or ep == args.episodes - 1:
                log.info(
                    "episode %d/%d: unlocks=%d return=%.2f "
                    "rt_ms=%.2f advice_pct=%.1f%%",
                    ep + 1, args.episodes, len(unlocks), ep_ret,
                    rt_ms_all[-1], advice_pct_all[-1] * 100.0,
                )
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    elapsed = time.time() - t0
    score, avg_unlocks = crafter_score(unlocks_all)
    avg_return = float(np.mean(returns_all)) if returns_all else 0.0
    avg_rt = float(np.mean(rt_ms_all)) if rt_ms_all else 0.0
    avg_advice_pct = float(np.mean(advice_pct_all)) if advice_pct_all else 0.0

    avg_advice_age_s = (
        float(np.mean(advice_age_means)) if advice_age_means else -1.0
    )
    summary = {
        "episodes": args.episodes,
        "crafter_score": score,
        "avg_unlocks_per_ep": avg_unlocks,
        "avg_return": avg_return,
        "avg_rt_ms": avg_rt,
        "avg_advice_pct": avg_advice_pct,
        "avg_advice_age_s": avg_advice_age_s,
        "wallclock_s": elapsed,
        "edge_host": args.edge_host,
        "edge_port": args.edge_port,
    }
    return summary


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--episodes", type=int, default=30)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--edge-host", default="127.0.0.1")
    p.add_argument("--edge-port", type=int, default=9100)
    p.add_argument("--out", type=Path, default=None,
                   help="Where to dump the JSON summary. Default: print only.")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    summary = asyncio.run(main_async(args))
    print()
    print("== frame_gen summary ==")
    for k, v in summary.items():
        if isinstance(v, float):
            print(f"  {k:22s} {v:.3f}")
        else:
            print(f"  {k:22s} {v}")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(summary, indent=2))
        print(f"\nsaved -> {args.out}")


if __name__ == "__main__":
    main()
