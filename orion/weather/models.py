"""Données météo internes à ORION, indépendantes du fournisseur.

Unités : températures dans l'unité configurée (°C par défaut), vent en km/h, précipitations en mm,
probabilité de pluie en %.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Location:
    name: str
    latitude: float
    longitude: float
    country: str = ""
    region: str = ""
    timezone: str = "auto"


@dataclass(frozen=True)
class Condition:
    code: int
    slug: str
    label: str


CONDITIONS = {
    0: ("clear", "ciel dégagé"), 1: ("mainly_clear", "ciel plutôt dégagé"), 2: ("partly_cloudy", "ciel partiellement nuageux"),
    3: ("overcast", "ciel couvert"), 45: ("fog", "brouillard"), 48: ("fog", "brouillard givrant"),
    51: ("drizzle", "bruine légère"), 53: ("drizzle", "bruine"), 55: ("drizzle", "bruine dense"),
    56: ("freezing_drizzle", "bruine verglaçante"), 57: ("freezing_drizzle", "bruine verglaçante"),
    61: ("light_rain", "pluie faible"), 63: ("rain", "pluie"), 65: ("heavy_rain", "forte pluie"),
    66: ("freezing_rain", "pluie verglaçante"), 67: ("freezing_rain", "forte pluie verglaçante"),
    71: ("light_snow", "neige faible"), 73: ("snow", "neige"), 75: ("heavy_snow", "fortes chutes de neige"),
    77: ("snow_grains", "grains de neige"), 80: ("rain_showers", "averses"), 81: ("rain_showers", "averses"),
    82: ("violent_showers", "fortes averses"), 85: ("snow_showers", "averses de neige"),
    86: ("snow_showers", "fortes averses de neige"), 95: ("thunderstorm", "orage"),
    96: ("thunderstorm_hail", "orage avec grêle"), 99: ("thunderstorm_hail", "orage avec forte grêle"),
}


def condition(code: int) -> Condition:
    """Code météo OMM (WMO) -> condition interne."""
    slug, label = CONDITIONS.get(code, ("unknown", "conditions inconnues"))
    return Condition(code, slug, label)


@dataclass(frozen=True)
class WeatherCurrent:
    location: str
    temperature: float
    feels_like: float | None
    humidity: int | None
    wind_speed: float | None
    precipitation: float | None
    condition: Condition
    timestamp: datetime


@dataclass(frozen=True)
class HourlyPoint:
    time: datetime
    temperature: float
    precipitation_probability: int | None
    precipitation: float | None
    code: int
    wind_speed: float | None


@dataclass(frozen=True)
class WeatherForecast:
    location: str
    start: datetime
    end: datetime
    temperature: float
    temperature_min: float
    temperature_max: float
    precipitation_probability: int | None
    precipitation: float
    condition: Condition
    wind_speed: float | None


def summarize(location: str, points: list[HourlyPoint], start: datetime, end: datetime) -> WeatherForecast | None:
    """Prévision d'une période à partir des heures qu'elle couvre : moyenne et extrêmes, risque de pluie
    maximal, cumul de pluie, condition la plus marquante, vent maximal."""
    hours = [p for p in points if start <= p.time < end]
    if not hours:
        return None
    temperatures = [p.temperature for p in hours]
    chances = [p.precipitation_probability for p in hours if p.precipitation_probability is not None]
    winds = [p.wind_speed for p in hours if p.wind_speed is not None]
    return WeatherForecast(
        location=location, start=hours[0].time, end=hours[-1].time,
        temperature=round(sum(temperatures) / len(temperatures), 1),
        temperature_min=min(temperatures), temperature_max=max(temperatures),
        precipitation_probability=max(chances) if chances else None,
        precipitation=round(sum(p.precipitation or 0.0 for p in hours), 1),
        condition=condition(max(p.code for p in hours)),
        wind_speed=max(winds) if winds else None,
    )
