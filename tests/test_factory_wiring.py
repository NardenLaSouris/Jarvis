"""Branchement réel (jarvis/factory.py) des règles d'appel, du suivi des mails et de « je sors », avec la vraie
configuration, des composants simulés et un dossier de données temporaire : aucune connexion, aucun appel."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.config import load_config  # noqa: E402
from jarvis.events import Event, EventBus  # noqa: E402
from jarvis.factory import build_call_rules, build_mail_actions  # noqa: E402
from jarvis.mail.provider import MemoryMailProvider  # noqa: E402
from jarvis.mail.tools import MailBox  # noqa: E402
from jarvis.presence import PresenceEngine  # noqa: E402
from jarvis.scheduling.manager import TimerManager  # noqa: E402


class Notifications:
    def __init__(self):
        self.sent = []

    def notify(self, notification):
        self.sent.append(notification)


class Calls:
    def __init__(self):
        self.requested, self.messages = [], []

    def request(self, reason, origin):
        from jarvis.call.session import CallSession

        call = CallSession("linphone", reason, origin, 0.0)
        self.requested.append(call)
        return call

    def reply(self, text):
        self.messages.append(text)


def test_factory_builds_and_connects_rules_and_mail_follow_up(tmp_path):
    cfg = load_config(ROOT / "config.toml")
    cfg = replace(cfg, memory=replace(cfg.memory, path=tmp_path / "memory.json"))
    bus = EventBus()
    engine = PresenceEngine(["owner"], bus, state_path=tmp_path / "presence.json")
    presence = SimpleNamespace(engine=engine)
    timers = TimerManager(bus)
    box = MailBox(MemoryMailProvider([]))
    watcher = SimpleNamespace(on_new=None)
    actions = build_mail_actions(cfg, box, watcher, SimpleNamespace(), timers, Notifications(), presence)
    try:
        assert watcher.on_new == actions.received and box.on_replied is not None
        rules, tools = build_call_rules(cfg, SimpleNamespace(), presence, timers, box, actions)
        assert {t.name for t in tools} >= {"create_call_rule", "list_call_rules", "explain_calls"}
        calls = Calls()
        rules.connect(calls, bus)
        rules.add({"trigger": {"type": "event", "event": "reminder_due"}, "conditions": [{"type": "away"}]},
                  "appelle-moi quand je suis absent et qu'un rappel arrive")
        assert engine.declare_departure("owner") == "away"
        bus.publish(Event("reminder.finished", "scheduling", {"reminder_id": "3", "message": "le garage"}))
        assert len(calls.requested) == 1 and (tmp_path / "call_rules.json").exists()
    finally:
        actions.stop()
