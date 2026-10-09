"""Compréhension des demandes : nombres en lettres, citations, références, demandes non reconnues par le routeur.

Correctifs mesurés par le banc bench/comprehension (dev.json pour régler, test.json mis de côté pour juger).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis.context import ConversationContext  # noqa: E402
from jarvis.router import claims_action  # noqa: E402
from jarvis.scheduling.durations import spoken_numbers  # noqa: E402
from jarvis.tools.planner import plan, quoted  # noqa: E402
from jarvis.tools.quick import quick_plan  # noqa: E402
from test_tools import PlannerLLM, make_core, routes, run_agent  # noqa: E402


@pytest.mark.parametrize("text, numbers", [
    ("Règle le son à vingt-cinq", {25}), ("soixante-quinze pour cent", {75}), ("quatre-vingt-dix-neuf", {99}),
    ("soixante et onze", {71}), ("quatre-vingts", {80}), ("cent pour cent", {100}), ("dix-sept", {17}),
    ("vingt et un", {21}), ("mets 30", {30}), ("à 40 pour cent", {40}),
])
def test_spoken_numbers_up_to_a_hundred(text, numbers):
    assert spoken_numbers(text) == numbers


def test_a_number_said_in_words_is_accepted_by_the_planner():
    core = make_core()
    data = plan(PlannerLLM({"type": "tool_call", "tool": "set_volume", "parameters": {"volume": 50}}),
                "Tu peux monter le son à cinquante s'il te plaît ?", core.registry)
    assert data == {"type": "tool_call", "tool": "set_volume", "parameters": {"volume": 50}}
    invented = plan(PlannerLLM({"type": "tool_call", "tool": "set_volume", "parameters": {"volume": 100}}),
                    "Mets-le plus fort", core.registry)
    assert invented is None


@pytest.mark.parametrize("text, expected", [
    ("Lis-moi cette phrase : éteins toutes les lumières", True), ("Répète après moi : verrouille le PC", True),
    ("Dis « coupe le son »", True), ("Lis-moi le premier", False), ("Éteins la lumière", False),
    ("Lis le fichier notes.txt", False), ("Dis-moi quel temps il fait", False),
])
def test_a_sentence_to_read_or_repeat_is_never_a_command(text, expected):
    assert quoted(text) is expected


def test_quoted_command_is_dropped_by_the_planner():
    core = make_core()
    data = plan(PlannerLLM({"type": "tool_call", "tool": "mute_volume", "parameters": {}}),
                "Répète après moi : coupe le son", core.registry)
    assert data is None


def context_after(tool, parameters=None, result=None):
    core = make_core()
    for name in ("read_mail", "summarize_mail", "check_mail", "list_timers", "create_timer"):
        if not core.registry.exists(name):
            from jarvis.tools.base import Param, Risk, Tool

            params = {"position": Param(int, "rang", False, hidden=True)} if "mail" in name and name != "check_mail" \
                else ({"duration": Param(str, "durée")} if name == "create_timer" else {})
            core.registry.register(Tool(name, "test", params, {}, Risk.SAFE, lambda **p: {}))
    context = ConversationContext(core.registry)
    context.record(tool, parameters or {}, result or {})
    return context


@pytest.mark.parametrize("text, expected", [
    ("Lis-moi le premier", {"type": "tool_call", "tool": "read_mail", "parameters": {"position": 1}}),
    ("Résume le deuxième", {"type": "tool_call", "tool": "summarize_mail", "parameters": {"position": 2}}),
    ("Et le troisième ?", {"type": "tool_call", "tool": "read_mail", "parameters": {"position": 3}}),
    ("Quel temps fait-il ?", None),
])
def test_mail_ranks_after_a_mail_list(text, expected):
    assert context_after("check_mail").resolve(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("Il reste combien de temps ?", {"type": "tool_call", "tool": "list_timers", "parameters": {}}),
    ("Ah non, fais-en un de 8 minutes", {"type": "tool_call", "tool": "create_timer",
                                          "parameters": {"duration": "8 minutes"}}),
    ("Plutôt vingt minutes", {"type": "tool_call", "tool": "create_timer", "parameters": {"duration": "20 minutes"}}),
    ("Merci beaucoup", None), ("Dans la cuisine", None),
])
def test_timer_follow_ups(text, expected):
    assert context_after("create_timer", {"duration": 300}, {"timer_id": 1}).resolve(text) == expected


def test_mail_ranks_need_a_previous_mail_list():
    assert context_after("get_time").resolve("Lis-moi le premier") is None


def test_an_unrecognised_request_is_planned_before_free_conversation():
    llm = PlannerLLM({"type": "tool_call", "tool": "list_running_applications", "parameters": {}})
    spoken, events = run_agent(["Dis voir ce que fabrique l'ordinateur"], llm, make_core())
    assert routes(events)[-1] == "tool:plan" and len(llm.planned) == 1


def test_an_action_wanted_but_not_precise_enough_gets_a_question_not_an_invention():
    llm = PlannerLLM({"type": "tool_call", "tool": "set_volume", "parameters": {"volume": 100}},
                     reply="Cette fonction n'est pas disponible.")
    spoken, events = run_agent(["Mets-le plus fort"], llm, make_core())
    assert llm.calls == [] and spoken[0].endswith("?")


def timer_core():
    from jarvis.tools.base import Risk, Tool

    core = make_core()
    for name in ("list_timers", "create_timer", "cancel_timer"):
        core.registry.register(Tool(name, "Minuteurs.", {}, {}, Risk.SAFE, lambda **p: {}))
    return core


@pytest.mark.parametrize("text", ["Et le minuteur, il en est où ?", "Il dit quoi le minuteur"])
def test_an_existing_feature_is_never_called_unavailable(text):
    llm = PlannerLLM(reply="Il reste deux minutes.")
    spoken, events = run_agent([text], llm, timer_core())
    assert llm.calls == [] and spoken[0].endswith("?") and "disponible" not in spoken[0]


def test_orion_objects_pattern():
    from jarvis.agent import ORION_OBJECTS
    from jarvis.personality import normalize

    assert ORION_OBJECTS.search(normalize("Résume-moi mes mails"))
    assert ORION_OBJECTS.search(normalize("Le minuteur, il en est où ?"))
    assert not ORION_OBJECTS.search(normalize("Pourquoi la lumière va plus vite que le son ?"))
    assert not ORION_OBJECTS.search(normalize("Raconte-moi une blague"))


def test_the_music_shortcut_ignores_other_domains():
    core = make_core()
    assert quick_plan("Mets la luminosité de la chambre à soixante-quinze pour cent", core.registry) is None or \
        quick_plan("Mets la luminosité de la chambre à soixante-quinze pour cent", core.registry)["tool"] != "spotify_play"


@pytest.mark.parametrize("reply", ["Minuteur pour 8 minutes mis en place.", "Le mail a bien été envoyé à Paul.",
                                   "Votre réveil de 7 heures est programmé."])
def test_invented_action_claims_are_detected(reply):
    assert claims_action(reply)


def test_honest_replies_are_not_taken_for_action_claims():
    assert not claims_action("Je n'ai pas lancé de minuteur, monsieur.")
    assert not claims_action("La lumière voyage plus vite que le son.")


def test_a_quoted_command_never_reaches_any_tool():
    from test_tools import Locker

    locker = Locker()
    llm = PlannerLLM({"type": "tool_call", "tool": "lock_pc", "parameters": {}}, reply="Verrouille le PC.")
    spoken, events = run_agent(["Répète après moi : verrouille le PC"], llm, make_core(locker=locker))
    assert locker.calls == 0 and not any("?" in s and "verrouill" in s.lower() for s in spoken)


def test_an_existing_feature_is_not_announced_unavailable_by_the_router():
    llm = PlannerLLM({"type": "tool_call", "tool": "list_timers", "parameters": {}})
    spoken, events = run_agent(["Est-ce que j'ai encore des minuteurs ?"], llm, timer_core())
    assert not any("disponible" in s for s in spoken)
