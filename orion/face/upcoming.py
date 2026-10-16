"""Échéance proche affichée sur le visage : un anneau ambre se remplit pendant le dernier quart d'heure avant un
rendez-vous du calendrier, ou avant la fin d'un minuteur ou d'un rappel (« Dentiste dans 12 min »).

Le visage interroge l'état 30 fois par seconde : le calendrier n'est relu qu'une fois par ``refresh`` secondes,
le temps restant est recalculé à chaque fois.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Callable

log = logging.getLogger(__name__)

HORIZON = 15 * 60  # secondes : l'anneau commence à se remplir un quart d'heure avant


class Upcoming:
    def __init__(self, calendar=None, timers=None, horizon: float = HORIZON, refresh: float = 30.0,
                 clock: Callable[[], datetime] = datetime.now, monotonic: Callable[[], float] = time.monotonic):
        self._calendar, self._timers = calendar, timers
        self._horizon, self._refresh = horizon, refresh
        self._clock, self._monotonic = clock, monotonic
        self._events: list[tuple[datetime, str]] = []
        self._read_at: float | None = None

    def _calendar_events(self, now: datetime) -> list[tuple[datetime, str]]:
        if self._calendar is None:
            return []
        if self._read_at is None or self._monotonic() - self._read_at >= self._refresh:
            self._read_at = self._monotonic()
            try:
                found = self._calendar.events(now, now + timedelta(seconds=self._horizon + self._refresh))
                self._events = [(e.start, e.title) for e in found if not e.all_day]
            except Exception as exc:  # le visage n'en dépend pas : pas d'anneau, rien d'autre
                log.debug("Échéance du visage : calendrier illisible (%s)", exc)
                self._events = []
        return self._events

    def current(self) -> dict | None:
        """{"label", "kind", "seconds", "window"} de l'échéance la plus proche dans l'horizon, ou None."""
        now = self._clock()
        candidates = [(start, title, "event", self._horizon) for start, title in self._calendar_events(now)]
        if self._timers is not None:
            candidates += [(t.expires_at, f"minuteur {_short(t.seconds)}", "timer", min(self._horizon, t.seconds))
                           for t in self._timers.timers()]
            candidates += [(r.expires_at, r.message, "reminder", min(self._horizon, r.seconds))
                           for r in self._timers.reminders()]
        soon = [(when, label, kind, window) for when, label, kind, window in candidates
                if 0 < (when - now).total_seconds() <= self._horizon]
        if not soon:
            return None
        when, label, kind, window = min(soon, key=lambda c: c[0])
        return {"label": label[:60], "kind": kind, "seconds": round((when - now).total_seconds()),
                "window": round(window)}


def _short(seconds: float) -> str:
    minutes = round(seconds / 60)
    return f"de {minutes} min" if minutes >= 1 else f"de {round(seconds)} s"
