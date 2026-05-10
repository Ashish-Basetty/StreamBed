# Advisor Wiring Plan

How to plug the trained student + teacher into StreamBed's existing
docker-compose / sidecar / daemon architecture so the two-tier system
runs end-to-end, with two-way communication between a local frame
source and the edge inference container.

Companion to [TwoTierAdvisor.md](TwoTierAdvisor.md) — that doc covers
the *what*; this one covers the *how*.

## Recap of what's already built

- [`experiments/advisor/checkpoints/ppo_teacher_final.zip`] — 1.66M
  param PPO teacher. Crafter score 10.39 over 50 deterministic episodes.
- [`experiments/advisor/checkpoints/ppo_student_distilled.zip`] —
  233K param student, BC-distilled from teacher. Score 8.00.
- CSTM_LOSSY (`CSTL`) and CSTM_RELIABLE (`CSTR`) tags wired through the
  Go sidecar protocol, the policy layer, and the Python sender/receiver
  API. Round-trip tests passing.

The 8.00 → 10.39 gap is the advisor headroom this experiment closes.

## End state

```
┌────────────────────────┐       ┌─────────────────────────┐
│ HOST PROCESS (local)   │       │ EDGE INFERENCE          │
│ - Crafter env          │ HTTP  │ (docker container)      │
│ - frame_gen / actuator │ <───> │ - student model         │
│ - asks edge for action │       │ - frame -> feature      │
│ - applies returned act │       │ - blends advisor advice │
└────────────────────────┘       └────────┬────────────────┘
                                          │ Python interface
                                          ▼
                                 ┌─────────────────────────┐
                                 │ EDGE SIDECAR (Go,       │
                                 │ docker, existing)       │
                                 └────────┬────────────────┘
                                          │ QUIC: CSTR feature, CSTR advice
                                          ▼
                                 ┌─────────────────────────┐
                                 │ SERVER SIDECAR (Go,     │
                                 │ docker, existing)       │
                                 └────────┬────────────────┘
                                          │
                                          ▼
                                 ┌─────────────────────────┐
                                 │ ADVISOR INFERENCE       │
                                 │ (docker container)      │
                                 │ - advisor_head          │
                                 │ - feature -> advice     │
                                 └─────────────────────────┘
```

## Tag inventory (current state of StreamBed protocol)

| Tag | Direction | Reliability | What it carries |
|---|---|---|---|
| `CHNK` | edge → server | lossy, droppable | video frame chunk |
| `EMBD` | edge → server | reliable | embedding / feature vector |
| `ACTN` | either | control, reliable | action signal (defined but unused — we adopt it) |
| `RATE` | server → edge | control, reliable | rate ceiling |
| `FBCK` | server → edge | control, reliable | bandwidth feedback |
| `CSTL` | either | lossy, droppable | user-defined bulk data |
| `CSTR` | either | reliable | user-defined priority message |

The wiring below uses **only existing tags**. No new protocol code in
StreamBed (sidecar / control plane / daemons / stream interface).

## Wire formats

Three boundaries, each with a fixed schema. All three reuse the
StreamBed tag convention (4-byte ASCII tag prefix) — including the
local host↔edge link, so any process that already speaks StreamBed
can drop in.

### Boundary 1 — host process ⇄ edge inference container (TCP socket)

Plain TCP socket on a published edge port (e.g. 9100). Length-prefixed
messages, each prefixed with the StreamBed tag identifying its kind:

```
[ 4-byte BE uint32 length (of tag+payload) ]
[ 4-byte tag                              ]
[ payload (UTF-8 JSON)                    ]
```

**Host → edge (frame, every step):**
- Tag: `CHNK` (host-side frames are lossy bulk just like video).
- Payload (JSON):
  ```json
  {"frame_idx": 1234, "timestamp": 1715204000.123,
   "frame_b64": "...64x64x3 base64 of uint8..."}
  ```

