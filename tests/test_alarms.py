"""Réveils : heure dite, sonnerie (arrêt, durée maximale, fichier ou secours), annonces, outils vocaux."""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis.capabilities import CapabilityRegistry  # noqa: E402
from jarvis.routines import MemoryRoutineStore, RoutineEngine, RoutineError, parse_routine  # noqa: E402
from jarvis.routines.alarm import AlarmPlayer, beep, load_sound  # noqa: E402
from jarvis.routines.announce import Announcer  # noqa: E402
from jarvis.router import IntentRouter  # noqa: E402
from jarvis.scheduling.clock import parse_clock  # noqa: E402
from jarvis.tools import DONE, PermissionManager, ToolCore, ToolRegistry, ToolResult, builtin_tools  # noqa: E402
from jarvis.tools.alarms import alarm_tools  # noqa: E402
from test_tools import PERSONALITY, FakeProcesses, FakeVolume, Launcher, Locker, PlannerLLM, routes, run_agent  # noqa: E402

SATURDAY_22H = datetime(2026, 10, 3, 22, 0)


@pytest.mark.parametrize("text, clock", [
    ("Réveille-moi à 7 heures", (7, 0)), ("réveil à 7h30", (7, 30)), ("à sept heures et demie", (7, 30)),
    ("six heures moins le quart", (5, 45)), ("à midi", (12, 0)), ("à 10 heures du soir", (22, 0)),
    ("à huit heures moins 10", (7, 50)), ("mets un réveil", None), ("dans 10 minutes", None),
])
def test_spoken_clock(text, clock):
    assert parse_clock(text) == clock


# --- Sonnerie -------------------------------------------------------------------------------------

class Sink:
    def __init__(self):
        self.played, self.stopped = [], 0

    def play(self, audio, rate):
        self.played.append((len(audio), rate))
        time.sleep(0.01)

    def stop(self):
        self.stopped += 1


def test_alarm_rings_until_stopped():
    sink = Sink()
    player = AlarmPlayer(sink, None, max_minutes=5)
    result = []
    thread = threading.Thread(target=lambda: result.append(player.ring()))
    thread.start()
    while not sink.played:
        time.sleep(0.01)
    assert player.ringing and player.stop() is True
    thread.join(2)
    assert result == [True] and not player.ringing and sink.stopped == 1
    assert player.stop() is False


def test_alarm_stops_by_itself_after_the_maximum():
    now = {"t": 0.0}
    sink = Sink()
    sink.play = lambda audio, rate: now.__setitem__("t", now["t"] + 60)
    prepared = []
    assert AlarmPlayer(sink, None, max_minutes=3, before=lambda: prepared.append(1), clock=lambda: now["t"]).ring() is False
    assert now["t"] == 180 and prepared == [1]


def test_sound_file_or_fallback(tmp_path):
    import soundfile

    wav = tmp_path / "alarme.wav"
    soundfile.write(str(wav), np.zeros((8000, 2), np.int16), 16000)
    audio, rate = load_sound(wav)
    assert rate == 16000 and audio.dtype == np.int16 and audio.ndim == 1 and len(audio) == 8000
    fallback, rate = load_sound(tmp_path / "absent.mp3")
    assert rate == 22050 and np.array_equal(fallback, beep())
    (tmp_path / "abime.mp3").write_bytes(b"pas du son")
    assert np.array_equal(load_sound(tmp_path / "abime.mp3")[0], beep())


# --- Routines : réveil, annonces, routines ponctuelles ---------------------------------------------

class FakeAlarm:
    def __init__(self):
        self.rings, self.ringing = 0, False

    def ring(self):
        self.rings += 1
        return True

    def stop(self):
        return False


def tools_core():
    registry = ToolRegistry()
    for tool in builtin_tools(processes=FakeProcesses(), launcher=Launcher(), locker=Locker(), volume=FakeVolume(),
                              clock=lambda: SATURDAY_22H):
        registry.register(tool)
    return ToolCore(registry, PermissionManager())


