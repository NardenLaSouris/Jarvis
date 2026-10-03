"""Système de notifications : modèle, gestionnaire, canal vocal, intégration aux événements, journal, cycle de vie."""

from __future__ import annotations

import random
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis.activity import ActivityLog, JsonlActivityStore  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.events import ALL, Event, EventBus  # noqa: E402
from jarvis.factory import build_notifications  # noqa: E402
from jarvis.notifications import (  # noqa: E402
    EventNotifications, Notification, NotificationChannel, NotificationManager, Priority, VoiceNotificationChannel,
    with_de,
)
from jarvis.personality import load_personality  # noqa: E402
from jarvis.scheduling import TimerManager  # noqa: E402

PERSONALITY = load_personality(ROOT / "personality.toml")
TIMER_TEXTS = ("Monsieur, votre minuteur de 10 minutes est terminé.",
               "Monsieur, le minuteur de 10 minutes vient de se terminer.")


def wait_until(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class Recorder:
    def __init__(self, bus):
        self.events = []
        bus.subscribe(ALL, self.events.append)

    def types(self, prefix="notification."):
        return [e.type for e in self.events if e.type.startswith(prefix)]


class MemoryChannel(NotificationChannel):
    def __init__(self, name="memoire", min_priority=Priority.LOW, error=None):
        super().__init__(min_priority)
        self.name, self.error = name, error
        self.received, self.started, self.stopped = [], 0, 0

    def send(self, notification):
        if self.error:
            raise self.error
        self.received.append(notification)

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


class Speaker:
    """TTS simulé de l'agent : enregistre ce qui serait prononcé."""

    def __init__(self, fail_on=()):
        self.said, self.fail_on = [], set(fail_on)

    def __call__(self, text):
        if text in self.fail_on:
            raise RuntimeError("TTS en panne")
        self.said.append(text)


def note(message="Votre minuteur est terminé.", priority=Priority.NORMAL, title="Minuteur terminé"):
    return Notification(title, message, "timer", priority)


# --- Modèle --------------------------------------------------------------------------------------

def test_notification_model():
    n = Notification("Minuteur terminé", "Votre minuteur de 10 minutes est terminé.", "timer",
                     metadata={"timer_id": "1"})
    assert n.priority is Priority.NORMAL and len(n.id) == 12 and n.created_at <= time.time()
    assert n.id != note().id and n.metadata["timer_id"] == "1"
    with pytest.raises(TypeError):
        n.metadata["timer_id"] = "2"
    with pytest.raises(AttributeError):
        n.message = "autre"
    payload = n.payload("voice")
    assert payload["priority"] == "normal" and payload["channel"] == "voice" and payload["subject"] == "Minuteur terminé (voice)"


@pytest.mark.parametrize("kwargs", [
    {"title": ""}, {"title": "   "}, {"title": "x" * 81}, {"message": ""}, {"message": "x" * 501}, {"message": None},
    {"source": "Timer!"}, {"source": ""}, {"priority": 1}, {"priority": "high"},
])
def test_invalid_notifications_are_refused(kwargs):
    values = {"title": "Rappel", "message": "Sortir le linge.", "source": "reminder", "priority": Priority.NORMAL,
              **kwargs}
    with pytest.raises(ValueError):
        Notification(**values)


def test_priorities_are_ordered():
    assert Priority.LOW < Priority.NORMAL < Priority.HIGH and Priority.HIGH.label == "high"


# --- Gestionnaire --------------------------------------------------------------------------------

def test_register_and_send_to_several_channels():
    bus = EventBus()
    recorder = Recorder(bus)
    manager = NotificationManager(bus)
    first, second = MemoryChannel("voix"), MemoryChannel("bureau")
    manager.register(first)
    manager.register(second)
    n = note()
    assert manager.notify(n) == ["voix", "bureau"]
    assert first.received == second.received == [n]
    assert recorder.types() == ["notification.created"] and manager.channels() == {"voix": True, "bureau": True}
    with pytest.raises(ValueError):
        manager.register(MemoryChannel("voix"))


def test_disable_enable_and_unregister():
    manager = NotificationManager()
    channel = MemoryChannel()
    manager.register(channel)
    manager.disable("memoire")
    assert manager.notify(note()) == [] and channel.received == [] and manager.channels() == {"memoire": False}
    manager.enable("memoire")
    assert manager.notify(note()) == ["memoire"]
    assert manager.unregister("memoire") and channel.stopped == 1 and manager.notify(note()) == []
    assert manager.unregister("absent") is False


def test_channels_choose_by_priority():
    manager = NotificationManager()
    everything, urgent = MemoryChannel("tout"), MemoryChannel("urgent", min_priority=Priority.HIGH)
    manager.register(everything)
    manager.register(urgent)
    assert manager.notify(note(priority=Priority.NORMAL)) == ["tout"]
    assert manager.notify(note(priority=Priority.HIGH)) == ["tout", "urgent"]


def test_a_failing_channel_does_not_stop_the_others(caplog):
    bus = EventBus()
    recorder = Recorder(bus)
    manager = NotificationManager(bus)
    manager.register(MemoryChannel("casse", error=RuntimeError("canal en panne")))
    healthy = MemoryChannel("sain")
    manager.register(healthy)
    assert manager.notify(note()) == ["sain"] and len(healthy.received) == 1
    assert recorder.types() == ["notification.created", "notification.failed"]
    failed = recorder.events[-1].payload
    assert failed["channel"] == "casse" and failed["error"] == "canal en panne" and "canal en panne" in caplog.text


def test_lifecycle_starts_and_stops_channels():
    manager = NotificationManager()

    class Broken(MemoryChannel):
        def stop(self):
            raise RuntimeError("arrêt impossible")

    channel = MemoryChannel()
    manager.register(Broken("casse"))
    manager.register(channel)
    manager.start()
    manager.stop()
    assert channel.started == 1 and channel.stopped == 1


# --- Canal vocal ---------------------------------------------------------------------------------

def test_voice_channel_speaks_in_fifo_order():
    bus = EventBus()
    recorder = Recorder(bus)
    voice = VoiceNotificationChannel(bus)
    for message in ("Premier.", "Deuxième.", "Troisième."):
        voice.send(note(message))
    assert voice.pending() == 3
    speaker = Speaker()
    assert voice.deliver(speaker) == 3 and speaker.said == ["Premier.", "Deuxième.", "Troisième."]
    assert voice.pending() == 0 and voice.deliver(speaker) == 0
    assert recorder.types() == ["notification.started", "notification.sent", "notification.finished"] * 3


def test_voice_channel_never_speaks_two_notifications_at_once():
    voice = VoiceNotificationChannel()
    speaking, overlaps = [], []

    def slow_speaker(text):
        if speaking:
            overlaps.append(text)
        speaking.append(text)
        time.sleep(0.02)
        speaking.remove(text)

    for i in range(5):
        voice.send(note(f"Message {i}."))
    threads = [threading.Thread(target=voice.deliver, args=(slow_speaker,)) for _ in range(1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlaps == [] and voice.pending() == 0


def test_voice_failure_is_reported_and_the_next_one_is_spoken(caplog):
    bus = EventBus()
    recorder = Recorder(bus)
    voice = VoiceNotificationChannel(bus)
    voice.send(note("Celui-ci échoue."))
    voice.send(note("Celui-ci passe."))
    speaker = Speaker(fail_on={"Celui-ci échoue."})
    assert voice.deliver(speaker) == 2 and speaker.said == ["Celui-ci passe."]
    assert recorder.types() == ["notification.started", "notification.failed", "notification.finished",
                                "notification.started", "notification.sent", "notification.finished"]
    assert "TTS en panne" in caplog.text


def test_voice_queue_limit_and_stop():
    bus = EventBus()
    recorder = Recorder(bus)
    manager = NotificationManager(bus)
    voice = VoiceNotificationChannel(bus, max_pending=2)
    manager.register(voice)
    for _ in range(3):
        manager.notify(note())
    assert voice.pending() == 2 and recorder.types().count("notification.failed") == 1
    manager.stop()
    assert voice.pending() == 0 and recorder.types().count("notification.failed") == 3
    assert recorder.events[-1].payload["error"] == "arrêt de JARVIS"
    assert manager.notify(note()) == [] and voice.pending() == 0
    voice.start()
    assert manager.notify(note()) == ["voice"]


# --- Événements -> notifications ------------------------------------------------------------------

def wire(bus, voice=True):
    manager = NotificationManager(bus)
    channel = VoiceNotificationChannel(bus) if voice else None
    if channel:
        manager.register(channel)
    EventNotifications(manager, PERSONALITY, random.Random(0)).attach(bus)
    return manager, channel


def test_timer_and_reminder_events_become_deterministic_notifications():
    bus = EventBus()
    _, voice = wire(bus)
    bus.publish(Event("timer.finished", "scheduling", {"timer_id": "1", "duration_seconds": 600}))
    bus.publish(Event("reminder.finished", "scheduling", {"reminder_id": "1", "message": "sortir les poubelles"}))
    bus.publish(Event("reminder.finished", "scheduling", {"reminder_id": "2", "message": "appeler Paul"}))
    bus.publish(Event("timer.created", "scheduling", {"timer_id": "2", "duration_seconds": 60}))
    speaker = Speaker()
    voice.deliver(speaker)
    assert speaker.said[0] in TIMER_TEXTS
    assert speaker.said[1] in ("Monsieur, vous m'aviez demandé de vous rappeler de sortir les poubelles.",
                               "Monsieur, je vous rappelle de sortir les poubelles.")
    assert speaker.said[2].endswith("d'appeler Paul.") and len(speaker.said) == 3
    assert all("Jules" not in s and " tu " not in s for s in speaker.said)
    assert with_de("de sortir le linge") == "de sortir le linge" and with_de("éteindre le four") == "d'éteindre le four"


def test_notifications_carry_source_priority_and_metadata():
    rules = EventNotifications(NotificationManager(), PERSONALITY, random.Random(0))
    timer = rules.build(Event("timer.finished", "scheduling", {"timer_id": "3", "duration_seconds": 30}))
    reminder = rules.build(Event("reminder.finished", "scheduling", {"reminder_id": "4", "message": "vérifier le four"}))
    assert (timer.source, timer.priority, timer.title, timer.metadata["timer_id"]) == ("timer", Priority.NORMAL,
                                                                                       "Minuteur terminé", "3")
    assert (reminder.source, reminder.title, reminder.metadata["message"]) == ("reminder", "Rappel", "vérifier le four")
    assert rules.build(Event("tool.executed", "tools", {})) is None


@pytest.mark.parametrize("create", [lambda m: m.create_timer(0.05), lambda m: m.create_reminder(0.05, "sortir le linge")])
def test_scheduling_to_voice_end_to_end(create):
    bus = EventBus()
    recorder = Recorder(bus)
    _, voice = wire(bus)
    timers = TimerManager(bus)
    timers.start()
    try:
        create(timers)
        assert wait_until(lambda: voice.pending() == 1)
    finally:
        timers.stop()
    speaker = Speaker()
    voice.deliver(speaker)
    assert len(speaker.said) == 1 and speaker.said[0].startswith("Monsieur, ")
    assert recorder.types("") == [recorder.events[0].type, recorder.events[1].type, "notification.created",
                                  "notification.started", "notification.sent", "notification.finished"]
    assert recorder.events[1].type.endswith(".finished")


def test_cancelled_items_never_notify():
    bus = EventBus()
    _, voice = wire(bus)
    timers = TimerManager(bus)
    timers.start()
    try:
        timers.cancel_timer(timers.create_timer(0.05).id)
        timers.cancel_reminder(timers.create_reminder(0.05, "sortir le linge").id)
        time.sleep(0.2)
    finally:
        timers.stop()
    assert voice.pending() == 0


def test_voice_disabled_keeps_events_and_activity_log(tmp_path):
    bus = EventBus()
    recorder = Recorder(bus)
    activity = ActivityLog(JsonlActivityStore(tmp_path / "activity.jsonl"))
    activity.attach(bus)
    manager, voice = wire(bus, voice=False)
    bus.publish(Event("timer.finished", "scheduling", {"timer_id": "1", "duration_seconds": 10}))
    assert voice is None and manager.channels() == {}
    assert recorder.types() == ["notification.created"]
    assert [e.type for e in activity.recent()] == ["timer.finished", "notification.created"]


def test_activity_log_records_created_sent_and_failed_in_causal_order(tmp_path):
    bus = EventBus()
    activity = ActivityLog(JsonlActivityStore(tmp_path / "activity.jsonl"))
    activity.attach(bus)
    _, voice = wire(bus)
    bus.publish(Event("timer.finished", "scheduling",
                      {"timer_id": "1", "duration_seconds": 600, "subject": "minuteur 1 (10 minutes)"}))
    bus.publish(Event("reminder.finished", "scheduling",
                      {"reminder_id": "1", "message": "sortir le linge", "subject": "rappel 1 : sortir le linge"}))
    voice.deliver(Speaker(fail_on={"Monsieur, je vous rappelle de sortir le linge.",
                                   "Monsieur, vous m'aviez demandé de vous rappeler de sortir le linge."}))
    assert [e.line()[11:] for e in activity.recent()] == [
        "timer.finished — minuteur 1 (10 minutes)", "notification.created — Minuteur terminé",
        "reminder.finished — rappel 1 : sortir le linge", "notification.created — Rappel",
        "notification.sent — Minuteur terminé (voice)", "notification.failed — Rappel (voice) (TTS en panne)"]


def test_a_broken_notification_layer_never_reaches_the_scheduler(caplog):
    bus = EventBus()

    class Exploding(NotificationManager):
        def notify(self, notification):
            raise RuntimeError("gestionnaire en panne")

    EventNotifications(Exploding(bus), PERSONALITY).attach(bus)
    timers = TimerManager(bus)
    timers.start()
    try:
        timer = timers.create_timer(0.05)
        assert wait_until(lambda: timer.status.value == "completed")
    finally:
        timers.stop()
    assert "gestionnaire en panne" in caplog.text


# --- Agent et configuration ----------------------------------------------------------------------

def test_agent_speaks_queued_notifications_with_its_tts():
    from test_tools import PlannerLLM, run_agent

    bus = EventBus()
    recorder = Recorder(bus)
    _, voice = wire(bus)
    bus.publish(Event("reminder.finished", "scheduling", {"reminder_id": "1", "message": "sortir le linge"}))
    bus.publish(Event("timer.finished", "scheduling", {"timer_id": "1", "duration_seconds": 600}))
    spoken, events = run_agent(["Raconte-moi une blague"], PlannerLLM(reply="Voici une blague."), notifications=voice)
    notices = [text for kind, text in events if kind == "notification"]
    assert len(notices) == 2 and "sortir le linge" in notices[0] and notices[1] in TIMER_TEXTS
    assert voice.pending() == 0 and recorder.types().count("notification.sent") == 2


def test_configuration_builds_the_notification_system():
    cfg = load_config(ROOT / "config.toml", local=False)
    assert cfg.notifications.voice_enabled
    bus = EventBus()
    manager, voice = build_notifications(cfg, PERSONALITY, bus)
    assert isinstance(voice, VoiceNotificationChannel) and manager.channels() == {"voice": True}
    off, none = build_notifications(replace(cfg, notifications=replace(cfg.notifications, voice_enabled=False)),
                                    PERSONALITY, EventBus())
    assert none is None and off.channels() == {}
