"""
Run all StreamBed tests with hierarchical grouping.

Usage:
  pytest tests/ -v -s                           # Run all tests
  pytest tests/ -v -s -m unit                   # Run unit tests only
  pytest tests/ -v -s -m integration_docker     # Run Docker integration tests only
  pytest tests/ -v -s -m "integration and not integration_docker"  # Integration without Docker

  python tests/run_all_tests.py [unit|integration|integration_docker|all]  # Run via script

Hierarchy:
  Unit tests (tests/unit/, fast, no external services):
    - test_frame_store
    - test_ttl_manager
    - test_inference
    - test_stream_interface
    - test_network_simulation
    - test_retrieval_api

  Integration tests (tests/, require external services):
    - integration_docker: test_failure_detection_docker (requires Docker)
    - integration_stream: test_controller_rerouting, test_integration_stream_to_storage
"""
import os
import sys

import pytest

# Ensure project root is on path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestUnit:
    """Unit tests: frame store, TTL, inference, stream interface, retrieval API."""

    @pytest.mark.unit
    def test_suite_documented(self):
        """Unit test suite is documented. Run with: pytest tests/unit/ -v -s"""
        pass


class TestIntegrationDocker:
    """Integration tests that require Docker and docker-compose."""

    @pytest.mark.integration
    @pytest.mark.integration_docker
    def test_suite_documented(self):
        """Docker integration suite. Run with: pytest tests/test_failure_detection_docker.py -v -s"""
        pass


class TestIntegrationStream:
    """Integration tests for stream/network behavior."""

    @pytest.mark.integration
    @pytest.mark.integration_stream
    def test_suite_documented(self):
        """Stream integration suite. Run with: pytest tests/test_controller_rerouting.py tests/test_integration_stream_to_storage.py -v -s"""
        pass
