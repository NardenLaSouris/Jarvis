"""Détection du wake word avec les modèles openWakeWord (inférence ONNX).

Réimplémentation minimale de l'inférence openWakeWord : le paquet officiel
importe scipy et scikit-learn pour ses outils d'entraînement, inutiles ici.

Le pipeline a deux étages :
  1. ``FeatureExtractor`` (générique, commun à tous les mots) :
     audio 16 kHz -> spectrogramme mel -> un embedding de 96 valeurs toutes les 80 ms ;
  2. le classifieur propre au wake word (le fichier ``model`` de la configuration) :
     les N derniers embeddings -> score entre 0 et 1.
Changer de wake word revient donc à changer uniquement le classifieur.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

import numpy as np
import onnxruntime as ort

SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280          # 80 ms : pas d'un embedding
EMBEDDING_DIM = 96
MEL_CONTEXT = 160 * 3         # recouvrement nécessaire au calcul des trames mel
MEL_WINDOW = 76               # trames mel par embedding
WARMUP_FRAMES = 5             # scores ignorés tant que les tampons se remplissent


def _session(path: Path) -> ort.InferenceSession:
    if not path.exists():
        raise FileNotFoundError(
            f"Modèle introuvable : {path}. Lancez `python scripts/download_models.py`."
        )
    options = ort.SessionOptions()
    options.inter_op_num_threads = 1
    options.intra_op_num_threads = 1
    return ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])


class FeatureExtractor:
    """Étage générique : audio -> embeddings. Utilisé en direct et pour l'entraînement."""

    def __init__(self, melspectrogram_model: Path, embedding_model: Path):
        self._mel = _session(melspectrogram_model)
        self._embed = _session(embedding_model)
        self.reset()

    def reset(self) -> None:
        self._audio = np.zeros(MEL_CONTEXT, dtype=np.int16)
        self._mels = np.ones((MEL_WINDOW, 32), dtype=np.float32)

    def _melspectrogram(self, audio: np.ndarray) -> np.ndarray:
        mel = self._mel.run(None, {"input": audio[None, :].astype(np.float32)})[0]
        return np.squeeze(mel, axis=(0, 1)) / 10.0 + 2.0  # normalisation attendue par l'embedding

    def process(self, frame: np.ndarray) -> np.ndarray:
        """Flux : un bloc de 80 ms -> un embedding (96,)."""
        audio = np.concatenate([self._audio, frame])
        self._audio = audio[-MEL_CONTEXT:]
        self._mels = np.vstack([self._mels, self._melspectrogram(audio)])[-MEL_WINDOW:]
        window = self._mels[None, :, :, None]
        return np.squeeze(self._embed.run(None, {"input_1": window})[0])

    def embed_clip(self, audio: np.ndarray) -> np.ndarray:
        """Hors ligne (entraînement) : clip entier -> embeddings (n_blocs, 96).

        Passe volontairement par le même chemin que le flux : le modèle mel
        normalise sur toute son entrée, un calcul « par lots » donnerait des
        caractéristiques différentes de celles vues en direct.
        """
        self.reset()
        n_frames = len(audio) // FRAME_SAMPLES
        frames = audio[: n_frames * FRAME_SAMPLES].reshape(n_frames, FRAME_SAMPLES)
        return np.stack([self.process(f) for f in frames]) if n_frames else np.zeros((0, EMBEDDING_DIM), np.float32)


class OpenWakeWordDetector:
    def __init__(self, model: Path, melspectrogram_model: Path, embedding_model: Path):
        self._extractor = FeatureExtractor(melspectrogram_model, embedding_model)
        self._model = _session(model)
        self._model_input = self._model.get_inputs()[0].name
        self.n_features = self._model.get_inputs()[0].shape[1]
        self.reset()

    def reset(self) -> None:
        self._extractor.reset()
        self._features: deque[np.ndarray] = deque(
            [np.zeros(EMBEDDING_DIM, dtype=np.float32)] * self.n_features, maxlen=self.n_features
        )
        self._frames_seen = 0

    def process(self, frame: np.ndarray) -> float:
        if frame.shape != (FRAME_SAMPLES,):
            raise ValueError(f"Bloc de {FRAME_SAMPLES} échantillons attendu, reçu {frame.shape}")
        self._features.append(self._extractor.process(frame))
        features = np.stack(self._features)[None, :, :].astype(np.float32)
        score = float(self._model.run(None, {self._model_input: features})[0][0][0])
        self._frames_seen += 1
        return score if self._frames_seen > WARMUP_FRAMES else 0.0
