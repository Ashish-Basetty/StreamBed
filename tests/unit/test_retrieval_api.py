import base64
import time

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shared.api.retrieval import create_retrieval_router
from shared.storage.frame_store import FrameStore

pytestmark = pytest.mark.unit


@pytest.fixture
def client(tmp_path):
    store = FrameStore(str(tmp_path))
    app = FastAPI()
    app.include_router(create_retrieval_router(store))
    return TestClient(app), store


def make_frame():
    return np.zeros((480, 640, 3), dtype=np.uint8)


def make_embedding():
    return np.random.rand(1280).astype(np.float32)


def test_health_empty_store(client):
    c, _ = client
    resp = c.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "stored_frames": 0}


def test_health_with_frames(client):
    c, store = client
    store.store("f1", 1000.0, make_frame(), make_embedding(), "v1", 3600)
    resp = c.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["stored_frames"] == 1


def test_get_frames_returns_correct_count(client):
    c, store = client
    store.store("f1", 1000.0, make_frame(), make_embedding(), "v1", 3600)
    store.store("f2", 2000.0, make_frame(), make_embedding(), "v1", 3600)
    resp = c.get("/api/v1/frames", params={"start": 0.0, "end": 9999.0})
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    assert len(data["frames"]) == 2


def test_get_frames_filters_by_timestamp(client):
    c, store = client
    store.store("f1", 1000.0, make_frame(), make_embedding(), "v1", 3600)
    store.store("f2", 5000.0, make_frame(), make_embedding(), "v1", 3600)
    resp = c.get("/api/v1/frames", params={"start": 0.0, "end": 2000.0})
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["frames"][0]["frame_id"] == "f1"


def test_get_frames_includes_jpeg_b64(client):
    c, store = client
    store.store("f1", 1000.0, make_frame(), make_embedding(), "v1", 3600)
    resp = c.get("/api/v1/frames", params={"start": 0.0, "end": 9999.0})
    jpeg_b64 = resp.json()["frames"][0]["frame_jpeg_b64"]
    assert len(jpeg_b64) > 0
    decoded = base64.b64decode(jpeg_b64)
    assert decoded[:2] == b'\xff\xd8'


def test_get_frames_includes_embedding(client):
    c, store = client
    emb = make_embedding()
    store.store("f1", 1000.0, make_frame(), emb, "v1", 3600)
    resp = c.get("/api/v1/frames", params={"start": 0.0, "end": 9999.0})
    returned_emb = resp.json()["frames"][0]["embedding"]
    assert returned_emb is not None
    assert len(returned_emb) == 1280


def test_get_frames_invalid_range(client):
    c, _ = client
    resp = c.get("/api/v1/frames", params={"start": 9999.0, "end": 0.0})
    assert resp.status_code == 400


def test_get_frames_no_results(client):
    c, store = client
    store.store("f1", 1000.0, make_frame(), make_embedding(), "v1", 3600)
    resp = c.get("/api/v1/frames", params={"start": 5000.0, "end": 9999.0})
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_get_embeddings_no_jpeg(client):
    c, store = client
    store.store("f1", 1000.0, make_frame(), make_embedding(), "v1", 3600)
    resp = c.get("/api/v1/embeddings", params={"start": 0.0, "end": 9999.0})
    assert resp.status_code == 200
    assert resp.json()["frames"][0]["frame_jpeg_b64"] == ""


def test_get_embeddings_invalid_range(client):
    c, _ = client
    resp = c.get("/api/v1/embeddings", params={"start": 9999.0, "end": 0.0})
    assert resp.status_code == 400


def test_get_embeddings_with_embedding(client):
    c, store = client
    emb = make_embedding()
    store.store("f1", 1000.0, make_frame(), emb, "v1", 3600)
    resp = c.get("/api/v1/embeddings", params={"start": 0.0, "end": 9999.0})
    returned_emb = resp.json()["frames"][0]["embedding"]
    assert returned_emb is not None
    np.testing.assert_array_almost_equal(returned_emb, emb.tolist(), decimal=5)


def test_get_frames_no_embedding_returns_null(client):
    c, store = client
    store.store("f1", 1000.0, make_frame(), None, "v1", 3600)
    resp = c.get("/api/v1/frames", params={"start": 0.0, "end": 9999.0})
    assert resp.json()["frames"][0]["embedding"] is None
