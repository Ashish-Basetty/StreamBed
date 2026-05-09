# Daemon as Lifecycle (Systemd-shape)

This doc captures the trajectory the daemon and sidecar are on, what's already
shipped in that direction, and what's left to do. Written near the end of a
working session — author's reasoning is preserved inline because we may not
revisit this for a while.

## North star

The **daemon** becomes a pure control-plane / lifecycle component. It does
NOT touch data bytes. The **sidecar** owns the entire data plane.

Mental model: the daemon is to the inference container + sidecar what
**systemd** is to a managed unit. Boots it, restarts it, signals it, publishes
config to it. That's it.

### Sidecar (Go) owns

- Chunking outbound (CHNK / EMBD).
- Tag dispatch (CHNK / EMBD / RATE / ACTN / FBCK).
- Rate enforcement (drop CHNK only; EMBD + control never drop).
- Bandwidth estimation (local sampling + remote feedback via FBCK).
- Reconnect on peer change.
- TLS, congestion control, etc. — already does these via QUIC.

### Daemon (Python) owns

- Spawn / manage the inference container.
- Spawn / manage the sidecar.
- Heartbeats / register / deregister with the controller.
- Lifecycle HTTP: `/deploy`, `/delete`, `/restart`, `/health`, `/status`.
- Config publication via files — notably `/config/stream-target.json`.

That's it. No chunking, no forwarding, no rate logic, no estimators.

## What's already shipped (don't redo)

These have all landed and are passing tests:

| layer | thing |
| --- | --- |
| Wire (Go) | `TagCHNK`, `TagEMBD`, `TagRATE`, `TagACTN`, `TagFBCK` constants. `PacketKind` distinguishes `KindLossyData` (CHNK), `KindLosslessData` (EMBD), `KindControl`, `KindUnknown`. |
| Wire (Python) | `CHUNK_MAGIC`, `EMBED_MAGIC`. `make_chunks(payload, stream_id=None, tag=CHUNK_MAGIC)` accepts a tag. |
| Sender split | A `StreamFrame` with both `frame` and `embedding` becomes two wire messages — frame-only (CHNK) and embedding-only (EMBD). Each gets its own `stream_id`. Both `StreamBedTCPSender` and `StreamBedUDPSender` do this via `_split_for_wire`. |
| Receiver | `_RecvProtocol.datagram_received` accepts both magics through the same reassembly logic. Server gets two `StreamFrame` objects per logical edge frame. |
| Sidecar bandwidth | `SamplingBackend` (samples `DatagramBytesSent`), `RemoteBackend` (driven by FBCK), `Composite(min)`. Edge wires them as `Composite(SentRate, RemoteRate)` for the policy. |
| Sidecar policy | Token-bucket. Drops only `KindLossyData`. Everything else passes but drains the bucket — keeps the rate cap honest. |
| Server feedback producer | Per-peer Go goroutine samples `DatagramBytesRecv` every 2s, EWMA-smooths, sends `FBCK + uint64 BE bps` over the control stream. |
| Edge feedback consumer | Reverse-direction control pump parses FBCK frames and updates `RemoteBackend`. RATE / ACTN dispatch slots reserved. |
| Cleaned up | All the `received_bps` JSON UDP plumbing, `ServerFeedbackBackend`, the daemon's feedback callback path. Gone. |

Tests cover all of the above (`go test ./...` and the Python unit suite).

## What's still to do — daemon retirement (its own PR)

This is the actual delta to ship next. Don't bundle with anything else; it's a
topology change with non-trivial blast radius.

### Step 1 — chunking moves to the sidecar (not to the inference container)

Earlier drafts of this doc had chunking moving "into the inference
container's sender." That's wrong. It just relocates a concern that
shouldn't live in the inference container in the first place. The
inference container's responsibility is `frame in → embedding out`,
nothing else.

Current path:
```
edge inference container  ──TCP──▶  daemon  ──chunks→UDP──▶  sidecar
```

Target path:
```
edge inference container
  ├─ ──UDP──▶  sidecar :video_port   (raw frame bytes, pipe-through)
  └─ ──UDP──▶  sidecar :embed_port   (embedding bytes from local model)
                              │
                              ▼
                  sidecar chunks + tags + ships
                  (CHNK on video_port, EMBD on embed_port)
```