**Edge → host (action, response per frame):**
- Tag: `ACTN` (action control signal — pre-existing tag, was reserved
  for exactly this purpose, never wired before).
- Payload (JSON):
  ```json
  {"frame_idx": 1234, "action": 5,
   "advice_used": true, "advice_age_steps": 3}
  ```

JSON over the stream means base64 inflates a 12,288-byte frame to ~16
KB — wasteful, but on loopback at 20 fps that's 320 KB/s, irrelevant.
The win is universal debuggability: any StreamBed-aware process can
inspect the stream with two lines of code, and a third-party tool can
inject test frames or sniff actions without learning a custom binary
schema. If profiling later shows JSON parsing dominates, swap to a
binary frame payload (raw 12,288 bytes) under the same tagged framing.

### Boundary 2 — edge inference ⇄ edge sidecar (existing StreamBed Python interface)

**Reuse `StreamBedUDPSender.send(StreamFrame)` directly** — no new code.
The existing `_split_for_wire` already produces exactly what we want:

```python
sf = StreamFrame(
    timestamp=ts,
    frame=raw_frame,        # 64×64×3 uint8 — goes out as CHNK (lossy, droppable)
    embedding=feature_vec,  # 128 float32 — goes out as EMBD (reliable, must arrive)
    model_version="student-v1",
    source_device_id="edge-001",
)
await sender.send(sf)
```

This *is* the dynamic interleaving you wanted. The sidecar's policy
layer drops CHNK first under bandwidth pressure but always delivers
EMBD. So:

- **Embeddings always arrive** → server advisor sees every frame's
  feature, regardless of network conditions.
- **Frames arrive when bandwidth allows** → server logs them for
  performance eval, dataset building, retrospective analysis. Server
  inference never reads the frame channel.

Server → edge advice rides `CSTR` (custom reliable) since it's small,
priority, and decoupled from the frame/embedding lifecycle:

- `sender.send_custom(advice_bytes, reliable=True)` server-side
- `recv_custom(timeout=...)` edge-side
- `advice_bytes`: `header(8B: frame_idx u32, ts f32) + 17 float32 advice logits` = 76 B

### Boundary 3 — sidecar ⇄ sidecar (existing QUIC protocol)

Already done. EMBD + CSTR ride QUIC datagrams reliably; CHNK rides them
as droppable; control tags ride the reliable control stream.

## Two-channel split: features for inference, frames best-effort

Server inference runs **only on embeddings**. The frame channel is
parallel, droppable, and consumed by non-inference paths.

```
frame  ─→ student.encoder ─→ 128-d feature ─→ student.head ─→ action
   │                              │
   │ ships as CHNK (lossy)        │ ships as EMBD (reliable)
   │ best-effort                  │ always arrives
   ▼                              ▼
SERVER FRAMESTORE          ADVISOR INFERENCE
- eval / metrics           - advisor_head(feature)
- offline retraining       - sends advice back as CSTR
- forensics                - score: needs every step's feature
```

**Why this split is the right design:**

- The advisor's job is real-time decision support. It must see every
  frame's semantic state — that's EMBD. EMBD is small (512 B) and
  cheap to keep reliable.
- Frames are 24× larger than embeddings (12 KB vs 512 B). Forcing
  them through reliable delivery would dominate the bandwidth budget
  and hurt embedding latency under congestion.
- Frames *are* useful — for retrospective evaluation, training-data
  curation, debugging — just not for real-time inference. Dropping
  some under load is fine; we still get most of them.
- This is exactly the workload StreamBed's CHNK/EMBD split was
  designed for. We're not bending the protocol; we're using it as
  intended.

**The advisor head is small.** ~5K params (128→64→17). Trains in
seconds on the same (student_feature, teacher_action) pairs we
already collected for BC distillation. Runs in <1 ms server-side.

**Failure plan if advisor head can't generalize.** If `advisor_head`
can't reach ≥90% argmax-accuracy vs teacher on a held-out split,
flip server inference to read the existing CHNK channel and run the
full teacher on those frames. The CHNK stream is already flowing
(for FrameStore); we just route it to the teacher instead of (or in
addition to) the advisor head. No protocol change required either way.

