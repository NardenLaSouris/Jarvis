"""État visuel de JARVIS : ce que le visage graphique affiche, indépendamment de son rendu.

Le reste de JARVIS écrit ici (état, niveau audio) ; le serveur du visage ne fait que lire.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Callable

import numpy as np

STATES = ("standby", "listening", "thinking", "speaking")
ACTIVITY = {"standby": 0.15, "listening": 0.4, "thinking": 0.85, "speaking": 0.6}
FRAME_SECONDS = 0.02
SILENCE_DB, LOUD_DB = -50.0, -12.0
SPEAKING_TAIL = 0.4


def envelope(audio: np.ndarray, rate: int, frame_seconds: float = FRAME_SECONDS) -> np.ndarray:
    """Niveau perçu (0..1) par tranche de 20 ms : RMS en dB ramené entre silence (-50 dBFS) et voix forte (-12 dBFS)."""
    samples = np.asarray(audio, dtype=np.float32).reshape(-1) / 32768.0
    size = max(1, int(rate * frame_seconds))
    count = int(math.ceil(len(samples) / size))
    if count == 0:
        return np.zeros(0, dtype=np.float32)
    padded = np.zeros(count * size, dtype=np.float32)
    padded[: len(samples)] = samples
    rms = np.sqrt(np.mean(padded.reshape(count, size) ** 2, axis=1))
    db = 20 * np.log10(np.maximum(rms, 1e-9))
    return np.clip((db - SILENCE_DB) / (LOUD_DB - SILENCE_DB), 0.0, 1.0).astype(np.float32)


class AudioActivity:
    """Niveau sonore de ce qui est réellement joué, calculé à partir des échantillons envoyés au haut-parleur.

    Chaque morceau joué est planifié à la suite du précédent ; ``level()`` donne le niveau à l'instant présent.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._segments: list[tuple[float, np.ndarray]] = []
        self._end = 0.0

    def feed(self, audio: np.ndarray, rate: int) -> None:
        levels = envelope(audio, rate)
        with self._lock:
            now = self._clock()
            start = max(now, self._end)
            self._segments = [s for s in self._segments if s[0] + len(s[1]) * FRAME_SECONDS > now - 1.0]
            self._segments.append((start, levels))
            self._end = start + len(np.asarray(audio).reshape(-1)) / rate

    def level(self, now: float | None = None) -> float:
        now = self._clock() if now is None else now
        with self._lock:
            for start, levels in self._segments:
                index = int((now - start) / FRAME_SECONDS)
                if 0 <= index < len(levels):
                    return float(levels[index])
        return 0.0

    def busy(self, now: float | None = None) -> bool:
        now = self._clock() if now is None else now
        with self._lock:
            return now < self._end

    @property
    def ends_at(self) -> float:
        with self._lock:
            return self._end


class VisualState:
    """État du visage : ``state``, ``audio_level``, ``activity`` et ``transition`` (s depuis le dernier changement).

    ``speaking`` posé par la lecture audio revient tout seul à l'état précédent quand le son se termine,
    si aucun événement de JARVIS n'a changé l'état entre-temps.
    """

    def __init__(self, audio: AudioActivity | None = None, clock: Callable[[], float] = time.monotonic):
        self.audio = audio or AudioActivity(clock)
        self._clock = clock
        self._lock = threading.Lock()
        self._state = "standby"
        self._changed_at = clock()
        self._manual_level: float | None = None
        self._manual_at = 0.0
        self._before_speaking: str | None = None

    def set_state(self, state: str) -> None:
        state = str(state).lower()
        if state not in STATES:
            raise ValueError(f"État visuel inconnu : {state} (attendus : {', '.join(STATES)})")
        with self._lock:
            self._before_speaking = None
            self._change(state)

    def standby(self) -> None:
        self.set_state("standby")

    def set_audio_level(self, level: float) -> None:
        """Niveau audio fourni de l'extérieur (0..1), prioritaire pendant 1 s sur le niveau mesuré."""
        value = float(level) if level is not None and math.isfinite(float(level)) else 0.0
        with self._lock:
            self._manual_level = min(1.0, max(0.0, value))
            self._manual_at = self._clock()

    def speak(self, audio: np.ndarray, rate: int) -> None:
        """Appelé à chaque morceau joué : mesure le niveau et passe en ``speaking``."""
        self.audio.feed(audio, rate)
        with self._lock:
            if self._state != "speaking":
                self._before_speaking = self._state
                self._change("speaking")

    def snapshot(self) -> dict:
        now = self._clock()
        with self._lock:
            if (self._state == "speaking" and self._before_speaking is not None
                    and now > self.audio.ends_at + SPEAKING_TAIL):
                self._change(self._before_speaking)
                self._before_speaking = None
            state = self._state
            manual = self._manual_level if now - self._manual_at < 1.0 else None
            since = now - self._changed_at
        if manual is not None:
            level, source = manual, "external"
        elif self.audio.busy(now) or now < self.audio.ends_at + SPEAKING_TAIL:
            level, source = self.audio.level(now), "measured"
        else:
            level, source = 0.0, "none"
        activity = ACTIVITY[state] + (0.4 * level if state == "speaking" else 0.0)
        return {"state": state, "audio_level": round(level, 3), "audio_source": source,
                "activity": round(min(1.0, activity), 3), "transition": round(since, 3)}

    def _change(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self._changed_at = self._clock()
