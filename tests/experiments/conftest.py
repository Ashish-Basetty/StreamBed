"""Pytest fixtures + marker registration for the experiment harness."""

from __future__ import annotations

from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "experiment: long-running adaptivity sweep that writes a CSV time-series.",
    )


@pytest.fixture()
def experiment_results_dir() -> Path:
    p = Path(__file__).resolve().parent / "results"
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture()
def experiment_runtime_dir() -> Path:
    """Mounted into the varying-proxy container as /etc/streambed (ro)."""
    p = Path(__file__).resolve().parent / "runtime"
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture()
def experiment_schedules_dir() -> Path:
    return Path(__file__).resolve().parent / "schedules"
