"""Météo : modèles, fournisseur Open-Meteo (réseau simulé), service, outil, planificateur, routage, événements.

Aucun test n'utilise Internet : Open-Meteo est simulé par httpx.MockTransport, le service par un faux fournisseur.
L'appel réel est dans scripts/weather_check.py (manuel).
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis.activity import ActivityLog, JsonlActivityStore  # noqa: E402
from jarvis.capabilities import CapabilityRegistry  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.events import ALL, EventBus  # noqa: E402
from jarvis.factory import build_weather  # noqa: E402
from jarvis.personality import load_personality  # noqa: E402
from jarvis.router import IntentRouter  # noqa: E402
from jarvis.tools import CONFIRM, DONE, REJECTED, PermissionManager, Risk, ToolCore, ToolRegistry, plan  # noqa: E402
from jarvis.tools.planner import plan_schema  # noqa: E402
from jarvis.tools.weather import weather_tool  # noqa: E402
from jarvis.weather import (  # noqa: E402
    Location, OpenMeteoProvider, WeatherCurrent, WeatherError, WeatherService, condition, period,
)
from jarvis.weather.models import HourlyPoint, summarize  # noqa: E402

PERSONALITY = load_personality(ROOT / "personality.toml")
NOW = datetime(2026, 10, 2, 15, 20)
NANTES = Location("Nantes", 47.21725, -1.55336, "FR", "Pays de la Loire", "Europe/Paris")


# --- Faux fournisseur --------------------------------------------------------------------------------

class FakeProvider:
    name = "faux"

    def __init__(self, error=None, unknown=()):
        self.error, self.unknown = error, set(unknown)
        self.calls = []

    def locate(self, query):
        self.calls.append(("locate", query))
        if query.lower() in self.unknown:
            raise WeatherError("unknown_location", f"Je ne trouve pas la ville « {query} ».")
        return replace(NANTES, name=query)

    def get_current(self, location):
        self.calls.append(("current", location.name))
        if self.error:
            raise self.error
        return WeatherCurrent(location.name, 17.24, 16.1, 72, 14.4, 0.0, condition(2), NOW)

    def get_forecast(self, location, start, end):
        self.calls.append(("forecast", location.name, start, end))
        if self.error:
            raise self.error
        points = [HourlyPoint(start.replace(hour=h), 12.0 + h - start.hour, 40, 0.5, 61, 18.0)
                  for h in range(start.hour, min(end.hour or 24, 24))]
        return summarize(location.name, points, start, end)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def service(provider=None, bus=None, monotonic=None):
    return WeatherService(provider or FakeProvider(), "Nantes", bus, current_ttl=300, forecast_ttl=900,
                          clock=lambda: NOW, monotonic=monotonic or Clock())


# --- Modèles -----------------------------------------------------------------------------------------

def test_conditions_and_summary():
    assert (condition(0).slug, condition(63).label, condition(95).slug) == ("clear", "pluie", "thunderstorm")
    assert condition(1234).slug == "unknown"
    start = datetime(2026, 10, 3, 6)
    points = [HourlyPoint(start.replace(hour=h), t, p, r, c, w)
              for h, t, p, r, c, w in [(6, 10.0, 10, 0.0, 2, 8.0), (7, 12.0, 60, 1.2, 61, 15.0), (8, 14.0, None, 0.3, 3, None),
                                       (13, 30.0, 99, 9.0, 95, 50.0)]]
    forecast = summarize("Nantes", points, start, start.replace(hour=12))
    assert (forecast.temperature, forecast.temperature_min, forecast.temperature_max) == (12.0, 10.0, 14.0)
    assert forecast.precipitation_probability == 60 and forecast.precipitation == 1.5
    assert forecast.condition.slug == "light_rain" and forecast.wind_speed == 15.0
    assert summarize("Nantes", points, start.replace(hour=20), start.replace(hour=22)) is None


# --- Périodes ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("day, moment, start, end, label", [
    ("today", "evening", (2, 18), (3, 0), "ce soir"),
    ("today", "afternoon", (2, 15), (2, 18), "cet après-midi"),
    ("today", "morning", (2, 6), (2, 12), "ce matin"),
    ("today", "day", (2, 15), (3, 0), "aujourd'hui"),
    ("tomorrow", "morning", (3, 6), (3, 12), "demain matin"),
    ("tomorrow", "day", (3, 0), (4, 0), "demain"),
    ("day_after_tomorrow", "evening", (4, 18), (5, 0), "après-demain soir"),
])
def test_periods_come_from_the_system_clock(day, moment, start, end, label):
    got_start, got_end, got_label = period(day, moment, NOW)
    assert (got_start.day, got_start.hour) == start and (got_end.day, got_end.hour) == end and got_label == label


def test_unknown_period():
    with pytest.raises(ValueError):
        period("next_week", "day", NOW)


# --- Fournisseur Open-Meteo (réseau simulé) ----------------------------------------------------------

def geocoding(results):
    return {"results": results, "generationtime_ms": 0.2}


CURRENT = {
    "current_units": {"temperature_2m": "°C", "apparent_temperature": "°C", "relative_humidity_2m": "%",
                      "wind_speed_10m": "km/h", "weather_code": "wmo code", "precipitation": "mm"},
    "current": {"time": "2026-10-02T15:15", "temperature_2m": 22.7, "apparent_temperature": 19.7,
                "relative_humidity_2m": 35, "wind_speed_10m": 10.5, "weather_code": 2, "precipitation": 0.0},
}
HOURLY = {
    "hourly_units": {"temperature_2m": "°C", "precipitation_probability": "%", "precipitation": "mm",
                     "weather_code": "wmo code", "wind_speed_10m": "km/h"},
    "hourly": {"time": [f"2026-10-03T{h:02d}:00" for h in range(24)],
               "temperature_2m": [10.0 + h * 0.5 for h in range(24)],
               "precipitation_probability": [h * 4 for h in range(24)],
               "precipitation": [0.2 if 8 <= h < 10 else 0.0 for h in range(24)],
               "weather_code": [61 if 8 <= h < 10 else 3 for h in range(24)],
               "wind_speed_10m": [10.0 + h for h in range(24)]},
}


def provider(handler, country="FR"):
    requests = []

    def record(request):
        requests.append(request)
        return handler(request)

    return OpenMeteoProvider(httpx.Client(transport=httpx.MockTransport(record)), country=country), requests


def json_reply(data, status=200):
    return lambda request: httpx.Response(status, json=data)


def test_locate_prefers_the_configured_country():
    p, requests = provider(json_reply(geocoding([
        {"name": "Nantes", "country_code": "US", "latitude": 40.0, "longitude": -100.0, "population": 900000},
        {"name": "Nantes", "country_code": "FR", "latitude": 47.2, "longitude": -1.55, "population": 325000,
         "admin1": "Pays de la Loire", "timezone": "Europe/Paris"},
        {"name": "Nantes", "country_code": "FR", "latitude": 46.0, "longitude": 0.5, "population": 200},
    ])))
    place = p.locate("Nantes")
    assert (place.name, place.country, place.latitude, place.timezone) == ("Nantes", "FR", 47.2, "Europe/Paris")
    url = requests[0].url
    assert url.host == "geocoding-api.open-meteo.com" and url.params["name"] == "Nantes" and url.params["language"] == "fr"
    tokyo, _ = provider(json_reply(geocoding([{"name": "Tokyo", "country_code": "JP", "latitude": 35.7,
                                               "longitude": 139.7, "population": 8336599}])))
    assert tokyo.locate("Tokyo").country == "JP"
    obscure, _ = provider(json_reply(geocoding([{"name": "Allion", "country_code": "CG", "latitude": -1.0,
                                                 "longitude": 15.0}])))
    with pytest.raises(WeatherError) as err:
        obscure.locate("Allion")
    assert err.value.code == "unknown_location"


@pytest.mark.parametrize("reply, code", [
    (geocoding([]), "unknown_location"), ({"generationtime_ms": 0.1}, "unknown_location"),
    (geocoding([{"name": "Nantes", "country_code": "FR", "latitude": 400, "longitude": 1}]), "invalid_response"),
    (geocoding([{"name": "Nantes", "country_code": "FR", "latitude": "47", "longitude": 1}]), "invalid_response"),
])
def test_locate_errors(reply, code):
    p, _ = provider(json_reply(reply))
    with pytest.raises(WeatherError) as err:
        p.locate("Nantes")
    assert err.value.code == code


def test_city_text_never_changes_the_url():
    p, requests = provider(json_reply(geocoding([])))
    with pytest.raises(WeatherError):
        p.locate("Nantes&latitude=0&url=http://evil")
    url = requests[0].url
    assert url.host == "geocoding-api.open-meteo.com" and url.params["name"] == "Nantes&latitude=0&url=http://evil"
    assert "url" not in url.params and url.scheme == "https"


def test_current_weather_is_parsed_in_french_units():
    p, requests = provider(json_reply(CURRENT))
    current = p.get_current(NANTES)
    assert (current.temperature, current.feels_like, current.humidity, current.wind_speed) == (22.7, 19.7, 35, 10.5)
    assert current.condition.slug == "partly_cloudy" and current.timestamp == datetime(2026, 10, 2, 15, 15)
    params = requests[0].url.params
    assert requests[0].url.host == "api.open-meteo.com"
    assert (params["temperature_unit"], params["wind_speed_unit"], params["precipitation_unit"]) == ("celsius", "kmh", "mm")
    assert params["latitude"] == "47.21725" and params["timezone"] == "Europe/Paris"


def test_forecast_window_is_summarized():
    p, requests = provider(json_reply(HOURLY))
    forecast = p.get_forecast(NANTES, datetime(2026, 10, 3, 6), datetime(2026, 10, 3, 12))
    assert (forecast.temperature_min, forecast.temperature_max) == (13.0, 15.5)
    assert forecast.precipitation_probability == 44 and forecast.precipitation == 0.4
    assert forecast.condition.slug == "light_rain" and forecast.wind_speed == 21.0
    assert requests[0].url.params["start_date"] == "2026-10-03"


@pytest.mark.parametrize("reply, code", [
    ({**CURRENT, "current_units": {**CURRENT["current_units"], "temperature_2m": "°F"}}, "invalid_response"),
    ({**CURRENT, "current_units": {**CURRENT["current_units"], "wind_speed_10m": "m/s"}}, "invalid_response"),
    ({**CURRENT, "current": {**CURRENT["current"], "temperature_2m": None}}, "incomplete_data"),
    ({**CURRENT, "current": "rien"}, "incomplete_data"),
    ({"current_units": CURRENT["current_units"]}, "incomplete_data"),
    ({"error": True, "reason": "Invalid"}, "invalid_response"),
])
def test_invalid_or_incomplete_current_data(reply, code):
    p, _ = provider(json_reply(reply))
    with pytest.raises(WeatherError) as err:
        p.get_current(NANTES)
    assert err.value.code == code


def test_invalid_forecast_data():
    broken = {**HOURLY, "hourly": {**HOURLY["hourly"], "temperature_2m": [1.0]}}
    p, _ = provider(json_reply(broken))
    with pytest.raises(WeatherError) as err:
        p.get_forecast(NANTES, datetime(2026, 10, 3, 6), datetime(2026, 10, 3, 12))
    assert err.value.code == "invalid_response"
    p, _ = provider(json_reply(HOURLY))
    with pytest.raises(WeatherError) as err:
        p.get_forecast(NANTES, datetime(2026, 10, 5, 6), datetime(2026, 10, 5, 12))
    assert err.value.code == "incomplete_data"


def raises(exc_type):
    def handler(request):
        raise exc_type("panne", request=request)
    return handler


@pytest.mark.parametrize("handler, code", [
    (raises(httpx.ReadTimeout), "weather_unavailable"), (raises(httpx.ConnectError), "weather_unavailable"),
    (json_reply({"error": True}, 500), "weather_unavailable"), (json_reply({}, 429), "rate_limited"),
    (lambda request: httpx.Response(200, text="<html>pas du json</html>"), "invalid_response"),
    (json_reply(["liste"]), "invalid_response"),
])
def test_network_and_http_errors(handler, code, caplog):
    p, _ = provider(handler)
    with pytest.raises(WeatherError) as err:
        p.get_current(NANTES)
    assert err.value.code == code
    assert "http" not in err.value.message.lower() and "Traceback" not in err.value.message


# --- Service ----------------------------------------------------------------------------------------

def test_default_and_explicit_location():
    fake = FakeProvider()
    s = service(fake)
    place, current = s.current()
    assert place.name == "Nantes" and current.temperature == 17.24
    place, _ = s.current("Lyon")
    assert place.name == "Lyon" and ("locate", "Lyon") in fake.calls


def test_cache_avoids_identical_calls_and_expires():
    fake, clock = FakeProvider(), Clock()
    s = service(fake, monotonic=clock)
    s.current()
    s.current()
    assert [c[0] for c in fake.calls] == ["locate", "current"]
    s.forecast("tomorrow", "morning")
    s.forecast("tomorrow", "morning")
    assert [c[0] for c in fake.calls].count("forecast") == 1
    clock.t += 301
    s.current()
    assert [c[0] for c in fake.calls].count("current") == 2
    s.forecast("tomorrow", "morning")
    assert [c[0] for c in fake.calls].count("forecast") == 1
    clock.t += 600
    s.forecast("tomorrow", "morning")
    assert [c[0] for c in fake.calls].count("forecast") == 2 and [c[0] for c in fake.calls].count("locate") == 1


def test_failures_are_not_cached():
    fake = FakeProvider(error=WeatherError("weather_unavailable", "indisponible"))
    s = service(fake)
    for _ in range(2):
        with pytest.raises(WeatherError):
            s.current()
    assert [c[0] for c in fake.calls].count("current") == 2


def test_events_requested_received_failed():
    bus = EventBus()
    seen = []
    bus.subscribe(ALL, seen.append)
    s = service(FakeProvider(unknown={"atlantide"}), bus)
    s.forecast("tomorrow", "morning", "Nantes")
    with pytest.raises(WeatherError):
        s.current("Atlantide")
    assert [e.type for e in seen] == ["weather.requested", "weather.received", "weather.requested", "weather.failed"]
    assert seen[1].payload == {"location": "Nantes", "request_type": "forecast", "period": "demain matin",
                               "subject": "météo Nantes (demain matin)"}
    assert seen[3].payload["error"] == "unknown_location" and seen[3].source == "weather"


def test_activity_log_records_weather_events(tmp_path):
    bus = EventBus()
    activity = ActivityLog(JsonlActivityStore(tmp_path / "activity.jsonl"))
    activity.attach(bus)
    s = service(FakeProvider(error=WeatherError("weather_unavailable", "indisponible")), bus)
    with pytest.raises(WeatherError):
        s.current()
    assert [e.line()[11:] for e in activity.recent()] == [
        "weather.requested — météo Nantes (maintenant)", "weather.failed — météo Nantes (maintenant) (weather_unavailable)"]


# --- Outil ------------------------------------------------------------------------------------------

def make_core(s=None):
    registry = ToolRegistry()
    registry.register(weather_tool(s or service()))
    return ToolCore(registry, PermissionManager())


def call(**parameters):
    return {"type": "tool_call", "tool": "get_weather", "parameters": parameters}


def test_get_weather_is_safe_and_never_asks():
    core = make_core()
    assert core.registry.get("get_weather").risk is Risk.SAFE
    outcome = core.submit(call())
    assert outcome.status == DONE and outcome.status != CONFIRM


def test_current_weather_without_location():
    result = make_core().submit(call()).result.result
    assert result == {"type": "current", "location": "Nantes", "country": "FR", "period": "maintenant",
                      "condition": "partly_cloudy",
                      "condition_text": "ciel partiellement nuageux", "temperature": 17, "feels_like": 16,
                      "humidity": 72, "wind_speed": 14, "precipitation": 0.0, "rain_risk": "pas de pluie",
                      "units": {"temperature": "°C", "wind_speed": "km/h", "precipitation": "mm", "humidity": "%",
                                "precipitation_probability": "%"}}


def test_forecast_with_location():
    fake = FakeProvider()
    result = make_core(service(fake)).submit(call(location="Lyon", day="tomorrow", moment="morning")).result.result
    assert result["type"] == "forecast" and result["location"] == "Lyon" and result["period"] == "demain matin"
    assert result["condition"] == "light_rain" and result["precipitation_probability"] == 40
    assert result["rain_risk"] == "pluie prévue"
    assert (result["temperature_min"], result["temperature_max"]) == (12, 17)
    assert fake.calls[-1][2] == datetime(2026, 10, 3, 6) and fake.calls[-1][3] == datetime(2026, 10, 3, 12)
    assert make_core().submit(call(day="tomorrow")).result.result["period"] == "demain"


@pytest.mark.parametrize("parameters", [
    {"day": "demain"}, {"moment": "soir"}, {"day": "tomorrow", "moment": "now"}, {"location": "Nantes&x=1"},
    {"location": "http://evil.example"}, {"location": "x" * 61}, {"location": "12345"}, {"location": ""},
    {"url": "https://api.open-meteo.com"}, {"latitude": 47.2}, {"day": 1},
])
def test_invalid_parameters(parameters):
    outcome = make_core().submit(call(**parameters))
    assert outcome.status in (REJECTED, DONE)
    assert not outcome.result.success and outcome.result.error == "invalid_parameters"


def test_provider_errors_become_honest_tool_failures():
    unavailable = service(FakeProvider(error=WeatherError("weather_unavailable",
                                                          "Je n'arrive pas à récupérer les données météo pour le moment.")))
    result = make_core(unavailable).submit(call()).result
    assert not result.success and result.error == "weather_unavailable"
    assert result.message == "Je n'arrive pas à récupérer les données météo pour le moment."
    result = make_core(service(FakeProvider(unknown={"atlantide"}))).submit(call(location="Atlantide")).result
    assert result.error == "unknown_location" and result.message == "Je ne trouve pas la ville « Atlantide »."


# --- Planificateur et routage ------------------------------------------------------------------------

class PlannerLLM:
    def __init__(self, proposal):
        self.proposal = proposal

    def chat_json(self, messages, schema):
        return self.proposal


def proposal(**parameters):
    return {"type": "tool_call", "tool": "get_weather", "parameters": parameters}


def test_schema_lists_allowed_days_and_moments():
    registry = make_core().registry
    params = plan_schema(registry)["anyOf"][1]["properties"]["parameters"]["properties"]
    assert params["day"]["enum"] == ["today", "tomorrow", "day_after_tomorrow"]
    assert params["moment"]["enum"] == ["now", "morning", "afternoon", "evening", "day"]
    assert params["location"] == {"type": "string"}


@pytest.mark.parametrize("text, parameters, accepted", [
    ("Quel temps fait-il ?", {}, True),
    ("Quel temps fait-il ?", {"location": "Paris"}, False),
    ("Quel temps fera-t-il à Lyon demain ?", {"location": "Lyon", "day": "tomorrow"}, True),
    ("Quel temps fait-il ?", {"day": "tomorrow"}, False),
    ("Il va pleuvoir après-demain ?", {"day": "tomorrow"}, False),
    ("Il va pleuvoir après-demain ?", {"day": "day_after_tomorrow"}, True),
    ("Est-ce qu'il va pleuvoir ce soir ?", {"moment": "evening"}, True),
    ("Quel temps fera-t-il demain ?", {"day": "tomorrow", "moment": "evening"}, False),
    ("Quelle température est prévue demain matin ?", {"day": "tomorrow", "moment": "morning"}, True),
    ("Quel temps fera-t-il à nant de demain ?", {"location": "Nantes", "day": "tomorrow"}, True),
    ("Quel temps fera-t-il demain ?", {"location": "Nantes", "day": "tomorrow"}, False),
])
def test_planner_rejects_unsaid_cities_days_and_moments(text, parameters, accepted):
    registry = make_core().registry
    result = plan(PlannerLLM(proposal(**parameters)), text, registry)
    assert (result is not None) == accepted


@pytest.mark.parametrize("text, route", [
    ("Quel temps fait-il ?", "tool:tool.action"), ("Il fait combien dehors ?", "tool:tool.action"),
    ("Est-ce qu'il va pleuvoir ce soir ?", "tool:tool.action"),
    ("Quelle température est prévue demain matin ?", "tool:tool.action"),
    ("Recherche-moi les prévisions météo pour Nantes", "web.search"),
    ("Cherche la météo de Lyon sur internet", "web.search"),
])
def test_routing_weather_versus_web_search(text, route):
    router = IntentRouter(PERSONALITY, CapabilityRegistry(), web_enabled=True, tools=("get_weather",))
    assert router.route(text).label == route


def test_without_tools_weather_questions_keep_the_previous_route():
    router = IntentRouter(PERSONALITY, CapabilityRegistry(), web_enabled=True)
    assert router.route("Quelle est la météo à Nantes ?").label == "web.search"


# --- Agent et configuration --------------------------------------------------------------------------

def test_agent_answers_from_the_weather_tool():
    from test_tools import PlannerLLM as AgentLLM
    from test_tools import result_sent_to_llm, run_agent

    llm = AgentLLM({"type": "tool_call", "tool": "get_weather", "parameters": {"day": "tomorrow"}},
                   reply="Demain à Nantes, comptez environ 14 degrés avec de la pluie faible.")
    spoken, events = run_agent(["Jarvis, quel temps fera-t-il demain ?"], llm, make_core())
    assert spoken == ["Demain à Nantes, comptez environ 14 degrés avec de la pluie faible."]
    sent = result_sent_to_llm(llm)
    assert sent["success"] is True and sent["result"]["period"] == "demain" and sent["result"]["location"] == "Nantes"


def test_configuration():
    cfg = load_config(ROOT / "config.toml")
    assert cfg.weather.enabled and cfg.weather.provider == "open-meteo" and cfg.weather.default_location
    weather = build_weather(cfg, None)
    assert isinstance(weather.provider, OpenMeteoProvider) and weather.default_location == cfg.weather.default_location
    assert build_weather(replace(cfg, weather=replace(cfg.weather, enabled=False)), None) is None
    with pytest.raises(ValueError):
        build_weather(replace(cfg, weather=replace(cfg.weather, provider="inconnu")), None)
    assert "api_key" not in (ROOT / "config.toml").read_text(encoding="utf-8").lower().split("[weather]")[1].split("[")[0]


def test_rain_risk_is_deterministic():
    from jarvis.tools.weather import _rain_risk

    assert [_rain_risk(p) for p in (0, 29, 30, 59, 60, 100)] == ["faible", "faible", "possible", "possible",
                                                                "probable", "probable"]
    assert _rain_risk(10, 0.4) == "pluie prévue" and _rain_risk(None) is None


def test_temperatures_end_sentences_so_answers_stay_short():
    from jarvis.streaming import split_sentences

    assert split_sentences("Demain il fera 24°C. Il y aura 23% de pluie. Le vent sera faible.") == [
        "Demain il fera 24°C.", "Il y aura 23% de pluie.", "Le vent sera faible."]
    assert split_sentences("Voir cf. la page 3. Fin.") == ["Voir cf. la page 3. Fin."]


def test_weather_failure_is_spoken_honestly_without_the_llm():
    from test_tools import PlannerLLM as AgentLLM
    from test_tools import run_agent

    broken = service(FakeProvider(error=WeatherError("weather_unavailable",
                                                     "Je n'arrive pas à récupérer les données météo pour le moment.")))
    llm = AgentLLM({"type": "tool_call", "tool": "get_weather", "parameters": {}},
                   reply="Il fait beau et 20 degrés, monsieur.")
    spoken, events = run_agent(["Quel temps fait-il ?"], llm, make_core(broken))
    assert spoken == ["Je n'arrive pas à récupérer les données météo pour le moment."] and llm.calls == []
