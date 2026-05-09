# QUIC Sidecar — A Walkthrough

A read-from-scratch tour of [sidecar/](../sidecar/) for someone who has never written Go. After this you should know **what each file does, what the data flow is, and where to add a new thing.** ~800 lines of Go total — not big.

---

## 1. Why the sidecar exists

The Python daemon (in [controller/DeploymentDaemon/](../controller/DeploymentDaemon/)) used to send video chunks **directly over UDP** from edge to server. UDP works, but:

- Every packet is unreliable in isolation. There's no congestion control, no loss recovery.
- Mixing reliable control messages (RATE limits, action commands) with lossy video on the same socket is awkward — you end up reinventing TCP-lite.
- TLS over raw UDP doesn't exist out of the box.

QUIC solves all three. It's a UDP-based protocol that gives you:

- **Unreliable datagrams** for video chunks — same "fire and forget" semantics as raw UDP, but with congestion control.
- **Reliable bidirectional streams** for control messages — like a TCP socket living inside the same connection.
- **Mandatory TLS 1.3** for free.

Rather than ripping QUIC support into the Python daemon (which would mean dragging a new C extension or rewriting the hot path), we put a tiny **sidecar** next to each daemon. The daemon stays unmodified — it speaks UDP to `127.0.0.1` like before. The sidecar speaks QUIC to its peer sidecar across the network.

```
daemon (Python) <-- UDP --> sidecar (Go) <-- QUIC --> sidecar (Go) <-- UDP --> daemon (Python)
       edge                        EDGE ROLE                            SERVER ROLE
```

Each sidecar is one Go binary; **the same binary runs in both roles**, the role is picked by the `SIDECAR_ROLE` env var (`edge` or `server`).

The sidecar is spawned and reaped by the daemon itself ([sidecar_supervisor.py](../controller/DeploymentDaemon/sidecar_supervisor.py)), not by docker-compose — one sidecar per daemon, named `streambed-{cluster}-{device_id}-sidecar`.

---

## 2. Data flow at a glance

### Edge side

```
Python daemon            edge sidecar                          peer
                                                               sidecar
  UDP write    ──>  ┌────────────────────┐
  to 127.0.0.1:9050 │  pumpUDPToQUIC     │  ──> QUIC datagram ──> server
                    │   classify magic   │      (CHNK)
                    │   if CHNK → datagram
                    │   if RATE/ACTN → stream      QUIC stream ──> server
                    └────────────────────┘
                    ┌────────────────────┐
  UDP write    <──  │  pumpControlToUDP  │  <── QUIC stream  <── server
  to daemon:9051   └────────────────────┘     (server feedback,
                                              JSON received_bps etc.)
```

### Server side

```
peer sidecar                    server sidecar                  server container

  QUIC datagram      ──>  ┌──────────────────┐  ──> UDP write 127.0.0.1:9000
                          │ pumpDatagramsToUDP │
                          ├──────────────────┤
  QUIC stream (control) ──>│ pumpControlToUDP │  ──> UDP write 127.0.0.1:9000
                          ├──────────────────┤
  QUIC stream (control) <──│ pumpFeedbackToQUIC│ <── UDP from server (feedback)
                          └──────────────────┘
```

The **server inference container is unchanged** — it still binds UDP on `9000` and reads/writes packets like before. The sidecar just terminates the QUIC half and re-emits over loopback UDP.

---

## 3. The wire protocol

The daemon classifies its own outbound packets by a **4-byte magic prefix**. The sidecar peeks at that prefix and decides which QUIC channel to use. See [internal/common/protocol.go](../sidecar/internal/common/protocol.go):

| Prefix | Meaning | Sidecar action |
| --- | --- | --- |
| `CHNK` | Video data chunk | Send as QUIC datagram (unreliable, no retry) |
| `RATE` | Rate-limit message | Send on reliable control stream |
| `ACTN` | Action message | Send on reliable control stream |
| `{` | Legacy JSON feedback (server `received_bps`) | Treat as control until daemon ports it |
| anything else | Unknown | Best-effort: send as datagram |

