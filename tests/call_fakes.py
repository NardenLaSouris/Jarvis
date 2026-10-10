"""Faux micros, sorties et passerelle des tests d'appel : en mémoire, sans réseau ni son, et sans rien importer du Core
(les tests de la passerelle tournent aussi dans son environnement Python minimal)."""

from __future__ import annotations

import numpy as np


class Mic:
    """Micro en mémoire : des blocs constants (valeur ``level``), sans fin."""

    def __init__(self, level: int, frame_samples: int = 160):
        self.sample_rate, self.frame_samples, self.level = 16000, frame_samples, level
        self.flushed = 0
        self.closed = False

    def read(self):
        return np.full(self.frame_samples, self.level, np.int16)

    def flush(self):
        self.flushed += 1

    def close(self):
        self.closed = True


class Speaker:
    def __init__(self):
        self.played, self.closed = [], False

    def play(self, audio, rate):
        self.played.append(len(audio))

    def close(self):
        self.closed = True


class Phone:
    """Passerelle factice : note ce que le Core lui demande."""

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def ring(self, call):
        if self.fail:
            raise ConnectionError("passerelle absente")
        self.calls.append(("ring", call.id))

    def cancel(self, call):
        self.calls.append(("cancel", call.id))

    def hang_up(self, call):
        self.calls.append(("hang_up", call.id))

    def answer(self, call):
        self.calls.append(("answer", call.id))
