"""Transcription locale avec faster-whisper (CTranslate2)."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
# Avertissement sans conséquence du cache HuggingFace sous Windows (pas de liens symboliques).
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from faster_whisper import WhisperModel  # noqa: E402

NO_SPEECH_THRESHOLD = 0.6


class FasterWhisperSTT:
    def __init__(
        self,
        model: str,
        language: str,
        device: str = "cpu",
        compute_type: str = "int8",
        beam_size: int = 1,
        download_root: Path | None = None,
    ):
        kwargs = dict(
            device=device,
            compute_type=compute_type,
            download_root=str(download_root) if download_root else None,
        )
        try:
            # Démarrage hors ligne si le modèle est déjà téléchargé.
            self._model = WhisperModel(model, local_files_only=True, **kwargs)
        except Exception:
            self._model = WhisperModel(model, **kwargs)
        self._language = language
        self._beam_size = beam_size

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        if sample_rate != 16000:
            raise ValueError("faster-whisper attend de l'audio à 16 kHz")
        samples = audio.astype(np.float32) / 32768.0
        segments, _ = self._model.transcribe(
            samples,
            language=self._language,
            beam_size=self._beam_size,
            condition_on_previous_text=False,
            vad_filter=False,
        )
        # Whisper « invente » parfois du texte sur du bruit : on écarte ces segments.
        kept = [s.text.strip() for s in segments if s.no_speech_prob < NO_SPEECH_THRESHOLD]
        return " ".join(kept).strip()
