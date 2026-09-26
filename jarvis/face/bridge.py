"""Branchement du visage sur JARVIS, sans toucher au pipeline vocal.

- ``FaceBridge.on_event`` : se branche sur le rappel d'événements existant de l'agent ;
- ``MeteredSink`` : enveloppe la sortie audio pour mesurer ce qui est réellement joué.
Toute erreur côté visage est avalée : l'interface est non critique.
"""

from __future__ import annotations

import logging
from typing import Callable

import numpy as np

from jarvis.face.state import VisualState

log = logging.getLogger(__name__)

EVENT_STATES = {
    "sleep": "standby",
    "wake": "listening",
    "listening": "listening",
    "stt": "listening",
    "user": "thinking",
    "routing": "thinking",
    "web": "thinking",
}


class FaceBridge:
    def __init__(self, visual: VisualState, forward: Callable[[str, str], None] | None = None):
        self.visual = visual
        self._forward = forward

    def on_event(self, kind: str, text: str) -> None:
        state = EVENT_STATES.get(kind)
        if state is not None:
            try:
                self.visual.set_state(state)
            except Exception:
                log.debug("Visage : changement d'état ignoré", exc_info=True)
        if self._forward is not None:
            self._forward(kind, text)


class MeteredSink:
    """Sortie audio inchangée, qui signale en plus au visage chaque morceau joué."""

    def __init__(self, sink, visual: VisualState):
        self._sink = sink
        self._visual = visual

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        try:
            self._visual.speak(audio, sample_rate)
        except Exception:
            log.debug("Visage : niveau audio ignoré", exc_info=True)
        self._sink.play(audio, sample_rate)

    def __getattr__(self, name):
        return getattr(self._sink, name)
