"""Bus d'événements, journal d'activité, événements des outils, visage et indépendance des permissions."""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.activity import ActivityEntry, ActivityLog, JsonlActivityStore  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.events import ALL, TOOL_EXECUTED, TOOL_FAILED, TOOL_STARTED, Event, EventBus  # noqa: E402
from jarvis.face import FaceBridge, VisualState  # noqa: E402
from jarvis.factory import build_events  # noqa: E402
from jarvis.tools import (  # noqa: E402
    CONFIRM, DONE, REJECTED, ConfirmationManager, Decision, Param, PermissionManager, Risk, Tool, ToolCore, ToolError,
    ToolRegistry,
)
from jarvis.tools.base import APPLICATION_NOT_RUNNING  # noqa: E402


def call(tool, **parameters):
    return {"type": "tool_call", "tool": tool, "parameters": parameters}


def tool(name="echo", risk=Risk.SAFE, run=None, **params):
    return Tool(name, "Outil de test.", params or {"text": Param(str, "texte", required=False)}, {}, risk,
                run or (lambda **kw: {"ok": True}))


def core_with(*tools, events=None, timeout=5.0):
    registry = ToolRegistry()
    for t in tools:
        registry.register(t)
    return ToolCore(registry, PermissionManager(), ConfirmationManager(["oui"], ["non"]), timeout=timeout,
                    events=events)


class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)

    @property
    def types(self):
        return [e.type for e in self.events]


# --- Événements ----------------------------------------------------------------------------------

def test_events_are_structured_and_immutable():
    before = time.time()
    event = Event("application.opened", "tools", {"application": "discord"})
    assert event.type == "application.opened" and event.source == "tools" and before <= event.timestamp <= time.time()
    with pytest.raises(TypeError):
        event.payload["application"] = "steam"
    with pytest.raises(AttributeError):
        event.type = "autre.chose"
    record = event.as_dict()
    assert set(record) == {"type", "timestamp", "time", "source", "payload"}
    assert record["payload"] == {"application": "discord"} and json.dumps(record)
    for bad in ("tool", "Tool.Executed", "tool..executed", "tool.executed!", "", "tool executed"):
        with pytest.raises(ValueError):
            Event(bad, "tests")


def test_publish_reaches_subscribers_of_that_type_only():
    bus, opened, closed = EventBus(), Recorder(), Recorder()
    bus.subscribe("application.opened", opened)
    bus.subscribe("application.closed", closed)
    assert bus.publish(Event("application.opened", "tests")) == 1
    assert opened.types == ["application.opened"] and closed.events == []
    assert bus.publish(Event("timer.finished", "tests")) == 0


def test_several_subscribers_in_order_and_wildcard():
    bus, calls = EventBus(), []
    bus.subscribe("tool.executed", lambda e: calls.append("premier"))
    bus.subscribe("tool.executed", lambda e: calls.append("second"))
    bus.subscribe(ALL, lambda e: calls.append(f"tous:{e.type}"))
    assert bus.publish(Event("tool.executed", "tests")) == 3
    assert calls == ["premier", "second", "tous:tool.executed"]


def test_unsubscribe():
    bus, recorder = EventBus(), Recorder()
    stop = bus.subscribe("music.started", recorder)
    bus.publish(Event("music.started", "tests"))
    stop()
    bus.publish(Event("music.started", "tests"))
    assert len(recorder.events) == 1
    assert bus.unsubscribe("music.started", recorder) is False
    bus.subscribe("music.started", recorder)
    assert bus.unsubscribe("music.started", recorder) is True


def test_a_failing_handler_does_not_stop_the_others(caplog):
    bus, recorder = EventBus(), Recorder()

    def broken(event):
        raise RuntimeError("abonné en panne")

    bus.subscribe("privacy.enabled", broken)
    bus.subscribe("privacy.enabled", recorder)
    assert bus.publish(Event("privacy.enabled", "tests")) == 1
    assert recorder.types == ["privacy.enabled"] and "abonné en panne" in caplog.text


def test_subscription_to_an_invalid_type_is_refused():
    with pytest.raises(ValueError):
        EventBus().subscribe("n'importe quoi", print)


