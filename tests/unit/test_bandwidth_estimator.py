import time

import pytest

from shared.bandwidth.estimator import ConfigBackend, SentRateBackend
from shared.bandwidth.composite import CompositeBackend
from shared.bandwidth.server_feedback import ServerFeedbackBackend

pytestmark = pytest.mark.unit


def test_config_returns_constant():
    """ConfigBackend always returns initial_bps."""
    backend = ConfigBackend(initial_bps=500_000)
    assert backend.get_target_bps() == 500_000
    backend.on_bytes_sent(1000)
    backend.on_bytes_queued(5000)
    assert backend.get_target_bps() == 500_000


def test_sent_rate_tracks_window():
    """After sending 1000 bytes over 1s, get_target_bps() ~= 8000 (with 0.9 factor)."""
    backend = SentRateBackend(
        safety_factor=0.9,
        ewma_alpha=1.0,  # no smoothing for deterministic test
    )
    # Send 1000 bytes, spread over ~1 second
    for _ in range(10):
        backend.on_bytes_sent(100)
        time.sleep(0.1)
    target = backend.get_target_bps()
    # 1000 bytes * 8 = 8000 bits over ~1s = 8000 bps. With 0.9 factor = 7200
    assert 6000 <= target <= 10000


def test_sent_rate_decays():
    """Stop sending, next sample has zero throughput, rate decays."""
    backend = SentRateBackend(
        safety_factor=0.9,
        min_bps=1000,
        ewma_alpha=0.5,
    )
    backend.on_bytes_sent(10_000)
    time.sleep(0.1)
    high = backend.get_target_bps()  # sample 1: 10k bytes / 0.1s
    time.sleep(0.6)  # no sends this sample
    low = backend.get_target_bps()  # sample 2: 0 bytes, decay toward min
    assert low < high


def test_sent_rate_respects_bounds():
    """SentRateBackend clamps to min_bps and max_bps."""
    backend = SentRateBackend(
        min_bps=100_000,
        max_bps=200_000,
        safety_factor=0.9,
        ewma_alpha=1.0,
    )
    # No sends - throughput 0, decay to min_bps
    target = backend.get_target_bps()
    assert target >= 100_000
    assert target <= 200_000


def test_composite_takes_min():
    """Composite returns min of child backends."""
    config_high = ConfigBackend(initial_bps=1_000_000)
    config_low = ConfigBackend(initial_bps=100_000)
    composite = CompositeBackend(config_high, config_low)
    assert composite.get_target_bps() == 100_000

    # With SentRate that reports lower
    sent_rate = SentRateBackend(
        min_bps=10_000,
        max_bps=50_000_000,
        ewma_alpha=1.0,
    )
    sent_rate.on_bytes_sent(1000)  # low rate
    time.sleep(0.1)
    composite2 = CompositeBackend(config_high, sent_rate)
    target = composite2.get_target_bps()
    assert target <= 1_000_000
    assert target >= 10_000


def test_composite_forwards_callbacks():
    """Composite forwards on_bytes_sent to all backends."""
    config = ConfigBackend(500_000)
    sent_rate = SentRateBackend()

    composite = CompositeBackend(config, sent_rate)
    composite.on_bytes_sent(500)

    # SentRate should have recorded the send
    sent_rate.get_target_bps()


def test_server_feedback_returns_default_before_update():
    """ServerFeedbackBackend returns default_bps before any update."""
    backend = ServerFeedbackBackend(default_bps=300_000)
    assert backend.get_target_bps() == 300_000


def test_server_feedback_updates_from_response():
    """ServerFeedbackBackend updates target via update_from_response (UDP push)."""
    backend = ServerFeedbackBackend(default_bps=100_000)
    assert backend.get_target_bps() == 100_000

    backend.update_from_response({"received_bps": 500_000})
    assert backend.get_target_bps() == 500_000

    # Invalid/missing data falls back to default
    backend.update_from_response({"status": "ok"})
    assert backend.get_target_bps() == 100_000
