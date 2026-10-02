"""Notifications : transforme certains événements (minuteur ou rappel terminé) en messages pour l'utilisateur.

Le texte vient des phrases de la personnalité ; les notifications attendent dans une file que l'agent
les prononce dès qu'il est libre (jamais pendant qu'il parle ou écoute une phrase).
Destination unique pour l'instant : la voix.
"""

from __future__ import annotations

import queue
import random
import time
from dataclasses import dataclass, field

from jarvis.events import Event, EventBus
from jarvis.personality import Personality
from jarvis.scheduling.durations import spoken_duration
from jarvis.scheduling.manager import REMINDER_FINISHED, TIMER_FINISHED

VOICE = "voice"
DEFAULT_PHRASES = {
    "timer_finished": ("{Title}, votre minuteur{of_duration} est terminé.",),
    "reminder_finished": ("{Title}, vous m'aviez demandé de vous rappeler {de_message}.",),
}


@dataclass(frozen=True)
class Notification:
    text: str
    event_type: str
    channel: str = VOICE
    created_at: float = field(default_factory=time.time)


def with_de(message: str) -> str:
    """« sortir le linge » -> « de sortir le linge » ; « appeler Paul » -> « d'appeler Paul »."""
    message = message.strip()
    for prefix in ("de ", "d'", "d’"):
        if message.lower().startswith(prefix):
            message = message[len(prefix):].lstrip()
    return f"d'{message}" if message[:1].lower() in "aeiouyhéèêàâîôû" else f"de {message}"


class NotificationManager:
    def __init__(self, personality: Personality, rng: random.Random | None = None):
        self._personality = personality
        self._rng = rng or random.Random()
        self._queue: queue.Queue[Notification] = queue.Queue()

    def attach(self, bus: EventBus) -> None:
        for event_type in (TIMER_FINISHED, REMINDER_FINISHED):
            bus.subscribe(event_type, self.on_event)

    def on_event(self, event: Event) -> None:
        text = self._render(event)
        if text:
            self._queue.put(Notification(text, event.type))

    def next(self) -> Notification | None:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def pending(self) -> int:
        return self._queue.qsize()

    def _render(self, event: Event) -> str:
        payload = event.payload
        if event.type == TIMER_FINISHED:
            seconds = payload.get("duration_seconds")
            key, values = "timer_finished", {"of_duration": f" de {spoken_duration(seconds)}" if seconds else ""}
        elif event.type == REMINDER_FINISHED:
            message = str(payload.get("message", "")).strip()
            key, values = "reminder_finished", {"message": message, "de_message": with_de(message)}
        else:
            return ""
        templates = self._personality.phrases.get(key) or DEFAULT_PHRASES[key]
        text = self._personality.render(self._rng.choice(templates), **values)
        return self._personality.without_user_name(text)