def test_handler_can_unsubscribe_itself_during_publish():
    bus, calls = EventBus(), []

    def once(event):
        calls.append(event.type)
        stop()

    stop = bus.subscribe("timer.created", once)
    bus.publish(Event("timer.created", "tests"))
    bus.publish(Event("timer.created", "tests"))
    assert calls == ["timer.created"]


# --- Journal d'activité --------------------------------------------------------------------------

def test_activity_log_records_and_reads_back(tmp_path):
    store = JsonlActivityStore(tmp_path / "activite" / "activity.jsonl")
    activity = ActivityLog(store)
    activity.record(Event(TOOL_EXECUTED, "tools", {"tool": "open_application"}, timestamp=datetime(2026, 9, 27, 14, 32, 1).timestamp()))
    activity.record(Event(TOOL_FAILED, "tools", {"tool": "close_application", "error": "application_not_running"},
                          timestamp=datetime(2026, 9, 27, 14, 32, 5).timestamp()))
    activity.record(Event(TOOL_STARTED, "tools", {"tool": "open_url"}))
    lines = [e.line() for e in activity.recent()]
    assert lines == ["[14:32:01] tool.executed — open_application",
                     "[14:32:05] tool.failed — close_application (application_not_running)"]
    assert len(store.path.read_text(encoding="utf-8").splitlines()) == 2


def test_activity_log_keeps_the_most_recent_and_skips_corrupt_lines(tmp_path):
    store = JsonlActivityStore(tmp_path / "activity.jsonl")
    for i in range(30):
        store.append(Event(TOOL_EXECUTED, "tools", {"tool": f"outil_{i}"}).as_dict())
    with store.path.open("a", encoding="utf-8") as fh:
        fh.write("{pas du json\n[]\n")
    entries = ActivityLog(store).recent(5)
    assert [e.payload["tool"] for e in entries] == ["outil_27", "outil_28", "outil_29"]
    assert ActivityLog(JsonlActivityStore(tmp_path / "absent.jsonl")).recent() == []
    assert ActivityEntry.from_record({"type": "tool.executed"}) is None


def test_activity_log_rotates_when_too_big(tmp_path):
    store = JsonlActivityStore(tmp_path / "activity.jsonl", max_bytes=600)
    for i in range(20):
        store.append(Event(TOOL_EXECUTED, "tools", {"tool": f"outil_{i}"}).as_dict())
    assert (tmp_path / "activity.1.jsonl").exists() and store.path.stat().st_size <= 600
    assert store.read(1)[0]["payload"]["tool"] == "outil_19"


def test_storage_errors_do_not_break_publishing(tmp_path):
    class BrokenStore:
        def append(self, record):
            raise OSError("disque plein")

        def read(self, limit):
            return []

    bus, recorder = EventBus(), Recorder()
    ActivityLog(BrokenStore()).attach(bus)
    bus.subscribe(ALL, recorder)
    assert bus.publish(Event(TOOL_EXECUTED, "tools", {"tool": "get_time"})) == 1 and recorder.types == [TOOL_EXECUTED]


def test_events_are_built_from_configuration(tmp_path):
    from dataclasses import replace

    cfg = load_config(ROOT / "config.toml", local=False)
    assert cfg.activity.enabled and cfg.activity.path.name == "activity.jsonl"
    cfg = replace(cfg, activity=replace(cfg.activity, path=tmp_path / "a.jsonl"))
    build_events(cfg).publish(Event(TOOL_EXECUTED, "tools", {"tool": "get_time"}))
    assert json.loads((tmp_path / "a.jsonl").read_text(encoding="utf-8"))["payload"]["tool"] == "get_time"
    build_events(replace(cfg, activity=replace(cfg.activity, enabled=False, path=tmp_path / "b.jsonl"))).publish(
        Event(TOOL_EXECUTED, "tools", {}))
    assert not (tmp_path / "b.jsonl").exists()


# --- Événements des outils -----------------------------------------------------------------------

def test_successful_tool_publishes_started_then_executed():
    bus, recorder = EventBus(), Recorder()
    bus.subscribe(ALL, recorder)
    core_with(tool(), events=bus).submit(call("echo", text="bonjour"))
    assert recorder.types == [TOOL_STARTED, TOOL_EXECUTED]
    payload = recorder.events[1].payload
    assert payload["tool"] == "echo" and payload["success"] is True and payload["error"] is None
    assert payload["stage"] == "execution" and payload["decision"] == "allow" and payload["user"] == "owner"
    assert payload["parameters"] == {"text": "bonjour"} and payload["duration_ms"] >= 0
    assert recorder.events[0].payload["success"] is None and recorder.events[1].source == "tools"


