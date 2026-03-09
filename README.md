# StreamBed

Final Project for CS 214: Big Data Systems. Distributed computation for vision-based models.

## Repository layout & testing

- `edge/` – edge device code (video capture, local inference, streaming, API).
- `server/` – server-side inference containers.
- `controller/` – orchestration, model deployment, routing, and heartbeats.
- `shared/` – common utilities (streaming protocol, storage, inference, APIs).
- `deployment_testing/` – scripts and compose files for integration tests.
- `tests/` – lightweight Python unit tests (currently covers the streaming protocol).

Run the unit tests with a Python environment that has `pytest` installed:

```sh
pip install pytest
python -m pytest tests/test_stream_interface.py
```

The UDP sender/receiver implementations in `shared/interfaces/stream_interface.py`
are exercised by the test.
