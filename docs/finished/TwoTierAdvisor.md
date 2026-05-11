# Two-Tier Advisor Experiment

A real workload for StreamBed: a fast policy on the edge plus a heavier
"advisor" on the server that periodically corrects it. The point is to
exercise the existing split-stream pipeline (CHNK = frames, EMBD =
embeddings, server → edge feedback channel) under a workload where each
piece carries semantically distinct information and a network drop has
visible consequences.

## Goal

Show that:

1. Server-side advice meaningfully improves edge agent score on a
   semantic task — the advisor is doing real work, not decoration.
2. Selective drop on the sidecar protects the *important* messages: when
   the network sheds CHNK frames the advisor still arrives and the edge
   still plays acceptably; when it sheds advisor messages the edge falls
   back to its solo policy without crashing.
3. Advisor cadence is a knob: cheaper-but-less-frequent advice still
   wins over no-advice in some regime. This is the "how often does the
   advisor need to land" question and it's the headline benchmark axis.

## Environment: Crafter

[Crafter](https://github.com/danijar/crafter) (Hafner 2021) is a 2D
Minecraft-like survival sandbox built as a benchmark for *semantic*
agent behavior.

Why it fits:
- 64×64 RGB pixel observations — small enough for a tiny VLM to read,
  big enough that StreamBed's frame channel has real bytes to drop.
- 22 explicit achievements (collect_wood, place_table, eat_cow,
  defeat_zombie, …). Aggregate score = log mean unlock rate. Gives a
  scalar metric per run.
- ~17-action discrete space (move, do, sleep, place_*, make_*).
- CPU-only, ~200 steps/sec — full training runs are tractable on a
  laptop and trivially fast on a small GCP VM.
- Standard published baselines (PPO, DreamerV3) — we have a teacher to
  distill from instead of training one from scratch.

What we ruled out:
- **Atari (Pong/Breakout)**: reflex tasks; a VLM has nothing useful to
  add over a 1M-param CNN. Wrong workload for an advisor.
- **MiniGrid/BabyAI**: too small, no pixel richness — the frame channel
  is meaningless.
- **MuJoCo**: proprioceptive (no frames). Defeats the point of
  StreamBed.
- **LIBERO + OpenVLA**: real-world relevant but needs a GPU and the
  7B-param VLA blows the $50 budget. Out of scope for this experiment.

## Edge agent

Small PPO policy, ~1M params, CNN backbone.

Realistic sourcing path: lift PPO from
[stable-baselines3](https://github.com/DLR-RM/stable-baselines3) +
write ~30 lines of Crafter wrapper glue (Crafter implements the gym
API). Train teacher first to convergence, then student via behavior
cloning on teacher rollouts. No pretrained Crafter weights are
reliably available off the shelf — expect to train both nets
ourselves. Total compute budget: a few hours on a small GCP VM.

The agent loop on the edge container:
```
for step in episode:
    obs = env.render()                       # 64x64x3 frame
    feat = encoder(obs)                      # ~256-dim feature
    edge_logits = policy(feat)
    advice = recv_latest_advice_or_none()    # non-blocking; may be stale
    final_logits = edge_logits + alpha * advice if advice else edge_logits
    action = sample(final_logits)
    obs, r, done = env.step(action)
    sidecar_send(CHNK, obs)
    sidecar_send(EMBD, feat)                 # or edge_logits, see below
```

Open question: what exactly does EMBD carry? Two options:
- **Encoder feature** (current StreamBed pattern). Server has to
  re-derive logits from feature. More work server-side.
- **Edge logits + value estimate**. Server sees what the edge thinks
  and corrects it directly. Probably better.

Default: edge logits + value. Decide during impl.

## Advisor (server side)

Two flavors. Ship A first; B is the stretch.

### A. Distilled-teacher advisor (primary)

Server runs a bigger CNN policy — ~10M params, same architecture family
as the edge net but wider/deeper. Trained with PPO to convergence on
Crafter ahead of time (one-off pretraining; can use any published
checkpoint or train ourselves in a few hours).

Per advisor request:
1. Receive frame (or feature, depending on EMBD choice).
2. Forward through teacher.
3. Emit action logits + value as the advice payload.
4. Send back over the sidecar's server→edge control channel as a new
   `ADVS` tag (see Wire Format below).

Cost: ~5–20 ms/forward on CPU, ~1 ms on GPU. Network: ~17 floats per
advice ≈ 100 bytes — trivially small.

Why this first: clean RL semantics, no text parsing, easy to ablate.

### B. SmolVLM captioning advisor (stretch)

Server runs [SmolVLM-256M-Instruct](https://huggingface.co/HuggingFaceTB/SmolVLM-256M-Instruct)
or [Moondream2](https://huggingface.co/vikhyatk/moondream2) as the advisor.

Per advisor request:
1. Receive frame.
2. Prompt the VLM: *"You are advising an agent in Crafter. The
   agent's hunger is X, its inventory is Y. What should it do
   next? Pick one of: move_north, move_east, …, do_attack, sleep."*
2. Parse VLM output → discrete action prior.
3. Send back a one-hot-ish prior as `ADVS`.

Cost: SmolVLM-256M is ~250 ms/inference on CPU, ~50 ms on a small GPU.
That dominates advisor cadence — realistically every 5–20 environment
steps, not every step.

Why second: text→action parsing is fragile, VLM output isn't reliably
structured even with format instructions. Worth doing only after A
proves the pipeline.

Risk: SmolVLM may simply not be smart enough about Crafter to be
useful. Mitigation: also try Moondream2 (~1.8B, slower but stronger);
fall back to Florence-2 grounding ("locate tree, locate cow") +
rule-based action selection.

## Wire format addition: generic user tags

Rather than a one-off `ADVS` tag, add two generic user-data tags so any
StreamBed app — not just this experiment — can plug in:

| Tag             | Direction | Reliability     | Purpose                                       |
|-----------------|-----------|-----------------|-----------------------------------------------|
| CHNK            | e → s     | lossy           | video frame chunk (existing)                  |
| EMBD            | e → s     | reliable        | embedding / logits (existing)                 |
| RATE / FBCK     | s → e     | reliable        | rate / bandwidth control (existing)           |
| **CSTM_LOSSY**    | either    | drop under load | **bulk app data, e.g. extra video, telemetry** |
| **CSTM_RELIABLE** | either    | must arrive     | **small priority app messages, e.g. advice**   |

Two new tags, not one, because the sidecar's policy layer needs to know
whether to drop under bandwidth pressure (mirrors the existing
CHNK/EMBD split). Direction is symmetric: the edge can send a
`CSTM_RELIABLE` to the server too, e.g. "I just unlocked an
achievement, please log it."

The advisor experiment uses `CSTM_RELIABLE` for advice payloads.
Payload is small (~100B), so reliability is cheap. The edge applies
the most recent CSTM_RELIABLE advice and discards stale queued
messages — advisor lateness is bounded by cadence, not queue depth.

This makes StreamBed a real **platform** (any app plugs in via CSTM)
instead of a one-off video+embedding pipeline. The advisor experiment
is the first user of CSTM, not a special case in the protocol.

## Advisor cadence — the headline benchmark axis

The interesting question: *"how often does the advisor need to land for
the edge agent to win?"*

Cadence regimes:
- **N=1** (every step): expensive, near-optimal, baseline upper bound.
- **N=5**: realistic for VLM advisor on CPU.
- **N=20**: very cheap, tests how stale advice can be.
- **N=∞** (no advisor): edge-only baseline.

This is the cleanest plot to put in the writeup: x-axis = advisor
cadence, y-axis = Crafter score. The advisor's *bandwidth × latency*
budget falls out of this plot directly.

## Benchmarks

Five experiments, ordered cheapest-to-run first:

1. **Sanity** — edge-only vs N=1 advisor on a clean network. Calibrates
   the gap. Even if narrow, the rest of the experiments still produce
   useful platform-level signal (CSTM tag, drop tolerance, cadence
   knob).
2. **Cadence sweep** — N ∈ {1, 5, 10, 20, ∞}, clean network, fixed eval
   episodes. Produces the headline plot.
3. **Drop tolerance** — fix advisor at N=5; vary CHNK loss rate via the
   existing throttle proxy from {0%, 10%, 30%, 60%}. Story: edge agent
   degrades gracefully because EMBD + ADVS still arrive.
4. **Advisor loss tolerance** — fix CHNK loss at 0; force ADVS drops by
   throttling the control stream (need a small `tc`/test hook). Story:
   edge falls back to solo policy, score floors at edge-only baseline.
5. **Server failover (stretch)** — kill the advisor server mid-episode,
   confirm edge keeps playing. This is the StreamBed
   controller-failover story applied to a live workload.

Each run: 100 eval episodes, mean Crafter score with 95% CI bars.

## Repository shape

New top-level dir, mirroring `edge/` and `server/`:

```
experiments/advisor/
  edge/
    Dockerfile
    edge_agent.py            # Crafter loop + edge policy + sidecar I/O
    policy_small.pt          # distilled student weights
  server/
    Dockerfile
    advisor_server.py        # receives frames, runs teacher, emits ADVS
    policy_teacher.pt        # 10M-param teacher weights
  shared/
    crafter_actions.py       # action enum (mirrors Crafter)
    advice_proto.py          # ADVS payload schema
  bench/
    run_cadence_sweep.py
    run_drop_tolerance.py
    plot_results.py
  docker-compose.advisor.yml # 1 edge + 1 advisor server + sidecars
  README.md
```

Reuse: `shared/interfaces/stream_interface.py`, the QUIC sidecar, the
controller, the throttle proxy. New code is just the agent loop, the
advisor model, and the ADVS tag plumbing.

## Phased execution

Roughly the order to write things — each phase is meant to be runnable
end-to-end before moving on, so we never have a half-built system.

### Phase 1 — environment + edge solo

- `pip install crafter`, confirm it runs in a Docker container.
- Write `edge_agent.py` skeleton: env loop, random policy, prints score.
- Train or grab a small PPO baseline. Score should be ~5+ on Crafter
  (random is ~1).
- **Exit criterion**: edge container plays Crafter solo, score logged.

### Phase 2 — advisor server (no advice yet)

- `advisor_server.py` running the teacher model, exposed on a port.
- Wire teacher into the Crafter env directly (not through StreamBed
  yet) — confirm teacher score is meaningfully > edge.
- **Exit criterion**: teacher PPO score >> edge PPO score on Crafter.

### Phase 3 — CSTM over the wire

- Add `TagCSTMLossy` and `TagCSTMReliable` constants to
  `sidecar/internal/common/protocol.go`.
- Wire CSTM_RELIABLE through the existing reliable stream path; wire
  CSTM_LOSSY through the datagram + token-bucket policy path.
- Plumb advisor advice as a CSTM_RELIABLE payload (server → edge).
- Edge applies latest advice in its action selection.
- Run cadence sweep on a clean local docker-compose.
- **Exit criterion**: cadence-sweep plot exists. CSTM is now a
  reusable platform primitive — that's a deliverable independent of
  the RL result.

### Phase 4 — drop tolerance benchmarks

- Wire experiments under existing `docker-compose.throttle.yml`.
- Run experiments 3 + 4.
- **Exit criterion**: drop-tolerance plot exists; CHNK-loss curve is
  flatter than ADVS-loss curve (the headline StreamBed story).

### Phase 5 — VLM advisor (stretch)

- Swap teacher CNN for SmolVLM-256M.
- Re-run cadence sweep at realistic VLM-cadence regime (N=5, 10, 20).
- **Exit criterion**: at least one VLM cadence beats edge-only.

### Phase 6 — failover (stretch)

- Use existing controller failover to fail the advisor server mid-run.
- Show edge agent continues playing.
- **Exit criterion**: video / log artifact for the writeup.

## Risks

- **Teacher–student gap is small.** If the 10M-param teacher only
  marginally outperforms the 1M edge net, the cadence-sweep plot will
  be flat. The platform deliverables (CSTM tag, advisor wire path,
  drop-tolerance benchmarks) still land regardless. Negative RL
  result is still a result; the writeup pivots to "platform under
  weak workload signal" rather than "advisor wins big."
- **VLM is too dumb about Crafter.** SmolVLM-256M was not trained on
  game frames. Caption quality may be useless. Mitigation: Phase 5 is
  stretch; Phase 1–4 stand alone with the distilled-teacher advisor.
- **Advisor latency dominates.** If the round trip is a meaningful
  fraction of the env step, "stale ADVS" becomes the default case.
  This actually makes the experiment *more* interesting — staleness
  vs cadence is the same axis. Mitigation: log advisor age per
  application, plot it.
- **Action-space mismatch (VLM advisor only).** VLM emits free text,
  parser maps to one of 17 actions. Build a strict parser + log
  parse-failure rate; treat parse failure as no-advice.

## Open questions

- EMBD payload: encoder feature vs edge logits + value? Decide during
  Phase 3.
- Advisor application rule: additive logit blend (`final = edge +
  α·advice`) vs replacement (`if advice fresh: use advice`). Try blend
  first.
- Advisor cadence: pure step-count (every N) vs uncertainty-triggered
  (when edge entropy > τ)? Pure step-count for the headline plot;
  uncertainty-triggered as a stretch ablation.
