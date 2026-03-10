import os
import subprocess
import time

import cv2
import numpy as np
import requests

COMPOSE_FILE = os.path.join(os.path.dirname(__file__), "docker-compose.yml")
VIDEO_PATH = os.path.join(os.path.dirname(__file__), "test_video.mp4")
SERVER_URL = "http://localhost:8001"
MEASURE_SECONDS = 30

CONDITIONS = [
    {"label": "clean",       "DELAY_MS": "0",  "LOSS_PCT": "0"},
    {"label": "50ms_delay",  "DELAY_MS": "50", "LOSS_PCT": "0"},
    {"label": "10pct_loss",  "DELAY_MS": "0",  "LOSS_PCT": "10"},
]


def generate_video():
    if os.path.exists(VIDEO_PATH):
        return
    out = cv2.VideoWriter(VIDEO_PATH, cv2.VideoWriter_fourcc(*"mp4v"), 30, (320, 240))
    for i in range(150):
        frame = np.full((240, 320, 3), (i * 3) % 256, dtype=np.uint8)
        out.write(frame)
    out.release()


def compose_down():
    subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE, "down", "-v", "--remove-orphans"],
        capture_output=True,
    )


def compose_up(env):
    e = os.environ.copy()
    e.update(env)
    subprocess.Popen(
        ["docker", "compose", "-f", COMPOSE_FILE, "up", "--build"],
        env=e,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


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


def get_frame_count():
    try:
        return requests.get(f"{SERVER_URL}/api/v1/health", timeout=2).json()["stored_frames"]
    except Exception:
        return 0


def run_condition(cond):
    env = {k: v for k, v in cond.items() if k != "label"}
    compose_down()
    compose_up(env)

    if not wait_healthy():
        compose_down()
        return None, None

    time.sleep(5)
    count_start = get_frame_count()
    t_start = time.time()
    time.sleep(MEASURE_SECONDS)
    count_end = get_frame_count()
    elapsed = time.time() - t_start

    compose_down()

    frames = count_end - count_start
    fps = frames / elapsed if elapsed > 0 else 0
    delivery = frames / (elapsed * 30) if elapsed > 0 else 0
    return fps, min(delivery, 1.0)


def main():
    generate_video()

    print(f"\n{'='*55}")
    print(f"  StreamBed Throughput Benchmark  ({MEASURE_SECONDS}s per condition)")
    print(f"{'='*55}")
    print(f"  {'Condition':<20} {'FPS':>8} {'Delivery':>10}")
    print(f"  {'-'*40}")

    for cond in CONDITIONS:
        label = cond["label"]
        fps, delivery = run_condition(cond)
        if fps is not None:
            print(f"  {label:<20} {fps:>8.1f} {delivery:>9.1%}")
        else:
            print(f"  {label:<20} {'FAILED':>8}")

    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
