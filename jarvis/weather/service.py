"""Service météo : ville par défaut, périodes relatives (calculées sur l'horloge du système), cache court et
événements weather.*. Il ne connaît que l'interface WeatherProvider."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Callable

from jarvis.events import Event, EventBus
from jarvis.weather.models import Location, WeatherCurrent, WeatherForecast
from jarvis.weather.provider import WeatherError, WeatherProvider

WEATHER_REQUESTED, WEATHER_RECEIVED, WEATHER_FAILED = "weather.requested", "weather.received", "weather.failed"
EVENT_TYPES = (WEATHER_REQUESTED, WEATHER_RECEIVED, WEATHER_FAILED)

DAYS = {"today": 0, "tomorrow": 1, "day_after_tomorrow": 2}
MOMENTS = {"morning": (6, 12), "afternoon": (12, 18), "evening": (18, 24), "day": (0, 24)}
DAY_LABELS = {"today": "aujourd'hui", "tomorrow": "demain", "day_after_tomorrow": "après-demain"}
MOMENT_LABELS = {
    ("today", "morning"): "ce matin", ("today", "afternoon"): "cet après-midi", ("today", "evening"): "ce soir",
    ("tomorrow", "morning"): "demain matin", ("tomorrow", "afternoon"): "demain après-midi",
    ("tomorrow", "evening"): "demain soir", ("day_after_tomorrow", "morning"): "après-demain matin",
    ("day_after_tomorrow", "afternoon"): "après-demain après-midi", ("day_after_tomorrow", "evening"): "après-demain soir",
}


def period(day: str, moment: str, now: datetime) -> tuple[datetime, datetime, str]:
    """(début, fin, libellé) d'une période relative ; pour aujourd'hui, la période commence au plus tôt maintenant."""
    if day not in DAYS or moment not in MOMENTS:
        raise ValueError(f"Période inconnue : {day} / {moment}")
    first, last = MOMENTS[moment]
    date = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=DAYS[day])
    start, end = date + timedelta(hours=first), date + timedelta(hours=last)
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    if day == "today" and start < current_hour < end:
        start = current_hour
    return start, end, MOMENT_LABELS.get((day, moment), DAY_LABELS[day])


class WeatherService:
    def __init__(self, provider: WeatherProvider, default_location: str, events: EventBus | None = None,
                 current_ttl: float = 300, forecast_ttl: float = 900, location_ttl: float = 86400,
                 clock: Callable[[], datetime] = datetime.now, monotonic: Callable[[], float] = time.monotonic):
        self.provider = provider
        self.default_location = default_location
        self._events = events
        self._ttl = {"current": current_ttl, "forecast": forecast_ttl, "location": location_ttl}
        self._clock = clock
        self._monotonic = monotonic
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def current(self, location: str | None = None) -> tuple[Location, WeatherCurrent]:
        query = (location or self.default_location).strip()
        place = self._request(query, "current", "maintenant", lambda: self._locate(query))
        report = self._request(query, "current", "maintenant",
                               lambda: self._cached(("current", place), "current", lambda: self.provider.get_current(place)),
                               done=place.name)
        return place, report

    def forecast(self, day: str, moment: str, location: str | None = None) -> tuple[Location, WeatherForecast, str]:
        query = (location or self.default_location).strip()
        start, end, label = period(day, moment, self._clock())
        place = self._request(query, "forecast", label, lambda: self._locate(query))
        report = self._request(query, "forecast", label,
                               lambda: self._cached(("forecast", place, start, end), "forecast",
                                                    lambda: self.provider.get_forecast(place, start, end)),
                               done=place.name)
        return place, report, label

    def _locate(self, query: str) -> Location:
        return self._cached(("location", query.lower()), "location", lambda: self.provider.locate(query))

    def _request(self, query: str, kind: str, label: str, action: Callable, done: str | None = None):
        if done is None:
            self._publish(WEATHER_REQUESTED, query, kind, label)
        try:
            result = action()
        except WeatherError as exc:
            self._publish(WEATHER_FAILED, query, kind, label, exc.code)
            raise
        if done is not None:
            self._publish(WEATHER_RECEIVED, done, kind, label)
        return result

    def _cached(self, key: tuple, kind: str, fetch: Callable):
        now = self._monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and hit[0] > now:
                return hit[1]
        value = fetch()
        with self._lock:
            self._cache[key] = (now + self._ttl[kind], value)
        return value

    def _publish(self, event_type: str, location: str, kind: str, label: str, error: str | None = None) -> None:
        if self._events is None:
            return
        payload = {"location": location, "request_type": kind, "period": label,
                   "subject": f"météo {location} ({label})"}
        if error:
            payload["error"] = error
        self._events.publish(Event(event_type, "weather", payload))
