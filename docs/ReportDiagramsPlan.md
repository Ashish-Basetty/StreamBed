# Base plan: report diagrams via centralized CSV results

## Context

The capstone report needs diagrams that show StreamBed's adaptivity under
changing network conditions plus the throughput tradeoffs across modes and
network profiles. Two underlying experiment streams already have prior
planning in the repo:

- **Adaptive sweep** — time-varying proxy + advisor cadence schedule,
  per-tag (CHNK/EMBD/CSTR/...) bytes over time. Plan in
  [docs/ExperimentHarness.md](docs/ExperimentHarness.md).
- **Throughput benchmark** — FPS / delivery% / latency across
  (clean, 50ms delay, 10% loss) × (embeddings, raw_frames). Lives in
  [tests/throughput/run_throughput.py](tests/throughput/run_throughput.py).

Implementation status (verified):

- Per-tag counters + Prometheus exposure: **done** in [sidecar/internal/metrics/metrics.go](sidecar/internal/metrics/metrics.go) (`BytesSentByTag` / `BytesRecvByTag`, served at `/metrics`).
- Varying proxy + compose: **done** at [tests/varying_proxy/](tests/varying_proxy/) and [docker-compose.varying.yml](docker-compose.varying.yml).
- Advisor cadence schedule: **done** at [experiments/advisor/server/advisor_server.py:77](experiments/advisor/server/advisor_server.py#L77) (`_cadence_loop`) and `--cadence-schedule` flag.
- **Missing**: `tests/experiments/` scaffolding (no harness, no schedules, no sweep test). `results/` does not exist. Throughput runner prints a stdout table — no CSV.

Goal of this change set: stand up a centralized `results/` directory, emit
wide-format CSV from both experiment streams, and add a small post-processing
script that derives long-format CSV for external graphing services (Vega,
Observable, Plotly, ggplot, etc.).

## Output layout

```
results/                                # new, gitignored
├── sweep/                              # ExperimentHarness time-series
│   ├── <UTC-iso>__sweep.csv            # wide, one row per poll_interval_s
│   └── <UTC-iso>__sweep_schedules.json # captured proxy+cadence schedule
├── throughput/                         # benchmark grid
│   └── <UTC-iso>__throughput.csv       # wide, one row per (mode, condition)
└── long/                               # derived, post-processed
    ├── <UTC-iso>__sweep.long.csv
    └── <UTC-iso>__throughput.long.csv
```

`results/` and all subdirs are created on first write. Add `results/` to
[.gitignore](.gitignore).

## CSV schemas

### Wide — sweep (`results/sweep/*.csv`)

Per [docs/ExperimentHarness.md](docs/ExperimentHarness.md):

```
t_s, throttle_bps, loss_pct, extra_latency_ms, cadence_n,
chnk_bytes_sent, embd_bytes_sent, cstr_bytes_recv,
embd_rate_hz, chnk_rate_hz, advice_rate_hz
```

One row per `poll_interval_s` (default 1.0). Source: scrape edge sidecar
`/metrics` for `streambed_sidecar_bytes_sent{tag="CHNK|EMBD"}` and server
sidecar for `streambed_sidecar_bytes_received{tag="CSTR"}`. Rates are
deltas / interval. `throttle_bps` / `loss_pct` / `extra_latency_ms` / `cadence_n`
come from the active schedule segment for that wall-clock `t_s`.

### Wide — throughput (`results/throughput/*.csv`)

```
mode, condition, delay_ms, loss_pct, n_frames, fps, delivery_pct, latency_ms
```

One row per `(mode, condition)` cell of the grid. Source: current
`measure()` return in [tests/throughput/run_throughput.py:179](tests/throughput/run_throughput.py#L179).

### Long — derived (`results/long/*.long.csv`)

Single schema across both experiments — drop straight into any graphing
service that expects tidy data:

```
experiment, run_id, x_name, x_value, series, value
```

- `experiment` = `sweep` | `throughput`
- `run_id` = the `<UTC-iso>` stem of the source file
- `x_name` = `t_s` (sweep) | `condition` (throughput)
- `series` = e.g. `chnk_bytes_sent`, `embd_rate_hz`, `fps`, `delivery_pct`
- `value` = numeric, no units (units in series name where ambiguous)

## Files to create

| Path | Purpose |
|---|---|
| `tests/experiments/__init__.py` | Package marker. |
| `tests/experiments/conftest.py` | Registers `experiment` pytest marker. `experiment_results_dir` fixture returning `<repo>/results/sweep`. Reuses existing `controller_url` from [tests/quic/conftest.py](tests/quic/conftest.py). |
| `tests/experiments/harness.py` | `MetricsPoller` (asyncio): every `poll_interval_s`, GET each sidecar `/metrics`, parse per-tag bytes via the line format already written by `metrics.write()` (see [sidecar/internal/metrics/metrics.go:103-105](sidecar/internal/metrics/metrics.go#L103-L105)). Tracks active schedule segment by wall-clock. Writes wide CSV via `csv.DictWriter`. Also captures the schedules into a sibling JSON for reproducibility. |
| `tests/experiments/schedules/stepped_burst.yml` | Proxy schedule: `0s→500kbps`, `30s→100kbps`, `60s→{50kbps, 2% loss, +80ms}`, `90s→25kbps`. |
| `tests/experiments/schedules/cadence_step.yml` | Advisor schedule: `0s→reply_every_n=1`, `45s→5`, `90s→20`. |
| `tests/experiments/test_interleaving_sweep.py` | One `@pytest.mark.experiment` test: starts the cluster with `docker-compose.yml + docker-compose.varying.yml`, bind-mounts both schedules, runs ~2 min, writes `results/sweep/<UTC-iso>__sweep.csv` + sidecar JSON. |
| `scripts/melt_results.py` | Stdlib-only Python script. Reads any wide CSV under `results/{sweep,throughput}/` and emits its long form into `results/long/<stem>.long.csv`. Idempotent. Invoked manually after a run, or as a pytest teardown hook. |
| `.gitignore` | Add `results/`. |

## Files to modify

| Path | Change |
|---|---|
| [tests/throughput/run_throughput.py](tests/throughput/run_throughput.py) | After the existing stdout table, append each `(mode, cond, fps, delivery, latency_ms)` row to a `csv.writer` writing to `results/throughput/<UTC-iso>__throughput.csv`. Keep the stdout print — it's useful during runs. Add `os.makedirs(results_dir, exist_ok=True)` at start. ~15 lines. |

## Existing functions / utilities to reuse

- [tests/quic/conftest.py](tests/quic/conftest.py) `scrape_metric()` for sidecar `/metrics` parsing — promote to a shared util the new harness can import (don't duplicate the parser).
- [tests/docker_utils.py](tests/docker_utils.py) `DockerComposeManager` — already used by `run_throughput.py`; the sweep test should use the same wrapper for parity.
- [sidecar/internal/metrics/metrics.go:103-105](sidecar/internal/metrics/metrics.go#L103-L105) Prometheus line format — the harness parser keys off this exact shape; no new sidecar work needed.

## Out of scope (intentionally)

- GCP wiring for the sweep (`infra/gcp/docker-compose.worker.experiment.yml`). Land local first; lift to GCP in a follow-up that just adds the compose file and a pytest marker — schema and writer code don't change.
- Soak and multiflow-fairness CSVs (user excluded from scope).
- Actual plot generation. The deliverable is CSVs; the graphing service is external.
- Replacing the stdout table in the throughput runner.

## Verification

End-to-end local run:

```sh
# 1. Sweep
docker compose -f docker-compose.yml -f docker-compose.varying.yml up -d
pytest -m experiment tests/experiments/test_interleaving_sweep.py -v -s
docker compose -f docker-compose.yml -f docker-compose.varying.yml down

# 2. Throughput
python tests/throughput/run_throughput.py

# 3. Derive long form
python scripts/melt_results.py
```

Pass criteria:

- `results/sweep/*.csv` exists with the schema above; `chnk_bytes_sent` rate visibly drops at the 100 kbps step; `embd_bytes_sent` stays roughly flat through all but the deepest throttle; `advice_rate_hz` follows `cadence_step.yml`.
- `results/throughput/*.csv` has 6 rows (2 modes × 3 conditions), columns match schema.
- `results/long/*.long.csv` exists for each wide file, with the 6-column tidy schema.
- Import a long CSV into any external graphing service and produce a plot end-to-end.

Regression — must still pass:

```sh
pytest -m integration_stream
pytest tests/test_dynamic_interleaving.py
go test ./sidecar/...
```
