"""Lumières : pièce déduite de la demande, outils sans LLM, phrases fixes ; pilote simulé (aucune ampoule réelle)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis.capabilities import CapabilityRegistry  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.router import IntentRouter  # noqa: E402
from jarvis.tools import PermissionManager, ToolCore, ToolError, ToolRegistry, ToolsCapability  # noqa: E402
from jarvis.tools.lights import TuyaDriver, light_tools, load_rooms, of  # noqa: E402
from jarvis.tools.planner import plan_schema, planner_prompt  # noqa: E402
from test_tools import PERSONALITY, PlannerLLM, routes, run_agent  # noqa: E402

ROOMS = {
    "chambre": {"name": "la chambre", "device_id": "bf-chambre", "ip": "192.168.1.174", "aliases": ["ma chambre"]},
    "entree": {"name": "l'entrée", "device_id": "bf-entree", "ip": "192.168.1.142", "aliases": ["entrée", "le couloir"]},
}


class FakeDriver:
    def __init__(self, unreachable=()):
        self.lights = {key: {"on": False, "brightness": 100, "mode": "white", "hue": None, "kelvin": 2700}
                       for key in ROOMS}
        self.unreachable = set(unreachable)
        self.calls = []

    def _light(self, room):
        if room.key in self.unreachable:
            raise ToolError("light_unreachable", f"La lumière {of(room.name)} ne répond pas.")
        return self.lights[room.key]

    def state(self, room):
        light = self._light(room)
        return {"on": light["on"], "brightness": light["brightness"], "mode": light["mode"]}

    def power(self, room, on):
        self._light(room)["on"] = on
        self.calls.append((room.key, "power", on))

    def brightness(self, room, percent):
        self._light(room)["brightness"] = percent
        self.calls.append((room.key, "brightness", percent))

    def colour(self, room, hue):
        self._light(room).update(mode="colour", hue=hue)
        self.calls.append((room.key, "colour", hue))

    def white(self, room, kelvin):
        self._light(room).update(mode="white", kelvin=kelvin)
        self.calls.append((room.key, "white", kelvin))


def rooms_from(table=ROOMS):
    return load_rooms(table, lambda key: f"cle-{key}-0123456789")


def core_with(driver, table=ROOMS):
    registry = ToolRegistry()
    for tool in light_tools(rooms_from(table), driver):
        registry.register(tool)
    return ToolCore(registry, PermissionManager())


def converse(texts, plans, driver, table=ROOMS):
    llm = PlannerLLM(*[{"type": "tool_call", "tool": tool, "parameters": params} for tool, params in plans])
    spoken, events = run_agent(texts, llm, core_with(driver, table))
    return spoken, events, llm


# --- Pièces ---------------------------------------------------------------------------------------

def test_room_is_found_in_the_request_or_asked():
    rooms = rooms_from()
    for text, key in (("Allume la chambre.", "chambre"), ("Éteins ma chambre", "chambre"),
                      ("Mets l'entrée en vert", "entree"), ("Allume le couloir à 30 %", "entree"),
                      ("Éteins toutes les lumières", "all"), ("Éteins les lumières", "all"),
                      ("Allume la lumière", "all"), ("Mets la lumière en bleu", "all")):
        assert rooms.resolve(text) == key, text
    single = rooms_from({"chambre": ROOMS["chambre"]})
    assert single.resolve("Allume la lumière") == "chambre"


def test_room_names_are_spoken_correctly():
    assert (of("la chambre"), of("l'entrée"), of("le salon"), of("les toilettes")) == (
        "de la chambre", "de l'entrée", "du salon", "des toilettes")


@pytest.mark.parametrize("table, message", [
    ({"chambre": {"name": "la chambre", "ip": "192.168.1.174"}}, "requis"),
    ({"all": {"name": "tout", "device_id": "x", "ip": "1.2.3.4"}}, "invalide"),
])
def test_invalid_rooms_are_refused(table, message):
    with pytest.raises(ValueError, match=message):
        rooms_from(table)


def test_missing_local_key_is_refused():
    with pytest.raises(ValueError, match="clé locale"):
        load_rooms(ROOMS, lambda key: "")


# --- Outils par la conversation -------------------------------------------------------------------

def test_light_on_with_brightness_in_the_named_room():
    driver = FakeDriver()
    spoken, events, llm = converse(["Jarvis, allume la chambre à 30 %."], [("light_on", {"brightness": 30})], driver)
    assert spoken == ["La lumière de la chambre est allumée à 30 %."] and llm.calls == []
    assert driver.calls == [("chambre", "power", True), ("chambre", "brightness", 30)]
    assert routes(events) == ["tool:tool.action"]


def test_all_lights_off_and_toggle():
    driver = FakeDriver()
    driver.lights["chambre"]["on"] = True
    spoken, _, _ = converse(["Éteins toutes les lumières.", "Allume ou éteins l'entrée."],
                            [("light_off", {}), ("light_toggle", {})], driver)
    assert spoken == ["Toutes les lumières sont éteintes.", "La lumière de l'entrée est allumée."]
    assert driver.lights["chambre"]["on"] is False and driver.lights["entree"]["on"] is True


def test_colours_whites_and_kelvins():
    driver = FakeDriver()
    spoken, _, _ = converse(["Mets l'entrée en vert.", "Remets l'entrée en blanc chaud.", "Mets la chambre à 5000 kelvins."],
                            [("set_color", {"color": "vert"}), ("set_color", {"color": "blanc chaud"}),
                             ("set_color_temperature", {"temperature": 5000})], driver)
    assert spoken == ["La lumière de l'entrée est en vert.", "La lumière de l'entrée est en blanc chaud.",
                      "La lumière de la chambre est à 5000 kelvins."]
    assert ("entree", "colour", 120) in driver.calls and ("entree", "white", 2700) in driver.calls
    assert ("chambre", "white", 5000) in driver.calls and driver.lights["chambre"]["on"] is True


def test_brightness_without_a_number_never_reaches_the_lights():
    driver = FakeDriver()
    spoken, _, _ = converse(["Baisse la lumière de la chambre."], [("set_brightness", {"brightness": 40})], driver)
    assert driver.calls == []


def test_without_room_all_lights_are_used():
    driver = FakeDriver()
    spoken, _, _ = converse(["Allume la lumière.", "Mets la lumière en bleu."],
                            [("light_on", {}), ("set_color", {"color": "bleu"})], driver)
    assert spoken == ["Toutes les lumières sont allumées.", "Toutes les lumières sont en bleu."]
    assert all(light["on"] and light["hue"] == 240 for light in driver.lights.values())


def test_unreachable_light_is_reported():
    driver = FakeDriver(unreachable={"chambre"})
    spoken, _, _ = converse(["Allume la chambre."], [("light_on", {})], driver)
    assert spoken == ["La lumière de la chambre ne répond pas."]


def test_the_room_is_never_chosen_by_the_llm():
    registry = core_with(FakeDriver()).registry
    assert "room" not in planner_prompt(registry)
    options = {o["properties"]["tool"]["const"]: o["properties"]["parameters"] for o in plan_schema(registry)["anyOf"][1:]}
    assert set(options["set_color"]["properties"]) == {"color"} and "vert" in options["set_color"]["properties"]["color"]["enum"]


# --- Routage, capacités, configuration -------------------------------------------------------------

def test_light_requests_go_to_the_tools_and_home_automation_is_no_longer_planned():
    registry = core_with(FakeDriver()).registry
    capabilities = CapabilityRegistry()
    capabilities.register(ToolsCapability(registry))
    router = IntentRouter(PERSONALITY, capabilities, tools=tuple(t.name for t in registry.list()))
    for text in ("Allume la chambre.", "Éteins les lumières.", "Mets l'entrée en vert."):
        assert router.route(text).label == "tool:tool.action", text
    assert "piloter vos lumières" in next(iter(capabilities)).description
    assert "la domotique" not in [label for label, _ in capabilities.planned()]
    without = IntentRouter(PERSONALITY, CapabilityRegistry())
    assert without.route("Allume la lumière du salon.").label.startswith("unavailable")


def test_configuration_and_keys(monkeypatch):
    import jarvis.factory as factory
    from dataclasses import replace

    cfg = load_config(ROOT / "config.toml", local=False)
    assert cfg.lights.enabled is False and factory.build_lights(cfg) == (None, None)
    on = replace(cfg, lights=replace(cfg.lights, enabled=True, rooms=ROOMS))
    monkeypatch.setattr(factory, "secret", lambda name, env: {"LIGHT_KEY_CHAMBRE": "k" * 16, "LIGHT_KEY_ENTREE": "e" * 16}
                        .get(name, ""))
    assert [t.name for t in factory.light_tools_for(*factory.build_lights(on))] == ["light_on", "light_off", "light_toggle", "set_brightness",
                                                          "set_color", "set_color_temperature"]


def test_tuya_driver_converts_scales():
    from jarvis.tools.lights import Room

    class Bulb:
        def __init__(self):
            self.calls = []

        def status(self):
            return {"dps": {"20": True, "21": "white", "22": 300}}

        def __getattr__(self, name):
            return lambda *args: self.calls.append((name, args)) or {}

    driver, bulb = TuyaDriver(), Bulb()
    room = Room("chambre", "la chambre", "id", "1.2.3.4", 3.5, "k")
    driver._bulbs["chambre"] = bulb
    assert driver.state(room) == {"on": True, "brightness": 30, "mode": "white"}
    driver.white(room, 6500)
    driver.colour(room, 120)
    assert bulb.calls == [("set_white_percentage", (30, 100)), ("set_hsv", (120 / 360, 1.0, 1.0))]


def test_invented_brightness_is_dropped_and_the_light_still_turns_on():
    driver = FakeDriver()
    spoken, _, _ = converse(["Allume l'entrée."], [("light_on", {"brightness": 100})], driver)
    assert spoken == ["La lumière de l'entrée est allumée."] and driver.calls == [("entree", "power", True)]


def test_brightness_alone_sets_every_light():
    driver = FakeDriver()
    spoken, events, _ = converse(["Luminosité 30 %.", "Mets la luminosité au maximum."],
                                 [("set_brightness", {"brightness": 30}), ("set_brightness", {"brightness": 100})], driver)
    assert spoken == ["Toutes les lumières sont à 30 %.", "Toutes les lumières sont à 100 %."]
    assert routes(events) == ["tool:tool.action"] * 2
    assert all(light["on"] and light["brightness"] == 100 for light in driver.lights.values())
