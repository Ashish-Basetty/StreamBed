import asyncio
import os
import subprocess
import sys
import time

import cv2
import numpy as np
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from shared.inference.mobilenet import MobileNetV2Model
from shared.interfaces.stream_interface import StreamBedUDPSender, StreamFrame

COMPOSE_FILE = os.path.join(os.path.dirname(__file__), "docker-compose.yml")
VIDEO_PATH = os.path.join(os.path.dirname(__file__), "test_video.mp4")
SERVER_URL = "http://localhost:8001"
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 9001
N_FRAMES = 50

CONDITIONS = [
    {"label": "clean",      "DELAY_MS": "0",  "LOSS_PCT": "0"},
    {"label": "50ms_delay", "DELAY_MS": "50", "LOSS_PCT": "0"},
    {"label": "10pct_loss", "DELAY_MS": "0",  "LOSS_PCT": "10"},
]

MODES = ["embeddings", "raw_frames"]


def generate_video():
    if os.path.exists(VIDEO_PATH):
        return
    out = cv2.VideoWriter(VIDEO_PATH, cv2.VideoWriter_fourcc(*"mp4v"), 30, (320, 240))
    for i in range(150):
        frame = np.full((240, 320, 3), (i * 3) % 256, dtype=np.uint8)
        out.write(frame)
    out.release()


def precompute(n):
    print("loading model and precomputing embeddings...")
    model = MobileNetV2Model(device="cpu")
    model.load()
    cap = cv2.VideoCapture(VIDEO_PATH)
    data = []
    while len(data) < n:
        ret, frame = cap.read()
        if not ret:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            continue
        result = model.process_frame(frame)
        data.append((frame.copy(), result.embedding.copy()))
    cap.release()
    print(f"precomputed {len(data)} frames")
    return data


def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, **kwargs)


def compose_down():
    run(["docker", "compose", "-f", COMPOSE_FILE, "down", "--remove-orphans"])


def compose_up(env):
    e = os.environ.copy()
    e.update(env)
    subprocess.Popen(
        ["docker", "compose", "-f", COMPOSE_FILE, "up", "-d"],
        env=e,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def restart_proxy(env):
    e = os.environ.copy()
    e.update(env)
    run(
        ["docker", "compose", "-f", COMPOSE_FILE, "up", "-d", "--force-recreate", "proxy"],
        env=e,
    )


def get_frame_count():
    try:
        return requests.get(f"{SERVER_URL}/api/v1/health", timeout=2).json()["stored_frames"]
    except Exception:
        return 0


def wait_healthy(timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{SERVER_URL}/api/v1/health", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


async def _send(data, mode):
    sender = StreamBedUDPSender()
    await sender.connect(PROXY_HOST, PROXY_PORT)
    await asyncio.sleep(0.1)
    for frame, embedding in data:
        sf = StreamFrame(
            timestamp=time.time(),
            frame=frame if mode == "raw_frames" else None,
            embedding=embedding if mode == "embeddings" else None,
            model_version="MobileNetV2-v1.0",
            source_device_id="benchmark-sender",
            frame_interleaving_rate=30.0,
        )
        await sender.send(sf)
        await asyncio.sleep(0)
    await sender.close()


def wait_stable(timeout=90):
    deadline = time.time() + timeout
    prev = get_frame_count()
    stable = 0
    while time.time() < deadline:
        time.sleep(1)
        curr = get_frame_count()
        if curr == prev:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
            prev = curr
    return get_frame_count()


def measure(data, mode):
    n_sent = len(data)
    count_before = get_frame_count()
    t_start = time.time()
    asyncio.run(_send(data, mode))
    t_sent = time.time()
    count_after = wait_stable()
    t_end = time.time()
    received = count_after - count_before
    elapsed = t_end - t_start
    fps = received / elapsed if elapsed > 0 else 0
    delivery = received / n_sent if n_sent > 0 else 0
    latency_ms = (t_end - t_sent) / max(received, 1) * 1000
    return fps, delivery, latency_ms


def main():
    generate_video()
    data = precompute(N_FRAMES)

    print("building images...")
    run(["docker", "compose", "-f", COMPOSE_FILE, "build"])

    compose_down()
    compose_up({"DELAY_MS": "0", "LOSS_PCT": "0"})

    print("waiting for server...")
    if not wait_healthy():
        print("server never came up")
        compose_down()
        return

    print(f"\n{'='*67}")
    print(f"  StreamBed Throughput Benchmark  ({N_FRAMES} frames per run)")
    print(f"{'='*67}")
    print(f"  {'Mode':<14} {'Condition':<14} {'FPS':>7} {'Delivery':>10} {'Latency':>10}")
    print(f"  {'-'*59}")

    for mode in MODES:
        for cond in CONDITIONS:
            label = cond["label"]
            env = {k: v for k, v in cond.items() if k != "label"}
            restart_proxy(env)
            time.sleep(5)
            fps, delivery, latency_ms = measure(data, mode)
            print(f"  {mode:<14} {label:<14} {fps:>7.1f} {delivery:>9.1%} {latency_ms:>8.0f}ms")

    print(f"{'='*67}\n")
    compose_down()


if __name__ == "__main__":
    main()
