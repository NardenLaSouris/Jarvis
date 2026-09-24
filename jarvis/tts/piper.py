"""Synthèse vocale locale avec Piper."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from piper import PiperVoice, SynthesisConfig


class PiperTTS:
    def __init__(self, voice: Path, length_scale: float = 1.0, volume: float = 1.0):
        if not voice.exists():
            raise FileNotFoundError(
                f"Voix Piper introuvable : {voice}. Lancez `python scripts/download_models.py`."
            )
        self._voice = PiperVoice.load(voice)
        self._config = SynthesisConfig(length_scale=length_scale, volume=volume)
        self.sample_rate = self._voice.config.sample_rate

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        chunks = [c.audio_int16_array for c in self._voice.synthesize(text, self._config)]
        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)
        return audio, self.sample_rate