The **port is the type tag**. Sidecar binds two local UDP listeners:

- `LOCAL_VIDEO_BIND` (e.g. `127.0.0.1:9050`) — incoming bytes get the
  CHNK tag, are eligible for drop under bandwidth pressure.
- `LOCAL_EMBED_BIND` (e.g. `127.0.0.1:9051`) — incoming bytes get the
  EMBD tag, never dropped.

The inference container does not import `make_chunks`, does not know
about magic bytes, does not implement framing. It only knows two
`socket.sendto` destinations.

#### Local wire shape between container and sidecar

Each UDP datagram from the container is a serialized **half-populated
`StreamFrame`** — same `serialize_stream_frame` we use today, but with
exactly one of `frame` or `embedding` populated:

- `LOCAL_VIDEO_BIND` receives `StreamFrame(frame=raw, embedding=None, …)`.
- `LOCAL_EMBED_BIND` receives `StreamFrame(frame=None, embedding=emb, …)`.

The sidecar treats each datagram as opaque bytes and chunks it. The
**server-side receiver** still parses `StreamFrame` to pull out
timestamp / source_device_id / payload — that hasn't changed and stays
the right correlation key for matching the two halves on the receiving
end.

Why keep `StreamFrame` for the local hop instead of inventing a new
slim header: zero new code on the deserialize side, and the metadata
fields (timestamp, source_device_id, model_version) are exactly what
the server needs to correlate. The local-hop overhead of a few bytes
of header per datagram doesn't matter at this rate.

#### Inference container loop (target shape)

```python
for frame, ts in video_source:
    emb = model(frame)                                # only "real work"
    video_sock.sendto(serialize_video_half(frame, ts))
    embed_sock.sendto(serialize_embed_half(emb, ts))
```

`serialize_video_half` / `serialize_embed_half` are one-line wrappers
around `serialize_stream_frame` with the appropriate field None'd out.
~10 lines of shipper code total in the container.

#### Changes in detail

- **Inference container** (`edge/app.py`):
  - Drop `StreamBedTCPSender` import.
  - Add a tiny shipper module (`edge/shipper.py`?) that owns two UDP
    sockets and exposes `ship_video(frame_bytes, ts)` and
    `ship_embedding(emb_bytes, ts)`.
  - Container's main loop becomes the snippet above.
- **Sidecar (Go)**:
  - Add a second UDP listener. `edge.Config` gets `LocalVideoBind` and
    `LocalEmbedBind` instead of one `LocalUDPBind`.
  - Two `pumpUDPToQUIC` goroutines, one per listener, each parameterized
    with the tag it should apply (`TagCHNK` or `TagEMBD`).
  - **Chunking moves into the sidecar.** Today the sidecar receives
    pre-chunked datagrams from the daemon and just forwards them. After
    this change, it receives full payloads from the container and does
    the chunking itself before `conn.SendDatagram`. Use the existing
    chunk format (`tag + stream_id + i/n/data_len + data`) — same wire,
    just produced one side closer to the egress.
  - Drop the `_split_for_wire` indirection on the Python side (no longer
    needed; container produces already-half-shaped messages).
- **Daemon** (`control-plane/DeploymentDaemon/`):
  - Delete `stream_proxy_manager.py` entirely.
  - Delete `_run_stream_tcp_server`, `_UDPSendOnlyProtocol`,
    `_bandwidth_poll_loop`, `should_drop_video_frame`. Roughly 200 lines.
  - `daemon_config.py` gets the new `LOCAL_VIDEO_BIND` / `LOCAL_EMBED_BIND`
    pair (passed through to the sidecar at spawn).
  - `tcp_utils.py` goes away.
- **Shared (Python)**:
  - `shared/stream_chunks.py` (`make_chunks`, `CHUNK_MAGIC`, `EMBED_MAGIC`,
    `CHUNK_SIZE`) becomes Go-only. The Python file can stay for the server
    side's reassembly logic, but the *outbound* chunker has no callers in
    Python anymore.
  - `_split_for_wire` and the senders' two-message logic in
    `shared/interfaces/stream_interface.py` become unnecessary on the
    egress side. The code can stay until the inference container is
    fully cut over; then drop it.

