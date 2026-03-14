"""Pytest fixtures and path setup for unit tests."""
import os
import sys

# Ensure project root is on path (tests/unit/ is two levels below root)
_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _root not in sys.path:
    sys.path.insert(0, _root)
