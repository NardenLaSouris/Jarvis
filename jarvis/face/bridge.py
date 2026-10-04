"""Branchement du visage sur JARVIS, sans toucher au pipeline vocal.

- ``FaceBridge.on_event`` : se branche sur le rappel d'événements existant de l'agent ;
- ``MeteredSink`` : enveloppe la sortie audio pour mesurer ce qui est réellement joué.
Toute erreur côté visage est avalée : l'interface est non critique.
"""

from __future__ import annotations

import logging
from typing import Callable

import numpy as np

import threading

from jarvis.events import SYSTEM_ERROR, SYSTEM_RECOVERED, TOOL_EXECUTED, TOOL_FAILED, TOOL_STARTED, Event, EventBus
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
    "tool": "thinking",
}

# Échecs techniques (le visage passe en rouge) ; un refus, une pièce inconnue ou un titre introuvable n'en sont pas.
TECHNICAL_ERRORS = {"execution_failed", "timeout", "light_unreachable", "device_unreachable", "spotify_unavailable",
                    "spotify_no_device", "calendar_unavailable", "files_disabled", "already_running"}

# Explications affichées sur le visage pour les pannes durables (le message de l'outil sert pour les échecs ponctuels).
SYSTEM_MESSAGES = {
    "llm": "Mon module de réflexion (Katana) ne répond pas : je fonctionne en mode réduit.",
    "micro": "Je n'entends plus le micro du PC : connexion perdue, nouvelle tentative en cours.",
}
CONVERSATION_MESSAGE = "Mon module de réflexion n'a pas répondu à votre demande."

BUS_STATES = {
    TOOL_STARTED: "thinking",
}


class FaceBridge:
    def __init__(self, visual: VisualState, forward: Callable[[str, str], None] | None = None):
        self.visual = visual
        self._forward = forward
        # Outils lancés hors conversation (routine, JARVIS Control) pendant la veille : le visage revient en veille
        # à la fin, au lieu de rester « en réflexion » jusqu'à la prochaine conversation.
        self._background = 0
        self._lock = threading.Lock()

    def _error(self, action) -> None:
        try:
            action()
        except Exception:
            log.debug("Visage : erreur non affichée", exc_info=True)

    def on_event(self, kind: str, text: str) -> None:
        if kind == "error":
            self._error(lambda: self.visual.flash_error("conversation", message=CONVERSATION_MESSAGE))
        if kind in ("wake", "sleep"):
            with self._lock:
                self._background = 0  # une conversation reprend la main sur le visage
        self._show(EVENT_STATES.get(kind))
        if self._forward is not None:
            self._forward(kind, text)

    def attach(self, bus: EventBus) -> None:
        """Écoute aussi les événements système (bus) qui ont un état visuel associé."""
        for event_type in (*BUS_STATES, TOOL_EXECUTED, TOOL_FAILED, SYSTEM_ERROR, SYSTEM_RECOVERED):
            bus.subscribe(event_type, self.on_bus_event)

    def on_bus_event(self, event: Event) -> None:
        if event.type in (SYSTEM_ERROR, SYSTEM_RECOVERED):
            source = str(event.payload.get("source", "système"))
            message = SYSTEM_MESSAGES.get(source) or str(event.payload.get("message", "")) or "Un composant est en panne."
            self._error(lambda: self.visual.set_error(source, event.type == SYSTEM_ERROR, message))
            return
        if event.type == TOOL_FAILED and event.payload.get("error") in TECHNICAL_ERRORS:
            message = str(event.payload.get("message") or "Une action n'a pas pu être faite.")
            self._error(lambda: self.visual.flash_error(f"outil:{event.payload.get('tool')}", message=message))
        if event.type in (TOOL_EXECUTED, TOOL_FAILED):
            with self._lock:
                if not self._background:
                    return
                self._background -= 1
                done = self._background == 0
            if done and self._current() == "thinking":
                self._show("standby")
            return
        if event.type == TOOL_STARTED and self._current() == "standby":
            with self._lock:
                self._background += 1
        self._show(BUS_STATES.get(event.type))

    def _current(self) -> str | None:
        try:
            return self.visual.snapshot()["state"]
        except Exception:
            return None

    def _show(self, state: str | None) -> None:
        if state is None:
            return
        try:
            self.visual.set_state(state)
        except Exception:
            log.debug("Visage : changement d'état ignoré", exc_info=True)


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
