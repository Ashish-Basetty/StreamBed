# Plan: Replace VIDEO_SOURCE with TCP Video Stream

## Context

The inference container currently pulls frames from a `VIDEO_SOURCE` env var (set to `"synthetic"` in docker-compose), which is a testing hack baked into the compose stack. The goal is to make the system production-like: the edge inference container always connects to an external TCP video stream server to receive frames. A `MockVideoServer` fixture replaces `VIDEO_SOURCE=synthetic` in tests. This removes conditional env-var logic from the daemon and makes the video ingestion path uniform regardless of environment.

---

## Wire Protocol

Reuse the existing StreamBed chunk format (`shared/stream_chunks.py`):
- **Tag**: `CHUNK_MAGIC` (`b"CHNK"`)
- **Payload**: `struct.pack(">HHH", H, W, C)` (6-byte shape header) + raw `uint8` frame bytes
- Framing: via `make_chunks` / `read_message` from the promoted `shared/tcp_framing.py`

---

## Files to Create

### `shared/tcp_framing.py` (new)
Promote `experiments/advisor/advisorlib/tcp_framing.py` to shared. Remove the advisor-specific `sys.path` manipulation. Exports `read_message`, `write_message`, `CHUNK_MAGIC`, `ACTN_MAGIC`.

### `shared/mock_video_server.py` (new)
Async TCP server. For each connected client, streams synthetic frames (numpy zeros, default 64×64×3) at ~30 fps using `write_message` + CHUNK_MAGIC. Shape is encoded in the 6-byte header.

```python
class MockVideoServer:
    def __init__(self, host="0.0.0.0", port=9200, frame_shape=(64, 64, 3), fps=30)
    async def start(self)   # asyncio.start_server
    async def stop(self)
    # per-client: infinite loop write_message(writer, CHUNK_MAGIC, pack_shape + frame.tobytes())
```

Pytest fixture (can live in `tests/conftest.py` or a new `tests/fixtures/mock_video.py`):
```python
@pytest.fixture
def mock_video_server():
    server = MockVideoServer(port=9200)
    asyncio.run_in_executor / threading to run event loop
    yield server
    server.stop()
```

---

## Files to Modify

### `shared/tcp_framing.py` ← also update advisor import
After creating the shared version, update `experiments/advisor/advisorlib/tcp_framing.py` to import from `shared.tcp_framing` instead (or just replace its body with a re-export).

### `edge/edge_config.py`
- Remove: `VIDEO_SOURCE = os.getenv("VIDEO_SOURCE", "0")`
- Add:
  ```python
  VIDEO_SERVER_HOST = os.getenv("VIDEO_SERVER_HOST", "")
  VIDEO_SERVER_PORT = int(os.getenv("VIDEO_SERVER_PORT", "9200"))
  ```

### `edge/app.py`
- Remove: `VIDEO_SOURCE` import, `cv2` import, `VIDEO_SOURCE` branching in `video_capture_loop`
- Fix pre-existing broken import: `StreamBedTCPSender` was removed; switch sender to `StreamBedUDPSender`
- Replace `video_capture_loop` with a TCP client that:
  1. Connects to `VIDEO_SERVER_HOST:VIDEO_SERVER_PORT` with retry (same pattern as `_connect_to_proxy_with_retry`)
  2. Calls `read_message(reader)` in a loop → decodes shape header + raw bytes → `np.frombuffer(...).reshape(H,W,C)`
  3. Runs inference, stores, sends StreamFrame (unchanged from current)
- Import `VIDEO_SERVER_HOST`, `VIDEO_SERVER_PORT` from `edge_config`

### `control-plane/DeploymentDaemon/daemon_config.py`
- Remove: `VIDEO_SOURCE = os.environ.get("VIDEO_SOURCE")`
- Add:
  ```python
  VIDEO_SERVER_HOST = os.environ.get("VIDEO_SERVER_HOST", "")
  VIDEO_SERVER_PORT = int(os.environ.get("VIDEO_SERVER_PORT", "9200"))
  ```

### `control-plane/DeploymentDaemon/main.py`
- Update imports from `daemon_config`: remove `VIDEO_SOURCE`, add `VIDEO_SERVER_HOST`, `VIDEO_SERVER_PORT`
- Update `DeployRequest`:
  ```python
  video_server_host: str | None = None  # overrides daemon's VIDEO_SERVER_HOST
  video_server_port: int | None = None
  ```
- In `deploy()` → `container_env`: remove the `VIDEO_SOURCE` block; always set:
  ```python
  container_env["VIDEO_SERVER_HOST"] = body.video_server_host or VIDEO_SERVER_HOST
  container_env["VIDEO_SERVER_PORT"] = str(body.video_server_port or VIDEO_SERVER_PORT)
  ```

### `docker-compose.yml`
- Remove `VIDEO_SOURCE=synthetic` from `daemon-edge1`, `daemon-edge2`, `daemon-edge3` environment blocks.
- No replacement needed (inference containers will wait/retry until a video server is available).

### `tests/deploy_utils.py`
- Add `video_server_host: str = ""` and `video_server_port: int = 9200` parameters to `deploy_all_inference` and `deploy_device`.
- Pass them in the deploy payload so the test-spawned mock server address reaches the inference container.

### `tests/conftest.py`
- Add `mock_video_server` fixture that starts `MockVideoServer` in a background thread and yields it. Determine host address: use `host.docker.internal` on Darwin (Mac) or the docker bridge gateway IP on Linux.
- Pass the server's address into `deploy_all_inference` calls in `deployed_inference_stack`.

---

## Verification

1. **Unit**: `pytest tests/unit/` — ensure no imports of `VIDEO_SOURCE` or `cv2` remain in edge/app.py.
2. **Mock server standalone**: `python -c "from shared.mock_video_server import MockVideoServer; ..."` connects a TCP client and receives frames.
3. **Integration**: `pytest tests/` with `deployed_inference_stack` — edge containers should log `[Edge] Frame N` without `VIDEO_SOURCE` in any env.
4. **Docker-compose**: `docker compose up` — edge daemons start without `VIDEO_SOURCE`; inference containers wait for video server (expected if no server is configured).
