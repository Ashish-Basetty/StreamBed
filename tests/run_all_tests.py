#!/usr/bin/env python3
"""
Run all StreamBed tests with hierarchical grouping.

Usage:
  python tests/run_all_tests.py              # Run all tests (Python + Go sidecar)
  python tests/run_all_tests.py unit         # Run unit tests only
  python tests/run_all_tests.py integration  # Run integration (excl. Docker)
  python tests/run_all_tests.py docker       # Run Docker integration tests
  python tests/run_all_tests.py go           # Run Go sidecar tests only
"""
import subprocess
import sys
from pathlib import Path

import pytest

SIDECAR_DIR = Path(__file__).resolve().parent.parent / "sidecar"


def run_go_tests() -> int:
    print(f"\n=== Running Go sidecar tests in {SIDECAR_DIR} ===")
    result = subprocess.run(
        ["go", "test", "-race", "./..."],
        cwd=SIDECAR_DIR,
    )
    return result.returncode


def main():
    args = sys.argv[1:]
    if not args or args[0] == "all":
        py_rc = pytest.main(["-v", "-s", "tests/"])
        go_rc = run_go_tests()
        return py_rc or go_rc
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