#### What this buys

- Inference container stays minimal: it doesn't know what QUIC is, what
  CHNK is, what bandwidth means.
- The sidecar is the single owner of "how data goes on the wire." Adding
  a third payload kind later (e.g. heartbeats, telemetry) is a port +
  tag pair, not three separate refactors.
- The "video pipe through" property is preserved by construction: the
  inference container forwards raw bytes on the video port without
  transformation — same bytes that came out of the camera, plus a
  metadata header.

### Step 2 — stream-target via file (single ingress, file-as-API)

The daemon stays the **single ingress** for controller-driven config. It
does NOT relay to the sidecar via RPC — it writes a config file that the
sidecar polls. Pattern carries forward to any future per-sidecar config
("sidecar-rate-floor", "sidecar-allowed-origins", etc.).

#### Path

`<daemon-data root>/config/stream-target.json` — a subdirectory of the
daemon's existing host-mounted data dir (e.g. `./daemon-data/<id>/`).
**Reuse the same volume** for the sidecar; mount it into both containers.
No new named volume.

#### Format

```json
{"target_ip": "...", "target_port": 4433}
```

#### Atomic writes (daemon side)

Writing to a file with normal "open + write + close" is not atomic.
While the daemon is mid-write, the sidecar's polling read might catch a
half-written file → JSON parse error → that update is effectively lost
until the next write (which polling would eventually catch, but ugly).

Fix: the standard **temp + rename** pattern. POSIX `rename()` IS atomic
at the filesystem level — the new file replaces the old in a single
indivisible operation. The reader always sees either the *previous
complete* file or the *new complete* file, never a partial mix.

Idiom (Python):

```python
def write_stream_target(path: str, target_ip: str, target_port: int) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump({"target_ip": target_ip, "target_port": target_port}, f)
    os.rename(tmp, path)  # atomic on POSIX
```

One helper, used by every writer. Reader needs no defensive logic.

#### Polling (sidecar side)

A background Go goroutine reads the file every N seconds — start at 1s.
If the parsed `(ip, port)` differs from current state → trigger reconnect
(see Step 3).

Why polling over fsnotify:
- No external dependency.
- A missed update is self-correcting — next poll catches it.
- Failover latency target ("noticeable seconds") is well within polling
  cadence.
- Atomic writes mean every poll either succeeds cleanly or sees the old
  file. There's no "transient inconsistent state to wait out."

If polling cadence becomes a complaint later, swap to fsnotify behind the
same internal interface. Not now.

#### Bootstrap

**File-only.** The daemon writes `stream-target.json` *before* spawning
the sidecar. Sidecar reads the file at startup. No env-var fallback. The
single source of truth means no "what if env and file disagree" branch.

Encapsulate the ordering in the daemon's spawn path so the invariant is
mechanically enforced:

```python
def spawn_sidecar(...):
    write_stream_target(config_path, ip, port)   # atomic, must succeed first
    docker_client.containers.run(SIDECAR_IMAGE, ...)
```

If `write_stream_target` raises, the sidecar never spawns.

### Step 3 — reconnect semantics on peer change

QUIC connections bind to a single peer. Switching peers means a new
dial. There's no in-place reconfigure.

#### `connect_new_peer` — one helper, used everywhere

Both the initial connect and the file-watcher-driven switch go through
the same function. If a connection already exists, tear it down; dial
the new peer.

Skeleton:

```go
type peerSwitcher struct {
    mu     sync.Mutex
    cur    *quictransport.Conn   // currently live connection
    cancel context.CancelFunc    // cancels pumps tied to cur
}

// connect_new_peer dials addr and atomically swaps it in as the active
// connection. Used both for first-connect and for file-watcher-triggered
// switches. If a previous connection exists, its pumps are cancelled and
// it's closed.
func (s *peerSwitcher) connect_new_peer(parent context.Context, addr string, tlsCfg *tls.Config, m *metrics.Registry) error {
    newConn, err := quictransport.Dial(parent, addr, tlsCfg, m)
    if err != nil {
        return err
    }
    pumpCtx, cancel := context.WithCancel(parent)
    // start pumps on newConn here, bound to pumpCtx
    // ...

    s.mu.Lock()
    oldConn := s.cur
    oldCancel := s.cancel
    s.cur = newConn
    s.cancel = cancel
    s.mu.Unlock()

    if oldCancel != nil {
        oldCancel()  // pumps exit
    }
    if oldConn != nil {
        oldConn.Close()
    }
    return nil
}
```

