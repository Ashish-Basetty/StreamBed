"""15-minute soak test: real chaosproxy + sidecars, scrape /metrics, assert
RSS slope ~ 0 after warmup. Tagged `soak`, not default CI.

Skipped unless STREAMBED_RUN_SOAK=1 to avoid CI runs.
"""
from __future__ import annotations

import os

import pytest

pytestmark = [pytest.mark.soak]


@pytest.mark.skipif(os.getenv("STREAMBED_RUN_SOAK") != "1", reason="set STREAMBED_RUN_SOAK=1 to run")
def test_sidecar_rss_stable_over_15min():
    pytest.skip("scaffold: requires running sidecar containers + metrics scraper")
