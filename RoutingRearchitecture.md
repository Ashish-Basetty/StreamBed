# Routing Rearchitecture

Currently, the setup for routing is a bit jank. Like we have a greedy routing algorithm. No point in doing major optimizations on a per cluster basis tbh (like round robin store), rerouting should be relatively uncommon.

## Current Setup

Routing decisions live in the controller (`controller/ControllerNode/main.py`). When a device calls `POST /register`, the controller writes a row to the SQLite `routing` table and that's it — no immediate push to the daemon.

**Edge registers:**
- `_assign_edge_to_least_loaded_server` counts existing routes per server, picks the minimum
- If no servers exist, silently returns — no routing row is written, edge is left untracked

**Server registers:**
- `_assign_unrouted_edges` finds edges with *no routing row* and assigns them to the new server
- Edges that register before any server still get assigned correctly when a server joins — `_assign_unrouted_edges` queries `devices` (not `routing`), so they're found and picked up

**Route delivery to daemons:**
- `health_monitor.py` runs `_sync_stream_targets_from_routing` every 30 seconds
- Sends `PUT /stream-target` to each edge daemon with the target IP/port
- On server failover, routes are pushed immediately — but on new registration, the daemon waits up to 30s

**Daemon side:**
- Daemon writes the target to `/data/stream-target.json` on shared volume
- Edge container polls that file every 2 seconds and updates `StreamProxyManager`
- `StreamProxyManager.forward_frame` drops frames if `ip is None`

**Problems:**
- Routing changes on registration aren't pushed immediately — up to 30s delay
- No explicit "no server available" state — edge just has a stale or missing target with no clear signal