"""
UDP throttling proxy for dynamic interleaving tests.
Receives from daemon, forwards to server at configurable bytes/sec.
"""
import asyncio
import os
import socket
import time


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


BIND_HOST = _env("BIND_HOST", "0.0.0.0")
BIND_PORT = int(_env("BIND_PORT", "9010"))
TARGET_HOST = _env("TARGET_HOST", "daemon-server1")
TARGET_PORT = int(_env("TARGET_PORT", "9000"))
RATE_BYTES_PER_SEC = float(_env("THROTTLE_RATE_BPS", "50000"))


async def run_proxy() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((BIND_HOST, BIND_PORT))
    sock.setblocking(False)

    target_addr = (TARGET_HOST, TARGET_PORT)
    loop = asyncio.get_running_loop()

    # Token bucket: rate in bytes/sec
    tokens = float(RATE_BYTES_PER_SEC)
    last_time = time.monotonic()

    print(f"[ThrottleProxy] Listening on {BIND_HOST}:{BIND_PORT}, forwarding to {target_addr} at {RATE_BYTES_PER_SEC} B/s")

    while True:
        try:
            data, addr = await loop.sock_recvfrom(sock, 65536)
        except Exception as e:
            print(f"[ThrottleProxy] recv error: {e}")
            break

        now = time.monotonic()
        elapsed = now - last_time
        tokens = min(RATE_BYTES_PER_SEC, tokens + elapsed * RATE_BYTES_PER_SEC)
        last_time = now

        need = len(data)
        if need > tokens:
            wait = (need - tokens) / RATE_BYTES_PER_SEC
            await asyncio.sleep(wait)
            tokens = 0
            last_time = time.monotonic()
        else:
            tokens -= need

        try:
            await loop.sock_sendto(sock, data, target_addr)
        except Exception as e:
            print(f"[ThrottleProxy] send error: {e}")

    sock.close()


def main() -> None:
    asyncio.run(run_proxy())


if __name__ == "__main__":
    main()