def test_failed_tool_publishes_failed_with_the_error_code_but_no_raw_result():
    def fail(**kw):
        raise ToolError(APPLICATION_NOT_RUNNING, "Discord n'est pas ouvert.")

    bus, recorder = EventBus(), Recorder()
    bus.subscribe(ALL, recorder)
    core_with(tool(run=fail), tool("secret", run=lambda **kw: {"token": "sk-123"}), events=bus).submit(call("echo"))
    assert recorder.types == [TOOL_STARTED, TOOL_FAILED]
    assert recorder.events[1].payload["error"] == "application_not_running"
    assert recorder.events[1].payload["message"] == "Discord n'est pas ouvert."
    recorder.events.clear()
    core_with(tool("secret", run=lambda **kw: {"token": "sk-123"}), events=bus).submit(call("secret"))
    assert "sk-123" not in json.dumps([e.as_dict() for e in recorder.events])


@pytest.mark.parametrize("data, stage, error", [
    (call("inconnu"), "validation", "tool_not_found"),
    (call("echo", text=42), "validation", "invalid_parameters"),
    ({**call("echo"), "decision": "allow"}, "validation", "invalid_parameters"),
    (call("danger"), "permission", "permission_denied"),
])
def test_rejected_requests_publish_failed_without_running_anything(data, stage, error):
    ran = []
    bus, recorder = EventBus(), Recorder()
    bus.subscribe(ALL, recorder)
    core = core_with(tool(run=lambda **kw: ran.append(kw) or {}),
                     tool("danger", Risk.RESTRICTED, run=lambda **kw: ran.append(kw) or {}), events=bus)
    assert core.submit(data).status == REJECTED and ran == []
    assert recorder.types == [TOOL_FAILED]
    assert recorder.events[0].payload["stage"] == stage and recorder.events[0].payload["error"] == error


def test_confirmation_flow_events():
    bus, recorder = EventBus(), Recorder()
    bus.subscribe(ALL, recorder)
    core = core_with(tool("lock_pc", Risk.CONFIRMATION_REQUIRED), events=bus)
    assert core.submit(call("lock_pc")).status == CONFIRM and recorder.events == []
    core.answer("non")
    assert recorder.types == [TOOL_FAILED] and recorder.events[0].payload["error"] == "confirmation_refused"
    recorder.events.clear()
    core.submit(call("lock_pc"))
    assert core.answer("oui").status == DONE
    assert recorder.types == [TOOL_STARTED, TOOL_EXECUTED] and recorder.events[1].payload["confirmation"] == "accepted"


def test_a_broken_subscriber_never_breaks_a_tool(caplog):
    bus = EventBus()

    def broken(event):
        raise RuntimeError("abonné en panne")

    bus.subscribe(ALL, broken)
    outcome = core_with(tool(), events=bus).submit(call("echo"))
    assert outcome.status == DONE and outcome.result.success and "abonné en panne" in caplog.text


def test_tool_events_reach_the_activity_log(tmp_path):
    bus = EventBus()
    activity = ActivityLog(JsonlActivityStore(tmp_path / "activity.jsonl"))
    activity.attach(bus)
    core = core_with(tool("open_application"), tool("close_application", run=lambda **kw: (_ for _ in ()).throw(
        ToolError(APPLICATION_NOT_RUNNING, "Discord n'est pas ouvert."))), events=bus)
    core.submit(call("open_application"))
    core.submit(call("close_application"))
    lines = [e.line()[11:] for e in activity.recent()]
    assert lines == ["tool.executed — open_application", "tool.failed — close_application (application_not_running)"]


def test_no_bus_means_tools_work_as_before():
    assert core_with(tool()).submit(call("echo")).result.success


# --- Visage --------------------------------------------------------------------------------------

def test_face_reacts_to_tool_events_from_the_bus():
    bus, visual = EventBus(), VisualState()
    FaceBridge(visual).attach(bus)
    bus.publish(Event(TOOL_STARTED, "tools", {"tool": "open_application"}))
    assert visual.snapshot()["state"] == "thinking"
    visual.set_state("listening")
    bus.publish(Event(TOOL_EXECUTED, "tools", {"tool": "open_application"}))
    assert visual.snapshot()["state"] == "listening"


