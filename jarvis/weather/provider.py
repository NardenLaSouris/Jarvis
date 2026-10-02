"""Interface d'un fournisseur météo : JARVIS ne dépend que d'elle, jamais du format d'une API."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from jarvis.weather.models import Location, WeatherCurrent, WeatherForecast

UNKNOWN_LOCATION = "unknown_location"
WEATHER_UNAVAILABLE = "weather_unavailable"
RATE_LIMITED = "rate_limited"
INVALID_RESPONSE = "invalid_response"
INCOMPLETE_DATA = "incomplete_data"
MISSING_API_KEY = "missing_api_key"


class WeatherError(Exception):
    """Échec prévu : un code stable et un message court en français (jamais de détail technique)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class WeatherProvider(Protocol):
    name: str

    def locate(self, query: str) -> Location: ...

    def get_current(self, location: Location) -> WeatherCurrent: ...

    def get_forecast(self, location: Location, start: datetime, end: datetime) -> WeatherForecast: ...
