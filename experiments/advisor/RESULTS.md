# Advisor Experiment Results

Running tally of every measured number. Updated as runs complete.

All Crafter scores use the canonical formula
`S = exp(mean(log(1 + 100·rate))) - 1` over 22 achievements, deterministic
argmax actions on the agent side, fixed seed=42 unless noted.

## Models

| Model | Trainable params | Encoder size | Crafter score | Eval episodes | Notes |
|---|---|---|---|---|---|
| Random | 0 | n/a | **1.25** | 30 | absolute floor |
| Teacher (PPO, 5M steps) | 1.66 M | 0.6 M | **10.39** | 50 | the canonical advisor / ceiling |
| BC student (frankenstein, separate encoder) | 233 K | 0.2 M | 8.00 | 50 | not used in current pipeline |
| shared_h64 (teacher.encoder + 34 K head) | 34 K | 0.6 M (shared) | 11.05 | 30 | head too capable; ≈ teacher |
| shared_h16 (teacher.encoder + 9 K head) | 9 K | 0.6 M (shared) | 10.51 | 30 | also ≈ teacher |
| **shared_h4** (teacher.encoder + 2 K head) | **2 K** | 0.6 M (shared) | **2.57** | 30 | bottlenecked, ~3× random — **the experiment baseline** |

The shared-encoder bottleneck dropoff is sharply non-linear: hidden=4 is barely
above random, hidden=16 already matches teacher. hidden=4 was picked because
the 8-point gap to teacher gives us maximum advisor headroom for a clean
demo.

## Pipeline phases

### Phase A — advisor head training

Skipped under the shared-encoder design. The "advisor" is teacher's own
`policy.mlp_extractor + action_net` running on incoming features — no
separate head to train, the perfect head already exists in the teacher
checkpoint.

### Phase B — host ⇄ edge TCP loop (no advisor)

| Path | Score | RTT | Notes |
|---|---|---|---|
| In-process (eval baseline) | 2.57 | n/a | reference |
| TCP via frame_gen ↔ edge_inference | **2.43** | 0.7-1.2 ms | within sampling noise of baseline |

Wire format: `make_chunks(payload, tag=CHNK)` for frames, `make_chunks(payload, tag=ACTN)` for actions.
Same chunk format as QUIC datagrams; reused via [`shared/stream_chunks.py`](../../shared/stream_chunks.py)
so any StreamBed sniffer can parse it.

### Phase C — advisor over the real wire (loopback UDP, no sidecar yet)

Edge ships `StreamFrame(frame=raw, embedding=feature)` per inferred frame.
The existing `_split_for_wire` produces one CHNK + one EMBD wire message.
Advisor server reads only EMBD, runs teacher's policy MLP + action_net on
the feature, sends 76-byte advice payload back via `send_custom(reliable=True)`
(CSTR tag). Edge's `recv_custom` background task drains advice into a
latest-slot read by the predict path.

| Configuration | Score | % gap closed | Avg advice age | Avg RTT |
|---|---|---|---|---|
| Edge solo (Phase B baseline) | 2.43 | 0% | n/a | 0.9 ms |
| **Edge + advisor (cadence=1, replacement)** | **8.35** | **76%** | 6 ms | 0.9 ms |
| Teacher (ceiling) | 10.39 | 100% | n/a | n/a |

The remaining ~2 points to teacher is the cost of 1-frame advice staleness
on the real wire: advice for frame N arrives after `predict(frame N)`
returns, so the edge actually applies advice from frame N-1. Could be
closed by holding the host response until advice lands, but that defeats
fire-and-forget and would block under any RTT spike.

### Phase D — docker-compose with real sidecars

**Wiring built. End-to-end run pending image push.**

The sidecar didn't have a server→edge application data path (only FBCK
flowed back over the control stream). Phase D now ships:

1. **Bidirectional QUIC sidecar.** Server role gains an optional
   `SERVER_REVERSE_UDP_BIND` listener; received packets are pumped onto the
   QUIC control stream (same channel pumpFeedback uses). Edge role gains an
   optional `LOCAL_RECV_UDP_TARGET`; non-FBCK control msgs are forwarded out
   to that UDP destination. CSTR advice rides this path; FBCK keeps its
   existing dispatch in `pumpControlIntoBandwidth`. Verified by
   [tests/quic/test_reverse_path.py](../../tests/quic/test_reverse_path.py).
2. **Daemon updates.**
   - `_spawn_sidecar_for_role` reads `SIDECAR_RECV_PORT` (edge) and
     `SIDECAR_SERVER_REVERSE_BIND_PORT` (server) and threads them through to
     `spawn_sidecar`.
   - Server's `LOCAL_SERVER_UDP` target switched from `127.0.0.1:<port>`
     (looped back to the sidecar itself) to `<DEVICE_ID>:<port>` (resolves
     to the inference container by docker network alias). This was a latent
     bug in the forward path too.
   - Edge inference containers now also get a network alias = DEVICE_ID
     (previously only server containers did).
   - `/deploy` now passes role-aware sidecar coords to inference containers:
     edge gets `SIDECAR_HOST`/`SIDECAR_FEED_PORT`/`ADVICE_LISTEN_PORT`,
     server gets `SIDECAR_HOST`/`FEED_LISTEN_PORT`/`SIDECAR_REVERSE_PORT`.
3. **Compose path fix.** docker-compose.yml + the three control-plane
   Dockerfiles all referenced the old `controller/` path; renamed to
   `control-plane/` to match the post-rename layout.
