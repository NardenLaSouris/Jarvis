"""Tests du wake word : configuration, chargement, traitement en flux, détection.

Les tests de détection utilisent des WAV de référence (tests/fixtures/wakeword/),
synthétisés avec des voix exclues de l'entraînement. Aucun accès réseau.
Lancement : python -m pytest tests/test_wakeword.py   (ou python tests/test_wakeword.py)
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.audio.files import read_wav  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.factory import build_wake_word  # noqa: E402
from jarvis.wakeword.openwakeword import FRAME_SAMPLES, FeatureExtractor  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "wakeword"
CFG = load_config(ROOT / "config.toml")
LEAD = 2 * 16000  # silence avant chaque clip : le détecteur tourne en continu en usage réel


def _max_score(detector, audio: np.ndarray) -> float:
    rng = np.random.default_rng(0)
    lead = rng.normal(0, 20, LEAD).astype(np.int16)
    stream = np.concatenate([lead, audio, np.zeros(8000, np.int16)])
    n = len(stream) // FRAME_SAMPLES
    detector.reset()
    scores = [detector.process(f) for f in stream[: n * FRAME_SAMPLES].reshape(n, FRAME_SAMPLES)]
    return max(scores)


def test_config_points_to_existing_model():
    ww = CFG.wake_word
    assert ww.phrase
    assert 0.0 < ww.threshold < 1.0
    for path in (ww.model, ww.melspectrogram_model, ww.embedding_model):
        assert path.exists(), path


def test_detector_loads_configured_model():
    detector = build_wake_word(CFG)
    assert detector.n_features == 16


def test_stream_processing_returns_scores():
    detector = build_wake_word(CFG)
    frame = np.random.default_rng(1).normal(0, 500, FRAME_SAMPLES).astype(np.int16)
    scores = [detector.process(frame) for _ in range(20)]
    assert all(isinstance(s, float) and 0.0 <= s <= 1.0 for s in scores)


def test_rejects_wrong_frame_size():
    detector = build_wake_word(CFG)
    try:
        detector.process(np.zeros(1000, np.int16))
    except ValueError:
        return
    raise AssertionError("un bloc de taille incorrecte doit être refusé")


def test_silence_and_noise_do_not_trigger():
    detector = build_wake_word(CFG)
    rng = np.random.default_rng(2)
    for audio in (np.zeros(5 * 16000, np.int16), rng.normal(0, 800, 5 * 16000).astype(np.int16)):
        assert _max_score(detector, audio) < CFG.wake_word.threshold


def test_batch_embeddings_match_streaming():
    ww = CFG.wake_word
    extractor = FeatureExtractor(ww.melspectrogram_model, ww.embedding_model)
    audio = np.random.default_rng(3).normal(0, 1000, 20 * FRAME_SAMPLES).astype(np.int16)
    batch = extractor.embed_clip(audio)
    extractor.reset()
    stream = np.stack([extractor.process(f) for f in audio.reshape(-1, FRAME_SAMPLES)])
    assert np.array_equal(batch, stream)


def test_reference_positives_are_detected():
    detector = build_wake_word(CFG)
    files = sorted((FIXTURES / "positive").glob("*.wav"))
    assert files, "WAV de référence manquants"
    detected = [_max_score(detector, read_wav(p)[0]) >= CFG.wake_word.threshold for p in files]
    # Voix jamais vues à l'entraînement : on exige une large majorité, pas la perfection.
    assert np.mean(detected) >= 0.8, [p.name for p, d in zip(files, detected) if not d]


def test_reference_negatives_are_rejected():
    detector = build_wake_word(CFG)
    files = sorted((FIXTURES / "negative").glob("*.wav"))
    assert files, "WAV de référence manquants"
    triggered = [p.name for p in files if _max_score(detector, read_wav(p)[0]) >= CFG.wake_word.threshold]
    assert not triggered, triggered


def test_count_activations_uses_refractory_period():
    from wakeword_training.evaluate import REFRACTORY_FRAMES, count_activations

    scores = np.zeros(200)
    scores[[10, 11, 12, 10 + REFRACTORY_FRAMES + 1, 150]] = 0.9
    assert count_activations(scores, 0.5) == 3


def test_exported_classifier_is_loadable():
    """Un classifieur exporté par l'outil d'entraînement se charge dans le détecteur."""
    try:
        import onnx  # noqa: F401  (dépendance d'entraînement uniquement)
    except ImportError:
        return
    import tempfile

    from jarvis.wakeword.openwakeword import OpenWakeWordDetector
    from wakeword_training.train import MLP, export_onnx

    mlp = MLP(16 * 96, 8, np.random.default_rng(0))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tiny.onnx"
        export_onnx(mlp.params, np.zeros(16 * 96), np.ones(16 * 96), 96, path)
        ww = CFG.wake_word
        detector = OpenWakeWordDetector(path, ww.melspectrogram_model, ww.embedding_model)
        score = detector.process(np.zeros(FRAME_SAMPLES, np.int16))
        assert 0.0 <= score <= 1.0
        # Même sortie que le réseau numpy (normalisation intégrée à l'export).
        x = np.random.default_rng(1).normal(size=(3, 16, 96)).astype(np.float32)
        import onnxruntime as ort

        onnx_out = ort.InferenceSession(str(path)).run(None, {"x": x})[0][:, 0]
        np_out = mlp.predict(x.reshape(3, -1))
        assert np.allclose(onnx_out, np_out, atol=1e-5)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
