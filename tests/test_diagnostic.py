"""Diagnostic à la demande (« lance un diagnostic de l'environnement ») : outil en lecture seule, résumé dit
clairement, jamais pris pour une musique à lancer."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import orion.diagnostics as diagnostics  # noqa: E402
from orion.config import load_config  # noqa: E402
from orion.diagnostics import FAIL, OK, WARN, Check, diagnostic_tool  # noqa: E402


def tool_with(monkeypatch, checks, **extra):
    monkeypatch.setattr(diagnostics, "run_health", lambda cfg, skip=(): checks)
    return diagnostic_tool(load_config(ROOT / "config.toml"), **extra)


def test_everything_fine(monkeypatch):
    tool = tool_with(monkeypatch, [Check("Disque", OK, "50 Go"), Check("Mémoire vive", OK, "8 Go")])
    assert tool.say(tool.execute({})) == "Diagnostic terminé, monsieur. Côté système, les 2 contrôles sont bons."


def test_failures_and_warnings_are_named_in_plain_words(monkeypatch):
    tool = tool_with(monkeypatch, [Check("LLM principal (worker)", FAIL, "injoignable", True),
                                   Check("Lumières Tuya (lecture)", WARN, "sans réponse : chambre"),
                                   Check("Disque", OK, "50 Go")])
    said = tool.say(tool.execute({}))
    assert "Côté système, 1 contrôles sur 3 sont bons." in said
    assert "En panne : le module de réflexion du Katana" in said and "les lumières (sans réponse : chambre)" in said


def test_agent_runs_the_diagnostic_instead_of_spotify(monkeypatch):
    from test_tools import PlannerLLM, make_core, run_agent

    core = make_core()
    core.registry.register(tool_with(monkeypatch, [Check("Disque", OK, "50 Go")]))
    llm = PlannerLLM(reply="Bien.")
    spoken, events = run_agent(["Lance un diagnostic de l'environnement"], llm, core=core)
    assert spoken[0].startswith("Diagnostic terminé") and llm.calls == []


def test_devices_online_offline_open_apps_phone_and_lights(monkeypatch):
    from types import SimpleNamespace

    from orion.tools.devices import load_devices

    devices = load_devices({"pc": {"name": "votre PC", "url": "http://192.0.2.10:8765", "default": True},
                            "portable": {"name": "votre PC portable", "url": "http://192.0.2.11:8765"},
                            "mini": {"name": "le mini-PC"}})

    class Agents:
        def call(self, device, tool, parameters):
            assert tool == "list_running_applications" and parameters == {}
            return {"applications": [{"application": "discord", "label": "Discord", "running": True},
                                     {"application": "steam", "label": "Steam", "running": False},
                                     {"application": "chrome", "label": "Chrome", "running": True}]}

    class Driver:
        def state(self, room):
            if room.key == "salon":
                raise OSError("hors tension")
            return {"on": room.key == "entree"}

    rooms = SimpleNamespace(rooms=lambda: [SimpleNamespace(key=k, name=n) for k, n in
                                          (("entree", "l'entrée"), ("chambre", "la chambre"), ("salon", "le salon"))])
    presence = SimpleNamespace(engine=SimpleNamespace(snapshot=lambda: {"users": {"owner": {"phone": "home"}}}))
    tool = tool_with(monkeypatch, [Check("Agent pc", OK, "ok"), Check("Agent portable", FAIL, "injoignable"),
                                   Check("Disque", OK, "50 Go")],
                     devices=devices, client=Agents(), presence=presence, rooms=rooms, driver=Driver())
    said = tool.say(tool.execute({}))
    assert "En ligne : le mini-PC ; votre PC, où sont ouverts Discord et Chrome." in said
    assert "Hors ligne : votre PC portable." in said
    assert "Votre iPhone est connecté au Wi-Fi de la maison." in said
    assert "Lumières : l'entrée allumée et la chambre éteinte." in said and "sans réponse : le salon" in said
    assert "En panne" not in said  # le PC portable éteint est « hors ligne », pas une panne