def engine_with(alarm=None, announce=None, routines=(), now=SATURDAY_22H):
    core = tools_core()
    said = []
    engine = RoutineEngine(MemoryRoutineStore(list(routines)), core.registry, core.submit,
                           lambda title, text: said.append(text), clock=lambda: now, sleep=lambda s: None,
                           alarm=alarm, announce=announce)
    return engine, core, said


def test_alarm_and_announcements_in_a_routine():
    alarm = FakeAlarm()
    engine, _, said = engine_with(alarm, lambda what: {"time": "Il est 7 heures.", "day": "Samedi. Beau temps."}[what])
    routine = engine.create({"name": "Lever", "trigger": {"type": "time", "time": "07:00", "days": [5]},
                             "actions": [{"type": "alarm"}, {"type": "announce", "what": "time"},
                                         {"type": "announce", "what": "day"}]})
    engine.run(routine["id"], wait=True)
    assert alarm.rings == 1 and said == ["Il est 7 heures.", "Samedi. Beau temps."]
    with pytest.raises(RoutineError, match="annoncer|Annonce"):
        parse_routine({"name": "x", "trigger": {"type": "manual"}, "actions": [{"type": "announce", "what": "loto"}]},
                      engine._registry)


def test_once_routines_disappear_after_firing_but_not_after_a_test():
    engine, _, _ = engine_with(FakeAlarm())
    routine = engine.create({"name": "Réveil", "once": True, "trigger": {"type": "time", "time": "07:00", "days": [6]},
                             "actions": [{"type": "alarm"}]})
    engine.run(routine["id"], source="manual", wait=True)
    assert engine.get(routine["id"])
    engine.run(routine["id"], source="time", wait=True)
    assert engine.list() == []


def test_alarm_unavailable_fails_the_step():
    engine, _, _ = engine_with(None)
    routine = engine.create({"name": "Réveil", "trigger": {"type": "manual"}, "actions": [{"type": "alarm"}]})
    engine.run(routine["id"], wait=True)
    assert engine.get(routine["id"])["last_status"] == "error"


def test_day_announcement():
    def run_tool(data):
        message = {"get_date": "Nous sommes le samedi 3 octobre 2026.", "get_weather": "Demain à Nantes : ciel dégagé."}
        return type("Outcome", (), {"status": DONE, "result": ToolResult(data["tool"], True, {}, message=message.get(data["tool"], ""))})()

    announcer = Announcer(run_tool, lambda: ["réveil à 7 h 30", "rappel « sortir le linge » à 18 h 00"])
    assert announcer.text("day") == ("Nous sommes le samedi 3 octobre 2026. Demain à Nantes : ciel dégagé. "
                                     "Au programme aujourd'hui : réveil à 7 h 30 ; rappel « sortir le linge » à 18 h 00.")
    assert Announcer(run_tool, lambda: []).text("day").endswith("Rien de prévu aujourd'hui.")


def test_today_lists_remaining_alarms_and_routines():
    engine, _, _ = engine_with(FakeAlarm(), now=datetime(2026, 10, 3, 6, 0))
    engine.create({"name": "Réveil", "trigger": {"type": "time", "time": "07:30", "days": []}, "actions": [{"type": "alarm"}]})
    engine.create({"name": "Soir", "trigger": {"type": "time", "time": "21:00", "days": []}, "actions": [{"type": "wait", "seconds": 1}]})
    engine.create({"name": "Lundi", "trigger": {"type": "time", "time": "08:00", "days": [0]}, "actions": [{"type": "wait", "seconds": 1}]})
    assert engine.today() == ["réveil à 7 h 30", "routine « Soir » à 21 heures"]


# --- Outils vocaux --------------------------------------------------------------------------------

def alarm_core(now=SATURDAY_22H):
    engine, core, _ = engine_with(FakeAlarm(), now=now)
    for tool in alarm_tools(engine):
        core.registry.register(tool)
    return engine, core


