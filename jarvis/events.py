"""Bus d'événements interne : les composants de JARVIS publient et écoutent sans se connaître.

Livraison synchrone, dans l'ordre d'abonnement. Un abonné qui échoue est journalisé et ignoré :
la publication ne lève jamais d'exception et les autres abonnés sont tout de même servis.
"""

from __future__ import annotations

import itertools
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Callable, Mapping

log = logging.getLogger(__name__)

ALL = "*"
TYPE = re.compile(r"^[a-z][a-z_]*(\.[a-z][a-z_]*)+$")

TOOL_STARTED = "tool.started"
TOOL_EXECUTED = "tool.executed"
TOOL_FAILED = "tool.failed"
# Panne d'un composant (worker LLM, micro distant...) et retour à la normale : payload {"source", "message"}.
SYSTEM_ERROR = "system.error"
SYSTEM_RECOVERED = "system.recovered"


@dataclass(frozen=True)
class Event:
    type: str
    source: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not TYPE.match(self.type):
            raise ValueError(f"Type d'événement invalide : {self.type!r} (attendu : domaine.action)")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    def as_dict(self) -> dict:
        return {"type": self.type, "timestamp": self.timestamp,
                "time": datetime.fromtimestamp(self.timestamp).isoformat(timespec="seconds"),
                "source": self.source, "payload": dict(self.payload)}


Handler = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[tuple[int, Handler]]] = {}
        self._order = itertools.count()
        self._lock = threading.Lock()

    def subscribe(self, event_type: str, handler: Handler) -> Callable[[], None]:
        """Abonne ``handler`` à un type (``ALL`` : tous les événements). Rend la fonction de désabonnement."""
        if event_type != ALL and not TYPE.match(event_type):
            raise ValueError(f"Type d'événement invalide : {event_type!r}")
        with self._lock:
            self._handlers.setdefault(event_type, []).append((next(self._order), handler))
        return lambda: self.unsubscribe(event_type, handler)

    def unsubscribe(self, event_type: str, handler: Handler) -> bool:
        with self._lock:
            handlers = self._handlers.get(event_type, [])
            for entry in handlers:
                if entry[1] == handler:
                    handlers.remove(entry)
                    return True
            return False

    def publish(self, event: Event) -> int:
        """Remet l'événement à ses abonnés, dans l'ordre d'abonnement (abonnés à ce type et à ``ALL``
        confondus) ; rend le nombre d'abonnés servis sans erreur."""
        with self._lock:
            entries = sorted([*self._handlers.get(event.type, ()), *self._handlers.get(ALL, ())])
        handlers = [handler for _, handler in entries]
        delivered = 0
        for handler in handlers:
            try:
                handler(event)
                delivered += 1
            except Exception:
                log.exception("Abonné %r en échec sur l'événement %s", getattr(handler, "__qualname__", handler),
                              event.type)
        return delivered
