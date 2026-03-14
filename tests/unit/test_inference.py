import numpy as np
import pytest

from shared.inference.mobilenet import MobileNetV2Model
from shared.inference.base_model import InferenceResult

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def model():
    m = MobileNetV2Model(device="cpu")
    m.load()
    return m


def random_frame(h=480, w=640):
    return np.random.randint(0, 256, (h, w, 3), dtype=np.uint8)


def test_model_version_string(model):
    assert isinstance(model.get_model_version(), str)
    assert len(model.get_model_version()) > 0


def test_preprocess_output_shape(model):
    frame = random_frame()
    preprocessed = model.preprocess(frame)
    assert preprocessed.shape == (1, 3, 224, 224)


def test_preprocess_output_dtype(model):
    frame = random_frame()
    preprocessed = model.preprocess(frame)
    assert preprocessed.dtype == np.float32


def test_infer_returns_inference_result(model):
    frame = random_frame()
    preprocessed = model.preprocess(frame)
    result = model.infer(preprocessed)
    assert isinstance(result, InferenceResult)


def test_infer_embedding_shape(model):
    frame = random_frame()
    result = model.process_frame(frame)
    assert result.embedding.ndim == 1
    assert result.embedding.shape[0] == 1280


def test_infer_embedding_dtype(model):
    frame = random_frame()
    result = model.process_frame(frame)
    assert result.embedding.dtype == np.float32


def test_infer_confidence_in_range(model):
    frame = random_frame()
    result = model.process_frame(frame)
    assert result.confidence is not None
    assert 0.0 <= result.confidence <= 1.0


def test_infer_label_is_string(model):
    frame = random_frame()
    result = model.process_frame(frame)
    assert isinstance(result.label, str)
    assert len(result.label) > 0


def test_infer_raw_output_shape(model):
    frame = random_frame()
    result = model.process_frame(frame)
    assert result.raw_output is not None
    assert result.raw_output.shape == (1, 1000)


def test_process_frame_consistent_with_infer(model):
    frame = random_frame()
    r1 = model.process_frame(frame)
    r2 = model.infer(model.preprocess(frame))
    np.testing.assert_array_almost_equal(r1.embedding, r2.embedding)
    assert r1.label == r2.label


def test_different_frames_produce_different_embeddings(model):
    r1 = model.process_frame(random_frame())
    r2 = model.process_frame(random_frame())
    assert not np.allclose(r1.embedding, r2.embedding)


def test_same_frame_produces_same_embedding(model):
    frame = random_frame()
    r1 = model.process_frame(frame)
    r2 = model.process_frame(frame)
    np.testing.assert_array_almost_equal(r1.embedding, r2.embedding)
