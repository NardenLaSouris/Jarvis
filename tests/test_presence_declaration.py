"""« ORION, je sors. » : déclaration explicite de départ (jarvis/presence/declaration.py, outil declare_departure,
moteur de présence). Transition immédiate et unique, délais habituels inchangés, phrases ambiguës sans effet,
départ annoncé pour plus tard, retour détecté normalement, état gardé au redémarrage."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis.presence import ARRIVAL_CONFIRMED, AWAY, DEPARTURE_CONFIRMED, HOME, PresenceSettings  # noqa: E402
from jarvis.presence.declaration import LATER, NOW, SHORT, STAY, parse_declaration  # noqa: E402
from jarvis.presence.engine import MOTION_WHILE_AWAY  # noqa: E402
from jarvis.presence.tools import departure_tool  # noqa: E402
from jarvis.tools import PermissionManager, ToolCore, ToolRegistry  # noqa: E402
from test_presence import House  # noqa: E402

NOW_DT = datetime(2026, 10, 11, 15, 30)


@pytest.mark.parametrize("text", ["ORION, je sors.", "Je pars de la maison.", "Je m'en vais.",
                                  "Je quitte la maison pour un moment.", "Je pars faire des courses.",
                                  "Bon allez, j'y vais.", "Je vais faire les courses"])
def test_explicit_departures(text):
    assert parse_declaration(text, NOW_DT).kind == NOW


@pytest.mark.parametrize("text, expected", [("Je sors dans une heure.", datetime(2026, 10, 11, 16, 30)),
                                             ("Je pars à 18 h.", datetime(2026, 10, 11, 18, 0)),
                                             ("Je m'en vais dans vingt minutes", datetime(2026, 10, 11, 15, 50))])
def test_departure_later_keeps_the_time_said(text, expected):
    declaration = parse_declaration(text, NOW_DT)
    assert declaration.kind == LATER and declaration.at == expected


@pytest.mark.parametrize("text", ["Je sors deux minutes.", "Je sors les poubelles.", "Je sors juste fumer",
                                  "Je sors 10 minutes", "Je sors pour cinq minutes, je reviens vite",
                                  "Je descends le courrier, je sors deux secondes", "Je sors le linge"])
def test_short_outings_are_not_an_absence(text):
    assert parse_declaration(text, NOW_DT).kind == SHORT


@pytest.mark.parametrize("text", ["Finalement je reste.", "Je ne sors plus", "Je reste à la maison"])
def test_staying(text):
    assert parse_declaration(text, NOW_DT).kind == STAY


@pytest.mark.parametrize("text", ["Appelle-moi quand je sors.", "Si je pars, éteins tout.", "Je sors ce soir ?",
                                  "Est-ce que je pars demain ?", "Je sors de la douche.", "Je pars du principe que oui",
                                  "Il dit que je sors trop", "Je suis sorti hier", "Tu sors ?", "Je reste sceptique",
                                  "Préviens-moi si je m'en vais sans fermer", "Je sors avec Léa"])
def test_ambiguous_or_other_sentences_never_declare_a_departure(text):
    assert parse_declaration(text, NOW_DT) is None


def test_declaration_is_immediate_and_published_once():
    house = House()
    assert house.engine.declare_departure("owner") == "away"
    assert house.state() == AWAY and house.engine.occupancy() == "empty"
    [departure] = house.of_type(DEPARTURE_CONFIRMED)
    assert departure.payload["reason"] == "départ déclaré" and departure.payload["signals"] == ["declared"]
    assert house.engine.declare_departure("owner") == "already"  # répété : rien de plus
    assert len(house.of_type(DEPARTURE_CONFIRMED)) == 1


def test_usual_timers_are_unchanged():
    settings = PresenceSettings()
    assert (settings.departure_window, settings.arrival_window, settings.threshold, settings.absence_timeout) == \
        (180.0, 300.0, 90, 1200.0)
    house = House()
    house.send("telephone_owner", "away")  # sans déclaration : toujours les délais habituels
    house.wait(200)
    assert house.state() != AWAY


def test_leaving_after_the_declaration_is_neither_a_return_nor_an_intrusion():
    house = House()
    house.engine.declare_departure("owner")
    house.send("mouvement_salon", "detected", after=30)  # on finit de se préparer
    house.send("porte_entree", "open", after=60)
    house.send("mouvement_entree", "detected", after=2)
    house.send("porte_entree", "closed", after=5)
    house.send("telephone_owner", "home", after=10)  # encore sur le Wi-Fi devant la porte
    house.wait(400)
    house.send("telephone_owner", "away", after=30)
    assert house.state() == AWAY
    assert not house.of_type(ARRIVAL_CONFIRMED) and not house.of_type(MOTION_WHILE_AWAY)


def test_return_is_detected_normally_after_a_declared_departure():
    house = House()
    house.engine.declare_departure("owner")
    house.send("telephone_owner", "away", after=60)
    house.wait(3600)
    house.send("telephone_owner", "home")
    house.send("porte_entree", "open", after=30)
    house.send("porte_entree", "closed", after=5)
    assert house.state() == HOME and len(house.of_type(ARRIVAL_CONFIRMED)) == 1


def test_departure_announced_for_later_waits_for_its_time_and_can_be_cancelled(tmp_path):
    house = House(tmp_path)
    assert house.engine.declare_departure("owner", house.t + 3600) == "planned"
    house.wait(1800)
    assert house.state() == HOME
    again = House(tmp_path)  # redémarrage : le départ annoncé est gardé
    again.t = house.t
    assert again.engine.planned_departure("owner") == house.t + 1800
    again.wait(1900)
    assert again.state() == AWAY and len(again.of_type(DEPARTURE_CONFIRMED)) == 1
    other = House()
    other.engine.declare_departure("owner", other.t + 3600)
    assert other.engine.cancel_declared("owner")
    other.wait(4000)
    assert other.state() == HOME


def test_tool_reads_the_sentence_not_the_llm():
    house = House()
    tool = departure_tool(house.engine, lambda: "owner", clock=lambda: datetime.fromtimestamp(house.t))
    core = ToolCore(ToolRegistry(), PermissionManager())
    core.registry.register(tool)
    plan = tool.parameters["plan"]
    assert plan.resolve("Appelle-moi quand je sors") is None
    assert plan.resolve("Je sors les poubelles") == "short"
    outcome = core.submit({"type": "tool_call", "tool": "declare_departure",
                           "parameters": {"plan": plan.resolve("ORION, je sors.")}})
    assert outcome.result.success and house.state() == AWAY
    assert "absent" in tool.say(outcome.result.result)
    refused = core.submit({"type": "tool_call", "tool": "declare_departure", "parameters": {"plan": "rm -rf"}})
    assert refused.status != "done"


def test_agent_declares_the_departure_without_the_llm():
    from test_tools import PlannerLLM, make_core, run_agent

    house = House()
    core = make_core()
    core.registry.register(departure_tool(house.engine, lambda: "owner"))
    llm = PlannerLLM(reply="Bonne journée.")
    spoken, events = run_agent(["Je pars faire des courses."], llm, core=core)
    assert house.state() == AWAY and llm.calls == []
    assert any("absent" in s for s in spoken)
