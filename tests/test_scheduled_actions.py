"""Actions programmées par la voix, rappels à heure précise, routines par la voix (tout simulé, silencieux)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis.scheduling import TimerManager  # noqa: E402
from jarvis.scheduling.actions import split_schedule  # noqa: E402
from jarvis.tools.quick import quick_plan  # noqa: E402
from jarvis.tools.routines import routine_tools  # noqa: E402
from jarvis.tools.timers import timer_tools  # noqa: E402
from test_routines import SOIR, Setup  # noqa: E402
from test_tools import PlannerLLM, routes, run_agent  # noqa: E402

NOW = datetime(2026, 10, 4, 20, 15)


@pytest.mark.parametrize("text, trigger, command, said", [
    ("Dans 10 minutes, allume la lumière de la chambre", {"type": "at", "at": "2026-10-04T20:25:00"},
     "allume la lumière de la chambre", "dans 10 minutes"),
    ("Allume la chambre dans un quart d'heure", {"type": "at", "at": "2026-10-04T20:30:00"}, "Allume la chambre",
     "dans 15 minutes"),
    ("Tous les jours à 21h, mets la lumière de la chambre à 30%", {"type": "time", "time": "21:00", "days": []},
     "mets la lumière de la chambre à 30%", "tous les jours à 21 heures"),
    ("Éteins tout à 23 h", {"type": "at", "at": "2026-10-04T23:00:00"}, "Éteins tout", "aujourd'hui à 23 heures"),
    ("Demain à 7 h 30, allume l'entrée", {"type": "at", "at": "2026-10-05T07:30:00"}, "allume l'entrée",
     "demain à 7 h 30"),
    ("Ce soir à 9 heures, mets le son à 20 %", {"type": "at", "at": "2026-10-04T21:00:00"}, "mets le son à 20 %",
     "aujourd'hui à 21 heures"),
    ("En semaine à 7 h, allume la chambre", {"type": "time", "time": "07:00", "days": [0, 1, 2, 3, 4]},
     "allume la chambre", "en semaine à 7 heures"),
    ("À 8 h, allume la chambre", {"type": "at", "at": "2026-10-05T08:00:00"}, "allume la chambre", "demain à 8 heures"),
])
def test_time_expressions_are_split_from_the_command(text, trigger, command, said):
    schedule = split_schedule(text, NOW)
    assert (schedule.trigger, schedule.command, schedule.said) == (trigger, command, said)


@pytest.mark.parametrize("text", ["Rappelle-moi dans 20 minutes de sortir", "Mets un minuteur de 10 minutes",
                                  "Réveille-moi à 7 h", "Allume la chambre", "Mets la lumière à 30 %",
                                  "Mets le son à 40"])
def test_requests_without_schedule_are_left_alone(text):
    assert split_schedule(text, NOW) is None


def agent_setup():
    setup = Setup(now=NOW)
    for tool in routine_tools(setup.engine):
        setup.core.registry.register(tool)
    return setup


def test_scheduled_action_is_saved_then_run_by_the_core_at_the_right_time():
    setup = agent_setup()
    spoken, events = run_agent(["Dans 10 minutes, allume la chambre à 40 %."], PlannerLLM(), setup.core,
                               fast_path=True, routines=setup.engine)
    assert spoken == ["C'est programmé dans 10 minutes : allume la chambre à 40 %."]
    assert setup.driver.calls == []
    routine = setup.engine.routines()[0]
    assert routine.once and routine.trigger == {"type": "at", "at": "2026-10-04T20:25:00"}
    assert routine.actions == ({"type": "tool", "tool": "light_on", "parameters": {"brightness": 40, "room": "chambre"}},)
    assert setup.engine.due(NOW + timedelta(minutes=9), 0) == []
    due = setup.engine.due(NOW + timedelta(minutes=10), 0)
    assert due == [(routine.id, "time")]
    setup.engine.run(routine.id, "time", wait=True)
    assert setup.driver.calls == [("chambre", "power", True), ("chambre", "brightness", 40)]
    assert setup.engine.routines() == []  # ponctuelle : effacée après exécution
    assert "tool:schedule (dans 10 minutes)" in routes(events)


def test_daily_schedule_by_voice():
    setup = agent_setup()
    spoken, _ = run_agent(["Tous les jours à 21 h, mets la lumière de l'entrée à 30 %."], PlannerLLM(), setup.core,
                          fast_path=True, routines=setup.engine)
    routine = setup.engine.routines()[0]
    assert routine.trigger == {"type": "time", "time": "21:00", "days": []} and not routine.once
    assert routine.actions[0]["parameters"] == {"brightness": 30, "room": "entree"}
    assert spoken[0].startswith("C'est programmé tous les jours à 21 heures")


def test_actions_needing_a_confirmation_cannot_be_scheduled():
    setup = agent_setup()
    spoken, _ = run_agent(["Dans 5 minutes, verrouille le PC."], PlannerLLM(), setup.core, fast_path=True,
                          routines=setup.engine)
    assert setup.engine.routines() == [] and "confirmation" in spoken[0]


def test_missed_one_shot_action_is_dropped_not_run_late():
    setup = agent_setup()
    setup.engine.create({"name": "x", "trigger": {"type": "at", "at": "2026-10-04T20:00:00"},
                         "actions": [{"type": "tool", "tool": "light_on", "parameters": {"room": "chambre"}}]})
    assert setup.engine.due(NOW, 0) == [] and setup.engine.routines() == []


def test_routines_by_voice_list_run_and_delete_with_confirmation():
    setup = Setup(routines=[{**SOIR, "id": "soir1"}], now=NOW)
    for tool in routine_tools(setup.engine):
        setup.core.registry.register(tool)
    core = setup.core
    assert core.submit({"tool": "list_routines"}).result.message == \
        "Vous avez 1 routine : « Soir », aujourd'hui à 21 heures."
    assert core.submit({"tool": "run_routine", "parameters": {"name": "soir"}}).result.success
    outcome = core.submit({"tool": "delete_routine", "parameters": {"name": "soir"}})
    assert outcome.status == "confirm" and len(setup.engine.routines()) == 1
    assert core.answer("oui").result.message == "La routine « Soir » est supprimée." and setup.engine.routines() == []
    assert not core.submit({"tool": "run_routine", "parameters": {"name": "matin"}}).result.success


def test_reminder_at_a_given_time():
    manager = TimerManager(clock=lambda: NOW)
    tools = {t.name: t for t in timer_tools(manager)}
    result = tools["create_reminder"].execute({"time": "21 heures 30", "message": "appeler Paul"})
    assert result["at"] == "21:30" and manager.reminders()[0].seconds == 75 * 60
    result = tools["create_reminder"].execute({"time": "8 heures", "message": "sortir"})
    assert manager.reminders()[1].expires_at == datetime(2026, 10, 5, 8, 0)  # heure passée : demain


def test_reminder_at_a_given_time_without_llm():
    setup = Setup(now=NOW)
    for tool in timer_tools(TimerManager(clock=lambda: NOW)):
        setup.core.registry.register(tool)
    assert quick_plan("Rappelle-moi à 18 h 30 d'appeler Paul", setup.core.registry) == {
        "type": "tool_call", "tool": "create_reminder", "parameters": {"time": "18 heures 30", "message": "appeler Paul"}}


def test_routine_commands_without_llm():
    setup = agent_setup()
    registry = setup.core.registry
    assert quick_plan("Quelles routines sont programmées ?", registry)["tool"] == "list_routines"
    assert quick_plan("Lance la routine Soir", registry)["parameters"] == {"name": "Soir"}
    assert quick_plan("Supprime la routine réveil", registry) == {
        "type": "tool_call", "tool": "delete_routine", "parameters": {"name": "réveil"}}
