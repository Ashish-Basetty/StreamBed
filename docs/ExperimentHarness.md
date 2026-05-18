# StreamBed Experiment Harness — Plan

## Context

StreamBed's central thesis is that under **changing** network conditions the system adapts: edge always splits each `StreamFrame` into a CHNK (frame, lossy) and EMBD (embedding, lossless) wire packet, and the Go QUIC sidecar's token-bucket policy preferentially drops CHNKs so embeddings always get through. Today this adaptivity is demonstrable only under a **constant** throttle (`tests/throttle_proxy/proxy.py`, `THROTTLE_RATE_BPS=50000`) and a **static** advisor cadence (`--reply-every-n`, `experiments/advisor/server/advisor_server.py:162`).

This plan adds the missing experiment dimension: time-varying network conditions, time-varying advisor cadence, per-payload-type throughput measurement, and a pytest-driven harness that emits CSV time-series — locally first, then on the GCP 5-VM cluster.

## Current state — quick verification (Phase 0)

Before adding anything, prove the existing adaptive path still works end-to-end. Wire path is:

- Edge split: [shared/interfaces/stream_interface.py:232](shared/interfaces/stream_interface.py#L232) `_split_for_wire()` produces CHNK + EMBD packets.
- Sidecar policy: [sidecar/internal/policy/policy.go:31-106](sidecar/internal/policy/policy.go#L31-L106) drops CHNKs when bucket empty, always passes EMBDs.
- Server FBCK: [sidecar/internal/server/feedback.go:27-63](sidecar/internal/server/feedback.go#L27-L63) → consumed at [sidecar/internal/edge/edge.go:256-294](sidecar/internal/edge/edge.go#L256-L294). Informational only today.
- Existing throttle test: [tests/test_dynamic_interleaving.py:89-100](tests/test_dynamic_interleaving.py#L89-L100) `test_throttled_path_receives_fewer_frames` at 50 KB/s expects <50 frames over 15 s.

**Phase 0 action**: run `pytest tests/test_dynamic_interleaving.py -v -s` with `docker-compose.yml + docker-compose.throttle.yml` up. If it passes, baseline interleaving is healthy. If not, fix before continuing.

## Design (user decisions already locked)

1. **Profile shape**: stepped tiers + bursty loss/latency spikes. No linear ramps, no trace replay.
2. **Advisor slowdown**: dynamic `reply_every_n` schedule. No sleep knob.
3. **EMBD vs EMBD+frames**: no mode flag — sweep throttle rate and let CHNKs naturally drop at the bucket.
4. **Harness**: `pytest -m experiment`, CSV/JSON artifacts in `tests/experiments/results/`.

## Files to create

| Path | Purpose |
|---|---|
| `tests/varying_proxy/proxy.py` | Fork of [tests/throttle_proxy/proxy.py](tests/throttle_proxy/proxy.py). Reads `SCHEDULE_PATH` (YAML list of `{at_s, bps, loss_pct, extra_latency_ms}`). One asyncio task advances active segment by wall-clock; recv loop reads `current_bps/loss/latency` under a lock. `random.random() < loss_pct` for drops, `asyncio.sleep(extra_latency_ms/1000)` before sendto. |
| `tests/varying_proxy/Dockerfile` | Same shape as [tests/throttle_proxy/Dockerfile](tests/throttle_proxy/Dockerfile). |
| `tests/varying_proxy/requirements.txt` | `pyyaml`. |
| `docker-compose.varying.yml` | Sibling of [docker-compose.throttle.yml](docker-compose.throttle.yml). Mounts `./tests/experiments/schedules/` read-only into the container; binds same `9010/udp`. |
| `tests/experiments/__init__.py` | Marker. |
| `tests/experiments/conftest.py` | Registers `experiment` pytest marker. `experiment_results_dir` fixture pointing at `tests/experiments/results/`. Reuses existing `controller_url`. |
| `tests/experiments/harness.py` | `MetricsPoller` class (asyncio): every N s, GET each sidecar's `/metrics`, parse `streambed_sidecar_bytes_sent{tag="CHNK\|EMBD\|CSTR"}`, sample advisor stdout/log for `total_advised`. Writes CSV via `csv.DictWriter`. `write_schedule(path, segments)` helper. |
| `tests/experiments/schedules/stepped_burst.yml` | Canonical proxy schedule: e.g. `0s→500kbps`, `30s→100kbps`, `60s→{50kbps, 2% loss, +80ms}`, `90s→25kbps`. |
| `tests/experiments/schedules/cadence_step.yml` | Advisor schedule: e.g. `0s→reply_every_n=1`, `45s→5`, `90s→20`. |
| `tests/experiments/test_interleaving_sweep.py` | Single `@pytest.mark.experiment` test: starts cluster, writes schedules, runs ~2 min, polls metrics, writes `results/<timestamp>__sweep.csv`. |
| `infra/gcp/docker-compose.worker.experiment.yml` | Sibling of [infra/gcp/docker-compose.worker.yml](infra/gcp/docker-compose.worker.yml). Adds `varying-proxy` service to edge VMs only. Image `ashishbasetty/streambed-varying-proxy:latest`. Mounts `/etc/streambed/schedule.yml`. Keeps baseline file untouched so existing GCP tests are unaffected. |

## Files to modify

| Path | Change |
|---|---|
| [sidecar/internal/metrics/metrics.go](sidecar/internal/metrics/metrics.go) | Add `BytesSentByTag map[string]*atomic.Uint64` keyed by tag string (CHNK / EMBD / CSTR / RATE / ACTN / FBCK / UNKN). Add `IncBytesByTag(kind common.Kind, n int)` helper. Emit Prometheus lines with `tag="..."` label in the existing handler (lines 44-58). |
| [sidecar/internal/policy/policy.go](sidecar/internal/policy/policy.go) | After `kind := common.ClassifyPrefix(p)` (~line 89), call `r.metrics.IncBytesByTag(kind, len(p))` for any non-dropped payload. Pass `*metrics.Registry` into `NewRateLimit`. Update `policy_test.go` to inject a stub. Grep all call sites of `NewRateLimit(` to wire through. |
| [experiments/advisor/server/advisor_server.py](experiments/advisor/server/advisor_server.py) | Add `--cadence-schedule PATH` flag. In `main()` before `asyncio.run(serve(...))`, spawn `asyncio.create_task(_cadence_loop(args, path))` that sleeps to each next `at_s` and assigns `args.reply_every_n`. The existing read at line 113 picks it up atomically. Keep `--reply-every-n` as initial value / fallback. |
| [tests/gcp/deploy_helpers.py](tests/gcp/deploy_helpers.py) | Add optional `via_proxy: bool = False` to `deployed_edge_server_pair` (line 94). When true, override the edge's outbound sidecar target to point at the varying-proxy on the edge VM. |

## Critical insertion-point question

The existing throttle proxy targets `server-001:9000` (the inference container's UDP port, [docker-compose.throttle.yml:21-22](docker-compose.throttle.yml#L21-L22)). For the *experiment* the proxy must sit in the edge-sidecar → server-sidecar QUIC datagram path — i.e. on the edge VM, forwarding to the **server VM's sidecar UDP port**, not to the inference container's port. This is the wire we actually want to model. Phase 1 should confirm the right insertion point with a single-VM smoke test before doing GCP work — otherwise we'll measure the wrong link.

## Schedule format

Two tiny YAML schemas, one loader pattern:

```yaml
# proxy schedule (varying_proxy)
- {at_s: 0,   bps: 500000, loss_pct: 0.00, extra_latency_ms: 0}
- {at_s: 30,  bps: 100000, loss_pct: 0.00, extra_latency_ms: 0}
- {at_s: 60,  bps: 50000,  loss_pct: 0.02, extra_latency_ms: 80}
- {at_s: 90,  bps: 25000,  loss_pct: 0.00, extra_latency_ms: 0}
```

```yaml
# advisor schedule (cadence_step.yml)
- {at_s: 0,  reply_every_n: 1}
- {at_s: 45, reply_every_n: 5}
- {at_s: 90, reply_every_n: 20}
```

Passed via env var `SCHEDULE_PATH=/etc/streambed/schedule.yml` + bind mount, so tests can write a fresh schedule per run without rebuilding images.

## CSV output

`results/<UTC-iso>__<test_name>.csv` columns:

```
t_s, throttle_bps, loss_pct, extra_latency_ms, cadence_n,
chnk_bytes_sent, embd_bytes_sent, cstr_bytes_recv,
embd_rate_hz, chnk_rate_hz, advice_rate_hz
```

One row per `poll_interval_s` (default 1.0). Polled from each sidecar's `/metrics` and the advisor's log/stdout.

## Order of execution

1. **Phase 0** — verify baseline `pytest tests/test_dynamic_interleaving.py` passes.
2. **Sidecar per-tag counters** ([metrics.go](sidecar/internal/metrics/metrics.go), [policy.go](sidecar/internal/policy/policy.go)). Rebuild sidecar image tagged `:experiment` (don't touch `:latest` — keeps baseline GCP tests safe).
3. **`varying_proxy`** + Dockerfile + `docker-compose.varying.yml`. Push `ashishbasetty/streambed-varying-proxy:latest`.
4. **Advisor `--cadence-schedule`** in [advisor_server.py](experiments/advisor/server/advisor_server.py). Mounted schedule — no image rebuild needed if running from source mount; otherwise rebuild advisor image.
5. **`tests/experiments/harness.py` + first test**. Run locally with `docker-compose.yml + docker-compose.varying.yml`.
6. **GCP wiring**: `docker-compose.worker.experiment.yml`, `deploy_helpers.via_proxy`. Run on GCP.

## Top risks

1. **Insertion point** — see above. Confirm with single-VM smoke test before GCP rollout.
2. **Sidecar image rebuild churn** — tag experimental sidecar `:experiment` and override `SIDECAR_IMAGE` in the experiment compose only, so baseline `:latest` stays untouched and `pytest -m gcp` keeps passing.
3. **Cadence schedule mutation race** — writing `args.reply_every_n = newN` from a background task is safe in CPython but the read at [advisor_server.py:113](experiments/advisor/server/advisor_server.py#L113) happens inside `async for sf in duplex.receive_stream()`. The schedule task must `await asyncio.sleep(...)` so the loop yields. Verify with a 5 s smoke that prints N on each transition.

## Verification

**Local** (no GCP needed):
```sh
docker compose -f docker-compose.yml -f docker-compose.varying.yml up -d
pytest -m experiment tests/experiments/test_interleaving_sweep.py -v -s
```
Pass criteria: `tests/experiments/results/*.csv` exists; `chnk_bytes_sent` rate visibly drops in rows aligned with the schedule's 100 kbps step; `embd_bytes_sent` rate stays roughly flat through all steps until the deepest throttle; `advice_rate_hz` follows the cadence schedule.

**Regression** (must still pass):
```sh
pytest -m integration_stream
pytest -m gcp tests/gcp/    # baseline GCP test, no experiment compose
```

**GCP**:
```sh
cd infra/gcp && ./vms.sh start
# IAP tunnel up (existing flow)
# Bring up experiment compose on edge VMs:
for vm in edge-01 edge-02; do
  gcloud compute ssh "$vm" --zone=us-central1-a --tunnel-through-iap \
    --command='cd ~/StreamBed && sudo docker compose -f infra/gcp/docker-compose.worker.yml -f infra/gcp/docker-compose.worker.experiment.yml up -d'
done
pytest -m "experiment and gcp" tests/experiments/ -v -s
cd infra/gcp && ./vms.sh stop  # budget lever
```
Pass criteria: same CSV shape as local; GCP-skip behavior still works when cluster is down.

## Out of scope (intentionally)

- Replay of real LTE bandwidth traces (user said no).
- Advisor sleep-per-response knob (user picked cadence-only).
- An EMBD-only edge mode flag (user picked natural-crossover via throttle sweep).
- New VMs or replacing the existing throttle proxy.
