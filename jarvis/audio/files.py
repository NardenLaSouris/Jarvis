"""Source et sortie audio basées sur des fichiers WAV (tests et diagnostic).

Permettent d'exécuter le pipeline complet sans micro : l'audio d'entrée est lu
depuis un WAV, et ce que JARVIS prononce est enregistré au lieu d'être joué.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from jarvis.audio.resample import resample


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError("WAV 16 bits attendu")
        audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
        if wav.getnchannels() > 1:
            audio = audio.reshape(-1, wav.getnchannels())[:, 0]
        return audio.copy(), wav.getframerate()


def write_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(audio.astype(np.int16).tobytes())


class ArraySource:
    """Rejoue un signal en mémoire bloc par bloc, puis s'épuise."""

    def __init__(self, audio: np.ndarray, sample_rate: int, frame_samples: int, source_rate: int | None = None):
        if source_rate and source_rate != sample_rate:
            audio = resample(audio, source_rate, sample_rate)
        pad = (-len(audio)) % frame_samples
        self._audio = np.concatenate([audio, np.zeros(pad, dtype=np.int16)])
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self._pos = 0

    def read(self) -> np.ndarray | None:
        if self._pos >= len(self._audio):
            return None
        frame = self._audio[self._pos : self._pos + self.frame_samples]
        self._pos += self.frame_samples
        return frame

    def flush(self) -> None:
        # Le temps d'un fichier ne s'écoule pas pendant que JARVIS parle : rien à ignorer.
        pass


class RecordingSink:
    """Conserve tout ce que JARVIS prononce (et peut l'écrire dans des WAV)."""

    def __init__(self, output_dir: Path | None = None):
        self.output_dir = output_dir
        self.played: list[tuple[np.ndarray, int]] = []

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        self.played.append((audio, sample_rate))
        if self.output_dir:
            write_wav(self.output_dir / f"jarvis_{len(self.played):02d}.wav", audio, sample_rate)
