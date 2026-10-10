"""Appel d'absence (jarvis/call/trigger.py) : seulement maison vide, seulement les événements choisis, phrase du
journal de la maison, limites du gestionnaire d'appels respectées."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.call import CallManager, CallSettings  # noqa: E402
from jarvis.call.trigger import AwayCallTrigger  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.events import Event, EventBus  # noqa: E402
from jarvis.presence import HouseJournal  # noqa: E402
from test_call import Mic, Phone, Speaker  # noqa: E402
from jarvis.audio.switch import SwitchableSink, SwitchableSource  # noqa: E402


class Presence:
    def __init__(self, occupancy="empty"):
        self.value = occupancy

    def occupancy(self):
        return self.value


def setup(tmp_path, occupancy="empty", **limits):
    phone = Phone()
    now = [1_000_000.0]
    from jarvis.call import CallLimitSettings

    manager = CallManager(CallSettings(device="pixel", limits=CallLimitSettings(**limits) if limits else CallLimitSettings()),
                          SwitchableSource(Mic(1), sleep=lambda s: None), SwitchableSink(Speaker()),
                          lambda call: (Mic(7), Speaker()), phone, clock=lambda: now[0])
    presence = Presence(occupancy)
    bus = EventBus()
    AwayCallTrigger(manager, presence, HouseJournal(tmp_path / "journal.jsonl")).attach(bus)
    return bus, manager, phone, presence, now


def leak(room="la salle de bain"):
    return Event("leak.detected", "sensor:simulated", {"sensor": "fuite_sdb", "kind": "leak", "room": room})


def test_leak_while_away_calls_with_the_journal_sentence(tmp_path):
    bus, manager, phone, _, _ = setup(tmp_path)
    bus.publish(leak())
    assert phone.calls and phone.calls[0][0] == "ring"
    assert manager.current.reason == "Une fuite d'eau a été détectée dans la salle de bain."
    assert manager.current.origin == "routine"


def test_no_call_when_someone_is_home_or_presence_is_unknown(tmp_path):
    for occupancy in ("occupied", "unknown"):
        bus, manager, phone, _, _ = setup(tmp_path, occupancy)
        bus.publish(leak())
        assert phone.calls == [] and manager.current is None


def test_only_the_chosen_events_call(tmp_path):
    bus, manager, phone, _, _ = setup(tmp_path)
    bus.publish(Event("appliance.finished", "sensor:simulated", {"sensor": "machine", "name": "la machine à laver"}))
    bus.publish(Event("mail.received", "mail", {"sender": "Banque", "subject": "Relevé"}))
    assert phone.calls == []
    bus.publish(Event("presence.motion_while_away", "presence", {"room": "le salon", "sensor": "mouvement_salon"}))
    assert manager.current.reason == "Un mouvement a été détecté dans le salon pendant votre absence."


def test_call_limits_still_apply(tmp_path):
    bus, manager, phone, _, now = setup(tmp_path)
    bus.publish(leak())
    first = manager.current
    manager.refused(first.id)
    now[0] += 60
    bus.publish(leak("la cuisine"))  # pause après un refus : pas de second appel, l'événement reste au journal
    assert manager.current is first and [c for c in phone.calls if c[0] == "ring"] == [("ring", first.id)]


def test_shipped_away_events_and_calls_off_by_default():
    cfg = load_config(ROOT / "config.toml", local=False)
    assert cfg.call.enabled is False
    assert set(cfg.call.away_events) == {"leak.detected", "smoke.detected", "presence.motion_while_away",
                                         "door.left_open"}
