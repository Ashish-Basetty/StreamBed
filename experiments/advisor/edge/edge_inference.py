"""Edge inference container.

Two concurrent input/output paths:

  1. Host ⇄ edge (TCP, this listens):
       - reads CHNK-tagged JSON frame messages from the host process
       - returns ACTN-tagged JSON action messages

  2. Edge ⇄ advisor (UDP via StreamBedUDPDuplex):
       - one connected datagram socket to the co-located sidecar (send frames,
         receive CSTR advice on the same FD)
       - background task drains `recv_custom()` for CSTR advice messages,
         updates a latest-advice slot
       - predict path reads the slot non-blocking, blends advice into
         student logits, reports advice_used / advice_age telemetry to host

If --advisor-host=none, the UDP path is skipped entirely and the edge
plays solo (Phase B baseline mode).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import math
import os
import struct
import time
from pathlib import Path

import numpy as np
import torch
from stable_baselines3 import PPO

from experiments.advisor.bench.train_shared_head import SmallHead

# StreamBed shared interfaces — sender/receiver implementations we reuse
# unchanged (no new code in StreamBed's protocol layer).
from shared.interfaces.stream_interface import (
    StreamBedUDPDuplex,
    StreamFrame,
)
from shared.tcp_framing import (
    ACTN_MAGIC,
    CHUNK_MAGIC,
    read_message,
    write_message,
)

log = logging.getLogger("edge_inference")

# Advice payload schema produced by advisor_server.py:
#   [ 8 B  timestamp f64 ]
#   [ 68 B 17 × float32 logits ]
ADVICE_HEADER_SIZE = 8
ADVICE_LOGITS_BYTES = 17 * 4
ADVICE_PAYLOAD_SIZE = ADVICE_HEADER_SIZE + ADVICE_LOGITS_BYTES


class EdgeInference:
    """Holds the loaded teacher + small student head, runs forward per
    incoming frame, and maintains the latest-advice slot updated by the
    background advice consumer task.

    Predict returns enough telemetry that the host can record advice age
    and usage rate per episode.
    """

    def __init__(
        self,
        teacher: PPO,
        head: SmallHead,
        head_meta: dict,
        blend_mode: str,
        alpha: float,
        max_advice_age_s: float,
        decay_tau_s: float,
        source_device_id: str,
    ):
        self.teacher = teacher
        self.head = head
        self.head_meta = head_meta
        self.frames_processed = 0

        # Blending config.
        self.blend_mode = blend_mode
        self.alpha = alpha
        self.max_advice_age_s = max_advice_age_s
        self.decay_tau_s = decay_tau_s
        self.source_device_id = source_device_id

        # Advice slot — written by advice_consumer, read by predict.
        # No lock needed: assignment of a tuple of immutable refs is
        # atomic in CPython, and "stale by one read" is fine.
        self._latest_advice: np.ndarray | None = None
        self._latest_advice_local_t: float = 0.0
        self.advice_recv_count = 0

    def set_advice(self, ts: float, logits: np.ndarray) -> None:
        self._latest_advice = logits
        self._latest_advice_local_t = time.monotonic()
        self.advice_recv_count += 1

    @torch.no_grad()
    def predict(
        self, frame_hwc_uint8: np.ndarray
    ) -> tuple[int, np.ndarray, bool, float]:
        """frame -> (action, feature, advice_used, advice_age_s).

        feature is the teacher.encoder output (what gets shipped on EMBD).
        The action is sampled from final_logits, where final_logits depends
        on blend_mode:
          - off:         student logits, ignore advice
          - replacement: advice logits (when fresh), else student
          - additive:    student + alpha * advice (when fresh), else student
          - decaying:    convex combination weighted by advice age:
                           w = exp(-age / decay_tau_s)
                           final = w * advice + (1 - w) * student
                         At age=0: behaves like replacement.
                         At age→∞: smoothly falls back to student.
                         Closes the catastrophic-cliff failure mode of
                         replacement at high cadence / RTT.
        """
        chw = np.transpose(frame_hwc_uint8, (2, 0, 1)).astype(np.float32)
        x = torch.from_numpy(chw).unsqueeze(0)
        raw_feat = self.teacher.policy.extract_features(x)
        if isinstance(raw_feat, tuple):
            feat = raw_feat[0]
        else:
            feat = raw_feat
        if feat.dim() > 2:
            feat = feat.reshape(feat.size(0), -1)
        student_logits_t = self.head(feat).squeeze(0)
        student_logits = student_logits_t.cpu().numpy()

        # Decide whether advice is fresh enough to apply.
        advice_used = False
        advice_age_s = -1.0
        if (self._latest_advice is not None
                and self.blend_mode != "off"):
            age = time.monotonic() - self._latest_advice_local_t
            if age <= self.max_advice_age_s:
                advice_age_s = age
                if self.blend_mode == "replacement":
                    final_logits = self._latest_advice
                elif self.blend_mode == "additive":
                    final_logits = student_logits + self.alpha * self._latest_advice
                elif self.blend_mode == "decaying":
                    # Logit-level convex combination weighted by staleness.
                    # exp(-age/τ): full advice at age=0, fades to student
                    # as age grows. τ is hand-tuned to ~the cadence cliff
                    # we measured (50 ms ≈ "useful for first 1-2 cadence
                    # periods at sim rate").
                    w = math.exp(-age / self.decay_tau_s) if self.decay_tau_s > 0 else 0.0
                    final_logits = w * self._latest_advice + (1.0 - w) * student_logits
                else:
                    final_logits = student_logits
                advice_used = True
            else:
                final_logits = student_logits
        else:
            final_logits = student_logits

        action = int(np.argmax(final_logits))
        feature_np = feat.squeeze(0).detach().cpu().numpy()
        self.frames_processed += 1
        return action, feature_np, advice_used, advice_age_s


def decode_advice(payload: bytes) -> tuple[float, np.ndarray]:
    if len(payload) != ADVICE_PAYLOAD_SIZE:
        raise ValueError(
            f"unexpected advice payload size: {len(payload)} (want {ADVICE_PAYLOAD_SIZE})"
        )
    ts = struct.unpack(">d", payload[:ADVICE_HEADER_SIZE])[0]
    logits = np.frombuffer(payload[ADVICE_HEADER_SIZE:], dtype=np.float32).copy()
    return ts, logits


async def advice_consumer(
    duplex: StreamBedUDPDuplex, inference: EdgeInference
) -> None:
    """Background task: drain CSTR advice messages from the receiver and
    update the inference's latest-advice slot. Runs forever; cancelled
    on shutdown."""
    log.info("advice_consumer started")
    while True:
        try:
            result = await duplex.recv_custom(timeout=1.0)
        except asyncio.CancelledError:
            log.info("advice_consumer cancelled")
            return
        except Exception as e:
            log.warning("advice_consumer error: %s", e)
            await asyncio.sleep(0.1)
            continue
        if result is None:
            continue
        tag, payload = result
        try:
            ts, logits = decode_advice(payload)
        except Exception as e:
            log.warning("decode_advice failed: %s", e)
            continue
        inference.set_advice(ts, logits)
        if inference.advice_recv_count % 200 == 1:
            log.info("advice received: count=%d", inference.advice_recv_count)


def decode_frame_msg(payload: bytes) -> tuple[int, float, np.ndarray]:
    msg = json.loads(payload)
    frame_idx = int(msg["frame_idx"])
    timestamp = float(msg["timestamp"])
    raw = base64.b64decode(msg["frame_b64"])
    frame = np.frombuffer(raw, dtype=np.uint8).reshape(64, 64, 3)
    return frame_idx, timestamp, frame


def encode_action_msg(
    frame_idx: int, action: int, advice_used: bool, advice_age_s: float
) -> bytes:
    return json.dumps({
        "frame_idx": frame_idx,
        "action": action,
        "advice_used": advice_used,
        "advice_age_s": advice_age_s,
    }).encode("utf-8")


async def handle_connection(
    inference: EdgeInference,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    duplex: StreamBedUDPDuplex | None = None,
) -> None:
    peer = writer.get_extra_info("peername")
    log.info("connection from %s", peer)
    try:
        while True:
            try:
                tag, payload = await read_message(reader)
            except asyncio.IncompleteReadError:
                log.info("connection closed by %s", peer)
                return
            except Exception as e:
                log.warning("read_message error from %s: %s", peer, e)
                return

            if tag != CHUNK_MAGIC:
                log.warning("unexpected tag %r from %s; ignoring", tag, peer)
                continue

            try:
                frame_idx, ts, frame = decode_frame_msg(payload)
            except Exception as e:
                log.warning("decode_frame_msg failed: %s", e)
                continue

            t0 = time.monotonic()
            action, feature, advice_used, advice_age_s = inference.predict(frame)
            inf_ms = (time.monotonic() - t0) * 1000.0

            if inference.frames_processed % 200 == 0:
                log.info(
                    "frames_processed=%d last_inference_ms=%.2f "
                    "advice_recv=%d",
                    inference.frames_processed, inf_ms,
                    inference.advice_recv_count,
                )

            # Send ACTN to the host before UDP upstream work. Scheduling
            # `duplex.send` first let the UDP coroutine run as soon as
            # `write_message` yields on `drain()`, interleaving heavy
            # serialization/chunking with the TCP reply and risking stalls or
            # failures visible to the host as an empty read.
            await write_message(
                writer, ACTN_MAGIC,
                encode_action_msg(frame_idx, action, advice_used, advice_age_s),
            )

            if duplex is not None:
                sf = StreamFrame(
                    timestamp=ts,
                    frame=frame,
                    embedding=feature,
                    model_version=f"shared-h{inference.head_meta['hidden']}-v1",
                    source_device_id=inference.source_device_id,
                )

                async def _udp_upstream(frame_to_send: StreamFrame = sf) -> None:
                    try:
                        await duplex.send(frame_to_send)
                    except Exception:
                        log.exception(
                            "duplex.send failed after host ACTN (peer=%s)", peer
                        )

                asyncio.create_task(_udp_upstream())
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


def load_inference(args: argparse.Namespace) -> EdgeInference:
    log.info("loading teacher=%s", args.teacher.name)
    teacher = PPO.load(str(args.teacher), device="cpu")
    log.info("loading student=%s", args.student.name)
    ckpt = torch.load(str(args.student), map_location="cpu", weights_only=False)
    head = SmallHead(in_dim=ckpt["in_dim"], hidden=ckpt["hidden"],
                     n_actions=ckpt["n_actions"])
    head.load_state_dict(ckpt["state_dict"])
    head.eval()
    log.info("ready: hidden=%d in_dim=%d blend_mode=%s",
             ckpt["hidden"], ckpt["in_dim"], args.blend_mode)
    return EdgeInference(
        teacher=teacher,
        head=head,
        head_meta=ckpt,
        blend_mode=args.blend_mode,
        alpha=args.alpha,
        max_advice_age_s=args.max_advice_age_s,
        decay_tau_s=args.decay_tau_s,
        source_device_id=args.source_device_id,
    )


async def serve(args: argparse.Namespace, inference: EdgeInference) -> None:
    duplex: StreamBedUDPDuplex | None = None
    advice_task: asyncio.Task | None = None

    advisor_disabled = (args.advisor_host or "").lower() == "none"
    if advisor_disabled:
        log.info("advisor disabled (--advisor-host=none); running solo")
    else:
        duplex = StreamBedUDPDuplex()
        await duplex.connect(args.advisor_host, args.advisor_port)
        log.info(
            "StreamBedUDPDuplex ↔ %s:%d (features out, CSTR advice in)",
            args.advisor_host,
            args.advisor_port,
        )
        advice_task = asyncio.create_task(advice_consumer(duplex, inference))

    try:
        server = await asyncio.start_server(
            lambda r, w: handle_connection(inference, r, w, duplex),
            args.host, args.port,
        )
        bound = ", ".join(str(s.getsockname()) for s in server.sockets or [])
        log.info("edge_inference TCP listening on %s", bound)
        async with server:
            await server.serve_forever()
    finally:
        if advice_task is not None:
            advice_task.cancel()
            try:
                await advice_task
            except (asyncio.CancelledError, Exception):
                pass
        if duplex is not None:
            await duplex.close()


def main():
    # Defaults can come from env vars set by the StreamBed deployment daemon
    # (see control-plane/DeploymentDaemon/main.py). CLI flags still win when
    # given — useful for local Phase C runs without the docker stack.
    p = argparse.ArgumentParser()
    p.add_argument("--teacher", type=Path,
                   default=Path(os.environ.get("TEACHER_PATH", "")) or None,
                   required="TEACHER_PATH" not in os.environ)
    p.add_argument("--student", type=Path,
                   default=Path(os.environ.get("STUDENT_PATH", "")) or None,
                   required="STUDENT_PATH" not in os.environ,
                   help="shared_h{N}.pt checkpoint.")
    # Host ⇄ edge TCP server.
    p.add_argument("--host", default=os.environ.get("EDGE_TCP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("EDGE_TCP_PORT", "9100")))
    # Edge ⇄ advisor UDP. --advisor-host=none disables.
    p.add_argument("--advisor-host",
                   default=os.environ.get("SIDECAR_HOST", "127.0.0.1"))
    p.add_argument("--advisor-port", type=int,
                   default=int(
                       os.environ.get(
                           "SIDECAR_UDP_PORT",
                           os.environ.get("SIDECAR_FEED_PORT", "9050"),
                       )
                   ))
    # Blending policy.
    p.add_argument("--blend-mode",
                   choices=["off", "replacement", "additive", "decaying"],
                   default="replacement")
    p.add_argument("--alpha", type=float, default=1.0,
                   help="Additive-mode weight: final = student + α·advice.")
    p.add_argument("--decay-tau-s", type=float, default=0.05,
                   help="Decaying-mode time constant: w = exp(-age/τ). "
                        "Default 50 ms tunes weight to fall to ~0.32 at "
                        "the empirical cadence-20 staleness (~57 ms).")
    p.add_argument("--max-advice-age-s", type=float, default=2.0,
                   help="Discard cached advice older than this (seconds). "
                        "Hard cap on top of soft decay.")
    p.add_argument("--source-device-id", default="advisor-edge-001")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    torch.set_num_threads(2)

    inference = load_inference(args)
    asyncio.run(serve(args, inference))


if __name__ == "__main__":
    main()