## Frame source: docker container vs local process

| | Local host process | Docker container |
|---|---|---|
| display access | direct (mss, screen capture work) | broken on macOS / Apple Silicon |
| dev iteration | conda env, breakpoints, REPL | rebuild container per change |
| matches deployment shape | yes — frame source ≠ inference cluster in real apps | no, blurs the boundary |
| networking overhead | one TCP hop to edge container | docker bridge + TCP |
| CI / reproducibility | depends on host conda env | reproducible image |

**Decision: local process.** The whole point of the edge↔server split
is that the frame source lives somewhere different from where the ML
runs. A robot, a phone, a user's machine. Putting frame_gen in docker
to match the rest is contracting the architecture into something less
realistic. Local is also where Crafter is easiest to drive and where
breakpoints work.

We'll make the host process trivially containerizable later if a CI
demo wants it.

## What's new vs what's reused

**Reuse as-is:**
- QUIC sidecar (`sidecar/`)
- Daemon (`control-plane/DeploymentDaemon/`) for lifecycle of new
  containers
- Controller + router for service discovery
- `StreamBedUDPSender.send_custom` / `recv_custom` (just shipped)
- The throttle proxy + `docker-compose.throttle.yml` for Phase 4

**New components:**

1. **`experiments/advisor/edge/`** — edge inference container.
   - `Dockerfile`
   - `edge_inference.py` — FastAPI server, loads
     `ppo_student_distilled.zip`, runs `/predict`, owns the
     `StreamBedUDPSender` for outbound CSTR.
   - `advice_consumer.py` — async task, drains `recv_custom`, keeps a
     `latest_advice` slot the predict path reads.

2. **`experiments/advisor/server/`** — advisor inference container.
   - `Dockerfile`
   - `advisor_server.py` — owns a `StreamBedUDPServerReceiver`,
     loads `advisor_head.pt`, processes incoming features, sends
     advice back via `send_custom`.
   - `advisor_head.pt` — the small head we'll train in step 1 below.

3. **`experiments/advisor/host/`** — local frame source.
   - `frame_gen.py` — Crafter env loop, calls
     `http://localhost:<edge_port>/predict`, applies returned action,
     logs reward.

4. **`experiments/advisor/docker-compose.advisor.yml`** — overlay that
   adds the edge + advisor containers to the existing setup.
   References the existing daemon/sidecar/router/controller services.

5. **`experiments/advisor/bench/train_advisor_head.py`** — collects
   (student_feature, teacher_action) pairs, trains the small head,
   saves `advisor_head.pt`. Single file, ~80 lines.

## Phased execution

Each phase ends with a runnable artifact.

### Phase A — train the advisor head

- Write `train_advisor_head.py`. Run student to collect 50K
  (state, feature, teacher_action) triples; supervised CE on the head.
- **Exit:** `advisor_head.pt` saved; head argmax accuracy vs teacher
  ≥ 90% on a held-out 5K split. If <90%, retry with bigger head;
  if still <90%, fall back to JPEG frames.

### Phase B — host ⇄ edge HTTP

- Write `frame_gen.py` and `edge_inference.py` (no sidecar yet).
- Edge inference loads student, exposes `/predict`. frame_gen drives
  Crafter and gets actions over HTTP.
- **Exit:** Crafter score from `frame_gen.py` matches the
  `eval_agent.py` baseline (~8.00) within noise. Confirms the HTTP
  hop doesn't break inference.

### Phase C — edge ⇄ server CSTR loop

- Write `advisor_server.py` and the `advice_consumer.py` task.
- Wire `send_custom` (feature) and `recv_custom` (advice) into the
  edge container.
- **Exit:** locally, with sidecars in the loop, edge inference
  container receives advice messages within 50 ms of sending features.
  Confirmed by per-frame latency log.