def submit(core, tool, **parameters):
    return core.submit({"type": "tool_call", "tool": tool, "parameters": parameters}).result


def test_create_list_and_cancel_alarms():
    engine, core = alarm_core()
    assert submit(core, "create_alarm", time="7 heures 30").message == "Réveil programmé demain à 7 h 30."
    assert submit(core, "create_alarm", time="23 heures").message == "Réveil programmé aujourd'hui à 23 heures."
    routines = engine.list()
    assert all(r["once"] for r in routines) and routines[0]["actions"] == [{"type": "alarm"}, {"type": "announce", "what": "day"}]
    assert submit(core, "list_alarms").message == "Vous avez 2 réveils : aujourd'hui à 23 heures ; demain à 7 h 30."
    ambiguous = submit(core, "cancel_alarm")
    assert not ambiguous.success and "Lequel" in ambiguous.message
    assert submit(core, "cancel_alarm", time="7 heures 30").message == "Le réveil de 7 h 30 est annulé."
    assert submit(core, "list_alarms").message == "Vous avez un réveil aujourd'hui à 23 heures."
    assert submit(core, "cancel_alarm").success and submit(core, "list_alarms").message == "Aucun réveil n'est programmé."


def test_tomorrow_is_taken_from_the_request():
    engine, core = alarm_core(now=datetime(2026, 10, 3, 6, 0))
    llm = PlannerLLM({"type": "tool_call", "tool": "create_alarm", "parameters": {"time": "7 heures"}},
                     {"type": "tool_call", "tool": "create_alarm", "parameters": {"time": "7 heures"}})
    spoken, events = run_agent(["Réveille-moi à 7 heures.", "Mets un réveil demain à 7 heures."], llm, core)
    assert spoken == ["Réveil programmé aujourd'hui à 7 heures.", "Réveil programmé demain à 7 heures."]
    assert routes(events) == ["tool:tool.action"] * 2


def test_wake_word_and_stop_stop_the_alarm():
    class Ringing:
        def __init__(self):
            self.ringing, self.stops = True, 0

        def stop(self):
            self.stops += 1
            was, self.ringing = self.ringing, False
            return was

    alarm = Ringing()
    spoken, events = run_agent(["Arrête."], PlannerLLM(), alarm=alarm)
    assert alarm.stops >= 1 and ("alarm", "Réveil arrêté") in events


def test_alarm_requests_route_to_the_tools():
    _, core = alarm_core()
    router = IntentRouter(PERSONALITY, CapabilityRegistry(), tools=tuple(t.name for t in core.registry.list()))
    for text in ("Réveille-moi à 7 heures.", "Mets une alarme à 6 h 30.", "Quels réveils sont programmés ?"):
        assert router.route(text).label == "tool:tool.action", text


def test_cancel_the_alarm_of_a_said_day():
    # Session QA : « annule le réveil de demain » : le LLM proposait « demain » comme heure, appel écarté, puis
    # « les rappels ne sont pas encore disponibles ».
    from jarvis.tools.quick import quick_plan

    engine, core = alarm_core()
    submit(core, "create_alarm", time="7 heures", day="tomorrow")
    engine.create({"name": "Réveil", "trigger": {"type": "time", "time": "23:30", "days": []},
                   "actions": [{"type": "alarm"}]})
    assert quick_plan("Annule le réveil de demain", core.registry) == {
        "type": "tool_call", "tool": "cancel_alarm", "parameters": {}}
    assert quick_plan("Supprime mon réveil de 7 heures", core.registry)["parameters"] == {"time": "7 heures"}
    day = core.registry.get("cancel_alarm").parameters["day"].resolve("Annule le réveil de demain")
    assert submit(core, "cancel_alarm", day=day).message == "Le réveil de 7 heures est annulé."
    assert [a["spoken"] for a in submit(core, "list_alarms").result["alarms"]] == ["23 h 30"]
