"""Événements -> notifications : la fin d'un minuteur ou d'un rappel devient une notification.

Le texte est déterministe (phrases de la personnalité, sans LLM). Le gestionnaire de minuteurs ne
connaît pas ce module : il publie ses événements, ce module les écoute.
"""

from __future__ import annotations

import random

from jarvis.events import Event, EventBus
from jarvis.notifications.manager import NotificationManager
from jarvis.notifications.models import Notification, Priority
from jarvis.personality import Personality, second_person, with_de
from jarvis.scheduling.durations import spoken_duration
from jarvis.scheduling.manager import REMINDER_FINISHED, TIMER_FINISHED

DEFAULT_PHRASES = {
    "timer_finished": ("{Title}, votre minuteur{of_duration} est terminé.",),
    "reminder_finished": ("{Title}, vous m'aviez demandé de vous rappeler {de_message}.",),
    "reminder_late": ("{Title}, pendant que j'étais arrêté, j'aurais dû vous rappeler {de_message}.",),
}


class EventNotifications:
    def __init__(self, manager: NotificationManager, personality: Personality, rng: random.Random | None = None):
        self._manager = manager
        self._personality = personality
        self._rng = rng or random.Random()

    def attach(self, bus: EventBus) -> None:
        for event_type in (TIMER_FINISHED, REMINDER_FINISHED):
            bus.subscribe(event_type, self.on_event)

    def on_event(self, event: Event) -> None:
        notification = self.build(event)
        if notification is not None:
            self._manager.notify(notification)

    def build(self, event: Event) -> Notification | None:
        payload = event.payload
        if event.type == TIMER_FINISHED:
            seconds = payload.get("duration_seconds")
            text = self._render("timer_finished", of_duration=f" de {spoken_duration(seconds)}" if seconds else "")
            return Notification("Minuteur terminé", text, "timer", Priority.NORMAL,
                                {"timer_id": payload.get("timer_id"), "duration_seconds": seconds})
        if event.type == REMINDER_FINISHED:
            message = second_person(str(payload.get("message", "")).strip())
            key = "reminder_late" if payload.get("late") else "reminder_finished"
            text = self._render(key, message=message, de_message=with_de(message))
            return Notification("Rappel", text, "reminder", Priority.NORMAL,
                                {"reminder_id": payload.get("reminder_id"), "message": message})
        return None

    def _render(self, key: str, **values: str) -> str:
        templates = self._personality.phrases.get(key) or DEFAULT_PHRASES[key]
        text = self._personality.render(self._rng.choice(templates), **values)
        return self._personality.without_user_name(text)