### Phase D — full docker-compose with the existing stack

- **Sidecar reverse path.** Add bidirectional support tightly integrated
  with the existing FBCK feedback channel. Server role: optional
  `SERVER_REVERSE_UDP_BIND` listener pumps app data onto the QUIC control
  stream. Edge role: non-FBCK control msgs are forwarded to an optional
  `LOCAL_RECV_UDP_TARGET` UDP destination. Both halves are opt-in so the
  forward-only flow stays the default.
- **Daemon wiring.** New env vars `SIDECAR_RECV_PORT` (edge) and
  `SIDECAR_SERVER_REVERSE_BIND_PORT` (server) drive sidecar reverse-path
  config. Inference-container `/deploy` now also gives edge containers a
  docker network alias = DEVICE_ID (previously server-only) and threads
  `SIDECAR_HOST` + role-aware ports into the container env.
- **Compose path fix.** `docker-compose.yml` + the three control-plane
  Dockerfiles still pointed at the pre-rename `controller/` path; updated.
- **Build images.** `experiments/advisor/edge/Dockerfile` and
  `experiments/advisor/server/Dockerfile` ship the inference scripts with
  checkpoints baked in.
- **Compose overlay.** `experiments/advisor/docker-compose.advisor.yml`
  flips daemon-edge1 / daemon-server1 to `STREAM_TRANSPORT=quic` with the
  new reverse-path ports, exposes the edge inference TCP port on host,
  and declares both inference images as `manual`-profile build targets.
- **Deploy helper.** `experiments/advisor/scripts/deploy_advisor.sh` POSTs
  to controller `/deploy` for both devices.
- Run `docker compose -f docker-compose.yml -f experiments/advisor/docker-compose.advisor.yml up`,
  then `bash experiments/advisor/scripts/deploy_advisor.sh`, then frame_gen
  on host pointing at `127.0.0.1:9100`.
- **Exit:** Crafter score with advisor live > 8.00. Per-frame log
  shows advice ages bounded.

### Phase E — cadence sweep (the headline experiment)

- frame_gen records score over N episodes for each cadence value
  N ∈ {1, 5, 10, 20, ∞}.
- Plot score vs cadence.
- **Exit:** the headline plot from TwoTierAdvisor.md.

### Phase F — drop tolerance (the StreamBed story)

- Layer in `docker-compose.throttle.yml` to throttle the edge sidecar's
  egress.
- Sweep CHNK / CSTR loss rates.
- **Exit:** the drop-tolerance plot — CSTR-loss curve degrades faster
  than CHNK-loss curve, demonstrating selective drop in action.

## Two-way communication on the edge inference container

The edge container has three concurrent roles:

1. **TCP server (asyncio)** — listens on the edge port, accepts host
   connections, reads tagged frame messages (CHNK), responds with
   tagged action messages (ACTN). One persistent socket per host.
2. **Sidecar sender** — owned by the predict path; per inference,
   constructs a `StreamFrame(frame=..., embedding=feature)` and calls
   the existing `StreamBedUDPSender.send()`. Goes out as one CHNK +
   one EMBD wire message via `_split_for_wire`.
3. **Sidecar receiver** — async task, drains `recv_custom`, writes
   to a `latest_advice` slot guarded by an asyncio.Lock or simple
   atomic ref.

Predict path pseudocode:
```python
async def handle_host_frame(msg):
    # msg already framed: {tag: CHNK, payload: {frame_idx, ts, frame_b64}}
    frame = decode_b64(msg["frame_b64"])
    feat, student_logits = student.encoder_and_head(frame)

    advice, advice_age = read_latest_advice()  # may be None / stale
    if advice is not None and advice_age < MAX_STALENESS:
        final_logits = student_logits + ALPHA * advice
        used = True
    else:
        final_logits = student_logits
        used = False

    action = sample(final_logits)

    # Fire and forget over the existing StreamBed pipe.
    # _split_for_wire turns this into CHNK (frame, lossy) + EMBD (feature, reliable).
    sf = StreamFrame(timestamp=msg["timestamp"], frame=frame,
                     embedding=feat, model_version="student-v1",
                     source_device_id=DEVICE_ID)
    asyncio.create_task(sidecar_sender.send(sf))

    # Reply to host with ACTN-tagged JSON over the same TCP socket.
    return {"action": int(action), "advice_used": used,
            "advice_age_steps": advice_age}
```

