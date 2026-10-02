"""Notification : un message destiné à l'utilisateur, indépendant de ce qui l'a produit et du canal qui le portera."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from types import MappingProxyType
from typing import Any, Mapping

from jarvis.events import Event, EventBus

NOTIFICATION_CREATED = "notification.created"
NOTIFICATION_SENT = "notification.sent"
NOTIFICATION_FAILED = "notification.failed"
NOTIFICATION_STARTED = "notification.started"
NOTIFICATION_FINISHED = "notification.finished"

MAX_TITLE = 80
MAX_MESSAGE = 500
SOURCE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")


class Priority(IntEnum):
    LOW = 0
    NORMAL = 1
    HIGH = 2

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class Notification:
    title: str
    message: str
    source: str
    priority: Priority = Priority.NORMAL
    metadata: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        for name, value, limit in (("title", self.title, MAX_TITLE), ("message", self.message, MAX_MESSAGE)):
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError(f"Notification : {name} vide ou trop long")
        if not isinstance(self.source, str) or not SOURCE.match(self.source):
            raise ValueError(f"Notification : source invalide {self.source!r}")
        if not isinstance(self.priority, Priority):
            raise ValueError(f"Notification : priorité invalide {self.priority!r}")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def payload(self, channel: str | None = None, error: str | None = None) -> dict:
        """Contenu des événements notification.* (journal d'activité, visage...)."""
        return {"notification_id": self.id, "title": self.title, "message": self.message, "source": self.source,
                "priority": self.priority.label, "channel": channel, "error": error,
                "subject": f"{self.title} ({channel})" if channel else self.title}


def publish(events: EventBus | None, event_type: str, notification: Notification, channel: str | None = None,
            error: str | None = None) -> None:
    if events is not None:
        events.publish(Event(event_type, "notifications", notification.payload(channel, error)))
