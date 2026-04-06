# StreamBed Architecture

## Overview

StreamBed is a distributed inference framework where the networking layer (routing, chunking,
rate limiting, bandwidth feedback) is completely transparent to inference containers. Edge and
server containers only deal with frames and action tokens — StreamBed handles everything else.

---

## System Topology

```
[Edge inference container]                    [Server inference container]
  sender.send(frame)  ──────────────────────►   async for frame in receiver:
  tokens = await sender.receive_action()              tokens = model.infer(frame)
                        ◄──────────────────────  receiver.send_action(tokens)
         ↕                                                    ↕
┌──────────────────────────────────────────────────────────────────────┐
│                      StreamBed layer (invisible)                      │
│       chunking · rate limiting · TCP↔UDP · bandwidth feedback        │
└──────────────────────────────────────────────────────────────────────┘
         ↕ TCP                                           ↕ UDP
  [Go Sidecar (edge daemon)]  ◄── RATE/ACTN UDP ──  [Server StreamBed layer]
         ↕ UDP
     [Server]
```

Full hop breakdown:
```
Edge container → TCP → Go Sidecar → UDP (chunked) → Server container
Edge container ← TCP ← Go Sidecar ← UDP (ACTN)   ← Server container
                        Go Sidecar ← UDP (RATE)   ← Server container (internal only)
```

---

## Components

### Go Sidecar (replaces Python proxy in daemon)

Runs alongside the Python daemon. Owns the entire data plane:
- TCP server: accepts connection from edge inference container
- UDP sender: forwards chunked frames to server
- UDP receiver: receives ACTN and RATE packets from server
- Polls `stream-target.json` for routing updates (written by Python daemon)
- Rate limiting and frame drop logic (bandwidth estimation lives here)

The Python daemon shrinks to pure control plane: deploy containers, register/deregister,
write `stream-target.json` on `/stream-target` PUT. Zero involvement in data path.

Interface between daemon and sidecar: `stream-target.json` (already file-based, no change needed).

### Python Daemon (control plane only)

Responsibilities after refactor:
- Docker container lifecycle (deploy, stop, remove)
- Device registration with controller
- Write `stream-target.json` when routing changes (`PUT /stream-target`)
- No proxy tasks, no asyncio UDP/TCP handling

### Edge Inference Container

Uses `StreamBedTCPSender` SDK. Doesn't know about UDP, chunking, or routing:
```python
sender = StreamBedTCPSender()
await sender.connect(STREAM_PROXY_HOST, STREAM_PROXY_PORT)  # connects to Go sidecar
sent = await sender.send(frame)
tokens = await sender.receive_action()  # blocks until server responds
```

### Server Inference Container

Uses `StreamBedUDPServerReceiver` SDK. Doesn't know about daemons or edge topology:
```python
receiver = StreamBedUDPServerReceiver()
await receiver.listen(host, port)
async for frame in receiver.receive_stream():
    tokens = model.infer(frame)
    await receiver.send_action(tokens)  # sends ACTN packet back to edge daemon
```

---

## Wire Protocol

Three packet types on UDP. Dispatch is by 4-byte magic prefix.

| Magic  | Name  | Direction         | Handler                          |
|--------|-------|-------------------|----------------------------------|
| `CHNK` | Chunk | edge→server       | Reassemble into StreamFrame      |
| `ACTN` | Action| server→edge daemon| Forward back on TCP to edge      |
| `RATE` | Rate  | server→edge daemon| Update bandwidth estimator only — never forwarded |

### ACTN packet format (server → daemon → edge)
```
ACTN (4) | payload_len (4) | payload (action tokens, arbitrary bytes)
```

### RATE packet format (server → daemon, internal)
```
RATE (4) | json_len (4) | {"received_bps": <float>}
```
(replaces existing raw JSON `{"received_bps": ...}` for explicit typing)

### Sidecar routing rule
```
UDP from server:
  ACTN → write directly on active TCP writer to edge
  RATE → parse received_bps, feed into bandwidth estimator (never forwarded)
  else → drop
```

---

## SDK Changes (shared/interfaces/stream_interface.py)

### StreamBedTCPSender (edge-side)
- `self._reader` already exists but is never read
- Add `receive_action() -> bytes` — reads length-prefixed ACTN payload from TCP conn
- No other changes; send path is unchanged

### StreamBedUDPServerReceiver (server-side)
- Add `send_action(tokens: bytes) -> None` — sends ACTN packet via `send_datagram`
  to `self.stream_source_addr` (already tracked)
- Existing `_on_datagram` callback already tracks source addr per packet
- RATE feedback send loop in `server/app.py` switches from raw JSON to RATE magic prefix

---

## Model Upgrade Path

Current: both edge and server run MobileNetV2 (redundant — server re-runs inference
on frames the edge already processed).

Target:
- **Edge**: remove local inference entirely, send raw frames only. Edge becomes a
  capture + stream device, reducing M1 load significantly.
- **Server**: upgrade to DINOv2 (or similar ViT) + lightweight action head on GPU.
  Inference latency will dominate e2e round-trip (~50–200ms for ViT vs. ~1ms network).

This changes the interesting research question: adaptive streaming policy should account
for GPU queue depth, not just network bandwidth. Frame dropping should prefer dropping
when the server is backed up, not just when bandwidth is saturated.

---

## Latency Budget (post-refactor, cross-region)

```
capture → TCP send → Go sidecar → UDP → server recv → infer → ACTN → TCP recv
  ~1ms       ~1ms       ~0.5ms    ~10-50ms   ~1ms      50-200ms  ~1ms    ~1ms
```

Network dominates only if inference is fast (small model). For DINOv2-scale models,
inference is the bottleneck — which makes the adaptive frame drop policy the most
impactful research lever.

---

## Infrastructure Plan

- **CPU nodes** (controller, daemons, edge sim): Hetzner CX21 ~$6/mo, 3 nodes ≈ $18/mo
- **GPU node** (server inference): Vast.ai or RunPod, RTX 3090 ~$0.25–0.40/hr
  - Run GPU only during test sessions — ~100 hrs total ≈ $30–40
- **Multi-region**: Hetzner has US/EU/Singapore — sufficient for cross-region latency tests
- **Total budget**: comfortably under $100

---

## Capstone Task Order (suggested)

1. Instrument e2e latency baseline (before any optimization)
2. Go sidecar — replaces Python proxy, architecture above
3. Verify embedding streaming end-to-end with full deployment tests
4. Model upgrade: DINOv2 on server, remove edge inference
5. Bidirectional action token channel (ACTN/RATE protocol + SDK changes)
6. Device authentication (API key header, simple)
7. Cloud deployment on Hetzner + Vast.ai
8. Adaptive streaming experiments (frame drop policy vs. inference queue depth)
9. Cross-region tests
10. Request router (nginx/Traefik in front of controller replicas — no custom consensus)
