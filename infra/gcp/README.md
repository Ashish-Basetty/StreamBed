# GCP Test Infra (Terraform)

Provisions the 5-VM cluster from [docs/GCPTestInfra.md](../../docs/GCPTestInfra.md): one controller, two servers, two edges, in a single VPC in `us-central1-a`.

## Mental model: Terraform vs AWS CDK

You came from AWS CDK, so the mapping helps:

| CDK concept | Terraform equivalent |
| --- | --- |
| `cdk init` | `terraform init` (downloads providers into `.terraform/`) |
| Stack (TS/Py code) | `*.tf` files (HCL — declarative, no `for` loops at the resource level except `for_each`/`count`) |
| `cdk synth` | `terraform plan` (shows what will change before apply) |
| `cdk deploy` | `terraform apply` |
| `cdk destroy` | `terraform destroy` |
| CFN stack state | `terraform.tfstate` (local file by default; gitignored) |
| `cdk.context.json` | `terraform.tfvars` (your inputs) |
| `Stack` outputs | `output "..."` blocks |
| L2 constructs | Modules (we're not using any here — vanilla resources) |

Differences worth noting:

- **State is local by default.** AWS CFN keeps state in CloudFormation. Terraform writes it to `terraform.tfstate` next to your `.tf` files. For a solo project this is fine. For team work, you'd put it in a GCS bucket via a `backend "gcs"` block.
- **No real "compile" step.** HCL is parsed at `plan`/`apply`. Errors show up there.
- **`for_each` is the workhorse.** It's how we make 4 worker VMs from one resource block. Closer to a `Map<string, Construct>` in CDK than a `for` loop.

## One-time setup

1. **Install tools** (macOS):
   ```bash
   brew install terraform google-cloud-sdk
   ```
2. **Auth gcloud** so Terraform can talk to GCP:
   ```bash
   gcloud auth application-default login
   ```
   This writes credentials to `~/.config/gcloud/application_default_credentials.json`. Terraform's google provider reads them automatically.
3. **Create a GCP project** (console: New Project → name `streambed-test` → attach billing → apply your $50 credit code).
4. **Set the budget alert** in the console: Billing → Budgets & alerts → New budget for $50, alerts at 50/90/100%. Do this before applying anything. See the doc for why.

## Apply

```bash
cd infra/gcp

# 1. Stamp out your inputs.
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars: set project_id, your_home_ip_cidr.
# Find your home IP: curl ifconfig.me

# 2. Init pulls down the google provider (~50 MB, one-time).
terraform init

# 3. Preview the change. Read the plan carefully — first apply will create
#    ~10 resources (VPC, subnet, 3 firewalls, 5 VMs, 1 internal address,
#    plus the API enablement).
terraform plan

# 4. Ship it.
terraform apply

# Outputs print at the end — controller_external_ip is the one you want.
```

After apply, the controller takes ~60s to finish its startup script (Docker install). Then SSH in:

```bash
$(terraform output -raw -json ssh_commands | jq -r '.["controller-01"]')
# or: gcloud compute ssh controller-01 --zone=us-central1-a --tunnel-through-iap
```

## Daily ops

Use `vms.sh` — reads VM names and zone from Terraform state, so it always
operates on whatever's currently provisioned:

```bash
./vms.sh stop      # power off every VM (cheap; ~$2/mo for disk only)
./vms.sh start     # boot every VM
./vms.sh status    # list current state, machine type, internal IP
```

Stopping is the single biggest cost lever — do it whenever you're not actively
testing. Stopped VMs cost ~$0 for compute; you keep paying boot-disk storage
(~$2/mo total).

**Tear it all down** when you're done with the cluster entirely:

```bash
terraform destroy
```

Releases the IPs, deletes the VMs and disks, removes the VPC.

## Layout

```
infra/gcp/
├── README.md                  # this file
├── versions.tf                # required Terraform & provider versions
├── variables.tf               # tunable inputs
├── main.tf                    # provider, VPC, subnet, firewalls, internal IP
├── vms.tf                     # controller + 4 workers
├── outputs.tf                 # IPs, VM names, copy-paste SSH commands
├── startup.sh                 # runs on each VM at first boot
├── terraform.tfvars.example   # copy → terraform.tfvars
└── .gitignore                 # state, lockfiles, your tfvars
```

## What's deliberately NOT here

- **No load balancer.** The horizontal-scaling router system handles that, separately.
- **No remote backend.** State stays local. Migrate to a GCS backend if you collaborate.
- **No Caddy / TLS.** That's a separate doc ([NginxControllerWrap.md](../../docs/NginxControllerWrap.md)).
- **No automatic deployment of StreamBed itself.** The startup script just installs Docker and stamps `/etc/default/streambed` with metadata. SSH in and `git pull && docker compose up` from there. Once that flow stabilizes, fold it into the startup script.
- **No spot/preemptible flag.** Would cut costs ~70%. Add `scheduling { preemptible = true; provisioning_model = "SPOT" }` to the workers when you're comfortable with them being killed mid-test. Keep the controller non-spot.

## Cost reminders

- 5 VMs always-on: ~$42/mo. $50 credits → 5 weeks. **Don't do this.**
- 5 VMs burst-only (8h/day × 4 days/week): ~$7/mo. ~7 months of credits.
- Forget to stop them over a long weekend: ~$3.60. Negligible.
- Forget to stop them for a month: ~$42. The budget alert will catch you at $25.

## Common gotchas

- **"Required `your_home_ip_cidr` not set."** Set it in `terraform.tfvars`. The variable has no default on purpose — leaving the dashboard wide open is the kind of thing that loses your credits to crypto miners overnight.
- **`Error: googleapi: Error 403: Compute Engine API has not been used in project ... before or it is disabled`** — `terraform apply` will enable it on first run, but if you race past too quickly it can fail. Re-run apply.
- **Startup script still running when you SSH in.** Look at `/var/log/syslog` or `journalctl -u google-startup-scripts.service`. Wait ~90s after VM creation.
- **External IP changed after stop/start.** That's expected with ephemeral IPs — they're released on stop. Fix: edit `vms.tf` to attach a `google_compute_address` for the controller's external IP. Costs ~$2.50/mo when the VM is stopped, free when running.
