#!/usr/bin/env python3
"""
Run all StreamBed tests with hierarchical grouping.

Usage:
  python tests/run_all_tests.py              # Build+push infra images, then run all tests
  python tests/run_all_tests.py unit         # Run unit tests only (no build)
  python tests/run_all_tests.py integration  # Run integration tests (excl. Docker, no build)
  python tests/run_all_tests.py docker       # Run Docker integration tests (no build)
  python tests/run_all_tests.py go           # Run Go sidecar tests only (no build)
  python tests/run_all_tests.py build        # Build+push infra images only (no tests)

Infra images built and pushed:
  ashishbasetty/streambed-quic-sidecar:latest  (Go sidecar, built from sidecar/)
  streambed daemon images                       (rebuilt via docker compose build)
"""
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SIDECAR_DIR = REPO_ROOT / "sidecar"
SIDECAR_IMAGE = "ashishbasetty/streambed-quic-sidecar:latest"
COMPOSE_FILES = ["-f", "docker-compose.yml",
                 "-f", "experiments/advisor/docker-compose.advisor.yml"]


def _run(cmd: list[str], cwd: Path = REPO_ROOT) -> int:
    print(f"\n$ {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=cwd).returncode


def run_go_tests() -> int:
    print(f"\n=== Running Go sidecar tests in {SIDECAR_DIR} ===")
    return _run(["go", "test", "-race", "./..."], cwd=SIDECAR_DIR)


def build_and_push() -> int:
    print("\n=== Building and pushing infra images ===")

    rc = _run(["go", "test", "-race", "./..."], cwd=SIDECAR_DIR)
    if rc:
        print("Go tests failed — aborting build.")
        return rc

    rc = _run(["docker", "build", "-t", SIDECAR_IMAGE, "."], cwd=SIDECAR_DIR)
    if rc:
        return rc
    rc = _run(["docker", "push", SIDECAR_IMAGE])
    if rc:
        return rc

    rc = _run(["docker", "compose", *COMPOSE_FILES,
               "build", "daemon-edge1", "daemon-server1"])
    return rc


def main():
    args = sys.argv[1:]
    if not args or args[0] == "all":
        rc = build_and_push()
        if rc:
            return rc
        py_rc = pytest.main(["-v", "-s", "tests/"])
        go_rc = run_go_tests()
        return py_rc or go_rc
    if args[0] == "build":
        return build_and_push()
    if args[0] == "unit":
        return pytest.main(["-v", "-s", "tests/unit/"])
    if args[0] == "integration":
        return pytest.main([
            "-v", "-s", "-m",
            "integration and not integration_docker",
            "tests/",
        ])
    if args[0] == "docker":
        return pytest.main([
            "-v", "-s", "-m", "integration_docker",
            "tests/",
        ])
    if args[0] == "go":
        return run_go_tests()
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
