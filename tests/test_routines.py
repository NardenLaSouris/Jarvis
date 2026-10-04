"""Routines : validation stricte (outils sans confirmation uniquement), stockage, déclenchement et exécution."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis.events import ALL, EventBus  # noqa: E402
from jarvis.routines import (  # noqa: E402
    JsonRoutineStore, MemoryRoutineStore, RoutineEngine, RoutineError, describe, next_time, parse_routine,
)
from jarvis.tools import PermissionManager, ToolCore, ToolRegistry, builtin_tools  # noqa: E402
from jarvis.tools.lights import light_tools  # noqa: E402
from test_lights import FakeDriver, rooms_from  # noqa: E402
from test_tools import FakeProcesses, FakeVolume, Launcher, Locker  # noqa: E402

SOIR = {"name": "Soir", "description": "Ambiance du soir", "trigger": {"type": "time", "time": "21:00", "days": []},
        "actions": [{"type": "tool", "tool": "set_brightness", "parameters": {"room": "chambre", "brightness": 30}},
                    {"type": "tool", "tool": "set_brightness", "parameters": {"room": "entree", "brightness": 20}},
                    {"type": "wait", "seconds": 2},
                    {"type": "say", "text": "Bonne soirée, monsieur."}]}


class Setup:
    def __init__(self, routines=(), unreachable=(), now=datetime(2026, 10, 5, 20, 59)):
        self.driver = FakeDriver(unreachable=unreachable)
        registry = ToolRegistry()
        for tool in [*light_tools(rooms_from(), self.driver),
                     *builtin_tools(processes=FakeProcesses(), launcher=Launcher(), locker=Locker(), volume=FakeVolume())]:
            registry.register(tool)
        self.events, self.history, self.said, self.slept = EventBus(), [], [], []
        self.core = ToolCore(registry, PermissionManager(), events=self.events)
        self.events.subscribe(ALL, self.history.append)
        self.now = now
        self.store = MemoryRoutineStore(list(routines))
        self.engine = RoutineEngine(self.store, registry, self.core.submit, lambda title, text: self.said.append(text),
                                    self.events, clock=lambda: self.now, sleep=self.slept.append)


# --- Validation -----------------------------------------------------------------------------------

def test_valid_routine_is_normalised():
    setup = Setup()
    routine = parse_routine(SOIR, setup.core.registry)
    assert routine.enabled and routine.trigger == {"type": "time", "time": "21:00", "days": []}
    assert routine.actions[0] == {"type": "tool", "tool": "set_brightness", "parameters": {"room": "chambre", "brightness": 30}}
    assert [describe(a) for a in routine.actions[2:]] == ["attendre 2 s", "dire : Bonne soirée, monsieur."]


@pytest.mark.parametrize("change, message", [
    ({"name": ""}, "Nom"),
    ({"name": "x" * 61}, "Nom"),
    ({"trigger": {"type": "time", "time": "25:00"}}, "HH:MM"),
    ({"trigger": {"type": "time", "time": "07:00", "days": [7]}}, "jours"),
    ({"trigger": {"type": "interval", "minutes": 0}}, "Intervalle"),
    ({"trigger": {"type": "cron", "expr": "* * *"}}, "champs inconnus"),
    ({"actions": []}, "de 1 à 20"),
    ({"actions": [{"type": "shell", "command": "calc"}]}, "type"),
    ({"actions": [{"type": "tool", "tool": "rm_rf"}]}, "inconnu"),
    ({"actions": [{"type": "tool", "tool": "lock_pc"}]}, "confirmation"),
    ({"actions": [{"type": "tool", "tool": "close_application", "parameters": {"application": "discord"}}]},
     "confirmation"),
    ({"actions": [{"type": "tool", "tool": "set_brightness", "parameters": {"room": "chambre", "brightness": 300}}]},
     "set_brightness"),
    ({"actions": [{"type": "tool", "tool": "set_brightness", "parameters": {"room": "garage", "brightness": 30}}]},
     "set_brightness"),
    ({"actions": [{"type": "wait", "seconds": 3600}]}, "Attente"),
    ({"actions": [{"type": "say", "text": ""}]}, "Phrase"),
    ({"enabled": "oui"}, "enabled"),
    ({"owner": "x"}, "champs inconnus"),
])
def test_invalid_routines_are_refused(change, message):
    with pytest.raises(RoutineError, match=message):
        parse_routine({**SOIR, **change}, Setup().core.registry)


def test_next_time_respects_days():
    trigger = {"type": "time", "time": "07:00", "days": [0, 1, 2, 3, 4]}
    saturday = datetime(2026, 10, 3, 22, 0)
    assert next_time(trigger, saturday) == datetime(2026, 10, 5, 7, 0)
    assert next_time({"type": "time", "time": "23:00", "days": []}, saturday) == datetime(2026, 10, 3, 23, 0)
    assert next_time({"type": "manual"}, saturday) is None


# --- Gestion et stockage ---------------------------------------------------------------------------

def test_crud_enable_duplicate_and_storage(tmp_path):
    store = JsonRoutineStore(tmp_path / "routines.json")
    setup = Setup()
    engine = RoutineEngine(store, setup.core.registry, setup.core.submit, lambda t, x: None, clock=lambda: setup.now)
    created = engine.create(SOIR)
    assert created["next_run"] == "2026-10-05T21:00:00" and created["last_run"] is None
    engine.set_enabled(created["id"], False)
    copy = engine.duplicate(created["id"])
    assert copy["name"] == "Soir (copie)" and copy["enabled"] is False and copy["id"] != created["id"]
    engine.update(copy["id"], {**SOIR, "name": "Matin", "trigger": {"type": "time", "time": "07:30", "days": [0]}})
    reloaded = RoutineEngine(store, setup.core.registry, setup.core.submit, lambda t, x: None)
    names = {r["name"]: r for r in reloaded.list()}
    assert set(names) == {"Soir", "Matin"} and names["Soir"]["enabled"] is False
    engine.delete(created["id"])
    assert [r["name"] for r in json.loads((tmp_path / "routines.json").read_text(encoding="utf-8"))] == ["Matin"]
    with pytest.raises(KeyError):
        engine.get(created["id"])


def test_invalid_stored_routines_are_kept_but_ignored():
    setup = Setup(routines=[{**SOIR, "id": "abc"}, {"id": "bad", "name": "Cassée", "actions": "x"}])
    assert [r["id"] for r in setup.engine.list()] == ["abc"]
    setup.engine.create({**SOIR, "name": "Autre"})
    assert any(r.get("name") == "Cassée" for r in setup.store.routines)


# --- Exécution et historique -----------------------------------------------------------------------

def test_manual_run_executes_every_step_through_the_tool_core():
    setup = Setup(routines=[{**SOIR, "id": "soir"}])
    assert setup.engine.run("soir", wait=True)
    assert setup.driver.lights["chambre"]["brightness"] == 30 and setup.driver.lights["entree"]["brightness"] == 20
    assert setup.slept == [2] and setup.said == ["Bonne soirée, monsieur."]
    routine_events = [e for e in setup.history if e.type.startswith("routine.")]
    assert [e.type for e in routine_events] == ["routine.started"] + ["routine.step"] * 4 + ["routine.finished"]
    assert routine_events[-1].payload["success"] is True and routine_events[0].payload["source"] == "manual"
    assert any(e.type == "tool.executed" for e in setup.history)
    assert setup.engine.get("soir")["last_status"] == "success"


def test_failed_step_stops_the_routine():
    setup = Setup(routines=[{**SOIR, "id": "soir"}], unreachable={"chambre"})
    setup.engine.run("soir", wait=True)
    steps = [e.payload for e in setup.history if e.type == "routine.step"]
    assert len(steps) == 1 and steps[0]["success"] is False and "ne répond pas" in steps[0]["message"]
    assert setup.said == [] and setup.engine.get("soir")["last_status"] == "error"


def test_time_trigger_fires_once_per_minute_on_matching_days():
    setup = Setup(routines=[{**SOIR, "id": "soir", "trigger": {"type": "time", "time": "21:00", "days": [0]}},
                            {**SOIR, "id": "off", "enabled": False}])
    assert setup.engine.due(datetime(2026, 10, 5, 20, 59), 0) == []
    assert setup.engine.due(datetime(2026, 10, 5, 21, 0, 1), 0) == [("soir", "time")]
    assert setup.engine.due(datetime(2026, 10, 5, 21, 0, 30), 0) == []
    assert setup.engine.due(datetime(2026, 10, 6, 21, 0), 0) == []
    assert setup.engine.due(datetime(2026, 10, 12, 21, 0), 0) == [("soir", "time")]


def test_interval_trigger():
    setup = Setup(routines=[{**SOIR, "id": "boucle", "trigger": {"type": "interval", "minutes": 10}}])
    setup.engine._interval_from["boucle"] = 0
    assert setup.engine.due(setup.now, 599) == []
    assert setup.engine.due(setup.now, 600) == [("boucle", "interval")]
    assert setup.engine.due(setup.now, 700) == []


def test_a_running_routine_is_not_started_twice():
    setup = Setup(routines=[{**SOIR, "id": "soir"}])
    setup.engine._state["soir"] = {"running": True}
    assert setup.engine.run("soir") is False


def test_day_programme_can_leave_routines_out():
    setup = Setup(routines=[{**SOIR, "id": "soir1"},
                            {"id": "rv", "name": "Réveil", "trigger": {"type": "time", "time": "21:30", "days": []},
                             "actions": [{"type": "alarm"}]}])
    assert setup.engine.today() == ["routine « Soir » à 21 heures", "réveil à 21 h 30"]
    assert setup.engine.today(routines=False) == ["réveil à 21 h 30"]
