"""Commandes simples sans LLM (jarvis.tools.quick) et mode dégradé de l'agent (worker LLM hors ligne).

Tout est simulé : lumières factices, outils factices, aucun LLM réel, aucune voix.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis.scheduling import TimerManager  # noqa: E402
from jarvis.tools import Param, Risk, Tool  # noqa: E402
from jarvis.tools.lights import light_tools  # noqa: E402
from jarvis.tools.quick import quick_plan  # noqa: E402
from jarvis.tools.timers import timer_tools  # noqa: E402
from test_lights import FakeDriver, rooms_from  # noqa: E402
from test_tools import PlannerLLM, make_core, routes, run_agent  # noqa: E402


def stub(name: str, **params: Param) -> Tool:
    return Tool(name, name, params, {}, Risk.SAFE, lambda **kw: {"ok": True, **kw}, say=lambda r: f"{name} fait.")


def full_core(driver=None):
    core = make_core()
    for tool in light_tools(rooms_from(), driver or FakeDriver()):
        core.registry.register(tool)
    for tool in timer_tools(TimerManager()):
        core.registry.register(tool)
    core.registry.register(stub("get_weather", location=Param(str, "ville", required=False),
                                day=Param(str, "jour", required=False), moment=Param(str, "moment", required=False)))
    core.registry.register(stub("create_alarm", time=Param(str, "heure")))
    return core


REGISTRY = full_core().registry


def call(tool, **parameters):
    return {"type": "tool_call", "tool": tool, "parameters": parameters}


@pytest.mark.parametrize("text, expected", [
    ("Allume la chambre", call("light_on")),
    ("Jarvis, allume la lumière", call("light_on")),
    ("Éteins les lumières", call("light_off")),
    ("Mets une lumière chaude", call("set_color", color="blanc chaud")),
    ("Mets la lumière en blanc froid", call("set_color", color="blanc froid")),
    ("Mets l'entrée en vert", call("set_color", color="vert")),
    ("Luminosité 30 %", call("set_brightness", brightness=30)),
    ("Mets la lumière de la chambre à 30 pour cent", call("set_brightness", brightness=30)),
    ("Mets la lumière au maximum", call("set_brightness", brightness=100)),
    ("Allume la chambre à 40 %", call("light_on", brightness=40)),
    ("Mets la chambre à 4000 kelvins", call("set_color_temperature", temperature=4000)),
    ("Mets le son à 40 %", call("set_volume", volume=40)),
    ("Mets le volume à fond", call("set_volume", volume=100)),
    ("Coupe le son", call("mute_volume")),
    ("Remets le son", call("unmute_volume")),
    ("Ouvre Discord", call("open_application", application="discord")),
    ("Lance-moi Steam", call("open_application", application="steam")),
    ("Ferme Chrome", call("close_application", application="chrome")),
    ("Mets un minuteur de 10 minutes", call("create_timer", duration="10 minutes")),
    ("Lance un minuteur d'une heure et demie", call("create_timer", duration="une heure et demie")),
    ("Rappelle-moi dans 20 minutes de sortir les poubelles",
     call("create_reminder", delay="20 minutes", message="sortir les poubelles")),
    ("Rappelle-moi dans 5 minutes d'appeler Paul et Marie",
     call("create_reminder", delay="5 minutes", message="appeler Paul et Marie")),
    ("Réveille-moi à 7 h 30", call("create_alarm", time="7 heures 30")),
    ("Quel temps fera-t-il demain à Lyon ?", call("get_weather", day="tomorrow", location="Lyon")),
    ("Quel temps fait-il ce soir ?", call("get_weather", moment="evening")),
    ("Verrouille le PC", call("lock_pc")),
])
def test_simple_commands_are_understood_without_llm(text, expected):
    assert quick_plan(text, REGISTRY) == expected


@pytest.mark.parametrize("text", [
    "Raconte-moi une blague", "N'allume pas la lumière", "Allume la lumière quand je rentre",
    "Baisse un peu la lumière", "Ouvre YouTube", "Ouvre le terminal", "Supprime le dossier Documents",
    "Est-ce que la lumière est allumée ?", "Tous les jours à 21 h, allume la chambre", "Pourquoi le ciel est bleu ?",
    "Mets un minuteur", "Ouvre Discord et fais-moi un café",
])
def test_anything_else_is_left_to_the_llm(text):
    assert quick_plan(text, REGISTRY) is None


def test_chained_commands_keep_their_order_and_their_words():
    data = quick_plan("Jarvis, allume la chambre à 30 %, en bleu", REGISTRY)
    assert [(c["tool"], c["parameters"], c["segment"]) for c in data["calls"]] == [
        ("light_on", {"brightness": 30}, "allume la chambre à 30 %"), ("set_color", {"color": "bleu"}, "en bleu")]
    data = quick_plan("Allume la chambre et l'entrée", REGISTRY)
    assert [(c["tool"], c["segment"]) for c in data["calls"]] == [("light_on", "Allume la chambre"),
                                                                  ("light_on", "l'entrée")]
    data = quick_plan("Ouvre Discord et mets le son à 30 %", REGISTRY)
    assert [c["tool"] for c in data["calls"]] == ["open_application", "set_volume"]


def test_tools_missing_from_the_registry_are_never_proposed():
    registry = make_core({"set_volume": {"enabled": False}}).registry
    assert quick_plan("Mets le son à 40 %", registry) is None and quick_plan("Allume la chambre", registry) is None


# --- Agent : raccourci et mode dégradé --------------------------------------------------------------

class DownLLM(PlannerLLM):
    """Worker LLM hors ligne : aucun appel ne doit lui parvenir pour une commande simple."""

    degraded = True

    def chat_json(self, messages, schema):
        raise AssertionError("le LLM ne doit pas choisir d'outil en mode dégradé")


def test_fast_path_skips_the_llm_for_a_simple_command():
    driver = FakeDriver()
    llm = PlannerLLM()
    spoken, events = run_agent(["Allume la chambre à 30 %."], llm, full_core(driver), fast_path=True)
    assert llm.planned == [] and llm.calls == [] and spoken == ["La lumière de la chambre est allumée à 30 %."]
    assert driver.calls == [("chambre", "power", True), ("chambre", "brightness", 30)]
    assert "tool:quick" in routes(events)


def test_without_fast_path_the_llm_still_chooses():
    llm = PlannerLLM(call("light_on"))
    run_agent(["Allume la chambre."], llm, full_core(), fast_path=False)
    assert len(llm.planned) == 1


def test_degraded_mode_runs_simple_commands_and_explains_the_rest():
    driver = FakeDriver()
    llm = DownLLM(reply="Bonjour.")
    spoken, events = run_agent(["Éteins l'entrée.", "Ouvre YouTube.", "Raconte-moi une blague."], llm,
                               full_core(driver))
    assert driver.calls == [("entree", "power", False)]
    assert spoken[0] == "La lumière de l'entrée est éteinte."
    assert "hors ligne" in spoken[1] or "mode réduit" in spoken[1]
    assert spoken[2] == "Bonjour."  # la conversation passe au LLM local (prompt court, voir FailoverLLM)
    assert any("mode dégradé" in r for r in routes(events))


def test_degraded_mode_keeps_confirmations():
    llm = DownLLM()
    spoken, _ = run_agent(["Verrouille le PC."], llm, full_core())
    assert spoken[0].endswith("?")
