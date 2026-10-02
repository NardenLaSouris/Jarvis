"""Gestionnaire de notifications : remet chaque notification aux canaux qui l'acceptent.

Il ignore d'où viennent les notifications (minuteur, rappel...) ; un canal en erreur est journalisé
sans empêcher les autres canaux ni JARVIS de continuer.
"""

from __future__ import annotations

import logging

from jarvis.events import EventBus
from jarvis.notifications.models import NOTIFICATION_CREATED, NOTIFICATION_FAILED, Notification, Priority, publish

log = logging.getLogger(__name__)


class NotificationChannel:
    """Destination d'une notification (voix, bureau, téléphone...). ``send`` ne doit pas bloquer longtemps."""

    name = "channel"

    def __init__(self, min_priority: Priority = Priority.LOW):
        self.min_priority = min_priority

    def accepts(self, notification: Notification) -> bool:
        return notification.priority >= self.min_priority

    def send(self, notification: Notification) -> None:
        raise NotImplementedError

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


class NotificationManager:
    def __init__(self, events: EventBus | None = None):
        self._events = events
        self._channels: dict[str, NotificationChannel] = {}
        self._disabled: set[str] = set()

    def register(self, channel: NotificationChannel) -> None:
        if channel.name in self._channels:
            raise ValueError(f"Canal de notification déjà enregistré : {channel.name}")
        self._channels[channel.name] = channel

    def unregister(self, name: str) -> bool:
        channel = self._channels.pop(name, None)
        self._disabled.discard(name)
        if channel is not None:
            self._stop(channel)
        return channel is not None

    def enable(self, name: str) -> None:
        self._disabled.discard(name)

    def disable(self, name: str) -> None:
        if name in self._channels:
            self._disabled.add(name)

    def channels(self) -> dict[str, bool]:
        """Canaux enregistrés -> actif ou non."""
        return {name: name not in self._disabled for name in self._channels}

    def notify(self, notification: Notification) -> list[str]:
        """Remet la notification aux canaux actifs qui l'acceptent ; rend leurs noms."""
        publish(self._events, NOTIFICATION_CREATED, notification)
        used = []
        for name, channel in list(self._channels.items()):
            if name in self._disabled or not channel.accepts(notification):
                continue
            try:
                channel.send(notification)
                used.append(name)
            except Exception as exc:
                log.exception("Canal de notification %s en échec", name)
                publish(self._events, NOTIFICATION_FAILED, notification, name, str(exc)[:200] or type(exc).__name__)
        if not used:
            log.info("Notification « %s » : aucun canal actif", notification.title)
        return used

    def start(self) -> None:
        for channel in self._channels.values():
            channel.start()

    def stop(self) -> None:
        for channel in self._channels.values():
            self._stop(channel)

    @staticmethod
    def _stop(channel: NotificationChannel) -> None:
        try:
            channel.stop()
        except Exception:
            log.exception("Arrêt du canal de notification %s impossible", channel.name)