4. **Advisor inference Dockerfiles.** `experiments/advisor/edge/Dockerfile`
   and `experiments/advisor/server/Dockerfile` build CPU-only Python images
   with teacher.zip + shared_h4.pt baked in. Container layout mirrors the
   repo so the scripts' `parents[3]` / `parents[1]` sys.path inserts work.
5. **`docker-compose.advisor.yml`** overlay flips daemon-edge1 and
   daemon-server1 to `STREAM_TRANSPORT=quic` with reverse-path ports set,
   declares both inference images as `manual`-profile build targets, and
   exposes the edge inference TCP port 9100 on the host for frame_gen.

#### Workflow

```bash
# 1. Build images (locally) and push to your registry of choice.
docker compose -f docker-compose.yml \
               -f experiments/advisor/docker-compose.advisor.yml \
               build advisor-edge advisor-server
docker push ashishbasetty/streambed-advisor-edge:latest
docker push ashishbasetty/streambed-advisor-server:latest

# 2. Bring up controller + router + the two daemons (their sidecars are
#    spawned at /deploy time, so don't worry about them here).
docker compose -f docker-compose.yml \
               -f experiments/advisor/docker-compose.advisor.yml up -d \
               controller router daemon-edge1 daemon-server1

# 3. Deploy the inference containers via the controller. This launches the
#    inference container, which triggers the daemon to also spawn the QUIC
#    sidecar with the right reverse-path wiring.
bash experiments/advisor/scripts/deploy_advisor.sh

# 4. Drive Crafter from the host.
conda activate streambed
python experiments/advisor/host/frame_gen.py \
       --episodes 30 --edge-host 127.0.0.1 --edge-port 9100
```

#### Open items before declaring Phase D done

- Build the two advisor images locally and push.
- Run the workflow above end-to-end. Exit criterion is **score > 8.00**
  (Phase B's 2.43 baseline plus advisor lift) with `advice_age_s` bounded
  on the per-frame log.
- If the advisor lift is < the Phase C result (8.35), suspect QUIC RTT,
  not protocol breakage — same code paths, just more hops.

### Phase E — cadence sweep (real wire, replacement mode)

Sweep over `--reply-every-n` on the advisor. 10 eps per cadence (noisy;
to be re-run at 30 eps for "final" numbers).

| Cadence (advisor reply every N EMBDs) | Score | Avg advice age | Lift vs solo |
|---|---|---|---|
| 1 | **6.59** | 7 ms | +3.92 |
| 5 | 5.70 | 14 ms | +3.03 |
| 10 | 2.72 | 38 ms | +0.05 |
| 20 | **1.90** | 57 ms | **−0.77 (hurts)** |
| ∞ (solo) | 2.67 | n/a | 0 |

#### Headline observations

1. **Freshness cliff between cadence 5 and 10.** Score drops by ~3 points
   when advice goes from ~14 ms to ~38 ms old. Past that, advice provides
   no lift.

2. **Cadence 20 actively hurts** (score 1.90 < solo 2.67). At ~57 ms
   staleness (≈ 11 sim-frames old), the world has moved enough that the
   teacher's old recommendation is misaligned with the current state, and
   replacement-mode blindly executes it anyway. This is a real risk in
   the current design: under any meaningful RTT, replacement may be
   *worse* than no advice.

3. **Sim-rate vs real-time mapping:** sim runs at ~200 fps, so sim
   cadence-N translates to real-time-20fps cadence-(N/10). The cliff at
   sim cadence-10 (~50 ms freshness) maps to "real-time 20 fps with a
   single RTT on a clean LAN." Anything slower (4G, contended wifi,
   satellite) lands past the cliff in this design.

#### Implications for Phase F (drop tolerance)

The replacement-mode cliff suggests the experiment should also run
**additive blending** (`final = student + α·advice`) before declaring
results. Additive lets advice gradually fade as it ages without
catastrophically replacing student behavior with stale priors.

### Phase E.1 — additive blending (planned)

Re-sweep with `--blend-mode additive --alpha 1.0` to see if additive
flattens the cliff at high cadence.

### Phase F — drop tolerance under throttle proxy

Pending. Layers in the existing `docker-compose.throttle.yml` to throttle
the edge sidecar's egress, sweeps CHNK / CSTR loss rates separately. Tests
the StreamBed selective-drop story.

## Bandwidth / latency notes

Everything above is loopback. RTT 0.5-1 ms is not realistic. Real link
expectations:

| Link | Typical RTT | Implication at 20 fps |
|---|---|---|
| LAN | 0.5-2 ms | <1 frame stale |
| 5G | 5-30 ms | <1 frame stale |
| 4G | 20-100 ms | 1-2 frames stale |
| Spotty wifi / contended cellular | 100-500 ms with jitter | 2-10 frames stale |
| Geostationary satellite | 500-700 ms | 10-14 frames stale |

The cadence sweep gives us the score-vs-staleness curve directly. If
score holds up at cadence ≥ 10, then 200 ms-spotty links are fine. If it
collapses past cadence 2, RTT becomes a first-order design constraint.

## Wire-level params (per inferred frame, edge → server)

- **CHNK** (lossy, droppable): one StreamFrame's frame half. ~12 KB raw uint8 64×64×3 + small header.
- **EMBD** (reliable): one StreamFrame's embedding half. 512 × 4 = 2 KB feature.
- **CSTR advice** (reliable, server → edge): 8 B timestamp + 17 × 4 = 76 B per advice payload.

At 20 fps with cadence=1: total wire ≈ 280 KB/s edge→server, 1.5 KB/s server→edge.
EMBD-only (CHNK fully dropped): 40 KB/s edge→server, 1.5 KB/s server→edge — fits in any link.
