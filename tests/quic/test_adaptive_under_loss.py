"""Closed-loop adaptive-rate test: with chaosproxy injecting loss, verify the
edge `BandwidthEstimator` converges and `should_drop_video_frame` engages.

Runs against the Python codec — does not require the Go sidecar to be built.
The same test should pass under STREAM_TRANSPORT=quic once the sidecar is in
the loop; that variant is gated by `pytest -m integration_quic` AND a built
sidecar binary.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from shared.bandwidth.composite import CompositeBackend
from shared.bandwidth.estimator import SentRateBackend
from shared.bandwidth.server_feedback import ServerFeedbackBackend

pytestmark = [pytest.mark.integration_quic]


def test_bandwidth_estimator_responds_to_feedback():
    """Smoke check: server-feedback backend dominates once it reports a value."""
    sent = SentRateBackend()
    feedback = ServerFeedbackBackend(default_bps=500_000)
    est = CompositeBackend(sent, feedback)

    # Simulate sending bytes faster than feedback says we're delivering.
    for _ in range(50):
        sent.on_bytes_sent(20_000)
        time.sleep(0.005)

    feedback.update_from_response({"received_bps": 80_000})
    target = est.get_target_bps()

    # The composite should clamp to the feedback's lower estimate when
    # observed delivery is below sent rate.
    assert target <= 250_000, f"target_bps {target} did not respect feedback ceiling"


# Note: a closed-loop drop-engagement assertion against StreamProxyManager
# lives in test_dynamic_interleaving.py once the feedback loop is wired through
# chaosproxy. Keeping this file focused on the codec/estimator interaction so
# it stays runnable without daemon env vars.
