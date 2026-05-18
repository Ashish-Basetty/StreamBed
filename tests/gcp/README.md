# GCP tests

Cross-host networking tests that target the 5-VM Terraform cluster in
[infra/gcp/](../../infra/gcp/). All tests are marked `pytest.mark.gcp` and
skipped by default — the standard `pytest` invocation never touches GCP.

## Prereqs

1. Cluster up: `cd infra/gcp && terraform apply && ./vms.sh start`.
2. Controller + router running on `controller-01`:
   `gcloud compute ssh controller-01 --zone=us-central1-a --tunnel-through-iap`
   then `sudo docker compose -f docker-compose.controller.yml up -d`.
3. Daemon running on each worker:
   `sudo docker compose -f docker-compose.worker.yml up -d`.
4. IAP tunnel on your laptop. Bind to `:18080` (not `:8080`) so it doesn't
   collide with a local docker-compose controller running on the laptop:
   ```
   gcloud compute start-iap-tunnel controller-01 8080 \
     --zone=us-central1-a --local-host-port=localhost:18080
   ```

## Run

```
pytest -m gcp tests/gcp/                       # uses --controller-url default (localhost:18080)
pytest -m gcp tests/gcp/ --controller-url=http://<external-ip>:8080
```

## When done

`cd infra/gcp && ./vms.sh stop` — single biggest cost lever.
