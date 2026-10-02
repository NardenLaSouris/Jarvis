"""Test manuel de la météo réelle (appelle Open-Meteo) : hors de la suite de tests automatiques.

Usage : python scripts/weather_check.py [ville]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.config import load_config  # noqa: E402
from jarvis.factory import build_weather  # noqa: E402
from jarvis.weather import WeatherError  # noqa: E402


def main() -> int:
    cfg = load_config(ROOT / "config.toml")
    weather = build_weather(cfg, None)
    if weather is None:
        print("Météo désactivée dans config.toml ([weather] enabled).")
        return 1
    city = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        place, current = weather.current(city)
        print(f"{place.name} ({place.region}, {place.country}) — maintenant : {current.temperature} °C, "
              f"ressenti {current.feels_like} °C, {current.condition.label}, humidité {current.humidity} %, "
              f"vent {current.wind_speed} km/h")
        for day, moment in (("today", "evening"), ("tomorrow", "morning"), ("tomorrow", "day"),
                            ("day_after_tomorrow", "day")):
            _, forecast, label = weather.forecast(day, moment, city)
            print(f"  {label} : {forecast.temperature_min:.0f} à {forecast.temperature_max:.0f} °C, "
                  f"{forecast.condition.label}, pluie {forecast.precipitation_probability} % "
                  f"({forecast.precipitation} mm), vent jusqu'à {forecast.wind_speed} km/h")
    except WeatherError as exc:
        print(f"Échec ({exc.code}) : {exc.message}")
        return 1
    print("Données météo : Open-Meteo.com (CC-BY 4.0)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