In `edge.Run`:

- Call `connect_new_peer(ctx, initial_addr)` once at startup.
- Spawn a goroutine that polls `stream-target.json`. On change, call
  `connect_new_peer(ctx, new_addr)`.
- The egress pump (`pumpUDPToQUIC`) reads `s.cur` under the mutex (or via
  an atomic.Pointer if we want to avoid lock contention on the hot path)
  and writes through that.

#### Hot swap (the "two concurrent connections" idea)

Yes — Go's `*quictransport.Conn` is just a pointer; holding two side by
side is trivial. The hot-swap pattern overlaps the new connection with
the old to avoid losing in-flight datagrams during a switch:

1. File watcher detects a change → `new_addr`.
2. Dial the new peer **in the background**. Don't touch the old conn yet.
3. When the new dial succeeds, atomically swap which conn the egress
   pump writes to (`s.cur = newConn`).
4. Mark the old conn as **draining**: stop sending new bytes through it,
   but keep the receive-side pump alive briefly so any in-flight FBCK
   from the old peer is processed.
5. After a short grace window (a couple seconds), close the old conn.

Trade-off: ~1 RTT of overlap, slightly more code, but no in-flight loss
on the egress side during a clean switch. Worth it when the new server
is already up before the old fails.

**Recommendation: ship the simple version first.** Dial new, swap, close
old. No overlap. Anything mid-flight on the old conn is dropped — but
for the "server died" case, nothing was getting through anyway. Add the
hot-swap overlap once you see real cases where the new server is alive
before the old one fully fails. It's an optimization, not a correctness
fix.

### Step 4 — drop non-QUIC mode entirely

`STREAM_TRANSPORT=udp` (the legacy, no-sidecar path) goes away. With the
daemon out of the data path, no one is left to read `stream-target.json`
and route raw UDP. Rather than retrofit the inference container or build
a sidecar bypass mode, just drop non-QUIC.

For comparison testing, pull old commits — the pre-QUIC code is preserved
in git history.

Files affected:
- `daemon_config.STREAM_TRANSPORT` — remove the variable, hardcode QUIC behavior.
- Anywhere checking `if STREAM_TRANSPORT == "quic"` — collapse to the single branch.
- `docker-compose.yml` — drop the `STREAM_TRANSPORT` env on every daemon.
- Any documentation references.

## Final daemon shape

```
control-plane/DeploymentDaemon/
├── main.py                  # FastAPI: /deploy, /delete, /restart,
                             # /status, /health, /stream-target.
                             # lifespan: register, write target file,
                             # spawn sidecar+container, heartbeats,
                             # deregister.
├── daemon_config.py         # env var loading
├── sidecar_supervisor.py    # spawn_sidecar / kill_sidecar (docker-py)
├── stream_target.py         # NEW: write_stream_target(path, ip, port)
                             # — atomic, single helper, used everywhere
                             # config is written to disk for the sidecar.
└── (no stream_proxy_manager, no tcp_utils, no bandwidth)
```

~100 lines of FastAPI + supervisors. Genuinely systemd-shaped.

## Final sidecar shape (additions over what's shipped)

```
sidecar/internal/
├── streamtarget/            # NEW: file polling
│   ├── streamtarget.go      # type-safe load, atomic-read
│   └── watcher.go           # poll loop -> emits new (ip,port) on change
├── chunker/                 # NEW: move chunk format from Python to Go
│   └── chunker.go           # tag + stream_id + i/n/data_len + data
├── edge/
│   └── edge.go              # two UDP listeners (video / embed),
│                            # uses peerSwitcher, spawns watcher goroutine,
│                            # calls chunker on each ingress datagram before
│                            # SendDatagram
└── (no quic-side wire format changes — already done)
```

The watcher goroutine emits onto a channel; `edge.Run` consumes from it
and calls `connect_new_peer` per event. Single owner of the connection
lifecycle.