Each control-stream message is **length-prefixed**: a 4-byte big-endian length, then the payload bytes. This is so the receiver knows where one logical message ends and the next begins, without needing a delimiter byte that could collide with JSON or binary content.

The two sides agree on the **ALPN** label `streambed-quic-v1`. Mismatch = handshake fails. If you ever break wire compatibility, bump this to `v2` and reject old peers.

---

## 4. Code map

```
sidecar/
├── cmd/streambed-quic-sidecar/main.go      ← entry point, flag/env parsing
├── internal/
│   ├── common/protocol.go                  ← magic constants + ClassifyPrefix
│   ├── edge/edge.go                        ← edge-role pumps
│   ├── server/server.go                    ← server-role pumps
│   ├── quictransport/transport.go          ← QUIC connection wrapper (used by both roles)
│   ├── quictransport/devcert.go            ← throwaway self-signed TLS (TODO: real mTLS)
│   ├── policy/policy.go                    ← placeholder for rate-limiting hook
│   └── metrics/metrics.go                  ← Prometheus counters + INFO log line
├── go.mod / go.sum                         ← Go module manifest, like requirements.txt+lockfile
└── Dockerfile                              ← static-binary, scratch-based image
```

What each file actually does:

- **[main.go](../sidecar/cmd/streambed-quic-sidecar/main.go) (75 lines)** — reads flags/env, builds a `Config`, calls either `edge.Run(...)` or `server.Run(...)` based on `SIDECAR_ROLE`. Also starts the metrics HTTP server on `:9100/metrics` and a periodic INFO log line.

- **[edge.go](../sidecar/internal/edge/edge.go) (136 lines)** — listens on local UDP, dials the peer sidecar's QUIC port. Two goroutines: one pumps daemon→QUIC, one pumps QUIC-control→daemon (feedback path).

- **[server.go](../sidecar/internal/server/server.go) (144 lines)** — listens on QUIC, accepts incoming peer connections. Per accepted peer, three goroutines: datagrams→UDP, control→UDP, and a return path UDP→QUIC-control for server feedback.

- **[transport.go](../sidecar/internal/quictransport/transport.go) (217 lines)** — the `Conn` type, which both roles use to call `SendDatagram`/`RecvDatagram`/`SendControl`/`RecvControl`. Handles the dance of opening/accepting the single bidirectional control stream after handshake.

- **[devcert.go](../sidecar/internal/quictransport/devcert.go) (55 lines)** — self-signs an in-memory ECDSA cert at startup. **Throwaway**; real production needs mTLS with rotated certs. There's already a TODO marking this.

- **[policy.go](../sidecar/internal/policy/policy.go) (21 lines)** — interface + a no-op implementation. Future home of "drop this video frame if we're over the rate budget" logic. Right now `Passthrough()` always returns the input unchanged.

- **[metrics.go](../sidecar/internal/metrics/metrics.go) (77 lines)** — atomic counters for sent/received bytes/packets, handshake duration, RTT. Exposes them at `/metrics` (for the test scraper) and as a periodic log line (for ops grep).

- **[common/protocol.go](../sidecar/internal/common/protocol.go) (48 lines)** — the magic-prefix table and `ClassifyPrefix`. Keep this in sync with the Python side ([shared/stream_chunks.py](../shared/stream_chunks.py)) if you ever change framing.

---

## 5. Just enough Go to read this code

You don't need to learn Go properly to make changes here. These are the things you'll see:

### Packages and imports

```go
package edge        // every file declares its package; matches the dir name

import (
    "context"       // stdlib
    "github.com/streambed/sidecar/internal/common"   // local
    "github.com/quic-go/quic-go"                     // third-party
)
```

Local packages are referenced by full path (`github.com/streambed/sidecar/...`). The mapping from import path → on-disk dir is set in [go.mod](../sidecar/go.mod). Don't worry about it; the existing imports are correct.

### Functions and method receivers

```go
func Run(ctx context.Context, cfg Config) error { ... }
//   ^name              ^ params               ^ return type

func (c *Conn) SendDatagram(p []byte) error { ... }
//      ^this is "self" — Conn's SendDatagram method
```

