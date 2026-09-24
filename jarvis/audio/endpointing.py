"""Capture d'une phrase : attend le début de la parole puis s'arrête sur un silence.

Détection par énergie (RMS) avec un seuil adaptatif au bruit ambiant : simple,
sans dépendance et peu coûteux en CPU. Les durées sont comptées en audio reçu,
pas en temps réel, ce qui rend le comportement identique en test sur fichier.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from jarvis.interfaces import AudioSource

PRE_ROLL_FRAMES = 3   # blocs conservés avant le début détecté (évite de couper la 1re syllabe)
MIN_SPEECH_FRAMES = 3  # blocs "forts" consécutifs requis pour considérer qu'on parle


def rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))


@dataclass
class EndpointerSettings:
    end_of_speech_silence: float = 0.9
    max_utterance: float = 15.0
    min_rms: float = 300.0
    speech_to_noise_ratio: float = 3.0


class UtteranceRecorder:
    def __init__(self, source: AudioSource, settings: EndpointerSettings):
        self._source = source
        self._s = settings
        self._frame_s = source.frame_samples / source.sample_rate
        self._noise = settings.min_rms / settings.speech_to_noise_ratio

    def _threshold(self) -> float:
        return max(self._s.min_rms, self._noise * self._s.speech_to_noise_ratio)

    def record(self, start_timeout: float) -> np.ndarray | None:
        """Retourne la phrase prononcée, ou None si personne n'a parlé à temps."""
        pre_roll: list[np.ndarray] = []
        loud_run = 0
        waited = 0.0

        # 1. Attente du début de la parole.
        while True:
            frame = self._source.read()
            if frame is None:
                return None
            level = rms(frame)
            if level > self._threshold():
                loud_run += 1
            else:
                loud_run = 0
                # Suivi lent du bruit de fond, uniquement sur les blocs calmes.
                self._noise = 0.95 * self._noise + 0.05 * level
            pre_roll = (pre_roll + [frame])[-(PRE_ROLL_FRAMES + MIN_SPEECH_FRAMES):]
            if loud_run >= MIN_SPEECH_FRAMES:
                break
            waited += self._frame_s
            if waited >= start_timeout:
                return None

        # 2. Enregistrement jusqu'au silence final.
        frames = list(pre_roll)
        silence = 0.0
        while silence < self._s.end_of_speech_silence and len(frames) * self._frame_s < self._s.max_utterance:
            frame = self._source.read()
            if frame is None:
                break
            frames.append(frame)
            silence = 0.0 if rms(frame) > self._threshold() else silence + self._frame_s
        return np.concatenate(frames)
