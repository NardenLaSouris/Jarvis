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
    options = {o["properties"]["tool"]["const"]: o["properties"]["parameters"] for o in plan_schema(registry)["anyOf"][1:-1]}
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
                                                          "set_color", "set_color_temperature", "set_scene", "light_status"]


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
    assert bulb.calls == [("set_white_percentage", (30, 100)), ("set_hsv", (120 / 360, 1.0, 0.3))]


def test_tuya_driver_reads_colour_brightness():
    from jarvis.tools.lights import Room

    class Bulb:
        def status(self):
            return {"dps": {"20": True, "21": "colour", "22": 1000, "24": "00f003e801f4"}}

    driver = TuyaDriver()
    room = Room("chambre", "la chambre", "id", "1.2.3.4", 3.5, "k")
    driver._bulbs["chambre"] = Bulb()
    assert driver.state(room) == {"on": True, "brightness": 50, "mode": "colour"}


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


# --- Demandes à plusieurs actions -----------------------------------------------------------------

def several(*calls):
    return {"type": "tool_calls", "calls": [{"tool": tool, "parameters": params, "segment": segment}
                                            for tool, params, segment in calls]}


def test_compound_request_runs_every_action_in_order_on_the_same_room():
    driver = FakeDriver()
    llm = PlannerLLM(several(("light_on", {"brightness": 30}, "allume la chambre à 30 %"),
                             ("set_color", {"color": "bleu"}, "en bleu")))
    spoken, events = run_agent(["Jarvis, allume la chambre à 30 %, en bleu."], llm, core_with(driver))
    assert driver.calls[:2] == [("chambre", "power", True), ("chambre", "brightness", 30)]
    assert driver.calls[-1] == ("chambre", "colour", 240) and {c[0] for c in driver.calls} == {"chambre"}
    assert driver.lights["entree"]["on"] is False and llm.calls == []
    assert spoken == ["La lumière de la chambre est allumée à 30 %.", "La lumière de la chambre est en bleu."]


def test_each_action_targets_the_room_of_its_own_words():
    driver = FakeDriver()
    driver.lights["entree"]["on"] = True
    llm = PlannerLLM(several(("light_on", {}, "allume la chambre"), ("light_off", {}, "éteins l'entrée")))
    run_agent(["Allume la chambre et éteins l'entrée."], llm, core_with(driver))
    assert driver.calls == [("chambre", "power", True), ("entree", "power", False)]


def test_compound_request_stops_at_the_first_failure():
    driver = FakeDriver(unreachable={"chambre"})
    llm = PlannerLLM(several(("light_on", {}, "allume la chambre"), ("light_off", {}, "éteins l'entrée")))
    spoken, _ = run_agent(["Allume la chambre et éteins l'entrée."], llm, core_with(driver))
    assert spoken == ["La lumière de la chambre ne répond pas."] and driver.calls == []


def test_compound_plan_with_an_invented_segment_falls_back_to_the_whole_request():
    from jarvis.tools.planner import plan

    registry = core_with(FakeDriver()).registry
    data = plan(PlannerLLM(several(("light_on", {}, "allume le garage"), ("set_color", {"color": "rouge"}, "en rouge"))),
                "Allume l'entrée en rouge", registry)
    assert [c["segment"] for c in data["calls"]] == ["Allume l'entrée en rouge", "en rouge"]
    single = plan(PlannerLLM(several(("light_on", {}, "allume"), ("set_brightness", {"brightness": 80}, "à 80 %"))),
                  "Allume la chambre", registry)
    assert single["tool"] == "light_on"


# --- Scènes, groupes, état, pièces inconnues, pannes partielles ------------------------------------

def test_scene_sets_power_white_and_brightness_on_each_light():
    driver = FakeDriver()
    spoken, _, _ = converse(["Mets la chambre en mode cinéma."], [("set_scene", {"scene": "cinema"})], driver)
    assert spoken == ["La lumière de la chambre est en ambiance cinéma."]
    assert driver.calls == [("chambre", "power", True), ("chambre", "white", 2700), ("chambre", "brightness", 10)]
    assert driver.lights["entree"]["on"] is False


def test_custom_scenes_override_and_validate():
    from jarvis.tools.lights import load_scenes

    scenes = load_scenes({"cinema": {"brightness": 20}, "fete": {"name": "fête", "color": "violet", "brightness": 80}})
    assert scenes["cinema"]["brightness"] == 20 and scenes["cinema"]["temperature"] == 2700
    assert scenes["fete"]["color"] == "violet"
    for bad in ({"x": {"brightness": 0}}, {"x": {"temperature": 9000}}, {"x": {"color": "fuchsia"}}):
        with pytest.raises(ValueError):
            load_scenes(bad)


