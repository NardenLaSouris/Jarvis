"""Outil météo : délègue au WeatherService, sans rien connaître du fournisseur.

Paramètres structurés uniquement (ville facultative, jour, moment) : le LLM ne compose ni URL ni date.
Chaque valeur doit être justifiée par la demande (une ville dite, « demain », « ce soir »...).
"""

from __future__ import annotations

import re

from jarvis.personality import normalize
from jarvis.stt.correction import sound
from jarvis.tools.base import INVALID_PARAMETERS, Param, Risk, Tool, ToolError
from jarvis.weather.provider import WeatherError
from jarvis.weather.service import DAYS, MOMENTS, WeatherService

CITY = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ' -]{0,59}$")
UNITS = {"wind_speed": "km/h", "precipitation": "mm", "humidity": "%", "precipitation_probability": "%"}
DAY_WORDS = {"tomorrow": (r"\bdemain\b",), "day_after_tomorrow": (r"\bapres demain\b",)}
MOMENT_WORDS = {"morning": (r"\bmatin(ee)?\b",), "afternoon": (r"\bapres midi\b", r"\baprem\b"),
                "evening": (r"\bsoir(ee)?\b", r"\bcette nuit\b", r"\bce soir\b")}


def _city(value: str) -> str:
    if not CITY.match(value):
        raise ToolError(INVALID_PARAMETERS, "Nom de ville invalide.")
    return value


def _said(text: str, patterns: tuple[str, ...]) -> bool:
    words = f" {normalize(text)} "
    return any(re.search(p, words) for p in patterns)


def _city_said(value: str, text: str) -> bool:
    """La ville figure dans la demande, à l'oreille près (« à nant de demain » pour « à Nantes demain »)."""
    wanted = {sound(w) for w in normalize(value).split()}
    return bool(wanted) and wanted <= {sound(w) for w in normalize(text).split()}


def _rain_risk(probability: int | None, precipitation: float | None = None) -> str | None:
    if precipitation:
        return "pluie en cours" if probability is None else "pluie prévue"
    if probability is None:
        return None
    return "faible" if probability < 30 else "possible" if probability < 60 else "probable"


def _day_said(value: str, text: str) -> bool:
    if value == "tomorrow":
        return _said(text, DAY_WORDS["tomorrow"]) and not _said(text, DAY_WORDS["day_after_tomorrow"])
    return value not in DAY_WORDS or _said(text, DAY_WORDS[value])


def _moment_said(value: str, text: str) -> bool:
    return value not in MOMENT_WORDS or _said(text, MOMENT_WORDS[value])


def weather_tool(service: WeatherService) -> Tool:
    units = {"temperature": getattr(service.provider, "temperature_symbol", "°C"), **UNITS}

    def run(location: str | None = None, day: str | None = None, moment: str | None = None) -> dict:
        day = day or "today"
        moment = moment or ("now" if day == "today" else "day")
        try:
            if moment == "now":
                if day != "today":
                    raise ToolError(INVALID_PARAMETERS, "« Maintenant » ne vaut que pour aujourd'hui.")
                place, current = service.current(location)
                return {"type": "current", "location": place.name, "country": place.country, "period": "maintenant",
                        "condition": current.condition.slug, "condition_text": current.condition.label,
                        "temperature": round(current.temperature), "feels_like": _rounded(current.feels_like),
                        "humidity": current.humidity, "wind_speed": _rounded(current.wind_speed),
                        "precipitation": current.precipitation,
                        "rain_risk": "pluie en cours" if current.precipitation else "pas de pluie", "units": units}
            place, forecast, label = service.forecast(day, moment, location)
        except WeatherError as exc:
            raise ToolError(exc.code, exc.message) from exc
        return {"type": "forecast", "location": place.name, "country": place.country, "period": label,
                "condition": forecast.condition.slug, "condition_text": forecast.condition.label,
                "temperature": round(forecast.temperature), "temperature_min": round(forecast.temperature_min),
                "temperature_max": round(forecast.temperature_max),
                "precipitation_probability": forecast.precipitation_probability,
                "precipitation": forecast.precipitation,
                "rain_risk": _rain_risk(forecast.precipitation_probability, forecast.precipitation),
                "wind_speed": _rounded(forecast.wind_speed), "units": units}

    return Tool(
        "get_weather",
        f"Donne la météo actuelle ou prévue (ville par défaut : {service.default_location}).",
        {"location": Param(str, "ville, uniquement si elle a été dite", required=False, max_length=60, check=_city,
                           evidence=_city_said),
         "day": Param(str, "today, tomorrow ou day_after_tomorrow", required=False, choices=tuple(DAYS),
                      evidence=_day_said),
         "moment": Param(str, "now (maintenant), morning, afternoon, evening ou day (journée entière)",
                         required=False, choices=("now", *MOMENTS), evidence=_moment_said)},
        {"type": "current ou forecast", "location": "ville", "period": "période", "condition_text": "temps",
         "temperature": "°C", "precipitation_probability": "% de risque de pluie",
         "rain_risk": "faible, possible, probable, pluie prévue ou en cours", "wind_speed": "km/h"},
        Risk.SAFE, run, say=weather_said)


RAIN_SAID = {"faible": "peu de risque de pluie", "possible": "pluie possible", "probable": "pluie probable",
             "pluie prévue": "pluie prévue", "pluie en cours": "il pleut"}


def weather_said(r: dict) -> str:
    """« Demain à Lyon : ciel couvert, de 12 à 21 degrés, pluie possible. » (sans LLM, rien d'inventé)."""
    unit = " degrés Fahrenheit" if r["units"]["temperature"] == "°F" else " degrés"
    if r["type"] == "current":
        parts = [r["condition_text"], f"{r['temperature']}{unit}"]
        if r["feels_like"] is not None and abs(r["feels_like"] - r["temperature"]) >= 3:
            parts.append(f"ressenti {r['feels_like']}")
        if r["precipitation"]:
            parts.append("il pleut")
        return f"À {r['location']}, {', '.join(parts)}."
    low, high = r["temperature_min"], r["temperature_max"]
    parts = [r["condition_text"], f"{low}{unit}" if low == high else f"de {low} à {high}{unit}"]
    if r["rain_risk"] in RAIN_SAID:
        parts.append(RAIN_SAID[r["rain_risk"]])
    period = r["period"]
    return f"{period[:1].upper()}{period[1:]} à {r['location']} : {', '.join(parts)}."


def _rounded(value: float | None) -> int | None:
    return round(value) if value is not None else None
