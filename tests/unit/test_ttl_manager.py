from unittest.mock import patch

import pytest

from shared.storage.ttl_manager import TTLManager

pytestmark = pytest.mark.unit


@pytest.fixture
def manager(tmp_path):
    return TTLManager(str(tmp_path), max_ttl=3600.0, min_ttl=30.0, critical_pct=0.10)


def mock_disk(free_fraction):
    total = 1_000_000_000
    free = int(total * free_fraction)
    used = total - free

    class Usage:
        pass

    u = Usage()
    u.total = total
    u.free = free
    u.used = used
    return u


def test_get_disk_usage_fractions_sum_to_one(manager):
    used, free = manager.get_disk_usage()
    assert abs(used + free - 1.0) < 1e-9


def test_get_disk_usage_fractions_in_range(manager):
    used, free = manager.get_disk_usage()
    assert 0.0 <= used <= 1.0
    assert 0.0 <= free <= 1.0


def test_compute_ttl_max_when_disk_plentiful(manager):
    with patch("shutil.disk_usage", return_value=mock_disk(1.0)):
        ttl = manager.compute_ttl()
    assert abs(ttl - 3600.0) < 1.0


def test_compute_ttl_min_at_critical_threshold(manager):
    with patch("shutil.disk_usage", return_value=mock_disk(0.10)):
        ttl = manager.compute_ttl()
    assert ttl == 30.0


def test_compute_ttl_min_below_critical_threshold(manager):
    with patch("shutil.disk_usage", return_value=mock_disk(0.01)):
        ttl = manager.compute_ttl()
    assert ttl == 30.0


def test_compute_ttl_midpoint(manager):
    with patch("shutil.disk_usage", return_value=mock_disk(0.55)):
        ttl = manager.compute_ttl()
    assert manager._min_ttl < ttl < manager._max_ttl


def test_compute_ttl_monotonically_decreases_as_disk_fills(manager):
    fractions = [0.9, 0.7, 0.5, 0.3, 0.1]
    ttls = []
    for frac in fractions:
        with patch("shutil.disk_usage", return_value=mock_disk(frac)):
            ttls.append(manager.compute_ttl())
    assert ttls == sorted(ttls, reverse=True)


def test_custom_min_max_ttl(tmp_path):
    m = TTLManager(str(tmp_path), max_ttl=1000.0, min_ttl=10.0, critical_pct=0.20)
    with patch("shutil.disk_usage", return_value=mock_disk(1.0)):
        assert abs(m.compute_ttl() - 1000.0) < 1.0
    with patch("shutil.disk_usage", return_value=mock_disk(0.0)):
        assert m.compute_ttl() == 10.0
