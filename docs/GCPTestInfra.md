# GCP Test Infrastructure Plan

Goal: a five-VM test cluster on Google Cloud — 1 controller, 2 servers, 2 edges — sharing a private network, used for end-to-end deployment and routing tests beyond what `docker compose` on a laptop covers.

Budget context: $50 in GCP credits, college-student wallet. The plan is built around staying under that with a comfortable margin.

## TL;DR cost answer

Not prohibitive. Three operating modes:

| mode | monthly cost | how long $50 lasts |
| --- | --- | --- |
| burst-only (start VMs for test sessions, stop afterward) — 8h/day × 20 days | ~$11 | ~4–5 months |
| always-on, smallest viable instances | ~$42 | ~5 weeks |
| always-on, all spot/preemptible VMs | ~$5–8 | ~6+ months |

Default to **burst-only**. You don't need the cluster running while you sleep.

## Topology

```
         GCP project: streambed-test
                  │
          VPC: streambed-vpc (single subnet, us-central1)
                  │
   ┌──────────────┼─────────────────────────────────────┐
   │              │                                     │
controller-01   server-01   server-02   edge-01     edge-02
e2-micro        e2-small    e2-small    e2-micro    e2-micro
external IP     internal    internal    internal    internal
   │              ▲           ▲           │           │
   │              └───────────┴───────────┴───────────┘
   │                 stream traffic over internal IPs
   │
SSH from laptop → IAP tunnel → all VMs
```

