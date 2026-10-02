"""Minuteurs et rappels : durées, gestionnaire, planificateur, événements, outils, cycle de vie.

Les échéances sont réelles mais très courtes (dixièmes de seconde) : le temps n'est pas simulé.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.activity import ActivityLog, JsonlActivityStore  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.events import ALL, EventBus  # noqa: E402
from jarvis.personality import load_personality  # noqa: E402
from jarvis.scheduling import DurationError, SchedulingError, Status, TimerManager, parse_duration, spoken_duration  # noqa: E402
from jarvis.scheduling.durations import duration_in_text  # noqa: E402
from jarvis.scheduling.scheduler import Scheduler  # noqa: E402
from jarvis.tools import CONFIRM, DONE, REJECTED, PermissionManager, Risk, ToolCore, ToolRegistry, plan  # noqa: E402
from jarvis.tools.timers import timer_tools  # noqa: E402

PERSONALITY = load_personality(ROOT / "personality.toml")


def wait_until(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class Recorder:
    def __init__(self, bus=None):
        self.events = []
        if bus is not None:
            bus.subscribe(ALL, self)

    def __call__(self, event):
        self.events.append(event)

    def types(self):
        return [e.type for e in self.events]


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def manager(bus):
    m = TimerManager(bus, max_seconds=3600, max_active=10)
    m.start()
    yield m
    m.stop()


# --- Durées --------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, seconds", [
    ("10 secondes", 10), ("30 secondes", 30), ("5 minutes", 300), ("1 heure", 3600), ("1 heure 30", 5400),
    ("1h30", 5400), ("une heure et demie", 5400), ("une demi-heure", 1800), ("un quart d'heure", 900),
    ("trois quarts d'heure", 2700), ("1 heure et quart", 4500), ("dans 25 minutes", 1500), ("vingt-cinq minutes", 1500),
    ("10 minutes et 30 secondes", 630), ("2 minutes 30", 150), ("dix secondes", 10), ("5 min", 300), ("90 s", 90),
    ("15 seconds", 15), ("1 hour 30 minutes", 5400),
])
def test_durations_are_parsed(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "demain", "quelques minutes", "un moment", "10", "0 minute", "-5 minutes",
                                  "10 pommes", "rm -rf /", "moins 5 minutes"])
def test_unclear_durations_are_refused(text):
    with pytest.raises(DurationError):
        parse_duration(text)


def test_spoken_duration_and_evidence():
    assert [spoken_duration(s) for s in (10, 600, 5400, 3661)] == [
        "10 secondes", "10 minutes", "1 heure 30 minutes", "1 heure 1 minute 1 seconde"]
    assert duration_in_text("1 heure", "Rappelle-moi dans une heure de vérifier le four")
    assert duration_in_text("15 minutes", "Fais-moi penser dans un quart d'heure à appeler Paul")
    assert not duration_in_text("5 minutes", "Mets un petit minuteur")
    assert not duration_in_text("20 minutes", "Mets un minuteur de 10 minutes")
    assert duration_in_text("15 seconds", "Rappelle-moi dans quinze secondes de sortir le linge")


# --- Gestionnaire : minuteurs --------------------------------------------------------------------

def test_timer_creation(manager, bus):
    recorder = Recorder(bus)
    timer = manager.create_timer(600)
    assert timer.id == "1" and timer.status is Status.ACTIVE and timer.seconds == 600
    assert 599 <= (timer.expires_at - timer.created_at).total_seconds() <= 601
    assert manager.timers() == [timer]
    assert recorder.events[0].type == "timer.created" and recorder.events[0].source == "scheduling"
    payload = recorder.events[0].payload
    assert payload["timer_id"] == "1" and payload["duration_seconds"] == 600 and "T" in payload["expires_at"]


def test_timer_expiry(manager, bus):
    recorder = Recorder(bus)
    timer = manager.create_timer(0.1)
    assert wait_until(lambda: "timer.finished" in recorder.types())
    assert timer.status is Status.COMPLETED and manager.timers() == []
    assert recorder.events[-1].payload == {**recorder.events[-1].payload, "timer_id": timer.id, "duration_seconds": 0}


def test_several_timers_run_independently_in_order(manager, bus):
    recorder = Recorder(bus)
    slow, fast, middle = manager.create_timer(0.3), manager.create_timer(0.1), manager.create_timer(0.2)
    assert [t.id for t in manager.timers()] == [fast.id, middle.id, slow.id]
    assert wait_until(lambda: recorder.types().count("timer.finished") == 3)
    finished = [e.payload["timer_id"] for e in recorder.events if e.type == "timer.finished"]
    assert finished == [fast.id, middle.id, slow.id]


def test_cancelled_timer_never_finishes(manager, bus):
    recorder = Recorder(bus)
    timer = manager.create_timer(0.1)
    assert manager.cancel_timer(timer.id).status is Status.CANCELLED
    time.sleep(0.25)
    assert recorder.types() == ["timer.created", "timer.cancelled"] and manager.timers() == []


def test_cancel_errors(manager):
    with pytest.raises(SchedulingError) as err:
        manager.cancel_timer("42")
    assert err.value.code == "timer_not_found"
    done = manager.create_timer(0.05)
    assert wait_until(lambda: done.status is Status.COMPLETED)
    with pytest.raises(SchedulingError) as err:
        manager.cancel_timer(done.id)
    assert err.value.code == "not_active" and "terminé" in err.value.message
    cancelled = manager.create_timer(60)
    manager.cancel_timer(cancelled.id)
    with pytest.raises(SchedulingError) as err:
        manager.cancel_timer(cancelled.id)
    assert err.value.code == "not_active" and "annulé" in err.value.message


@pytest.mark.parametrize("seconds", [0, -1, 3601, 1e9, True, "10", None])
def test_invalid_durations_are_refused(manager, seconds):
    with pytest.raises(SchedulingError) as err:
        manager.create_timer(seconds)
    assert err.value.code == "invalid_duration" and manager.timers() == []


def test_limit_of_active_items(bus):
    manager = TimerManager(bus, max_active=3)
    for _ in range(2):
        manager.create_timer(60)
    manager.create_reminder(60, "boire de l'eau")
    with pytest.raises(SchedulingError) as err:
        manager.create_timer(60)
    assert err.value.code == "limit_reached"


def test_cancel_and_expiry_races_produce_exactly_one_outcome(bus):
    recorder = Recorder(bus)
    manager = TimerManager(bus, max_active=100)
    manager.start()
    try:
        timers = [manager.create_timer(0.02) for _ in range(40)]
        for timer in timers:
            try:
                manager.cancel_timer(timer.id)
            except SchedulingError:
                pass
        assert wait_until(lambda: all(t.status is not Status.ACTIVE for t in timers))
        time.sleep(0.05)
        for timer in timers:
            outcomes = [e.type for e in recorder.events if e.payload.get("timer_id") == timer.id and e.type != "timer.created"]
            assert len(outcomes) == 1, (timer.id, outcomes)
            assert outcomes[0] == ("timer.cancelled" if timer.status is Status.CANCELLED else "timer.finished")
    finally:
        manager.stop()


# --- Gestionnaire : rappels ----------------------------------------------------------------------

def test_reminder_lifecycle(manager, bus):
    recorder = Recorder(bus)
    reminder = manager.create_reminder(0.1, "  sortir   le linge. ")
    assert reminder.message == "sortir le linge" and reminder.status is Status.ACTIVE
    assert manager.reminders() == [reminder] and manager.timers() == []
    assert wait_until(lambda: "reminder.finished" in recorder.types())
    finished = recorder.events[-1].payload
    assert finished["reminder_id"] == reminder.id and finished["message"] == "sortir le linge"
    assert reminder.status is Status.COMPLETED


def test_reminder_cancel_and_several(manager, bus):
    recorder = Recorder(bus)
    first = manager.create_reminder(0.1, "vérifier le four")
    second = manager.create_reminder(0.15, "appeler Paul")
    manager.cancel_reminder(first.id)
    assert wait_until(lambda: "reminder.finished" in recorder.types())
    time.sleep(0.1)
    assert recorder.types() == ["reminder.created", "reminder.created", "reminder.cancelled", "reminder.finished"]
    assert recorder.events[-1].payload["message"] == "appeler Paul" and second.status is Status.COMPLETED
    with pytest.raises(SchedulingError) as err:
        manager.cancel_reminder("99")
    assert err.value.code == "reminder_not_found"


@pytest.mark.parametrize("message", ["", "   ", "x" * 201, "ligne\x00autre", None, 42])
def test_invalid_reminder_messages(manager, message):
    with pytest.raises(SchedulingError) as err:
        manager.create_reminder(60, message)
    assert err.value.code == "invalid_message"


def test_events_payloads(manager, bus):
    recorder = Recorder(bus)
    timer = manager.create_timer(600)
    manager.cancel_timer(timer.id)
    reminder = manager.create_reminder(1200, "sortir le linge")
    manager.cancel_reminder(reminder.id)
    created, cancelled, r_created, r_cancelled = (e.payload for e in recorder.events)
    assert set(created) == {"timer_id", "duration_seconds", "expires_at", "subject"}
    assert created["subject"] == "minuteur 1 (10 minutes)" and cancelled["timer_id"] == "1"
    assert r_created["message"] == "sortir le linge" and r_created["delay_seconds"] == 1200
    assert r_cancelled["subject"] == "rappel 1 : sortir le linge"


# --- Cycle de vie et planificateur ---------------------------------------------------------------

def test_one_thread_for_all_timers_and_clean_stop(bus):
    before = {t.ident for t in threading.enumerate()}
    manager = TimerManager(bus)
    manager.start()
    manager.start()
    for _ in range(15):
        manager.create_timer(60)
    workers = [t for t in threading.enumerate() if t.ident not in before]
    assert len(workers) == 1 and workers[0].daemon and workers[0].name == "planificateur"
    manager.stop()
    assert not manager.running and not workers[0].is_alive()


def test_scheduler_survives_a_failing_callback():
    seen = []

    def on_due(key):
        seen.append(key)
        if key == "a":
            raise RuntimeError("panne")

    scheduler = Scheduler(on_due)
    scheduler.start()
    scheduler.schedule("a", 0.01)
    scheduler.schedule("b", 0.05)
    assert wait_until(lambda: seen == ["a", "b"])
    assert scheduler.cancel("absent") is False
    scheduler.stop()


def test_stopped_manager_does_not_fire(bus):
    recorder = Recorder(bus)
    manager = TimerManager(bus)
    manager.start()
    manager.create_timer(0.1)
    manager.stop()
    time.sleep(0.2)
    assert recorder.types() == ["timer.created"]


# --- Journal d'activité --------------------------------------------------------------------------

def test_activity_log_records_timer_and_reminder_events(manager, bus, tmp_path):
    activity = ActivityLog(JsonlActivityStore(tmp_path / "activity.jsonl"))
    activity.attach(bus)
    timer = manager.create_timer(0.05)
    reminder = manager.create_reminder(60, "sortir le linge")
    manager.cancel_reminder(reminder.id)
    assert wait_until(lambda: timer.status is Status.COMPLETED)
    lines = [e.line()[11:] for e in activity.recent()]
    assert lines == ["timer.created — minuteur 1 (0 seconde)", "reminder.created — rappel 1 : sortir le linge",
                     "reminder.cancelled — rappel 1 : sortir le linge", "timer.finished — minuteur 1 (0 seconde)"]


# --- Outils --------------------------------------------------------------------------------------

def make_core(manager):
    registry = ToolRegistry()
    for tool in timer_tools(manager):
        registry.register(tool)
    return ToolCore(registry, PermissionManager())


def call(tool, **parameters):
    return {"type": "tool_call", "tool": tool, "parameters": parameters}


def test_all_timer_tools_are_safe_and_never_ask(manager):
    tools = timer_tools(manager)
    assert {t.name: t.risk for t in tools} == {name: Risk.SAFE for name in (
        "create_timer", "cancel_timer", "list_timers", "create_reminder", "cancel_reminder", "list_reminders")}
    core = make_core(manager)
    assert core.submit(call("create_timer", duration="10 minutes")).status == DONE
    assert core.submit(call("create_reminder", delay="20 minutes", message="sortir le linge")).status == DONE
    for name in ("list_timers", "list_reminders", "cancel_timer", "cancel_reminder"):
        assert core.submit(call(name)).status != CONFIRM


def test_create_and_list_timers_through_tools(manager):
    core = make_core(manager)
    result = core.submit(call("create_timer", duration="trois quarts d'heure")).result
    assert result.success and result.result["timer_id"] == "1" and result.result["duration"] == "45 minutes"
    core.submit(call("create_timer", duration="10 minutes"))
    listing = core.submit(call("list_timers")).result.result
    assert listing["count"] == 2 and [t["timer_id"] for t in listing["timers"]] == ["2", "1"]
    assert set(listing["timers"][0]) == {"timer_id", "duration", "remaining", "ends_at"}


@pytest.mark.parametrize("duration, code", [
    ("demain", "invalid_parameters"), ("0 minute", "invalid_parameters"), ("quelques minutes", "invalid_parameters"),
    ("2 heures", "invalid_duration"), ("rm -rf /", "invalid_parameters"),
])
def test_invalid_timer_durations_through_tools(manager, duration, code):
    outcome = make_core(manager).submit(call("create_timer", duration=duration))
    assert outcome.status == REJECTED if code == "invalid_parameters" else not outcome.result.success
    assert outcome.result.error == code and manager.timers() == []


@pytest.mark.parametrize("data", [
    call("create_timer", duration=600), call("create_timer", seconds=600), call("create_timer", duration="10 minutes",
                                                                                   callback="os.system('calc')"),
    call("create_reminder", delay="10 minutes"), call("create_reminder", delay="10 minutes", message=""),
    call("cancel_timer", timer_id="1; rm"), call("cancel_timer", timer_id=1), call("cancel_reminder", reminder_id="abc"),
])
def test_malformed_timer_requests_are_refused(manager, data):
    assert make_core(manager).submit(data).status == REJECTED and manager.timers() == manager.reminders() == []


def test_cancel_timer_targets(manager):
    core = make_core(manager)
    assert core.submit(call("cancel_timer")).result.error == "timer_not_found"
    core.submit(call("create_timer", duration="10 minutes"))
    only = core.submit(call("cancel_timer")).result
    assert only.success and only.result == {"timer_id": "1", "duration": "10 minutes", "cancelled": True}
    core.submit(call("create_timer", duration="10 minutes"))
    core.submit(call("create_timer", duration="5 minutes"))
    ambiguous = core.submit(call("cancel_timer")).result
    assert ambiguous.error == "ambiguous_target" and "Lequel" in ambiguous.message
    assert core.submit(call("cancel_timer", duration="5 minutes")).result.result["timer_id"] == "3"
    assert core.submit(call("cancel_timer", duration="20 minutes")).result.error == "timer_not_found"
    assert core.submit(call("cancel_timer", timer_id="2")).result.result["timer_id"] == "2"
    assert core.submit(call("cancel_timer", timer_id="2")).result.error == "not_active"


def test_reminder_tools(manager):
    core = make_core(manager)
    created = core.submit(call("create_reminder", delay="25 minutes", message="sortir les poubelles")).result
    assert created.success and created.result["message"] == "sortir les poubelles"
    core.submit(call("create_reminder", delay="1 heure", message="vérifier le four"))
    listing = core.submit(call("list_reminders")).result.result
    assert listing["count"] == 2 and [r["message"] for r in listing["reminders"]] == ["sortir les poubelles",
                                                                                      "vérifier le four"]
    assert core.submit(call("cancel_reminder")).result.error == "ambiguous_target"
    cancelled = core.submit(call("cancel_reminder", message="le four")).result
    assert cancelled.success and cancelled.result["message"] == "vérifier le four"
    assert core.submit(call("cancel_reminder", message="linge")).result.error == "reminder_not_found"
    assert core.submit(call("cancel_reminder")).result.result["message"] == "sortir les poubelles"


class PlannerLLM:
    def __init__(self, proposal):
        self.proposal = proposal

    def chat_json(self, messages, schema):
        return self.proposal


def test_the_planner_never_invents_a_duration(manager):
    registry = make_core(manager).registry
    guess = {"type": "tool_call", "tool": "create_timer", "parameters": {"duration": "5 minutes"}}
    assert plan(PlannerLLM(guess), "Mets un petit minuteur", registry) is None
    assert plan(PlannerLLM(guess), "Mets un minuteur de cinq minutes", registry) == guess
    reminder = {"type": "tool_call", "tool": "create_reminder", "parameters": {"delay": "1 heure", "message": "four"}}
    assert plan(PlannerLLM(reminder), "Rappelle-moi dans une heure de vérifier le four", registry) == reminder
    assert plan(PlannerLLM(reminder), "Rappelle-moi de vérifier le four", registry) is None


def test_agent_close_stops_services():
    sys.path.insert(0, str(ROOT / "tests"))
    from test_tools import PlannerLLM as AgentLLM
    from test_tools import run_agent

    class Service:
        stopped = 0

        def stop(self):
            Service.stopped += 1

    class Broken:
        def stop(self):
            raise RuntimeError("arrêt impossible")

    agent_holder = {}
    import jarvis.agent as agent_module

    original = agent_module.Agent.run

    def run_and_keep(self):
        agent_holder["agent"] = self
        return original(self)

    agent_module.Agent.run = run_and_keep
    try:
        run_agent([], AgentLLM(), services=(Broken(), Service()))
    finally:
        agent_module.Agent.run = original
    agent_holder["agent"].close()
    assert Service.stopped == 1


def test_configuration():
    cfg = load_config(ROOT / "config.toml")
    assert cfg.timers.enabled and cfg.timers.max_hours == 24 and cfg.timers.max_active == 20


@pytest.mark.parametrize("claim", [
    "Vous serez rappelé dans 25 secondes pour sortir le linge.", "C'est noté, monsieur.",
    "Je vous rappelle dans 30 secondes.", "Votre minuteur est lancé.", "Le rappel a été programmé.",
])
def test_unbacked_timer_promises_are_blocked(claim):
    from jarvis.router import claims_action

    assert claims_action(claim)


@pytest.mark.parametrize("sentence", ["Vous ne serez pas rappelé.", "Je vous rappelle que Paris est en France.",
                                      "Je ne peux pas lancer de minuteur."])
def test_honest_sentences_are_not_blocked(sentence):
    from jarvis.router import claims_action

    assert not claims_action(sentence)


@pytest.mark.parametrize("heard", ["Rappele-moi dans 25 secondes de sortir le linge.", "Rapelle moi de manger",
                                   "Mets un minuteur de 10 minutes", "Quels rappels sont prévus ?"])
def test_timer_requests_reach_the_tools(heard):
    from jarvis.capabilities import CapabilityRegistry
    from jarvis.router import IntentRouter

    router = IntentRouter(PERSONALITY, CapabilityRegistry(), tools=("create_timer", "create_reminder"))
    assert router.route(heard).label == "tool:tool.action"


def test_remaining_time_is_rounded_to_the_minute():
    from jarvis.scheduling.durations import spoken_remaining

    assert spoken_remaining(3598) == "1 heure"
    assert spoken_remaining(125) == "2 minutes"
    assert spoken_remaining(45) == "45 secondes"


def test_timer_and_reminder_tools_speak_without_the_llm(manager):
    tools = {t.name: t for t in timer_tools(manager)}

    def say(name, **parameters):
        tool = tools[name]
        return tool.say(tool.execute(parameters))

    assert say("list_timers") == "Aucun minuteur n'est en cours."
    assert say("create_timer", duration=300).startswith("Minuteur de 5 minutes lancé, il sonnera à ")
    assert say("list_timers") == "Il reste 5 minutes sur votre minuteur de 5 minutes."
    assert say("cancel_timer") == "Votre minuteur de 5 minutes est annulé."
    assert say("create_reminder", delay=1200, message="sortir mon linge") == (
        "Entendu, je vous rappellerai de sortir votre linge dans 20 minutes.")
    assert say("list_reminders") == "Je dois vous rappeler de sortir votre linge dans 20 minutes."
    assert say("cancel_reminder") == "Le rappel de sortir votre linge est annulé."
