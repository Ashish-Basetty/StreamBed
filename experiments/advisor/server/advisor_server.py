"""Server-side advisor: receives features (EMBD) from the edge, runs
teacher.policy.mlp_extractor + action_net on them, sends advice back as
CSTR (CSTM_RELIABLE).

Phase C wiring: this binds a UDP port directly. Edge connects to it
directly for testing. Phase D will put the sidecar in the middle — the
protocol on both ends is unchanged; the sidecar just relays via QUIC
with policy-aware drop semantics.

Server inference reads ONLY the embedding from each incoming
StreamFrame. Frames (CHNK channel, lossy) flow into a separate
FrameStore for retrospective eval — not used for advice generation.
That's the bandwidth-aware split: features always arrive, frames are
nice-to-have.

Advice payload format on the wire:
    [ 8 B  timestamp f64 (matches StreamFrame.timestamp) ]
    [ 68 B 17 × float32 logits ]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import struct
import time
from pathlib import Path

import numpy as np
import torch
from stable_baselines3 import PPO

from shared.heartbeat import heartbeat_loop
from shared.interfaces.stream_interface import (
    StreamBedUDPSender,
    StreamBedUDPServerReceiver,
)

log = logging.getLogger("advisor_server")

ADVICE_HEADER_SIZE = 8           # double timestamp
ADVICE_LOGITS_BYTES = 17 * 4     # float32 logits
ADVICE_PAYLOAD_SIZE = ADVICE_HEADER_SIZE + ADVICE_LOGITS_BYTES  # 76


class Advisor:
    """Holds the loaded teacher and runs its policy MLP on incoming
    features. Stateless apart from the loaded weights and a request
    counter for telemetry."""

    def __init__(self, teacher: PPO):
        self.teacher = teacher
        self.requests = 0

    @torch.no_grad()
    def advise(self, feature: np.ndarray) -> np.ndarray:
        """feature: (D,) float32 → 17 float32 logits (np)."""
        x = torch.from_numpy(feature).float().unsqueeze(0)
        latent_pi, _ = self.teacher.policy.mlp_extractor(x)
        logits = self.teacher.policy.action_net(latent_pi).squeeze(0)
        self.requests += 1
        return logits.cpu().numpy().astype(np.float32)


def encode_advice(timestamp: float, logits: np.ndarray) -> bytes:
    """Pack a single advice message: 8 B ts + 68 B logits."""
    if logits.shape != (17,):
        raise ValueError(f"expected (17,) logits, got {logits.shape}")
    return struct.pack(">d", float(timestamp)) + logits.astype(np.float32).tobytes()


async def serve(args: argparse.Namespace, advisor: Advisor) -> None:
    heartbeat_task = asyncio.create_task(heartbeat_loop(model_version="advisor-server"))
    receiver = StreamBedUDPServerReceiver()
    await receiver.listen(args.feed_host, args.feed_port)
    log.info("listening for features on %s:%d", args.feed_host, args.feed_port)

    sender = StreamBedUDPSender()
    await sender.connect(args.advice_host, args.advice_port)
    log.info("advice will go to %s:%d (reply_every_n=%d)",
             args.advice_host, args.advice_port, args.reply_every_n)

    last_log_ts = time.monotonic()
    embd_count = 0   # rolling, for log line
    chnk_count = 0
    embd_seen_total = 0  # global, for cadence gating

    try:
        async for sf in receiver.receive_stream():
            if sf.embedding is not None:
                embd_count += 1
                embd_seen_total += 1

                # Cadence gate: with --reply-every-n=N, server replies to
                # one EMBD out of every N. N=1 means reply to every one
                # (full saturation). N=0 means never reply (advisor is a
                # sink — useful to verify "no advice" baseline still
                # exercises the wire path).
                should_reply = (
                    args.reply_every_n > 0
                    and (embd_seen_total % args.reply_every_n == 0)
                )
                if should_reply:
                    feat = np.asarray(sf.embedding, dtype=np.float32).flatten()
                    advice = advisor.advise(feat)
                    payload = encode_advice(sf.timestamp, advice)
                    asyncio.create_task(sender.send_custom(payload, reliable=True))
            elif sf.frame is not None:
                chnk_count += 1
                # No-op for now. Phase F or beyond plugs this into FrameStore.

            if time.monotonic() - last_log_ts >= 5.0:
                log.info(
                    "rolling: embd=%d chnk=%d total_advised=%d (cadence_n=%d)",
                    embd_count, chnk_count, advisor.requests, args.reply_every_n,
                )
                embd_count = 0
                chnk_count = 0
                last_log_ts = time.monotonic()
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except (asyncio.CancelledError, Exception):
            pass
        await sender.close()
        await receiver.stop()


def main():
    # Defaults pull from env vars set by the StreamBed deployment daemon
    # (see control-plane/DeploymentDaemon/main.py). CLI flags still win.
    p = argparse.ArgumentParser()
    p.add_argument("--teacher", type=Path,
                   default=Path(os.environ.get("TEACHER_PATH", "")) or None,
                   required="TEACHER_PATH" not in os.environ)
    p.add_argument("--feed-host", default="0.0.0.0",
                   help="UDP host to listen on for incoming StreamFrames.")
    p.add_argument("--feed-port", type=int,
                   default=int(os.environ.get("FEED_LISTEN_PORT", "9101")),
                   help="UDP port to listen on for incoming StreamFrames.")
    p.add_argument("--advice-host",
                   default=os.environ.get("SIDECAR_HOST", "127.0.0.1"),
                   help="UDP host to send advice to (edge inference's "
                        "advice receiver, or the local sidecar in Phase D).")
    p.add_argument("--advice-port", type=int,
                   default=int(os.environ.get("SIDECAR_REVERSE_PORT", "9102")),
                   help="UDP port to send advice to.")
    p.add_argument("--reply-every-n", type=int,
                   default=int(os.environ.get("REPLY_EVERY_N", "1")),
                   help="Reply with advice on every Nth EMBD received. "
                        "N=1 (default) replies to all. N=0 disables replies "
                        "(advisor becomes a sink — wire path still exercised).")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    torch.set_num_threads(2)

    log.info("loading teacher=%s", args.teacher.name)
    teacher = PPO.load(str(args.teacher), device="cpu")
    advisor = Advisor(teacher)
    log.info("advisor ready")

    asyncio.run(serve(args, advisor))


if __name__ == "__main__":
    main()
