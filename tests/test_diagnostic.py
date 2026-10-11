"""Diagnostic à la demande (« lance un diagnostic de l'environnement ») : outil en lecture seule, résumé dit
clairement, jamais pris pour une musique à lancer."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import jarvis.diagnostics as diagnostics  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.diagnostics import FAIL, OK, WARN, Check, diagnostic_tool  # noqa: E402


def tool_with(monkeypatch, checks):
    monkeypatch.setattr(diagnostics, "run_health", lambda cfg: checks)
    return diagnostic_tool(load_config(ROOT / "config.toml"))


def test_everything_fine(monkeypatch):
    tool = tool_with(monkeypatch, [Check("Disque", OK, "50 Go"), Check("Mémoire vive", OK, "8 Go")])
    assert tool.say(tool.execute({})) == "Diagnostic terminé, monsieur : les 2 contrôles sont bons. Tout fonctionne."


def test_failures_and_warnings_are_named_in_plain_words(monkeypatch):
    tool = tool_with(monkeypatch, [Check("LLM principal (worker)", FAIL, "injoignable", True),
                                   Check("Lumières Tuya (lecture)", WARN, "sans réponse : chambre"),
                                   Check("Disque", OK, "50 Go")])
    said = tool.say(tool.execute({}))
    assert said.startswith("Diagnostic terminé : 1 contrôles sur 3")
    assert "En panne : le module de réflexion du Katana" in said and "les lumières (sans réponse : chambre)" in said


def test_agent_runs_the_diagnostic_instead_of_spotify(monkeypatch):
    from test_tools import PlannerLLM, make_core, run_agent

    core = make_core()
    core.registry.register(tool_with(monkeypatch, [Check("Disque", OK, "50 Go")]))
    llm = PlannerLLM(reply="Bien.")
    spoken, events = run_agent(["Lance un diagnostic de l'environnement"], llm, core=core)
    assert spoken[0].startswith("Diagnostic terminé") and llm.calls == []