The advisor's response is decoupled from the request-response cycle.
This is intentional: if the advisor is slow or the network is
throttled, the edge keeps making decisions on its own — that's the
graceful degradation story.

## Open questions

- **Action blending: additive (`student + α·advice`) vs
  replacement (`if advice fresh, override`).** Default to additive
  with α=1.0. Try replacement as an ablation if advisor head accuracy
  is high (>95%).
- **Advice cadence trigger: every N frames vs uncertainty-triggered
  (when student entropy > τ).** Phase E uses pure step-count;
  uncertainty-triggered is a stretch ablation.
- **Multiple edges, one advisor.** Plan supports it (CSTR is keyed
  by `(source_device_id, frame_idx)`). Phase G would be running 3
  edges through one advisor and seeing if score holds.
- **Where does `advisor_head.pt` live in the image?** Bake it into
  the `experiments/advisor/server` image at build time; no need for a
  shared volume since it's small and read-only.

## Real-world relevance

This isn't just a contrived demo. The shape of the system — small
fast edge model + heavier remote advisor + bandwidth-aware
selective-drop wire — maps onto real production patterns:

- **Robot fleets.** Each robot runs a small policy locally for
  reflexes; a fleet-wide model on a central server provides
  higher-level corrections. Network on a factory floor / warehouse /
  field site is shared, lossy, sometimes wifi, sometimes 4G —
  exactly where selective drop matters. (This is the design tier
  Tesla, Boston Dynamics, Anyscale's robot-fleet customers run.)
- **Mobile / on-device AI with cloud assist.** Phone runs distilled
  model for normal use; sends features to cloud for complex queries
  (e.g. Apple Visual Intelligence, Google Lens). Mobile networks are
  bandwidth-variable and latency-variable — graceful degradation is
  not optional.
- **Industrial IoT anomaly detection.** Sensor edges stream feature
  vectors continuously to a central anomaly model; full sensor
  traces stream best-effort for forensics if anomaly fires. Pattern
  is identical to ours.
- **Game streaming + AI assist.** Game runs locally; an AI assistant
  in the cloud provides hints / coaching by watching frames. Network
  is unreliable (home wifi, cellular). Selective drop on frames vs
  guaranteed delivery on hint messages matches StreamBed exactly.
- **Autonomous driving (development tier).** Vehicle runs production
  perception model; cloud-side a heavier model labels rare scenes
  for retraining. Edge-cloud bandwidth is contested; you want
  embeddings always, raw video only when budget allows. Same
  pattern.

**Honest caveats.**

- Most production systems today use plain HTTP/gRPC over reliable
  networks because their bandwidth is plentiful — selective drop only
  matters when bandwidth is genuinely scarce. The use case is real
  but somewhat niche.
- StreamBed's QUIC/sidecar/policy stack is more operationally complex
  than `requests.post(...)`. The complexity pays back when you need
  the policy semantics; until then, simpler tools win.
- The "advisor head" pattern (small remote head consuming local
  features) is novel-ish. Closer analogs in the literature are
  "split learning" and "federated edge inference" — there's
  research, but production deployments are rare.

**Where this design wins.** When you can name a real bandwidth
constraint (mobile data caps, satellite, congested factory wifi,
shared 5G slice) and a real reliability asymmetry (the embedding
must arrive; the frame is nice-to-have), the StreamBed pattern is
the right tool. When you can't name those constraints, simpler
RPC works fine. This experiment is a load-bearing demo for the
cases where the constraints are real.
