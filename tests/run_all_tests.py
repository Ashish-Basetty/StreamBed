#!/usr/bin/env python3
"""
Run all StreamBed tests with hierarchical grouping.

Usage:
  python tests/run_all_tests.py              # Run all tests
  python tests/run_all_tests.py unit         # Run unit tests only
  python tests/run_all_tests.py integration  # Run integration (excl. Docker)
  python tests/run_all_tests.py docker       # Run Docker integration tests
"""
import sys

import pytest


def main():
    args = sys.argv[1:]
    if not args or args[0] == "all":
        return pytest.main(["-v", "-s", "tests/"])
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
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
