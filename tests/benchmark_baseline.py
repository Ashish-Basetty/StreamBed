import argparse
import asyncio
import socket
import struct
import time
import threading

import numpy as np

from shared.interfaces.stream_interface import (
    StreamBedUDPSender,
    StreamBedUDPReceiver,
    StreamFrame,
)
from shared.inference.mobilenet import MobileNetV2Model


def make_embedding_frame(i):
    return StreamFrame(
        timestamp=float(i),
        frame=None,
        embedding=np.random.rand(32).astype(np.float32),
        model_version="v1",
        source_device_id="bench-edge",
        frame_interleaving_rate=30.0,
    )


async def run_streambed(n_frames):
    receiver = StreamBedUDPReceiver()
    await receiver.listen("127.0.0.1", 0)
    port = receiver._transport.get_extra_info("socket").getsockname()[1]
    queue = receiver._queue

    sender = StreamBedUDPSender()
    await sender.connect("127.0.0.1", port)

    t0 = time.perf_counter()
    for i in range(n_frames):
        await sender.send(make_embedding_frame(i))
    await asyncio.sleep(0.3)
    t1 = time.perf_counter()

    received = 0
    while not queue.empty():
        queue.get_nowait()
        received += 1

    await sender.close()
    await receiver.stop()
    return received, t1 - t0


def run_tcp_baseline(n_frames):
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    port = server_sock.getsockname()[1]

    received_count = [0]

    def server_thread():
        conn, _ = server_sock.accept()
        while True:
            header = conn.recv(4)
            if not header or len(header) < 4:
                break
            length = struct.unpack(">I", header)[0]
            data = b""
            while len(data) < length:
                chunk = conn.recv(length - len(data))
                if not chunk:
                    break
                data += chunk
            if len(data) == length:
                received_count[0] += 1
        conn.close()
        server_sock.close()

    t = threading.Thread(target=server_thread, daemon=True)
    t.start()

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect(("127.0.0.1", port))

    t0 = time.perf_counter()
    for i in range(n_frames):
        payload = np.random.rand(32).astype(np.float32).tobytes()
        header = struct.pack(">I", len(payload))
        client.sendall(header + payload)
    t1 = time.perf_counter()

    client.close()
    t.join(timeout=2.0)
    return received_count[0], t1 - t0


def run_inference_benchmark(n_frames):
    model = MobileNetV2Model(device="cpu")
    model.load()
    frame = np.random.randint(0, 256, (224, 224, 3), dtype=np.uint8)

    t0 = time.perf_counter()
    for _ in range(n_frames):
        model.process_frame(frame)
    t1 = time.perf_counter()
    return n_frames, t1 - t0


def main():
    parser = argparse.ArgumentParser(description="StreamBed baseline benchmark")
    parser.add_argument("--frames", type=int, default=50)
    parser.add_argument("--inference", action="store_true")
    args = parser.parse_args()

    n = args.frames
    print(f"\n{'='*55}")
    print(f"  StreamBed Baseline Benchmark  ({n} frames)")
    print(f"{'='*55}")

    tcp_recv, tcp_elapsed = run_tcp_baseline(n)
    tcp_fps = tcp_recv / tcp_elapsed if tcp_elapsed > 0 else 0
    print(f"\n  [TCP baseline]")
    print(f"    Sent:     {n}")
    print(f"    Received: {tcp_recv}")
    print(f"    Elapsed:  {tcp_elapsed*1000:.1f} ms")
    print(f"    FPS:      {tcp_fps:.1f}")

    udp_recv, udp_elapsed = asyncio.run(run_streambed(n))
    udp_fps = udp_recv / udp_elapsed if udp_elapsed > 0 else 0
    print(f"\n  [StreamBed UDP]")
    print(f"    Sent:     {n}")
    print(f"    Received: {udp_recv}")
    print(f"    Elapsed:  {udp_elapsed*1000:.1f} ms")
    print(f"    FPS:      {udp_fps:.1f}")

    if args.inference:
        inf_frames, inf_elapsed = run_inference_benchmark(n)
        inf_fps = inf_frames / inf_elapsed
        print(f"\n  [Inference (MobileNetV2, CPU)]")
        print(f"    Frames:   {inf_frames}")
        print(f"    Elapsed:  {inf_elapsed*1000:.1f} ms")
        print(f"    FPS:      {inf_fps:.1f}")

    print(f"\n{'='*55}\n")


if __name__ == "__main__":
    main()