- All VMs in the same zone (`us-central1-a`) and subnet so traffic is internal and free of egress cost.
- Only the controller gets an external IP (so the frontend on your laptop can hit `/clusters` etc.). Workers reachable only over the VPC.
- SSH via [Identity-Aware Proxy](https://cloud.google.com/iap/docs/using-tcp-forwarding) — no public SSH ports, no need for a bastion VM.

## Machine sizing

| role | count | type | vCPU/RAM | hourly | monthly always-on |
| --- | --- | --- | --- | --- | --- |
| controller | 1 | `e2-micro` | 0.25–2 / 1 GB | $0.00838 | ~$6 |
| server | 2 | `e2-small` | 0.5–2 / 2 GB | $0.01675 | ~$12 each, $24 total |
| edge | 2 | `e2-micro` | 0.25–2 / 1 GB | $0.00838 | ~$6 each, $12 total |

Total compute always-on: **~$42/month**.

Why not all `e2-micro`? Server containers run PyTorch + MobileNetV2 — 1 GB RAM is tight once Docker, the daemon, and the inference process all coexist. `e2-small` at 2 GB has the headroom that prevents an OOM panic mid-test. Edges and the controller are I/O-bound and fit fine in 1 GB.

If you want to be aggressive: try `e2-micro` for everything first. If servers OOM, upgrade. You can resize a stopped VM in seconds.

## Storage and network costs

- **Boot disks**: 10 GB `pd-standard` per VM × 5 = 50 GB × $0.04/GB-month = **$2/month**. Negligible.
- **Network egress**: traffic *within* a zone or VPC is free. The only paid egress is from the controller's external IP to your laptop. The free tier covers 1 GB/month outbound to most regions, more than enough for browsing the dashboard.
- **External IP on running VM**: free.
- **Static external IP unattached or attached to stopped VM**: $0.00347/hr ≈ $2.50/month each. Don't reserve static IPs unless you really need them. Use ephemeral IPs (the default).

## Cost-saving levers, in order of how much they save

1. **Stop the VMs when you're not testing.** This is the single biggest lever. Stopped VMs cost $0 for compute; you only keep paying for boot-disk storage (~$0.40/VM/month). One `gcloud compute instances stop` command per VM, or wrap it in a script.
2. **Use spot VMs.** Spot pricing is 60–91% off list. `e2-micro` spot is ~$0.0025/hr. Risk: GCP can preempt your VM with 30 seconds notice. For test traffic that's fine — your daemons should already handle restarts since that's what StreamBed is built for. Add `--provisioning-model=SPOT` when creating.
3. **Use the always-free tier** for the controller. Each GCP account gets one `e2-micro` per month free in `us-west1`, `us-central1`, or `us-east1`. Drop the controller into that slot and save ~$6/month.
4. **Don't reserve static IPs.** Ephemeral works for testing.
5. **Set a budget alert.** Billing → Budgets & alerts → create budget for $50 with email at 50/90/100%. This is the failsafe; configure it before you create anything.

Stack #1 (stop when idle) and #2 (spot) and you're paying pennies.

## What this does NOT cover

- **No load balancing.** Out of scope for this test infra — handled separately by the planned horizontal-scaling router system. Hit the single controller VM's external IP directly.
- **No managed instance groups, no autoscaling.** Five hand-managed VMs.
- **No Cloud SQL, no managed Redis.** SQLite on the controller VM is fine.
- **No GKE.** Containers run via the daemon-on-VM model the project already uses; bringing up GKE would dominate the budget.
- **No cross-region anything.** Everything in `us-central1-a`.

## Provisioning approach

Two stages. Both should live in `infra/gcp/` (new directory):

### Stage 1: bootstrap (one-time, manual gcloud)

A bash script that creates the VPC, firewall rules, and five VMs with a startup script. Suggested file: `infra/gcp/bootstrap.sh`. Idempotent (`gcloud compute networks describe ... || gcloud compute networks create ...`).

Firewall rules needed:
- `allow-internal` — all TCP/UDP within the subnet (so daemons and stream traffic flow).
- `allow-iap-ssh` — TCP 22 from `35.235.240.0/20` (the IAP CIDR) so you can SSH via tunnel.
- `allow-controller-http` — TCP 8080 from `0.0.0.0/0` (or your home IP only — better) for the dashboard.
- Block everything else by default.

### Stage 2: VM startup script

Each VM needs Docker. Use a [startup script](https://cloud.google.com/compute/docs/instances/startup-scripts) attached at create time:

```bash
#!/bin/bash
set -e
apt-get update
apt-get install -y docker.io docker-compose-plugin git
systemctl enable --now docker
usermod -aG docker $(whoami)
```

The StreamBed daemon image gets pulled by the daemon itself once the VM has Docker; no need to bake AMIs.

### Naming and metadata

Each VM gets metadata that the daemon reads to identify itself:

| metadata key | example value | used by |
| --- | --- | --- |
| `device-id` | `server-001`, `edge-002` | daemon `/register` body |
| `device-type` | `server` or `edge` | same |
| `device-cluster` | `gcp-test` | same |
| `controller-url` | `http://10.x.x.x:8080` | daemon to find controller |

Daemon reads these via the GCE metadata service (`http://metadata.google.internal/computeMetadata/v1/instance/attributes/...`). If the daemon today reads from env vars or a config file, the startup script can pull metadata into env on boot.

### Why not Terraform?

For five VMs, gcloud bash is fine and lets you iterate fast. If the cluster grows past ~10 VMs or you want IaC for cleanup-on-destroy, migrate to Terraform later — the structure here translates 1:1.

## Concrete next-step checklist

In order, smallest-risk first. Don't skip the budget alert.

1. **Create a GCP project.** Console → New project → name it `streambed-test`. Enable billing, attach the credit code.
2. **Set the budget alert** ($50, alerts at 50/90/100%) before doing anything else.
3. **Enable Compute Engine API.**
4. **Create the VPC + firewall rules** via gcloud (~5 commands).
5. **Create one e2-micro VM as a smoke test.** SSH in via `gcloud compute ssh --tunnel-through-iap`. Confirm Docker installed by the startup script. Tear it down.
6. **Bring up all five VMs** with the proper roles in metadata.
7. **Smoke test**: from your laptop, hit `http://<controller-external-ip>:8080/clusters` — should return the cluster `gcp-test` once daemons register.
8. **Run** `tests/test_routing_table.py` against the cloud cluster instead of localhost (parameterize `CONTROLLER_URL`).
9. **`gcloud compute instances stop`** all five when done. Resume next session with `start`.

## Realistic budget under this plan

Burst-only, 5 VMs ×  ~5 hours of testing × 4 sessions/week:
- Compute: 5 × $0.012 average × 20 hours/week × 4 weeks ≈ **$5/month**
- Storage: 5 × 10 GB × $0.04 ≈ **$2/month**
- Network: ~free at this scale.
- **Total: ~$7/month.** $50 covers ~7 months of casual testing.

If you forget to stop and leave them running for a weekend (~60h × 5 VMs × $0.012 avg ≈ $3.60), no big deal. The danger is a *month* of forgotten always-on, which would burn ~$42. The budget alert at 50% will email you before that happens.

