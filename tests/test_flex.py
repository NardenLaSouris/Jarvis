"""Mode Flex (jarvis/flex.py, règle « flex » de jarvis/call/rules.py) : présentation fidèle à ce qui est branché,
enceintes si vous êtes là, appel après une minute sinon (annulé si vous rentrez), jamais deux présentations à la fois,
reprise après redémarrage. Aucun appel réel, aucun son."""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis.call.rules import FLEX_MAX_AGE, GAVE_UP, PLANNED, PRESENTED, TRYING  # noqa: E402
from jarvis.events import Event  # noqa: E402
from jarvis.flex import Facts, inventory, is_flex, presentation  # noqa: E402
from test_call_rules import World  # noqa: E402

FULL = Facts(tools=frozenset({"create_reminder", "create_timer", "create_alarm", "read_mail", "light_on", "spotify_play",
                              "open_application", "find_files", "get_weather", "wake_pc", "shutdown_pc",
                              "create_call_rule"}),
             llm_host="le Katana", computers=("votre PC", "votre PC portable"), lights=2, presence_sensors=5,
             calls=True, mail_watch=True, mail_follow_up=True, wake_mac=True, learning=True, extra={"web": True})
BARE = Facts(tools=frozenset({"create_reminder"}))
FLEX = {"trigger": {"type": "manual"}, "conditions": [], "channel": "auto", "action": "flex", "delay_seconds": 60,
        "repeat": {"attempts": 1, "interval_minutes": 5}}


# --- Présentation fidèle -----------------------------------------------------------------------------------

def test_presentation_only_claims_what_is_really_there():
    rich = " ".join(presentation(FULL, rng=random.Random(3)))
    bare = " ".join(presentation(BARE, rng=random.Random(3)))
    for claim in ("le Katana", "éclairage", "musique", "vous appeler", "échéances", "applications"):
        assert claim in rich or claim.replace("vous appeler", "joins par téléphone") in rich
        assert claim not in bare
    assert "rappels" in bare and "minuteurs" not in bare
    assert "plusieurs enceintes" not in rich  # indisponible : jamais présenté
    assert "il ne manque qu'un réglage" in rich  # sous condition : dit comme tel


def test_inventory_separates_operational_conditional_and_unavailable():
    states = {name: state for name, state, _ in inventory(FULL)}
    assert states["lumières"] == "opérationnel" and states["allumer un PC"] == "sous conditions"
    assert states["appels et messages"] == "sous conditions" and states["plusieurs enceintes"] == "indisponible"
    assert {name: state for name, state, _ in inventory(BARE)}["lumières"] == "indisponible"


def test_voice_is_a_woman_addressing_monsieur_and_the_phone_version_is_shorter():
    for seed in range(6):
        home = presentation(FULL, rng=random.Random(seed))
        phone = presentation(FULL, phone=True, rng=random.Random(seed))
        text = " ".join(home)
        assert "monsieur" in text and "Jules" not in text and "assistante" in text and "muet " not in text
        assert len(" ".join(phone)) < len(text) and len(home) >= 6
        assert all(len(sequence) < 330 for sequence in home)  # séquences courtes, pas un monologue


def test_presentations_vary():
    assert len({tuple(presentation(FULL, rng=random.Random(seed))) for seed in range(8)}) > 1


@pytest.mark.parametrize("text, expected", [("Flex", True), ("Lance le mode flex", True), ("Fais ta présentation", True),
                                            ("Montre-leur ce que tu sais faire", True), ("Présente-toi", False),
                                            ("Désactive le mode flex", False), ("Quelle heure est-il ?", False)])
def test_flex_phrases(text, expected):
    assert is_flex(text) is expected


# --- Canal selon la présence --------------------------------------------------------------------------------

def world(tmp_path, state="HOME", speakers=True):
    w = World(tmp_path, state=state)
    w.rules.ensure_builtin("flex", FLEX, "mode Flex")
    w.home_said = []
    w.rules.flex_text = lambda phone: presentation(FULL, phone, rng=random.Random(1))
    w.rules.flex_present_home = w.home_said.append
    w.rules.speaker_ready = lambda: speakers
    return w


def flex_job(w):
    return next(j for j in w.rules.jobs if j.get("action") == "flex")


def test_at_home_the_speakers_present_and_nobody_is_called(tmp_path):
    w = world(tmp_path)
    assert w.rules.trigger_flex() == "home"
    assert len(w.home_said) == 1 and w.home_said[0][0].startswith(("Bonjour", "Je m'appelle"))
    assert w.calls.requested == [] and flex_job(w)["state"] == PRESENTED


