# StreamBed

Final Project for CS 214: Big Data Systems. Distributed computation for vision-based models.

## Repository layout & testing

- `edge/` – edge device code (video capture, local inference, streaming, API).
- `server/` – server-side inference containers.
- `controller/` – orchestration, model deployment, routing, and heartbeats.
- `shared/` – common utilities (streaming protocol, storage, inference, APIs).
- `tests/` – pytest test suite (unit and integration).

## Testing

Install pytest, then run tests:

```sh
pip install pytest
pytest tests/ -v -s
```

### Test markers

Tests are tagged with pytest markers. Run specific suites:

| Command | Description |
|---------|-------------|
| `pytest tests/ -v -s -m unit` | Unit tests only (fast, no external services) |
| `pytest tests/ -v -s -m integration_stream` | Stream/network integration tests |
| `pytest tests/ -v -s -m integration_docker` | Docker-based failure detection (requires Docker) |
| `pytest tests/ -v -s -m "integration and not integration_docker"` | Integration tests without Docker |

### Test layout

- **`tests/unit/`** – Unit tests: frame store, TTL manager, inference, stream interface, network simulation, retrieval API.
- **`tests/test_controller_rerouting.py`** – Integration: edge failure and rerouting to another server.
- **`tests/test_integration_stream_to_storage.py`** – Integration: UDP stream → frame store.
- **`tests/test_failure_detection_docker.py`** – Integration: failure detection with Docker and docker-compose.

### Run via script

```sh
python tests/run_all_tests.py              # Run all tests
python tests/run_all_tests.py unit         # Unit tests only
python tests/run_all_tests.py integration  # Integration (excl. Docker)
python tests/run_all_tests.py docker       # Docker integration tests
```