The `*` is "pointer to" — `*Conn` means "pass by reference, not by copy." `[]byte` is "slice of bytes" — same concept as Python's `bytes` for our purposes.

### Multiple return values

Go functions often return `(value, error)` instead of raising:

```go
n, _, err := udp.ReadFromUDP(buf)
if err != nil {
    return err
}
// use buf[:n]
```

The `_` discards a value. The `if err != nil` block is the universal "did this fail" check; Go has no exceptions, so you'll see this *everywhere*.

### Goroutines and channels

```go
go func() { errc <- pumpUDPToQUIC(ctx, udp, conn, cfg) }()
//  ^ this whole thing runs in a new goroutine (lightweight thread)

errc := make(chan error, 2)   // a channel that holds error values, buffer 2

select {
case <-ctx.Done():            // cancellation arrived
    return ctx.Err()
case e := <-errc:             // one of the pumps errored
    return e
}
```

Goroutines are how Go does concurrency. Each pump function runs in its own goroutine and writes its eventual exit error into a shared channel. The `select` block is "wait for whichever happens first." Same shape as Python's `asyncio.gather` with the first-failure pattern.

### Context for cancellation

Every long-running function takes `ctx context.Context` as its first param. `ctx.Done()` returns a channel that closes when the program is shutting down (SIGINT/SIGTERM). Loops that read forever check `ctx.Done()` so they can break out cleanly. You'll thread `ctx` through any function you add.

### Defer

```go
defer udp.Close()
```

"Run this when the function returns, no matter how it returns." Like Python's `with` block but without the indentation. Used for closing files, connections, releasing locks.

### Error wrapping

```go
return fmt.Errorf("quic dial %s: %w", peerAddr, err)
```

`%w` wraps the original error so `errors.Is` upstream can still match it. You'll see this in the transport layer.

### Atomic counters

```go
c.m.DatagramsSent.Add(1)
c.m.DatagramBytesSent.Add(uint64(len(p)))
```

`atomic.Uint64` is a thread-safe counter — multiple goroutines can `.Add()` to it without locks. Used in metrics.

That's 95% of the syntax you need to understand the existing files. If something else looks weird, ask.

---

## 6. Build and run

### Local build

```bash
cd sidecar
go build -o /tmp/streambed-quic-sidecar ./cmd/streambed-quic-sidecar
SIDECAR_ROLE=server QUIC_BIND=:4433 /tmp/streambed-quic-sidecar &
SIDECAR_ROLE=edge PEER_ADDRESS=localhost:4433 /tmp/streambed-quic-sidecar &
```

You don't need to install Go via Homebrew unless you want it locally — the Dockerfile does the build inside `golang:1.22-alpine` and ships a static binary on `scratch`. So `docker build` just works without any local Go toolchain.

### Run via the daemon (production-ish)

In [docker-compose.yml](../docker-compose.yml), set `STREAM_TRANSPORT=quic` on the daemons. They'll spawn sidecars at lifespan startup. Look for the `streambed-default-edge-001-sidecar` container after `docker compose up`.

### Tests

```bash
cd sidecar
go test ./...
```

The only existing unit test is [protocol_test.go](../sidecar/internal/common/protocol_test.go), exercising `ClassifyPrefix`. Integration tests live on the Python side and exercise the full daemon + sidecar pair.

---

## 7. How to add stuff

The most likely changes, with where to make them:

### "I want a new control message type, like DROP"

1. Add a new magic constant in [common/protocol.go](../sidecar/internal/common/protocol.go):
   ```go
   var MagicDROP = [4]byte{'D', 'R', 'O', 'P'}
   ```
2. Add it to the switch in `ClassifyPrefix`:
   ```go
   case MagicRATE, MagicACTN, MagicDROP:
       return KindControl
   ```
3. The transport layer doesn't care — control messages all share one stream and are length-prefixed. Receivers on both ends parse the magic from the payload itself.
4. Add the corresponding emitter on the Python side ([shared/](../shared/)) and a handler on the receiver side.

### "I want to drop frames when over a rate budget"