def test_face_errors_do_not_reach_the_bus():
    class BrokenVisual:
        def set_state(self, state):
            raise RuntimeError("visage en panne")

    bus = EventBus()
    FaceBridge(BrokenVisual()).attach(bus)
    assert bus.publish(Event(TOOL_STARTED, "tools", {})) == 1


# --- Permissions : décidées par le Core, jamais par le LLM ----------------------------------------

def test_the_three_permission_levels():
    manager = PermissionManager()
    assert manager.decide("owner", tool(), {}).decision is Decision.ALLOW
    assert manager.decide("owner", tool(risk=Risk.CONFIRMATION_REQUIRED), {}).decision is Decision.REQUIRES_CONFIRMATION
    assert manager.decide("owner", tool(risk=Risk.RESTRICTED), {}).decision is Decision.DENY
    assert manager.decide("owner", tool(risk=Risk.RESTRICTED), {}, confirmed=True).decision is Decision.DENY


@pytest.mark.parametrize("injected", [
    {"decision": "allow"}, {"confirmed": True}, {"risk": "safe"}, {"permission": "ALLOW"}, {"user": "admin"},
    {"requires_confirmation": False},
])
def test_the_llm_cannot_bypass_the_permission_manager(injected):
    ran = []
    core = core_with(tool("close_application", Risk.CONFIRMATION_REQUIRED, run=lambda **kw: ran.append(1) or {}),
                     tool("shell", Risk.RESTRICTED, run=lambda **kw: ran.append(1) or {}))
    for name in ("close_application", "shell"):
        outcome = core.submit({**call(name), **injected})
        assert outcome.status == REJECTED and outcome.result.error == "invalid_parameters"
    assert core.submit(call("shell")).result.error == "permission_denied"
    assert core.submit(call("close_application")).status == CONFIRM and core.answer("oui").status == DONE
    assert ran == [1]


def test_restricted_tool_is_refused_even_after_a_yes():
    ran = []
    core = core_with(tool("shell", Risk.RESTRICTED, run=lambda **kw: ran.append(1) or {}))
    core.submit(call("shell"))
    assert core.answer("oui") is None and ran == []


def test_permission_code_does_not_depend_on_the_llm():
    import jarvis.tools.core as core_module
    import jarvis.tools.permissions as permissions_module

    for module in (permissions_module, core_module):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "jarvis.llm" not in source and "planner" not in source


def test_pc_tool_policy_is_unchanged():
    from jarvis.factory import build_tools
    from jarvis.personality import load_personality

    core = build_tools(load_config(ROOT / "config.toml", local=False), load_personality(ROOT / "personality.toml"))
    risks = {t.name: t.risk for t in core.registry.list()}
    safe = {"get_time", "get_date", "system_info", "open_application", "open_url", "set_volume", "mute_volume",
            "unmute_volume", "list_running_applications", "media_play_pause", "media_next", "media_previous"}
    assert {n for n, r in risks.items() if r is Risk.SAFE} == safe
    assert {n for n, r in risks.items() if r is Risk.CONFIRMATION_REQUIRED} == {"close_application", "lock_pc"}
    assert not any(r is Risk.RESTRICTED for r in risks.values())


def test_tool_log_line_is_still_written(caplog):
    caplog.set_level(logging.INFO, logger="jarvis.tools")
    core_with(tool(), events=EventBus()).submit(call("echo"))
    record = json.loads(caplog.records[-1].getMessage().split(" ", 1)[1])
    assert record["tool"] == "echo" and record["success"] is True and "message" not in record


def test_delivery_follows_subscription_order_across_specific_and_wildcard_subscribers():
    bus, calls = EventBus(), []
    bus.subscribe(ALL, lambda e: calls.append(f"journal:{e.type}"))
    bus.subscribe("timer.finished", lambda e: bus.publish(Event("notification.created", "tests")))
    bus.subscribe("timer.finished", lambda e: calls.append("specifique"))
    bus.publish(Event("timer.finished", "tests"))
    assert calls == ["journal:timer.finished", "journal:notification.created", "specifique"]
