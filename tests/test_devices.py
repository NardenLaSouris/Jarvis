"""Appareils du réseau : JARVIS agit sur un PC via son agent, choisi par les mots de la demande, jamais sur le Core.

Deux vrais agents tournent sur 127.0.0.1 (ports libres) avec des actions simulées : aucune action réelle.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import jarvis.tools.applications as apps_module  # noqa: E402
import jarvis.tools.audio as audio_module  # noqa: E402
from jarvis.tools import PermissionManager, ToolCore, ToolRegistry, builtin_tools  # noqa: E402
from jarvis.tools.builtin import PC_TOOLS  # noqa: E402
from jarvis.tools.devices import AgentClient, load_devices, remote_tool  # noqa: E402
from jarvis.tools.planner import plan_schema, planner_prompt  # noqa: E402
from jarvis.winagent import AgentConfig, AgentServer  # noqa: E402
from jarvis.winagent.__main__ import pc_actions  # noqa: E402
from test_tools import PERSONALITY, FakeProcesses, FakeVolume, Launcher, Locker, PlannerLLM, routes, run_agent  # noqa: E402

TOKEN = "t" * 40


@pytest.fixture(autouse=True)
def no_real_actions(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("action réelle interdite dans les tests automatiques")

    monkeypatch.setattr(apps_module.subprocess, "Popen", forbidden)
    monkeypatch.setattr(apps_module.subprocess, "run", forbidden)
    monkeypatch.setattr(audio_module.VolumeControl, "set", forbidden)
    monkeypatch.setattr(audio_module.VolumeControl, "set_mute", forbidden)
    monkeypatch.setattr(apps_module, "find_command", lambda entry: [f"C:/Apps/{entry.key}.exe"])
    monkeypatch.setattr(apps_module.Application, "processes", property(lambda self: self.app.windows_processes))


class Machine:
    """Un PC simulé et son agent."""

    def __init__(self):
        self.processes = FakeProcesses({"Discord.exe"})
        self.launcher, self.locker, self.volume = Launcher(self.processes, ("steam.exe",)), Locker(), FakeVolume(level=30)
        registry = ToolRegistry()
        for tool in builtin_tools(processes=self.processes, launcher=self.launcher, locker=self.locker,
                                  volume=self.volume, open_wait=0.2, close_wait=0.2):
            if tool.name in PC_TOOLS:
                registry.register(tool)
        self.agent = AgentServer(AgentConfig("127.0.0.1", 0, frozenset({"127.0.0.1"}), TOKEN), registry)
        self.agent.start()
        host, port = self.agent.address
        self.url = f"http://{host}:{port}"


@pytest.fixture
def machines():
    pc, laptop = Machine(), Machine()
    yield pc, laptop
    pc.agent.stop()
    laptop.agent.stop()


def devices_for(pc_url, laptop_url):
    return load_devices({
        "pc": {"name": "votre PC", "url": pc_url, "default": True, "aliases": ["mon pc", "mon ordinateur", "mon ordi"]},
        "portable": {"name": "votre PC portable", "url": laptop_url,
                     "aliases": ["mon pc portable", "le portable", "mon ordinateur portable", "katana"]},
        "mini": {"name": "le mini-PC", "aliases": ["mini pc", "le serveur"]},
    })


def converse(texts, llm, devices, timeout=5.0):
    return run_agent(texts, llm, core_for(devices, timeout))


def core_for(devices, timeout=5.0):
    client = AgentClient(TOKEN, timeout=timeout)
    registry = ToolRegistry()
    for tool in builtin_tools(processes=FakeProcesses(), launcher=Launcher(), locker=Locker(), volume=FakeVolume()):
        registry.register(remote_tool(tool, devices, client) if tool.name in PC_TOOLS else tool)
    return ToolCore(registry, PermissionManager())


# --- Configuration et choix de l'appareil ---------------------------------------------------------

def test_devices_are_named_by_their_aliases_longest_first():
    devices = devices_for("http://a:1", "http://b:2")
    assert devices.default.key == "pc"
    for text, key in (("Ouvre Discord sur mon PC portable.", "portable"), ("Ferme Steam sur mon PC.", "pc"),
                      ("Coupe le son du Katana", "portable"), ("Verrouille mon ordinateur portable", "portable"),
                      ("Ouvre Discord sur le mini-PC", "mini"), ("Ouvre Discord", None)):
        found = devices.find(text)
        assert (found.key if found else None) == key, text


@pytest.mark.parametrize("table, message", [
    ({"pc": {"url": "http://a:1"}}, "par défaut"),
    ({"pc": {"url": "http://a:1", "default": True}, "b": {"url": "http://b:1", "default": True}}, "par défaut"),
    ({"pc": {"default": True}}, "par défaut"),
    ({"PC!": {"url": "http://a:1", "default": True}}, "invalide"),
    ({"pc": {"url": "ftp://a", "default": True}}, "url invalide"),
])
def test_invalid_device_configuration_is_refused(table, message):
    with pytest.raises(ValueError, match=message):
        load_devices(table)


def test_no_devices_means_the_core_acts_on_its_own_machine():
    assert load_devices({}) is None


def test_the_device_is_never_chosen_by_the_llm():
    registry = core_for(devices_for("http://a:1", "http://b:2")).registry
    assert "device" not in planner_prompt(registry)
    options = {o["properties"]["tool"]["const"]: o["properties"]["parameters"] for o in plan_schema(registry)["anyOf"][1:]}
    assert "device" not in options["open_application"]["properties"]


# --- Actions sur les appareils, par la conversation -----------------------------------------------

def test_actions_go_to_the_device_named_in_the_request(machines):
    pc, laptop = machines
    open_steam = {"type": "tool_call", "tool": "open_application", "parameters": {"application": "steam"}}
    llm = PlannerLLM(open_steam, open_steam)
    spoken, events = converse(["Jarvis, ouvre Steam.", "Ouvre Steam sur mon PC portable."], llm,
                              devices_for(pc.url, laptop.url))
    assert spoken == ["Steam est ouvert.", "Steam est ouvert sur votre PC portable."]
    assert pc.launcher.calls == [["C:/Apps/steam.exe"]] and laptop.launcher.calls == [["C:/Apps/steam.exe"]]
    assert llm.calls == []


def test_confirmation_names_the_device_and_runs_there_only(machines):
    pc, laptop = machines
    llm = PlannerLLM({"type": "tool_call", "tool": "lock_pc", "parameters": {}})
    spoken, events = converse(["Verrouille le portable.", "Oui."], llm, devices_for(pc.url, laptop.url))
    assert spoken[0] == "Voulez-vous que je verrouille l'ordinateur sur votre PC portable ?"
    assert laptop.locker.calls == 1 and pc.locker.calls == 0


def test_the_core_machine_is_never_acted_upon(machines):
    pc, laptop = machines
    llm = PlannerLLM({"type": "tool_call", "tool": "lock_pc", "parameters": {}},
                     {"type": "tool_call", "tool": "set_volume", "parameters": {"volume": 50}})
    spoken, events = converse(["Verrouille le mini PC.", "Mets le son à 50 % sur le serveur."], llm,
                              devices_for(pc.url, laptop.url))
    assert spoken == ["Je n'agis pas sur le mini-PC.", "Je n'agis pas sur le mini-PC."]
    assert pc.locker.calls == laptop.locker.calls == 0 and pc.volume.level == laptop.volume.level == 30


def test_direct_intents_reach_the_default_device_and_named_ones_their_device(machines):
    pc, laptop = machines
    pc.volume.is_muted = laptop.volume.is_muted = True
    llm = PlannerLLM({"type": "tool_call", "tool": "unmute_volume", "parameters": {}})
    spoken, events = converse(["Remets le son.", "Remets le son sur le Katana."], llm, devices_for(pc.url, laptop.url))
    assert routes(events) == ["tool:unmute", "tool:tool.action"]
    assert not pc.volume.is_muted and not laptop.volume.is_muted
    assert spoken == ["Le son est revenu, à 30 %.", "Le son est revenu, à 30 % sur votre PC portable."]


def test_unreachable_device_is_reported_without_crash(machines):
    pc, laptop = machines
    laptop.agent.stop()
    llm = PlannerLLM({"type": "tool_call", "tool": "open_application", "parameters": {"application": "discord"}})
    spoken, events = converse(["Ouvre Discord sur mon PC portable."], llm, devices_for(pc.url, laptop.url), timeout=1.0)
    assert spoken == ["Votre PC portable ne répond pas."]
    laptop.agent.start()


def test_wrong_token_is_refused_by_the_agent(machines):
    pc, laptop = machines
    devices = devices_for(pc.url, laptop.url)
    tool = remote_tool(next(t for t in builtin_tools() if t.name == "lock_pc"), devices, AgentClient("x" * 40))
    core = ToolCore(ToolRegistry(), PermissionManager())
    core.registry.register(tool)
    core.submit({"type": "tool_call", "tool": "lock_pc", "parameters": {"device": "pc"}})
    outcome = core.answer("oui")
    assert outcome.result.success is False and pc.locker.calls == 0


def test_agent_exposes_only_pc_actions():
    names = {t.name for t in pc_actions({}).list()}
    assert names == set(PC_TOOLS)
    assert "get_weather" not in names and "create_timer" not in names
    assert "lock_pc" not in {t.name for t in pc_actions({"lock_pc": {"enabled": False}}).list()}


def test_routes_are_unchanged_by_devices(machines):
    pc, laptop = machines
    spoken, events = converse(["Quelle heure est-il ?"], PlannerLLM(), devices_for(pc.url, laptop.url))
    assert routes(events) == ["tool:time"] and PERSONALITY.assistant_name == "JARVIS"
