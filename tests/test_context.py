"""Contexte conversationnel temporaire : compléments courts après une action (simulé, sans LLM ni voix)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis.context import ConversationContext  # noqa: E402
from test_lights import FakeDriver  # noqa: E402
from test_quick import full_core  # noqa: E402
from test_tools import FakeVolume, PlannerLLM, make_core, routes, run_agent  # noqa: E402


def test_light_complements_follow_the_previous_action():
    driver = FakeDriver()
    llm = PlannerLLM()
    spoken, events = run_agent(["Allume la chambre.", "À 30 %.", "Non, l'entrée.", "En bleu.", "Et la chambre aussi."],
                               llm, full_core(driver), fast_path=True)
    assert llm.planned == [] and llm.calls == []
    assert spoken == ["La lumière de la chambre est allumée.", "La lumière de la chambre est à 30 %.",
                      "La lumière de l'entrée est à 30 %.", "La lumière de l'entrée est en bleu.",
                      "La lumière de la chambre est en bleu."]
    assert routes(events).count("tool:context") == 4
    assert driver.lights["entree"]["brightness"] == 30 and driver.lights["chambre"]["hue"] == 240


def test_and_tomorrow_after_the_time_gives_tomorrows_date():
    spoken, events = run_agent(["Quelle heure est-il ?", "Et demain ?"], PlannerLLM(), make_core())
    assert spoken[0] == "Il est 13 heures 42."
    assert spoken[1] == "Demain, nous serons le dimanche 27 septembre 2026." and routes(events)[1] == "tool:context"


def test_tomorrow_date_by_direct_question():
    spoken, _ = run_agent(["Quel jour serons-nous demain ?"], PlannerLLM(), make_core())
    assert spoken == ["Demain, nous serons le dimanche 27 septembre 2026."]


def test_weather_follow_up_keeps_the_city():
    context = ConversationContext(full_core().registry)
    context.record("get_weather", {"location": "Lyon"})
    assert context.resolve("Et demain ?") == {"type": "tool_call", "tool": "get_weather",
                                              "parameters": {"location": "Lyon", "day": "tomorrow"}}
    assert context.resolve("Et à Lyon ?") is None  # suite gérée par la relance d'outil existante


def test_volume_complement():
    volume = FakeVolume()
    spoken, _ = run_agent(["Mets le son à 30 %.", "Plutôt 45 %."], PlannerLLM(), make_core(volume=volume),
                          fast_path=True)
    assert volume.level == 45 and spoken[1] == "Le volume est à 45 %."


def test_unknown_room_in_a_complement_is_refused():
    driver = FakeDriver()
    spoken, _ = run_agent(["Allume la chambre.", "Dans le bureau."], PlannerLLM(), full_core(driver), fast_path=True)
    assert "bureau" in spoken[1] and driver.calls == [("chambre", "power", True)]


def test_new_requests_are_not_mistaken_for_complements():
    context = ConversationContext(full_core().registry)
    context.record("light_on", {"room": "chambre"})
    for text in ("Mets le son à 30 %", "Allume l'entrée", "Raconte-moi une blague", "Quelle heure est-il ?",
                 "Ferme Discord", "Est-ce que la chambre est bien rangée et que tu vas bien ?"):
        assert context.resolve(text) is None, text


def test_context_is_forgotten_when_the_conversation_ends():
    context = ConversationContext(full_core().registry)
    assert context.resolve("À 30 %.") is None
    context.record("light_on", {"room": "chambre"})
    context.clear()
    assert context.resolve("À 30 %.") is None
