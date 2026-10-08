"""Fournisseur Open-Meteo (https://open-meteo.com) : sans clé API, recherche de ville intégrée.

Usage gratuit non commercial (domotique personnelle incluse), données sous licence CC-BY 4.0.
Adresses fixes ; seuls des paramètres construits par ORION sont envoyés. Les réponses sont des
données non fiables : seuls les champs attendus sont lus, et leurs types et unités sont vérifiés.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime

import httpx

from jarvis.weather.models import HourlyPoint, Location, WeatherCurrent, WeatherForecast, condition, summarize
from jarvis.weather.provider import (
    INCOMPLETE_DATA, INVALID_RESPONSE, RATE_LIMITED, UNKNOWN_LOCATION, WEATHER_UNAVAILABLE, WeatherError,
)

log = logging.getLogger(__name__)

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TEMPERATURE_UNITS = {"celsius": "°C", "fahrenheit": "°F"}
CURRENT_FIELDS = "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code,precipitation"
HOURLY_FIELDS = "temperature_2m,precipitation_probability,precipitation,weather_code,wind_speed_10m"
UNAVAILABLE_MESSAGE = "Je n'arrive pas à récupérer les données météo pour le moment."
FOREIGN_MIN_POPULATION = 15000


def _number(value, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise WeatherError(INCOMPLETE_DATA, UNAVAILABLE_MESSAGE)
    return float(value)


def _optional(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _time(value) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise WeatherError(INVALID_RESPONSE, UNAVAILABLE_MESSAGE) from exc


class OpenMeteoProvider:
    name = "open-meteo"

    def __init__(self, client: httpx.Client | None = None, timeout: float = 8.0, country: str = "FR",
                 language: str = "fr", temperature_unit: str = "celsius"):
        if temperature_unit not in TEMPERATURE_UNITS:
            raise ValueError(f"Unité de température inconnue : {temperature_unit} (celsius ou fahrenheit)")
        self.temperature_symbol = TEMPERATURE_UNITS[temperature_unit]
        self._units = {"temperature_unit": temperature_unit, "wind_speed_unit": "kmh", "precipitation_unit": "mm"}
        self._expected = {"temperature_2m": self.temperature_symbol, "wind_speed_10m": "km/h", "precipitation": "mm"}
        self._client = client or httpx.Client(headers={"User-Agent": "ORION-assistant/1.0"})
        self._timeout = timeout
        self._country = country.upper()
        self._language = language

    def locate(self, query: str) -> Location:
        data = self._get(GEOCODING_URL, {"name": query, "count": 10, "language": self._language, "format": "json"})
        results = [r for r in data.get("results") or [] if isinstance(r, dict)]
        if not results:
            raise WeatherError(UNKNOWN_LOCATION, f"Je ne trouve pas la ville « {query[:40]} ».")
        local = [r for r in results if r.get("country_code") == self._country]
        foreign = [r for r in results if (r.get("population") or 0) >= FOREIGN_MIN_POPULATION]
        if not local and not foreign:
            raise WeatherError(UNKNOWN_LOCATION, f"Je ne trouve pas la ville « {query[:40]} ».")
        best = max(local or foreign, key=lambda r: r.get("population") or 0)
        latitude, longitude = _optional(best.get("latitude")), _optional(best.get("longitude"))
        if latitude is None or longitude is None or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise WeatherError(INVALID_RESPONSE, UNAVAILABLE_MESSAGE)
        timezone = best.get("timezone") if isinstance(best.get("timezone"), str) else "auto"
        return Location(str(best.get("name") or query)[:60], latitude, longitude, str(best.get("country_code") or ""),
                        str(best.get("admin1") or "")[:60], timezone)

    def get_current(self, location: Location) -> WeatherCurrent:
        data = self._forecast(location, {"current": CURRENT_FIELDS})
        self._check_units(data.get("current_units"))
        current = data.get("current")
        if not isinstance(current, dict):
            raise WeatherError(INCOMPLETE_DATA, UNAVAILABLE_MESSAGE)
        humidity = _optional(current.get("relative_humidity_2m"))
        return WeatherCurrent(
            location=location.name,
            temperature=_number(current.get("temperature_2m"), "temperature_2m"),
            feels_like=_optional(current.get("apparent_temperature")),
            humidity=round(humidity) if humidity is not None else None,
            wind_speed=_optional(current.get("wind_speed_10m")),
            precipitation=_optional(current.get("precipitation")),
            condition=condition(int(_number(current.get("weather_code"), "weather_code"))),
            timestamp=_time(current.get("time")),
        )

    def get_forecast(self, location: Location, start: datetime, end: datetime) -> WeatherForecast:
        data = self._forecast(location, {"hourly": HOURLY_FIELDS, "start_date": start.date().isoformat(),
                                         "end_date": end.date().isoformat()})
        self._check_units(data.get("hourly_units"))
        hourly = data.get("hourly")
        if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
            raise WeatherError(INCOMPLETE_DATA, UNAVAILABLE_MESSAGE)
        columns = {key: hourly.get(key) for key in HOURLY_FIELDS.split(",")}
        if any(not isinstance(v, list) or len(v) != len(hourly["time"]) for v in columns.values()):
            raise WeatherError(INVALID_RESPONSE, UNAVAILABLE_MESSAGE)
        points = []
        for i, moment in enumerate(hourly["time"]):
            temperature, code = _optional(columns["temperature_2m"][i]), _optional(columns["weather_code"][i])
            if temperature is None or code is None:
                continue
            chance = _optional(columns["precipitation_probability"][i])
            points.append(HourlyPoint(_time(moment), temperature, round(chance) if chance is not None else None,
                                      _optional(columns["precipitation"][i]), int(code),
                                      _optional(columns["wind_speed_10m"][i])))
        forecast = summarize(location.name, points, start.replace(tzinfo=None), end.replace(tzinfo=None))
        if forecast is None:
            raise WeatherError(INCOMPLETE_DATA, UNAVAILABLE_MESSAGE)
        return forecast

    def _forecast(self, location: Location, params: dict) -> dict:
        return self._get(FORECAST_URL, {"latitude": location.latitude, "longitude": location.longitude,
                                        "timezone": location.timezone or "auto", **self._units, **params})

    def _check_units(self, units) -> None:
        if not isinstance(units, dict):
            raise WeatherError(INVALID_RESPONSE, UNAVAILABLE_MESSAGE)
        for field, unit in self._expected.items():
            if field in units and units[field] != unit:
                log.warning("Météo : unité inattendue pour %s (%s)", field, units[field])
                raise WeatherError(INVALID_RESPONSE, UNAVAILABLE_MESSAGE)

    def _get(self, url: str, params: dict) -> dict:
        try:
            response = self._client.get(url, params=params, timeout=self._timeout)
        except httpx.TimeoutException as exc:
            log.warning("Météo : délai dépassé (%s)", url)
            raise WeatherError(WEATHER_UNAVAILABLE, UNAVAILABLE_MESSAGE) from exc
        except httpx.HTTPError as exc:
            log.warning("Météo : service injoignable (%s) : %s", url, type(exc).__name__)
            raise WeatherError(WEATHER_UNAVAILABLE, UNAVAILABLE_MESSAGE) from exc
        if response.status_code == 429:
            log.warning("Météo : limite de requêtes atteinte")
            raise WeatherError(RATE_LIMITED, "Le service météo est momentanément saturé.")
        if response.status_code != 200:
            log.warning("Météo : erreur HTTP %s (%s)", response.status_code, url)
            raise WeatherError(WEATHER_UNAVAILABLE, UNAVAILABLE_MESSAGE)
        try:
            data = response.json()
        except ValueError as exc:
            raise WeatherError(INVALID_RESPONSE, UNAVAILABLE_MESSAGE) from exc
        if not isinstance(data, dict) or data.get("error"):
            log.warning("Météo : réponse refusée ou illisible (%s)", url)
            raise WeatherError(INVALID_RESPONSE, UNAVAILABLE_MESSAGE)
        return data