1. Implement a real `policy.Policy` in [policy/policy.go](../sidecar/internal/policy/policy.go). Replace `Passthrough()` with one that tracks recent CHNK byte volume against a configured ceiling and returns `nil` (= drop) when over.
2. Wire the new policy into `Config.Policy` in [main.go](../sidecar/cmd/streambed-quic-sidecar/main.go) — read the budget from a flag/env.
3. The pump in [edge.go](../sidecar/internal/edge/edge.go) (`pumpUDPToQUIC`) already calls `cfg.Policy.OnEgress(buf[:n])` and skips when it returns `nil` — no transport-layer change needed.

### "I want to surface a new metric"

1. Add a counter to `metrics.Registry` in [metrics/metrics.go](../sidecar/internal/metrics/metrics.go) — `atomic.Uint64`.
2. `.Add(...)` to it from wherever the event happens (transport, edge, server).
3. Add a `fmt.Fprintf` line in `Registry.write` so it shows up in `/metrics`.
4. Optionally add it to `LogLoop` if you want it grep-able in stdout.

### "I want real TLS instead of self-signed dev certs"

This is the deferred [devcert.go](../sidecar/internal/quictransport/devcert.go) TODO. The shape:

1. Generate per-device certs out of band (controller signs, daemon mounts).
2. Replace `DevTLSConfig(...)` with a function that loads cert+key from disk paths passed via env.
3. Set `RootCAs` on the client side and `ClientCAs` + `ClientAuth: tls.RequireAndVerifyClientCert` on the server.

Bigger lift. Sequence it after device-auth lands on the controller side so the certs come from a single source of truth.

### "I want to support multiple peer connections per server-side sidecar"

Already supported — `server.go` uses `ListenAll` and accepts forever. The peer fan-out happens at the transport layer; each accepted connection runs in its own goroutine. You'd only need new code if you want to *route across* peers (e.g. relay between two edges via the server sidecar) — at which point you're essentially building a new role.

### "I want to log when a particular packet got dropped"

Easiest place is right where the drop happens. In `pumpUDPToQUIC`:

```go
payload := cfg.Policy.OnEgress(buf[:n])
if payload == nil {
    log.Printf("policy dropped %d bytes", n)
    continue
}
```

Be aware: `log.Printf` from a hot loop will spam. Prefer a counter (see "surface a new metric" above) and only log on configurable thresholds.

---

## 8. Things to be careful of

- **Goroutine leaks.** Every goroutine you spawn must have a path to exit — typically by checking `ctx.Done()` in the loop, or by reading from a channel that gets closed elsewhere. Forgetting this means the goroutine sticks around forever.
- **`buf := make([]byte, 65535)` is per-goroutine.** If you share a buffer across goroutines, you get races. Either `make` a new one inside the goroutine or copy the data out before sending it down a channel.
- **Datagrams over QUIC have a size cap.** `MaxDatagramPayload = 1300` in [common/protocol.go](../sidecar/internal/common/protocol.go). The Python side already chunks at this boundary; if you bypass `CHNK` framing you have to chunk yourself.
- **Don't add stdlib `log.Fatalf` calls deep in pumps.** It calls `os.Exit(1)` and skips defers. Bubble errors back through `errc` instead.
- **The control stream is one stream, both directions, length-prefixed.** Don't open new streams casually — the sidecar's design assumes exactly one bidirectional control stream per peer connection.

---

## 9. Quick reference: file → most-likely reason to touch it

| Goal | File |
| --- | --- |
| Add a packet kind | `internal/common/protocol.go` |
| Change edge-side dispatch | `internal/edge/edge.go` |
| Change server-side dispatch | `internal/server/server.go` |
| Tweak QUIC parameters (timeouts, datagrams enable) | `internal/quictransport/transport.go` |
| Replace dev TLS | `internal/quictransport/devcert.go` |
| Add rate-limiting / drop policy | `internal/policy/policy.go` |
| Add a metric / log line | `internal/metrics/metrics.go` |
| Add a flag / env var | `cmd/streambed-quic-sidecar/main.go` |

When you've decided what you want to add, point at the table above to figure out which file gets opened, then jump to §7 for the recipe.
