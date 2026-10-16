"""La veille d'un rappel ou d'un rendez-vous : publie « schedule.upcoming » à EVE_HOUR heures la veille, pour les
règles d'appel « appelle-moi la veille… » (jarvis/call/rules.py, événement item_tomorrow).

Lecture seule des rappels (jarvis.scheduling) et de l'agenda (jarvis.agenda) ; chaque rappel ou rendez-vous n'est
annoncé qu'une fois. Un élément ajouté après l'heure de sa veille (rappel de 5 h créé à 1 h 42) n'est pas annoncé
en retard : la veille est passée.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, time, timedelta
from typing import Callable

from jarvis.events import Event, EventBus

log = logging.getLogger(__name__)

EVE_HOUR = 18  # la veille à 18 h : assez tôt pour s'organiser, sans réveiller
UPCOMING = "schedule.upcoming"


def eve_of(when: datetime) -> datetime:
    return datetime.combine(when.date() - timedelta(days=1), time(EVE_HOUR))


class EveWatch:
    def __init__(self, events: EventBus, timers=None, calendar=None, clock: Callable[[], datetime] = datetime.now,
                 every: float = 30.0):
        self._events, self._timers, self._calendar, self._clock, self._every = events, timers, calendar, clock, every
        self._last = clock() - timedelta(seconds=every * 2)
        self._sent: set[str] = set()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def items(self, now: datetime) -> list[dict]:
        found = []
        if self._timers is not None:
            for r in self._timers.reminders():
                found.append({"item_key": f"rappel:{r.id}:{r.expires_at.isoformat()}", "kind": "rappel",
                              "title": (r.message or "Rappel")[:120], "at": r.expires_at, "all_day": False})
        if self._calendar is not None:
            for e in self._calendar.events(now, now + timedelta(days=2)):
                found.append({"item_key": f"agenda:{e.id}:{e.start.isoformat()}", "kind": "rendez-vous",
                              "title": e.title[:120], "at": e.start, "all_day": e.all_day})
        return found

    def check(self) -> int:
        """Publie les veilles arrivées depuis le dernier passage ; rend leur nombre."""
        now, published = self._clock(), 0
        try:
            items = self.items(now)
        except Exception:  # noqa: BLE001 - agenda en ligne injoignable : nouvel essai au prochain passage
            log.exception("Veille des rappels et rendez-vous : lecture impossible")
            items = []
        for item in items:
            eve = eve_of(item["at"])
            if self._last < eve <= now < item["at"] and item["item_key"] not in self._sent:
                self._sent.add(item["item_key"])
                payload = {**item, "at": item["at"].isoformat()}
                self._events.publish(Event(UPCOMING, "scheduling", payload))
                published += 1
                log.info("Veille : %s « %s » demain", item["kind"], item["title"][:40])
        self._last = now
        return published

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="veille-agenda", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stopping.set()

    def _loop(self) -> None:
        while not self._stopping.wait(self._every):
            self.check()
