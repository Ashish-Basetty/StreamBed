# Sidecar Unified-Port Refactor (deferred)

A future refactor that collapses the four UDP ports between an inference
container and its sidecar into one bidirectional UDP socket per side. Today
the wiring is asymmetric: app dials sidecar on port A for outbound, sidecar
dials app on port B for inbound. This doc proposes replacing both flows with
a single connected UDP socket, where the sidecar replies to the source
address it last saw.

This is a quality-of-life refactor, not a correctness fix. Phase D works
without it.

## Today (after Phase D)

```
                 ┌──────────────── inference container ────────────────┐
                 │                                                     │
features (UDP)   │  StreamBedUDPSender    →   dials  →  sidecar:9050   │
                 │  StreamBedUDPReceiver  ←   listens 0.0.0.0:9102     │
                 │                                                     │
                 └─────────────────────────────────────────────────────┘
                                 │                       ▲
                                 ▼                       │
                       ┌──────── sidecar ────────────────────┐
                       │  LOCAL_UDP_BIND       0.0.0.0:9050  │
                       │  LOCAL_RECV_UDP_TARGET edge-001:9102│
                       └─────────────────────────────────────┘
```

Two ports each side. Two configs (`SIDECAR_FEED_PORT`, `ADVICE_LISTEN_PORT`,
`LOCAL_UDP_BIND`, `LOCAL_RECV_UDP_TARGET`). Daemon has to attach a docker
network alias = `DEVICE_ID` to every inference container so the sidecar can
resolve it for the reverse-path dial.

## Proposed (unified port)

```
                 ┌──────── inference container ────────┐
                 │                                     │
                 │  one bidirectional UDP socket       │
                 │  dial sidecar:9050; recv on same    │
                 │                                     │
                 └────────────────┬────────────────────┘
                                  │
                       ┌────────────────────────────────┐
                       │  sidecar listens 0.0.0.0:9050  │
                       │  reply uses recvfrom() addr    │
                       └────────────────────────────────┘
```

One port per side. App-side socket sends features and receives advice on the
same FD. Sidecar's outbound reply addr is whatever it last saw inbound.

### Concrete changes

**Python interface (`shared/interfaces/stream_interface.py`)**

- `StreamBedUDPSender` is dial-only; `StreamBedUDPReceiver` is listen-only.
  These need to merge into one duplex class — call it `StreamBedUDPDuplex` —
  that owns a single `asyncio.DatagramTransport` and exposes both `send()` /
  `send_custom()` and `recv_one()` / `recv_custom()`. The existing send/recv
  classes can stay as facades that wrap a `Duplex` until callers migrate.
- Receive path on the connected socket: when the protocol is constructed
  with `remote_addr=...`, only datagrams from that peer are delivered; that
  matches what we want (sidecar is the only thing on the other end).

**Sidecar Go (`sidecar/internal/edge`, `sidecar/internal/server`)**

- Remove `LocalRecvUDPTarget` (edge) and the dialed-out `recvOut` socket.
  Replace with: per-message `WriteToUDP(payload, lastSrcAddr)` on the same
  UDP socket that received the inbound packets.
- Track last source addr in a `sync/atomic.Value` updated by
  `pumpUDPToQUIC` on each inbound read. The reverse pump
  (`pumpControlIntoBandwidth` for non-FBCK msgs on the edge,
  `pumpDatagramsToUDP` on the server) reads the addr atomically and writes
  back on the same FD.
- Server role: same idea. `LocalUDPBindAddr` becomes the single port; the
  app dials in, sends advice, and gets nothing back today, but if we ever
  add server→app messages it's trivial.

**Daemon (`control-plane/DeploymentDaemon`)**

- Drop `SIDECAR_RECV_PORT` and `SIDECAR_SERVER_REVERSE_BIND_PORT` from
  `daemon_config.py` — both halves are now the same port as the existing
  forward port.
- Drop `ADVICE_LISTEN_PORT` and `FEED_LISTEN_PORT` from the inference
  container env. The app no longer binds an inbound listener.
- Drop the `DEVICE_ID` network alias for edge inference containers — the
  sidecar no longer needs to resolve them by name. (Server containers
  still need it for the forward-path dial.)

### Net surface-area delta

| Surface             | Before | After |
|---------------------|--------|-------|
| Sidecar env vars    | 4 new  | 0 new |
| Inference env vars  | 3 per role | 1 per role (`SIDECAR_HOST:PORT`) |
| Docker aliases      | edge + server | server only |
| Python classes      | Sender + Receiver | Duplex (or Sender/Receiver as facades) |

## Why we didn't do this in Phase D

1. **Multi-app risk.** Source-addr-as-reply-route works cleanly with one
   app per sidecar. Two apps dialing the same sidecar — say, an edge
   sidecar serving two inference containers concurrently — break the
   model: there's no way for the sidecar to know which one a given advice
   msg is for. Today we have one app per sidecar by daemon construction,
   but the protocol doesn't enforce it. The Phase D plumbing is uglier
   but doesn't restrict topology. The unified-port design either restricts
   to 1:1 or needs a per-app routing key in the message, which is a wire
   change.
2. **Stateful sidecars.** "Remember the last app source addr" is per-peer
   state the sidecar would have to manage: age-out on silence, re-learn
   on app restart, atomic update. Today the sidecar is fully stateless
   w.r.t. local peers — the address is config, refreshed by Docker DNS.
3. **Sender/Receiver split is load-bearing for tests.** The whole QUIC
   integration test fixture spawns Sender and Receiver on opposite ends.
   Merging them into Duplex changes ~12 test fixtures; would also force
   `_split_for_wire` semantics out into the duplex layer.
4. **Phase D was already 5 components of work.** Sidecar Go change,
   daemon update, compose path fix, two Dockerfiles, an overlay. Adding
   a Python interface refactor on top would have stretched a single
   feature into a multi-week change. The asymmetric design ships now,
   the unified design ships later if/when 1:1 enforcement turns into
   actual policy.

## When to revisit

- If we find ourselves writing connection-tracking logic in the sidecar
  for any other reason (multi-tenancy, per-app rate limits, per-app drop
  policy), unified-port comes along almost for free.
- If the daemon's port plumbing keeps growing (Phase F throttle?),
  collapsing it now to one port per direction is worth the refactor.
- If we ship the Python SDK externally — fewer ports for users to wire
  up is a real DX win.

## Out of scope for this doc

- The QUIC sidecar's *peer* identity (one QUIC connection per peer
  sidecar) is already correct and doesn't change.
- Bidirectional FBCK lives on the QUIC control stream and is unaffected.
- The inter-sidecar protocol is unchanged; this refactor only touches the
  sidecar↔local-app boundary.
