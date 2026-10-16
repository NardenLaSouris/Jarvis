"""Règles d'appel (jarvis/call/rules.py, rule_parser.py, rule_tools.py) : validation par le Core, éléments ancrés
dans la phrase, déduplication, tentatives limitées, accusé « c'est noté », refus, limites anti-harcèlement, retour à
la maison, reprise après redémarrage, sources non fiables ignorées, outils et conversation. Aucun appel réel."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis.call.manager import CallRefused  # noqa: E402
from jarvis.call.rule_parser import RuleQuestion, build_rule  # noqa: E402
from jarvis.call.rules import (  # noqa: E402
    ACKNOWLEDGED, ANSWERED, FAILED, GAVE_UP, LIMIT_RETRY, MAX_JOB_AGE, PLANNED, REFUSED, RESOLVED, TRYING,
    UNACKNOWLEDGED, CallRules, RuleError, describe_rule, is_acknowledgement, validate_rule,
)
from jarvis.call.session import CallSession  # noqa: E402
from jarvis.events import Event, EventBus  # noqa: E402

NOW = datetime(2026, 10, 11, 15, 30)


class FakeCalls:
    def __init__(self):
        self.requested, self.messages, self.refuse = [], [], None

    def request(self, reason, origin):
        if self.refuse:
            raise CallRefused(self.refuse)
        call = CallSession("linphone", reason, origin, 0.0)
        self.requested.append(call)
        return call

    def reply(self, text):
        self.messages.append(text)


class World:
    def __init__(self, tmp_path, state="AWAY", away_since=0.0):
        self.t = 10_000.0
        self.now = NOW
        self.bus = EventBus()
        self.calls = FakeCalls()
        self.presence = SimpleNamespace(users={"owner": SimpleNamespace(state=state, away_since=away_since)})
        self.path = tmp_path / "call_rules.json"
        self.rules = self.make()

    def make(self):
        rules = CallRules(self.path, self.calls, self.presence, clock=lambda: self.now, monotonic=lambda: self.t)
        rules.attach(self.bus)
        return rules

    def rule(self, **changes):
        data = {"trigger": {"type": "event", "event": "reminder_due"}, "conditions": [{"type": "away"}],
                "repeat": {"attempts": 2, "interval_minutes": 15}, **changes}
        return self.rules.add(data, "appelle-moi quand un rappel arrive et que je suis absent")

    def reminder(self, number="7", message="appeler le garage", source="scheduling"):
        self.bus.publish(Event("reminder.finished", source, {"reminder_id": number, "message": message}))

    def call_event(self, kind, index=-1):
        call = self.calls.requested[index]
        self.bus.publish(Event(kind, "call", {"call": call.id, "end_reason": kind}))

    def later(self, seconds):
        self.t += seconds
        self.now += timedelta(seconds=seconds)
        self.rules.tick()

    def job(self):
        return self.rules.jobs[-1]


# --- Validation et compréhension ------------------------------------------------------------------------------

def test_core_refuses_incoherent_or_unknown_rules():
    for bad in ({"trigger": {"type": "event", "event": "shell"}},
                {"trigger": {"type": "event", "event": "reminder_due"},
                 "conditions": [{"type": "away"}, {"type": "home"}]},
                {"trigger": {"type": "event", "event": "reminder_due"}, "repeat": {"attempts": 50}},
                {"trigger": {"type": "event", "event": "mail_reply"}},
                {"trigger": {"type": "at", "at": (NOW + timedelta(days=90)).isoformat()}},
                {"trigger": {"type": "at", "at": (NOW - timedelta(hours=1)).isoformat()}}):
        with pytest.raises(RuleError):
            validate_rule(bad, NOW)


def test_llm_elements_must_be_said_and_dates_are_computed_by_the_core():
    data = {"type": "rule", "event": "reminder_due", "conditions": ["away", "important"], "channel": "call"}
    rule, _ = build_rule(data, "Appelle-moi quand je ne suis pas à la maison et qu'un rappel arrive", NOW, {})
    assert rule["conditions"] == [{"type": "away"}]  # « important » jamais dit : écarté
    with pytest.raises(RuleQuestion):  # événement inventé : on demande
        build_rule({"type": "rule", "event": "smoke", "conditions": [], "channel": "call"},
                   "Appelle-moi quand je suis absent", NOW, {})
    rule, _ = build_rule({"type": "rule", "event": "at", "at_said": "demain à 18 h", "conditions": ["away"],
                          "channel": "call"}, "Appelle-moi demain à 18 h si je ne suis toujours pas rentré", NOW, {})
    assert rule["trigger"] == {"type": "at", "at": "2026-10-12T18:00:00"} and rule["once"]
    with pytest.raises(RuleQuestion):  # « ce mail » sans mail lu
        build_rule({"type": "rule", "event": "mail_reply", "conditions": [], "channel": "call"},
                   "Appelle-moi quand je reçois une réponse à ce mail", NOW, {})
    rule, label = build_rule({"type": "rule", "event": "at", "at_said": "vendredi", "conditions": ["mail_unanswered"],
                              "channel": "call"}, "Appelle-moi si je n'ai pas répondu à ce message avant vendredi",
                             NOW, {"mail": {"thread": "<a@b>", "subject": "Dossier"}})
    assert rule["trigger"]["at"] == "2026-10-16T18:00:00" and rule["conditions"][0]["thread"] == "<a@b>"
    assert "« Dossier »" in describe_rule(rule, {label[0]: label[1]})
    with pytest.raises(RuleQuestion):
        build_rule({"type": "question", "question": "Pour quel événement, monsieur ?"}, "Appelle-moi", NOW, {})


def test_acknowledgements():
    assert is_acknowledgement("C'est noté") and is_acknowledgement("Bien reçu, merci")
    assert not is_acknowledgement("Rappelle-moi dans une heure")


# --- Moteur --------------------------------------------------------------------------------------------------

def test_reminder_while_away_calls_once_even_if_the_event_is_replayed(tmp_path):
    w = World(tmp_path)
    w.rule()
    w.reminder()
    w.reminder()  # même rappel publié deux fois
    assert len(w.calls.requested) == 1 and w.job()["state"] == TRYING
    assert w.calls.requested[0].reason == "Votre rappel arrive à échéance : appeler le garage."
    assert w.calls.requested[0].origin == "routine"


def test_no_call_when_home_and_the_decision_is_explained(tmp_path):
    w = World(tmp_path, state="HOME")
    w.rule()
    w.reminder()
    assert w.calls.requested == []
    assert "vous étiez à la maison" in " ".join(w.rules.explain())


def test_no_answer_is_retried_a_limited_number_of_times_then_a_message(tmp_path):
    w = World(tmp_path)
    w.rule()
    w.reminder()
    w.call_event("call.expired")
    w.later(60)
    assert len(w.calls.requested) == 1  # intervalle pas encore écoulé
    w.later(15 * 60)
    assert len(w.calls.requested) == 2
    w.call_event("call.expired")
    for _ in range(10):
        w.later(20 * 60)
    assert len(w.calls.requested) == 2 and w.job()["state"] == GAVE_UP
    assert len(w.calls.messages) == 1 and "pas réussi à vous joindre" in w.calls.messages[0]


def test_answered_and_acknowledged(tmp_path):
    w = World(tmp_path)
    w.rule()
    w.reminder()
    w.call_event("call.connected")
    call_id = w.calls.requested[0].id
    assert w.rules.awaiting_ack(call_id) and w.job()["state"] == ANSWERED
    assert w.rules.acknowledge(call_id) and w.job()["state"] == ACKNOWLEDGED
    w.call_event("call.ended")
    w.later(3600)
    assert w.calls.messages == [] and len(w.calls.requested) == 1


def test_answered_without_acknowledgement_sends_a_summary_but_never_calls_again(tmp_path):
    w = World(tmp_path)
    w.rule()
    w.reminder()
    w.call_event("call.connected")
    w.call_event("call.ended")
    w.later(3600)
    assert w.job()["state"] == UNACKNOWLEDGED and len(w.calls.requested) == 1
    assert len(w.calls.messages) == 1 and "c'est noté" in w.calls.messages[0]
    assert w.rules.acknowledge() and w.job()["state"] == ACKNOWLEDGED  # « c'est noté » par message


def test_refused_call_is_not_repeated(tmp_path):
    w = World(tmp_path)
    w.rule()
    w.reminder()
    w.call_event("call.refused")
    w.later(3600)
    assert w.job()["state"] == REFUSED and len(w.calls.requested) == 1 and len(w.calls.messages) == 1


def test_anti_harassment_limits_postpone_without_counting_then_give_up(tmp_path):
    w = World(tmp_path)
    w.rule()
    w.calls.refuse = "dernier appel trop récent"
    w.reminder()
    assert w.job()["state"] == PLANNED and w.job()["attempts"] == 0
    w.calls.refuse = None
    w.later(LIMIT_RETRY + 1)
    assert w.job()["state"] == TRYING and w.job()["attempts"] == 1
    w2 = World(tmp_path / "b")
    w2.rule()
    w2.calls.refuse = "trop d'appels sur la dernière heure"
    w2.reminder()
    for _ in range(int(MAX_JOB_AGE / LIMIT_RETRY) + 3):
        w2.later(LIMIT_RETRY + 1)
    assert w2.job()["state"] == GAVE_UP and w2.calls.requested == []


def test_return_home_resolves_pending_calls(tmp_path):
    w = World(tmp_path)
    w.rule()
    w.reminder()
    w.call_event("call.expired")
    w.presence.users["owner"].state = "HOME"
    w.bus.publish(Event("presence.arrival_confirmed", "presence", {"user": "owner"}))
    w.later(20 * 60)
    assert w.job()["state"] == RESOLVED and len(w.calls.requested) == 1


def test_restart_resumes_jobs_and_never_duplicates(tmp_path):
    w = World(tmp_path)
    w.rule()
    w.reminder()
    assert w.job()["state"] == TRYING
    w.rules = w.make()  # redémarrage pendant l'appel
    assert w.job()["state"] == FAILED
    w.reminder()  # rappel annoncé de nouveau (en retard) : même fait, pas de deuxième série
    w.later(61)
    assert len(w.rules.jobs) == 1 and len(w.calls.requested) == 2


def test_time_rule_checks_at_the_time_said(tmp_path):
    w = World(tmp_path, state="HOME")
    w.rules.add({"trigger": {"type": "at", "at": (NOW + timedelta(hours=2)).isoformat()},
                 "conditions": [{"type": "away"}]}, "appelle-moi dans deux heures si je ne suis pas rentré")
    w.later(3600)
    assert w.calls.requested == []
    w.presence.users["owner"].state = "AWAY"
    w.later(3700)
    assert len(w.calls.requested) == 1
    w.later(3700)
    assert len(w.calls.requested) == 1 and w.rules.rules[0]["done"]


def test_forged_sources_are_ignored(tmp_path):
    w = World(tmp_path)
    w.rule()
    w.reminder(source="mail")  # un « rappel » publié par autre chose que le gestionnaire de rappels
    assert w.calls.requested == []


def test_disabling_or_deleting_cancels_pending_calls(tmp_path):
    w = World(tmp_path)
    w.rule()
    w.reminder()
    w.call_event("call.expired")
    w.rules.set_enabled(1, False)
    w.later(3600)
    assert len(w.calls.requested) == 1
    w.reminder("8")
    assert len(w.calls.requested) == 1
    w.rules.remove(1)
    assert w.rules.rules == []


# --- Outils et conversation ---------------------------------------------------------------------------------

class RuleLLM:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def chat_json(self, messages, schema):
        self.calls.append(messages)
        return self.answers.pop(0)


def test_tools_create_after_confirmation_list_disable_delete(tmp_path):
    from jarvis.call.rule_parser import RuleParser
    from jarvis.call.rule_tools import call_rule_tools
    from jarvis.tools import PermissionManager, ToolCore, ToolRegistry

    w = World(tmp_path)
    llm = RuleLLM({"type": "rule", "event": "reminder_due", "conditions": ["away"], "channel": "call"})
    core = ToolCore(ToolRegistry(), PermissionManager())
    for tool in call_rule_tools(w.rules, RuleParser(llm, clock=lambda: NOW), lambda: {"mail": None, "reminder": None}):
        core.registry.register(tool)
    text = "Appelle-moi quand je ne suis pas à la maison et qu'un rappel arrive à échéance"
    asked = core.submit({"type": "tool_call", "tool": "create_call_rule", "parameters": {"request": text}})
    assert asked.status == "confirm" and "vous appeler quand un rappel arrive à échéance, si vous êtes absent" \
        in asked.question
    assert w.rules.rules == []  # rien avant le « oui »
    assert core.answer("oui").result.success and len(w.rules.rules) == 1
    listed = core.submit({"type": "tool_call", "tool": "list_call_rules", "parameters": {}})
    assert "1 règle" in core.registry.get("list_call_rules").say(listed.result.result)
    core.submit({"type": "tool_call", "tool": "set_call_rule_enabled", "parameters": {"enabled": "non"}})
    assert not w.rules.rules[0]["enabled"]
    core.submit({"type": "tool_call", "tool": "delete_call_rule", "parameters": {"position": 1}})
    core.answer("oui")
    assert w.rules.rules == []


def test_agent_asks_a_targeted_question_then_completes_the_rule(tmp_path):
    from jarvis.call.rule_parser import RuleParser
    from jarvis.call.rule_tools import call_rule_tools
    from test_tools import PlannerLLM, make_core, run_agent

    w = World(tmp_path)
    core = make_core()
    rule_llm = RuleLLM({"type": "question", "question": "Pour quel événement, monsieur ?"},
                       {"type": "rule", "event": "reminder_due", "conditions": ["away"], "channel": "call"})
    for tool in call_rule_tools(w.rules, RuleParser(rule_llm, clock=lambda: NOW), lambda: {}):
        core.registry.register(tool)
    spoken, events = run_agent(["Appelle-moi quand je suis absent", "Quand un rappel arrive", "Oui"],
                               PlannerLLM(reply="Bien."), core=core)
    assert spoken[0] == "Pour quel événement, monsieur ?"
    assert spoken[1].startswith("Voulez-vous que je crée cette règle")
    assert len(w.rules.rules) == 1 and w.rules.rules[0]["rule"]["trigger"]["event"] == "reminder_due"


def test_builtin_rule_calls_for_a_reminder_while_away_and_stays_removed(tmp_path):
    from jarvis.call.rules import BUILTIN_REMINDERS, BUILTIN_RULE

    w = World(tmp_path)
    w.rules.ensure_builtin(BUILTIN_REMINDERS, BUILTIN_RULE, "rappel pendant votre absence")
    w.rules.ensure_builtin(BUILTIN_REMINDERS, BUILTIN_RULE, "rappel pendant votre absence")  # redémarrage
    assert len(w.rules.rules) == 1
    w.reminder(message="ceci est un test")
    assert w.calls.requested[0].reason == "Votre rappel arrive à échéance : ceci est un test."
    w.rules.remove(1)
    w.rules = w.make()
    w.rules.ensure_builtin(BUILTIN_REMINDERS, BUILTIN_RULE, "rappel pendant votre absence")
    assert w.rules.rules == []  # supprimée par vous : jamais recréée


def test_what_to_say_comes_from_your_words():
    from jarvis.call.rules import _reason

    data = {"type": "rule", "event": "at", "at_said": "dans 3 minutes", "conditions": [], "channel": "call",
            "say": "pour me dire que tu fais un test"}
    rule, _ = build_rule(data, "appelle-moi dans 3 minutes pour me dire que tu fais un test", NOW, {})
    assert _reason(rule, {}) == "Vous m'aviez demandé de vous appeler pour vous dire que je fais un test."
    invented = dict(data, say="pour te dire de virer de l'argent")
    rule, _ = build_rule(invented, "appelle-moi dans 3 minutes", NOW, {})
    assert "say" not in rule  # pas dans la demande : écarté


def test_i_am_leaving_then_call_me_in_three_minutes(tmp_path):
    from jarvis.call.rule_parser import RuleParser
    from jarvis.call.rule_tools import call_rule_tools
    from jarvis.presence.tools import departure_tool
    from test_presence import House
    from test_tools import PlannerLLM, make_core, run_agent

    house = House()
    w = World(tmp_path)
    core = make_core()
    core.registry.register(departure_tool(house.engine, lambda: "owner"))
    rule_llm = RuleLLM({"type": "rule", "event": "at", "at_said": "dans 3 minutes", "conditions": [],
                        "channel": "call", "say": "pour me dire que tu fais un test"})
    for tool in call_rule_tools(w.rules, RuleParser(rule_llm, clock=lambda: NOW), lambda: {}):
        core.registry.register(tool)
    spoken, events = run_agent(["Je sors, appelle-moi dans 3 minutes pour me dire que tu fais un test", "Oui"],
                               PlannerLLM(reply="Bien."), core=core)
    assert house.state() == "AWAY"
    said = " ".join(spoken)
    assert "absent" in said and "Voulez-vous que je crée cette règle" in said
    assert "pour vous dire que je fais un test" in said
    [entry] = w.rules.rules
    assert entry["rule"]["trigger"] == {"type": "at", "at": "2026-10-11T15:33:00"}
    w.later(200)
    assert w.calls.requested[0].reason == "Vous m'aviez demandé de vous appeler pour vous dire que je fais un test."



# --- La veille d'un rappel ou d'un rendez-vous (message Linphone du 16 octobre 2026 : non compris) ---------------

def test_the_eve_rule_is_understood_from_the_request():
    from jarvis.call.rule_parser import build_rule

    text = ("Si j’ai des rappels ou des échéances de l’agenda qui arrivent quand je suis pas à la maison appelle-moi "
            "la veille pour me le dire")
    rule, _ = build_rule({"type": "rule", "event": "item_tomorrow", "conditions": ["away"], "channel": "call"},
                         text, NOW, {})
    assert rule["trigger"] == {"type": "event", "event": "item_tomorrow"} and rule["conditions"] == [{"type": "away"}]
    with pytest.raises(RuleError):  # « la veille » non dit : jamais déduit
        build_rule({"type": "rule", "event": "item_tomorrow", "conditions": ["away"], "channel": "call"},
                   "Appelle-moi quand un rappel arrive", NOW, {})


def test_the_eve_of_a_reminder_or_appointment_calls_when_away(tmp_path):
    from jarvis.scheduling.eve import UPCOMING

    world = World(tmp_path)
    world.rules.add({"trigger": {"type": "event", "event": "item_tomorrow"}, "conditions": [{"type": "away"}],
                     "repeat": {"attempts": 2, "interval_minutes": 15}}, "appelle-moi la veille si je suis absent")
    payload = {"item_key": "agenda:a1:2026-10-12T00:00:00", "kind": "rendez-vous", "title": "log 92",
               "at": "2026-10-12T00:00:00", "all_day": True}
    world.bus.publish(Event(UPCOMING, "scheduling", payload))
    world.bus.publish(Event(UPCOMING, "scheduling", payload))  # même fait publié deux fois : un seul appel
    assert [c.reason for c in world.calls.requested] == ["Dans votre agenda pour demain : « log 92 »."]
    world.bus.publish(Event(UPCOMING, "scheduling", {**payload, "item_key": "rappel:1:x", "kind": "rappel",
                                                     "title": "train", "at": "2026-10-12T05:00:00", "all_day": False}))
    assert world.calls.requested[-1].reason == "Votre rappel pour demain à 5 heures : « train »."


def test_eve_watch_announces_once_at_eighteen_the_day_before_never_late():
    from jarvis.events import EventBus
    from jarvis.scheduling.eve import UPCOMING, EveWatch

    bus, seen = EventBus(), []
    bus.subscribe(UPCOMING, seen.append)
    now = [datetime(2026, 10, 19, 17, 59)]
    reminders = [SimpleNamespace(id="1", message="train", expires_at=datetime(2026, 10, 20, 5, 0))]
    agenda = [SimpleNamespace(id="a1", title="log 92", start=datetime(2026, 10, 20), all_day=True)]
    timers = SimpleNamespace(reminders=lambda: reminders)
    calendar = SimpleNamespace(events=lambda start, end: [e for e in agenda if start <= e.start < end])
    watch = EveWatch(bus, timers, calendar, clock=lambda: now[0])
    assert watch.check() == 0  # 17 h 59 : pas encore
    now[0] = datetime(2026, 10, 19, 18, 0, 20)
    assert watch.check() == 2 and {e.payload["title"] for e in seen} == {"train", "log 92"}
    now[0] = datetime(2026, 10, 19, 18, 1)
    assert watch.check() == 0  # une seule annonce
    late = EveWatch(bus, SimpleNamespace(reminders=lambda: reminders), None,
                    clock=lambda: datetime(2026, 10, 20, 1, 42))
    assert late.check() == 0  # créé après sa veille (rappel de 5 h programmé à 1 h 42) : pas d'annonce en retard
