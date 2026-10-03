"""Sonnerie des réveils : un fichier audio (MP3, WAV, FLAC... fourni par l'utilisateur) joué en boucle jusqu'à
l'arrêt (« Jarvis », « arrête ») ou une durée maximale ; une sonnerie de secours s'il n'y a pas de fichier."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

RATE = 22050
CHUNK_SECONDS = 2


def beep(rate: int = RATE) -> np.ndarray:
    """Sonnerie de secours : trois bips puis un silence."""
    t = np.arange(int(rate * 0.18)) / rate
    tone = (0.35 * 32767 * np.sin(2 * np.pi * 880 * t)).astype(np.int16)
    gap = np.zeros(int(rate * 0.12), np.int16)
    return np.concatenate([tone, gap, tone, gap, tone, np.zeros(int(rate * 0.8), np.int16)])


def load_sound(path: Path | None) -> tuple[np.ndarray, int]:
    if path is not None and Path(path).exists():
        try:
            import soundfile

            data, rate = soundfile.read(str(path), dtype="int16", always_2d=True)
            return data.mean(axis=1).astype(np.int16), rate
        except Exception as exc:
            log.warning("Sonnerie illisible (%s) : sonnerie de secours utilisée (%s)", path, exc)
    elif path is not None:
        log.info("Pas de sonnerie %s : sonnerie de secours utilisée", path)
    return beep(), RATE


class AlarmPlayer:
    def __init__(self, sink, sound: Path | None = None, max_minutes: float = 5.0,
                 before: Callable[[], None] | None = None, clock: Callable[[], float] = time.monotonic):
        self._sink = sink
        self._sound_path = sound
        self._max_seconds = max_minutes * 60
        self._before = before
        self._clock = clock
        self._stopped = threading.Event()
        self.ringing = False

    def ring(self) -> bool:
        """Sonne jusqu'à l'arrêt (True) ou la durée maximale (False)."""
        audio, rate = load_sound(self._sound_path)
        if self._before is not None:
            try:
                self._before()
            except Exception:
                log.exception("Préparation de la sonnerie impossible")
        self._stopped.clear()
        self.ringing = True
        deadline = self._clock() + self._max_seconds
        chunk = rate * CHUNK_SECONDS
        try:
            while not self._stopped.is_set() and self._clock() < deadline:
                for start in range(0, len(audio), chunk):
                    if self._stopped.is_set() or self._clock() >= deadline:
                        break
                    self._sink.play(audio[start:start + chunk], rate)
        finally:
            self.ringing = False
        return self._stopped.is_set()

    def stop(self) -> bool:
        """Arrête la sonnerie en cours ; False si rien ne sonnait."""
        if not self.ringing:
            return False
        self._stopped.set()
        stop = getattr(self._sink, "stop", None)
        if stop is not None:
            stop()
        return True