def test_groups_target_their_rooms_and_are_validated():
    rooms = load_rooms(ROOMS, lambda key: "k" * 16, {"etage": {"name": "l'étage", "rooms": ["chambre", "entree"],
                                                                "aliases": ["en haut"]}})
    assert rooms.resolve("Éteins en haut") == "etage" and [r.key for r in rooms.targets("etage")] == ["chambre", "entree"]
    with pytest.raises(ValueError):
        load_rooms(ROOMS, lambda key: "k" * 16, {"x": {"rooms": ["garage"]}})


def test_unknown_room_is_refused_instead_of_lighting_the_whole_house():
    driver = FakeDriver()
    spoken, _, _ = converse(["Mets la lumière du bureau à 30 %."], [("set_brightness", {"brightness": 30})], driver)
    assert driver.calls == [] and "bureau" in spoken[0]
    assert rooms_from().resolve("Allume la lumière") == "all"


def test_status_is_read_from_the_bulbs_and_stored_in_the_home_state():
    from jarvis.home import HomeState

    driver = FakeDriver(unreachable={"entree"})
    driver.lights["chambre"].update(on=True, brightness=30)
    home = HomeState()
    registry = ToolRegistry()
    for tool in light_tools(rooms_from(), driver, home=home):
        registry.register(tool)
    core = ToolCore(registry, PermissionManager())
    outcome = core.submit({"tool": "light_status", "parameters": {"room": "chambre"}})
    assert outcome.result.message == "La lumière de la chambre est allumée à 30 %."
    assert home.get("lights.chambre.power") == {**home.get("lights.chambre.power"), "value": True, "confirmed": True}
    outcome = core.submit({"tool": "light_status", "parameters": {"room": "all"}})
    assert "ne répond pas" in outcome.result.message and "30 %" in outcome.result.message
    outcome = core.submit({"tool": "light_status", "parameters": {"room": "entree"}})
    assert not outcome.result.success
    core.submit({"tool": "light_off", "parameters": {"room": "chambre"}})
    assert home.get("lights.chambre.power")["value"] is False and home.get("lights.chambre.power")["confirmed"] is False


def test_one_unreachable_light_does_not_stop_the_others():
    driver = FakeDriver(unreachable={"entree"})
    spoken, _, _ = converse(["Allume la lumière."], [("light_on", {})], driver)
    assert driver.lights["chambre"]["on"] is True
    assert spoken == ["La lumière de la chambre est allumée. Mais la lumière de l'entrée ne répond pas."]


def test_all_lights_are_commanded_in_parallel():
    import threading
    import time

    class SlowDriver(FakeDriver):
        def power(self, room, on):
            time.sleep(0.3)
            super().power(room, on)

    table = {**ROOMS, **{f"piece{i}": {"name": f"la pièce {i}", "device_id": f"d{i}", "ip": f"10.0.0.{i}"}
                         for i in range(2)}}
    driver = SlowDriver()
    driver.lights.update({k: {"on": False, "brightness": 100, "mode": "white", "hue": None, "kelvin": 2700}
                          for k in table})
    registry = ToolRegistry()
    for tool in light_tools(rooms_from(table), driver):
        registry.register(tool)
    started = time.perf_counter()
    outcome = ToolCore(registry, PermissionManager()).submit({"tool": "light_off", "parameters": {"room": "all"}})
    assert outcome.result.success and time.perf_counter() - started < 0.9 and threading.active_count() >= 1


def test_one_command_at_a_time_per_bulb_with_one_retry():
    import threading
    import time

    from jarvis.tools.lights import Room

    class Bulb:
        def __init__(self):
            self.busy, self.overlaps, self.calls = False, 0, 0

        def status(self):
            self.calls += 1
            if self.busy:
                self.overlaps += 1
            self.busy = True
            time.sleep(0.01)
            self.busy = False
            if self.calls == 1:
                return {"Error": "Unexpected Payload from Device"}  # erreur passagère : nouvel essai
            return {"dps": {"20": True, "21": "white", "22": 500}}

    driver, bulb = TuyaDriver(), Bulb()
    room = Room("chambre", "la chambre", "id", "1.2.3.4", 3.5, "k")
    driver._bulbs["chambre"] = bulb
    threads = [threading.Thread(target=lambda: driver.state(room)) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert bulb.overlaps == 0 and bulb.calls == 13  # 12 lectures, une erreur rattrapée, jamais deux à la fois