def test_away_waits_one_minute_then_calls_with_the_phone_version(tmp_path):
    w = world(tmp_path, state="AWAY")
    assert w.rules.trigger_flex() == "waiting"
    w.later(30)
    assert w.calls.requested == []
    w.later(31)
    [call] = w.calls.requested
    assert call.reason.startswith("Permettez-moi de me présenter") and call.origin == "routine"
    w.call_event("call.connected")
    assert w.rules.awaiting_ack(call.id) is None  # pas de « dites c'est noté » pendant une présentation
    w.call_event("call.ended")
    assert flex_job(w)["state"] == PRESENTED and w.home_said == [] and w.calls.messages == []


def test_coming_home_during_the_minute_cancels_the_call_for_the_speakers(tmp_path):
    w = world(tmp_path, state="AWAY")
    w.rules.trigger_flex()
    w.later(20)
    w.presence.users["owner"].state = "HOME"
    w.bus.publish(Event("presence.arrival_confirmed", "presence", {"user": "owner"}))
    w.later(120)
    assert w.calls.requested == [] and len(w.home_said) == 1 and flex_job(w)["state"] == PRESENTED


def test_never_two_presentations_at_once(tmp_path):
    w = world(tmp_path, state="AWAY")
    assert w.rules.trigger_flex() == "waiting"
    assert w.rules.trigger_flex() == "already"
    w2 = world(tmp_path / "b")
    w2.rules.trigger_flex()
    assert w2.rules.trigger_flex() == "already"  # tout juste présentée aux enceintes
    w2.later(200)
    assert w2.rules.trigger_flex() == "home" and len(w2.home_said) == 2


def test_restart_resumes_the_waiting_presentation_or_drops_a_stale_one(tmp_path):
    w = world(tmp_path, state="AWAY")
    w.rules.trigger_flex()
    w.rules = w.make()  # redémarrage pendant la minute d'attente
    w.rules.flex_text = lambda phone: presentation(FULL, phone, rng=random.Random(1))
    w.later(61)
    assert len(w.calls.requested) == 1
    stale = world(tmp_path / "c", state="AWAY")
    stale.rules.trigger_flex()
    stale.rules = stale.make()
    stale.rules.flex_text = lambda phone: presentation(FULL, phone)
    stale.later(FLEX_MAX_AGE + 5)
    assert stale.calls.requested == [] and flex_job(stale)["state"] == GAVE_UP


def test_no_answer_is_not_repeated_and_disabled_flex_does_nothing(tmp_path):
    w = world(tmp_path, state="AWAY")
    w.rules.trigger_flex()
    w.later(61)
    w.call_event("call.expired")
    w.later(3600)
    assert len(w.calls.requested) == 1 and flex_job(w)["state"] == GAVE_UP and len(w.calls.messages) == 1
    w.rules.set_flex_enabled(False)
    assert w.rules.trigger_flex() == "disabled"


def test_home_without_speakers_calls_instead(tmp_path):
    w = world(tmp_path, speakers=False)
    assert w.rules.trigger_flex() == "call" and flex_job(w)["state"] == TRYING and w.home_said == []


def test_delay_is_configurable(tmp_path):
    w = world(tmp_path, state="AWAY")
    w.rules.set_builtin_delay("flex", 120)
    w.rules.trigger_flex()
    w.later(90)
    assert w.calls.requested == [] and flex_job(w)["state"] == PLANNED
    w.later(31)
    assert len(w.calls.requested) == 1


def test_agent_routes_flex_without_the_llm(tmp_path):
    from jarvis.flex import flex_tools
    from test_tools import PlannerLLM, make_core, run_agent

    w = world(tmp_path)
    core = make_core()
    for tool in flex_tools(w.rules):
        core.registry.register(tool)
    llm = PlannerLLM(reply="Bien.")
    spoken, events = run_agent(["Flex"], llm, core=core)
    assert spoken[0] == "Avec plaisir, monsieur." and llm.calls == [] and len(w.home_said) == 1


def test_the_background_thread_really_calls_after_the_delay(tmp_path):
    import time as real_time

    w = world(tmp_path, state="AWAY")
    w.rules.tick_seconds = 0.02
    w.rules.connect(w.calls, w.bus)  # comme en service : le fil des échéances démarre
    try:
        assert w.rules.trigger_flex() == "waiting"
        w.t += 61  # l'horloge avance ; c'est le fil, pas le test, qui doit appeler
        deadline = real_time.monotonic() + 3
        while not w.calls.requested and real_time.monotonic() < deadline:
            real_time.sleep(0.02)
        assert len(w.calls.requested) == 1
    finally:
        w.rules.stop()


def test_flex_sent_after_leaving_is_not_blocked_by_the_home_presentation(tmp_path):
    w = world(tmp_path)
    assert w.rules.trigger_flex() == "home"
    w.presence.users["owner"].state = "AWAY"  # « je sors » juste après
    assert w.rules.trigger_flex() == "waiting"
