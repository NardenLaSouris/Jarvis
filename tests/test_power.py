"""Arrêt et allumage des PC (orion/tools/power.py) : arrêt toujours confirmé et exécuté par l'agent du PC,
allumage par paquet de réveil vers l'adresse matérielle configurée. Aucune action réelle."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orion.tools import PermissionManager, ToolCore, ToolRegistry, builtin_tools  # noqa: E402
from orion.tools.base import Risk  # noqa: E402
from orion.tools.builtin import PC_TOOLS  # noqa: E402
from orion.tools.devices import load_devices  # noqa: E402
from orion.tools.planner import named  # noqa: E402
from orion.tools.power import magic_packet, shutdown_tool, wake_tool  # noqa: E402

DEVICES = {
    "pc": {"name": "votre PC", "url": "http://192.0.2.10:8765", "default": True, "aliases": ["mon pc"],
           "mac": "F0-2F-74-B0-81-2A"},
    "portable": {"name": "votre PC portable", "url": "http://192.0.2.11:8765", "aliases": ["katana"]},
    "mini": {"name": "le mini-PC", "aliases": ["le serveur"]},
}


def core_with(*tools) -> ToolCore:
    core = ToolCore(ToolRegistry(), PermissionManager())
    for tool in tools:
        core.registry.register(tool)
    return core


def test_shutdown_always_asks_and_runs_only_after_yes():
    calls = []
    core = core_with(shutdown_tool(lambda: calls.append(1) or True))
    first = core.submit({"type": "tool_call", "tool": "shutdown_pc", "parameters": {}})
    assert first.status == "confirm" and calls == []
    assert core.answer("oui").result.success and calls == [1]
    core.submit({"type": "tool_call", "tool": "shutdown_pc", "parameters": {}})
    core.answer("non")
    assert calls == [1]


def test_shutdown_is_a_pc_action_run_by_the_agent():
    assert "shutdown_pc" in PC_TOOLS
    tool = next(t for t in builtin_tools() if t.name == "shutdown_pc")
    assert tool.risk is Risk.CONFIRMATION_REQUIRED


def test_wake_sends_the_packet_to_the_named_device_only():
    sent = []
    devices = load_devices(DEVICES)
    core = core_with(wake_tool(devices, sent.append))
    outcome = core.submit({"type": "tool_call", "tool": "wake_pc", "parameters": {"device": "pc"}})
    assert outcome.result.success and sent == ["f0:2f:74:b0:81:2a"]
    refused = core.submit({"type": "tool_call", "tool": "wake_pc", "parameters": {"device": "portable"}})
    assert not (refused.status == "done" and refused.result.success) and sent == ["f0:2f:74:b0:81:2a"]  # pas d'adresse matérielle : rien


def test_magic_packet_and_invalid_addresses():
    packet = magic_packet("f0:2f:74:b0:81:2a")
    assert len(packet) == 102 and packet[:6] == b"\xff" * 6 and packet[6:12] == bytes.fromhex("f02f74b0812a")
    with pytest.raises(ValueError):
        load_devices({**DEVICES, "pc": {**DEVICES["pc"], "mac": "pas une adresse"}})


def test_power_actions_must_name_a_computer():
    assert named("shutdown_pc", "Éteins mon PC") and named("wake_pc", "Allume le Katana")
    assert not named("shutdown_pc", "Éteins tout") and not named("wake_pc", "Allume la lumière")
    assert not named("set_color", "Niquel")  # remarque après une action : pas une nouvelle action


def test_unclear_single_word_asks_again_once_then_gives_up():
    calls = []
    core = core_with(shutdown_tool(lambda: calls.append(1) or True))
    core.submit({"type": "tool_call", "tool": "shutdown_pc", "parameters": {}})
    again = core.answer("Bof")
    assert again.status == "confirm" and "Répondez oui ou non" in again.question and calls == []
    assert core.answer("oui").result.success and calls == [1]
    core.submit({"type": "tool_call", "tool": "shutdown_pc", "parameters": {}})
    core.answer("Bof")
    assert core.answer("Bof") is None and core.answer("oui") is None and calls == [1]  # abandonnée


def test_yep_confirms_with_the_personality_lists():
    from orion.personality import load_personality
    from orion.tools import ConfirmationManager

    personality = load_personality(ROOT / "personality.toml")
    calls = []
    core = core_with(shutdown_tool(lambda: calls.append(1) or True))
    core.confirmations = ConfirmationManager(personality.confirm_yes, personality.confirm_no)
    core.submit({"type": "tool_call", "tool": "shutdown_pc", "parameters": {}})
    assert core.answer("Yep").result.success and calls == [1]


def test_face_returns_to_standby_after_a_message():
    from orion.face.bridge import FaceBridge

    class Visual:
        state = "standby"

        def set_state(self, state):
            self.state = state

        def snapshot(self):
            return {"state": self.state}

    visual = Visual()
    bridge = FaceBridge(visual)
    for kind in ("message", "routing", "tool", "message_reply"):
        bridge.on_event(kind, "")
    assert visual.state == "standby"