The two UDP listeners each run their own `pumpUDPToQUIC` parameterized
with the tag (`TagCHNK` for the video listener, `TagEMBD` for the embed
listener). Both share a single `quictransport.Conn`. The policy applies
to both — but only the video listener's traffic is droppable
(`KindLossyData`), so embeddings always pass.

## Failure modes after the refactor

| failure | today | after |
| --- | --- | --- |
| Daemon crashes mid-forwarding | data lost in flight | no data path on the daemon — nothing to lose |
| Daemon crashes between deploys | new deploys queue, no data effect | same |
| Sidecar crashes | data path dies | data path dies; daemon restarts sidecar (with supervision) |
| Inference container crashes | data path dies | data path dies; daemon restarts container |
| Controller pushes new stream-target while sidecar mid-handshake | atomicity unclear | atomic file write + polling: next poll picks up the new value |
| `stream-target.json` missing | n/a | sidecar fails fast at boot (file is bootstrap) |
| `stream-target.json` unparseable mid-run | n/a | sidecar logs and keeps using last good target |
| Hot-swap: new peer down | retries old until file changes again | dial fails, log, keep using old conn until next file change |

## Migration order

If shipping incrementally:

1. **Land the file-as-config infra first.** Write `stream_target.py`,
   make the daemon write the file at sidecar spawn time, even though
   nothing reads it yet. Verify the file shows up in the volume.
2. **Add the sidecar-side polling + `connect_new_peer`.** Sidecar still
   uses the env var for first connect; file watcher kicks in as a
   secondary update path. Verify peer changes propagate.
3. **Switch sidecar bootstrap to file-only.** Remove env-var fallback.
   Daemon's spawn function writes the file synchronously before docker run.
4. **Drop non-QUIC.** Collapse all `STREAM_TRANSPORT` branches.
5. **Move chunking into the sidecar; container becomes a simple
   shipper.** Sidecar gains a second UDP listener so port = type tag.
   Inference container writes raw frame bytes to the video port and
   embedding bytes to the embed port via two `socket.sendto` calls —
   no chunking, no magic-byte awareness. Sidecar absorbs the chunk
   format. Delete `stream_proxy_manager`, the daemon's TCP server, and
   the Python-side `_split_for_wire` helpers.

Each step is independently shippable. The blast radius widens as you go;
4 and 5 are the ones that actually take traffic out of the daemon's path.

## Things deferred (don't conflate with this work)

- **Per-peer metrics in the sidecar.** Today's `metrics.Registry` is shared
  across all peer connections on the server side. For true per-peer FBCK
  rate accuracy we'd need `ln.Accept(ctx, nil)` and a Registry held by
  each `Conn`. Worth doing when more than one edge connects to a single
  server, not before.
- **Inference-container-side awareness of CHNK drops.** The edge has no
  way to know its CHNKs are being dropped by the policy. Could surface
  via a polled sidecar counter or a control-stream message. Useful for
  edge-side adaptive frame rate; not needed for correctness.
- **mTLS replacing the dev TLS cert.** Already a TODO in [devcert.go](../sidecar/internal/quictransport/devcert.go).
  Sequence after device auth lands on the controller side.
- **Per-endpoint timeouts on the router proxy.** 30s default; some endpoints
  (`/deploy`) might want longer for cold-pull.

## Glossary (for re-onboarding after a long pause)

| term | meaning |
| --- | --- |
| **CHNK** | Wire tag for raw video data. Lossy: sidecar policy may drop these to enforce a rate cap. |
| **EMBD** | Wire tag for embeddings (inference output). Lossless: never dropped by policy. |
| **RATE / ACTN** | Wire tags for downstream→upstream control directives. Reserved; no producers yet. |
| **FBCK** | Wire tag for upstream→downstream telemetry (server-reported received_bps). 4-byte tag + 8-byte BE uint64 payload. |
| **`KindLossyData` / `KindLosslessData` / `KindControl` / `KindUnknown`** | Sidecar's `PacketKind` enum. Channel routing: Lossy + Lossless → datagrams; Control → reliable stream. Drop policy: only Lossy. |
| **`stream-target.json`** | File the daemon writes to publish the QUIC peer address; sidecar polls. |
| **`peerSwitcher` / `connect_new_peer`** | Sidecar's connection lifecycle owner. One function for both initial dial and reconnect on file change. |
