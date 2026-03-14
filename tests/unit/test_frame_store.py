import time
import tempfile
import os

import numpy as np
import pytest

from shared.storage.frame_store import FrameStore

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path):
    return FrameStore(str(tmp_path))


def make_frame():
    return np.zeros((480, 640, 3), dtype=np.uint8)


def make_embedding():
    return np.random.rand(1280).astype(np.float32)


def test_store_and_count(store):
    store.store("f1", 1000.0, make_frame(), make_embedding(), "v1", 3600)
    assert store.count() == 1


def test_store_multiple(store):
    for i in range(5):
        store.store(f"f{i}", float(i), make_frame(), make_embedding(), "v1", 3600)
    assert store.count() == 5


def test_query_by_timestamp_range(store):
    store.store("f1", 1000.0, make_frame(), make_embedding(), "v1", 3600)
    store.store("f2", 2000.0, make_frame(), make_embedding(), "v1", 3600)
    store.store("f3", 3000.0, make_frame(), make_embedding(), "v1", 3600)

    results = store.query_by_timestamp(1500.0, 2500.0)
    assert len(results) == 1
    assert results[0].frame_id == "f2"


def test_query_inclusive_bounds(store):
    store.store("f1", 1000.0, make_frame(), make_embedding(), "v1", 3600)
    store.store("f2", 2000.0, make_frame(), make_embedding(), "v1", 3600)

    results = store.query_by_timestamp(1000.0, 2000.0)
    assert len(results) == 2


def test_query_ordered_by_timestamp(store):
    store.store("f3", 3000.0, make_frame(), make_embedding(), "v1", 3600)
    store.store("f1", 1000.0, make_frame(), make_embedding(), "v1", 3600)
    store.store("f2", 2000.0, make_frame(), make_embedding(), "v1", 3600)

    results = store.query_by_timestamp(0.0, 9999.0)
    timestamps = [r.timestamp for r in results]
    assert timestamps == sorted(timestamps)


def test_query_no_results(store):
    store.store("f1", 1000.0, make_frame(), make_embedding(), "v1", 3600)
    results = store.query_by_timestamp(5000.0, 6000.0)
    assert results == []


def test_store_without_embedding(store):
    store.store("f1", 1000.0, make_frame(), None, "v1", 3600)
    results = store.query_by_timestamp(0.0, 9999.0)
    assert len(results) == 1
    assert results[0].embedding_path is None


def test_frame_file_written_to_disk(store, tmp_path):
    sf = store.store("f1", 1000.0, make_frame(), make_embedding(), "v1", 3600)
    assert os.path.exists(sf.frame_path)


def test_embedding_file_written_to_disk(store, tmp_path):
    emb = make_embedding()
    sf = store.store("f1", 1000.0, make_frame(), emb, "v1", 3600)
    assert sf.embedding_path is not None
    assert os.path.exists(sf.embedding_path)
    loaded = np.load(sf.embedding_path)
    np.testing.assert_array_almost_equal(loaded, emb)


def test_delete_expired_removes_entries(store):
    store.store("f1", 1000.0, make_frame(), make_embedding(), "v1", ttl_seconds=0.01)
    time.sleep(0.05)
    deleted = store.delete_expired()
    assert deleted == 1
    assert store.count() == 0


def test_delete_expired_removes_files(store):
    sf = store.store("f1", 1000.0, make_frame(), make_embedding(), "v1", ttl_seconds=0.01)
    frame_path = sf.frame_path
    emb_path = sf.embedding_path
    time.sleep(0.05)
    store.delete_expired()
    assert not os.path.exists(frame_path)
    assert not os.path.exists(emb_path)


def test_delete_expired_keeps_valid_entries(store):
    store.store("f1", 1000.0, make_frame(), make_embedding(), "v1", ttl_seconds=0.01)
    store.store("f2", 2000.0, make_frame(), make_embedding(), "v1", ttl_seconds=9999)
    time.sleep(0.05)
    store.delete_expired()
    assert store.count() == 1
    results = store.query_by_timestamp(0.0, 9999.0)
    assert results[0].frame_id == "f2"


def test_store_replace_existing(store):
    store.store("f1", 1000.0, make_frame(), make_embedding(), "v1", 3600)
    store.store("f1", 2000.0, make_frame(), make_embedding(), "v2", 3600)
    assert store.count() == 1
    results = store.query_by_timestamp(0.0, 9999.0)
    assert results[0].model_version == "v2"
    assert results[0].timestamp == 2000.0
