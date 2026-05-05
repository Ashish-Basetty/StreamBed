#!/usr/bin/env bash
# Daily-driver helper for the GCP test cluster.
#
#   ./vms.sh start    # boot every VM
#   ./vms.sh stop     # power off every VM (cheap; preserves state, ~$2/mo storage)
#   ./vms.sh status   # show current state of each VM
#
# Reads `vm_names` and `zone` from `tofu output`, so it always operates on
# whatever the current Terraform state actually provisioned.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Pick whichever's installed.
if command -v tofu >/dev/null 2>&1; then
  TF=tofu
elif command -v terraform >/dev/null 2>&1; then
  TF=terraform
else
  echo "error: neither tofu nor terraform on PATH" >&2
  exit 1
fi

if ! command -v gcloud >/dev/null 2>&1; then
  echo "error: gcloud not on PATH" >&2
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "error: jq not on PATH (brew install jq)" >&2
  exit 1
fi

verb="${1:-}"
case "$verb" in
  start|stop|status) ;;
  *)
    echo "usage: $0 {start|stop|status}" >&2
    exit 2
    ;;
esac

# Pull from terraform state. Avoid `mapfile` so this works on macOS's
# default bash 3.2.
zone=$("$TF" output -raw zone 2>/dev/null || true)
names=()
while IFS= read -r name; do
  [[ -n "$name" ]] && names+=("$name")
done < <("$TF" output -json vm_names 2>/dev/null | jq -r '.[]?')

if [[ -z "$zone" || ${#names[@]} -eq 0 ]]; then
  echo "error: couldn't read 'zone' or 'vm_names' from terraform state." >&2
  echo "       have you run '$TF apply'?" >&2
  exit 3
fi

case "$verb" in
  start)
    echo "Starting ${#names[@]} VM(s) in $zone..."
    gcloud compute instances start "${names[@]}" --zone="$zone"
    ;;
  stop)
    echo "Stopping ${#names[@]} VM(s) in $zone..."
    gcloud compute instances stop "${names[@]}" --zone="$zone"
    ;;
  status)
    gcloud compute instances list \
      --filter="zone:($zone) AND name:(${names[*]})" \
      --format="table(name,status,machineType.basename(),networkInterfaces[0].networkIP)"
    ;;
esac
