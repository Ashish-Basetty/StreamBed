# Capstone Task List and Goals

## Core Features
- [ ] Verify embedding streaming through full deployment tests
- [ ] Handle new device authentication
- [ ] Verify adaptive streaming rate, optimize for round-trip latency on inference channel
- [ ] Build out request router, so user can hit any router container and its routed to appropriate controller. Ideally with a master routing table/replicas and some consensus algorithm to recover on failure
- [✔️] Device management portal! Should show specific device cluster along with device statuses. Should provide a frontend interface, including docker model updates, and a model versioning management page.

## Testing/Infrastructure
- [ ] Identify best (and cheapest!) cloud provider/platform to test performance on
- [ ] Automated deployment tests on dedicated infrastructure

## Optimizations
- [ ] Measure e2e round-trip latency
- [ ] Rewrite core packet rerouting code in Go
- [ ] Cross-region testing
- [ ] Migrate to QUIC? Or custom lightweight QUIC protocol?

## Real Inference System
- [ ] Setup edge reactive model
- [ ] Use model predictive control + server-based model