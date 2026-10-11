"""Appels vers le téléphone jumelé (jarvis/call) : états, limites, bascule du micro et de la sortie, conversation.

Horloge simulée, faux téléphone (sources et sorties en mémoire) : aucun réseau, aucun son.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.audio.switch import CALL, HOLD, HOME, SwitchableSink, SwitchableSource  # noqa: E402
from jarvis.call import CallError, CallLimitSettings, CallManager, CallRefused, CallSession, CallSettings  # noqa: E402
from jarvis.call.session import (  # noqa: E402
    ACCEPTED, CONNECTED, ENDED, EXPIRED, FAILED, REFUSED, REQUESTED, RINGING,
)
from jarvis.config import load_config  # noqa: E402
from call_fakes import Mic, Phone, Speaker  # noqa: E402
from test_tools import PERSONALITY, PlannerLLM, run_agent  # noqa: E402


class World:
    def __init__(self, device="pixel", notifier=None, terminal_error=False, **limits):
        self.t = 1_000_000.0
        self.home_mic, self.home_speaker = Mic(1), Speaker()
        self.phone_mic, self.phone_speaker = Mic(7), Speaker()
        self.source = SwitchableSource(self.home_mic, sleep=lambda s: None)
        self.sink = SwitchableSink(self.home_speaker)
        self.phone = notifier or Phone()
        self.events = []

        def open_terminal(call):
            if terminal_error:
                raise OSError("passerelle muette")
            return self.phone_mic, self.phone_speaker

        settings = CallSettings(device=device, limits=CallLimitSettings(**limits) if limits else CallLimitSettings())
        self.calls = CallManager(settings, self.source, self.sink, open_terminal, self.phone, clock=lambda: self.t,
                                 on_event=lambda kind, data: self.events.append((kind, data)))

    def wait(self, seconds):
        self.t += seconds
        self.calls.tick()

    def connect(self, reason="Votre colis est arrivé."):
        call = self.calls.request(reason, "routine")
        self.calls.accepted(call.id)
        self.calls.connected(call.id)
        return call


# --- Session --------------------------------------------------------------------------------------------

def test_session_allows_only_planned_transitions():
    call = CallSession("pixel", "test", "routine", 0.0)
    assert call.state == REQUESTED and call.id
    with pytest.raises(CallError):
        call.move(CONNECTED, 1.0)
    for state in (RINGING, ACCEPTED, CONNECTED, ENDED):
        call.move(state, 1.0)
    assert call.final and call.end_reason == ENDED
    with pytest.raises(CallError):
        call.move(ACCEPTED, 2.0)  # « accepté » rejoué après la fin : sans effet
    assert set(call.describe()) == {"call", "device", "origin", "state", "end_reason", "states"}


# --- Demande et limites ---------------------------------------------------------------------------------

@pytest.mark.parametrize("origin", ["content", "llm", "", "mail"])
def test_only_the_core_can_request_a_call(origin):
    world = World()
    with pytest.raises(CallRefused):
        world.calls.request("test", origin)
    assert world.phone.calls == []


def test_no_paired_device_no_call():
    world = World(device="")
    with pytest.raises(CallRefused, match="aucun appareil"):
        world.calls.request("test", "routine")


def test_one_call_at_a_time_and_minimum_interval():
    world = World()
    call = world.calls.request("test", "routine")
    assert call.state == RINGING and world.phone.calls == [("ring", call.id)]
    with pytest.raises(CallRefused, match="déjà en cours"):
        world.calls.request("encore", "routine")
    world.calls.accepted(call.id)
    world.calls.connected(call.id)
    world.calls.hung_up(call.id)
    world.calls.finish()
    with pytest.raises(CallRefused, match="trop récent"):
        world.calls.request("encore", "routine")
    world.wait(601)
    assert world.calls.request("encore", "routine").state == RINGING


def test_at_most_n_calls_per_hour():
    world = World(max_per_hour=2, min_interval=0, refusal_pause=0)
    for _ in range(2):
        call = world.calls.request("test", "routine")
        world.calls.accepted(call.id)
        world.calls.connected(call.id)
        world.calls.hung_up(call.id)
        world.calls.finish()
        world.wait(60)
    with pytest.raises(CallRefused, match="dernière heure"):
        world.calls.request("test", "routine")
    world.wait(3600)
    world.calls.request("test", "routine")


def test_unanswered_call_expires_stops_ringing_and_pauses_calls():
    world = World()
    call = world.calls.request("test", "routine")
    world.wait(39)
    assert call.state == RINGING
    world.wait(2)
    assert call.state == EXPIRED and ("cancel", call.id) in world.phone.calls
    world.wait(700)
    with pytest.raises(CallRefused, match="sans réponse"):
        world.calls.request("test", "routine")
    world.wait(1200)
    world.calls.request("test", "routine")


def test_refusal_on_the_phone_pauses_calls():
    world = World()
    call = world.calls.request("test", "routine")
    world.calls.refused(call.id)
    assert call.state == REFUSED and ("cancel", call.id) not in world.phone.calls
    world.wait(700)
    with pytest.raises(CallRefused):
        world.calls.request("test", "routine")


def test_gateway_down_fails_cleanly():
    world = World(notifier=Phone(fail=True))
    call = world.calls.request("test", "routine")
    assert call.state == FAILED and world.source.mode == HOME


def test_audio_never_established_fails_after_the_timeout():
    world = World()
    call = world.calls.request("test", "routine")
    world.calls.accepted(call.id)
    world.wait(16)
    assert call.state == FAILED and ("hang_up", call.id) in world.phone.calls


def test_terminal_error_fails_without_switching():
    world = World(terminal_error=True)
    call = world.calls.request("test", "routine")
    world.calls.accepted(call.id)
    world.calls.connected(call.id)
    assert call.state == FAILED and world.source.mode == HOME and world.sink.active is world.home_speaker


def test_unknown_or_replayed_signals_are_refused():
    world = World()
    call = world.calls.request("test", "routine")
    with pytest.raises(CallError):
        world.calls.accepted("autre-appel")
    with pytest.raises(CallError):
        world.calls.connected(call.id)  # pas encore accepté
    world.calls.refused(call.id)
    with pytest.raises(CallError):
        world.calls.accepted(call.id)


# --- Bascule du micro et de la sortie ----------------------------------------------------------------------

def test_connected_call_switches_mic_and_output_then_back_home():
    world = World()
    call = world.connect()
    assert world.source.mode == CALL and world.sink.active is world.phone_speaker
    frame = world.source.read()
    assert frame is not None and int(frame[0]) == 7  # le micro du téléphone, pas celui de la maison
    world.sink.play(np.zeros(100, np.int16), 22050)
    assert world.phone_speaker.played == [100] and world.home_speaker.played == []
    assert world.calls.take_connected() is call and world.calls.take_connected() is None
    world.calls.hung_up(call.id)
    assert world.source.mode == HOLD and not world.calls.active()
    assert not world.source.read().any()  # silence, jamais l'audio de la maison pris pour la suite de l'appel
    world.calls.finish()
    assert world.source.mode == HOME and int(world.source.read()[0]) == 1
    assert world.phone_mic.closed and world.phone_speaker.closed and world.home_mic.flushed > 0


def test_orion_hangs_up_when_the_conversation_ends():
    world = World()
    call = world.connect()
    world.calls.finish()
    assert call.state == ENDED and ("hang_up", call.id) in world.phone.calls and world.source.mode == HOME


def test_call_is_ended_after_the_maximum_duration():
    world = World()
    call = world.connect()
    world.wait(901)
    assert call.state == ENDED and call.end_reason == "durée maximale atteinte"


def test_silent_phone_gives_silence_instead_of_blocking():
    class Stuck(Mic):
        def __init__(self):
            super().__init__(9)
            self.release = threading.Event()

        def read(self):
            self.release.wait(5)
            return None

    source = SwitchableSource(Mic(1), sleep=lambda s: None)
    stuck = Stuck()
    source.use_call(stuck)
    frame = source.read()  # rend la main en ~20 ms (deux blocs), jamais bloqué par un réseau coupé
    assert frame is not None and not frame.any() and len(frame) == 160
    stuck.release.set()
    source.use_home()


# --- Conversation ---------------------------------------------------------------------------------------

class FakeCalls:
    """Un appel décroché dès la veille ; le téléphone raccroche après ``answers`` réponses d'ORION."""

    def __init__(self, reason="Votre colis est arrivé.", answers=1):
        self.call = CallSession("pixel", reason, "routine", 0.0)
        self.given, self.finished, self.checks, self.answers = False, 0, 0, answers

    def take_connected(self):
        if self.given:
            return None
        self.given = True
        return self.call

    def active(self):
        self.checks += 1
        return self.checks <= self.answers

    def finish(self):
        self.finished += 1


def test_answered_call_starts_a_conversation_without_wake_word_and_hangs_up_at_the_end():
    calls = FakeCalls()
    llm = PlannerLLM(reply="Il fait beau.")
    spoken, events = run_agent(["Quel temps fait-il ?"], llm, calls=calls)
    greetings = [PERSONALITY.render(t) for t in PERSONALITY.phrases["call_greeting"]]
    assert ("wake", "Appel décroché") in events
    assert any(spoken[0] == f"{g} Votre colis est arrivé." for g in greetings)
    assert calls.finished == 1  # ORION raccroche et revient à la maison


def test_phone_hang_up_ends_the_conversation():
    calls = FakeCalls(answers=0)
    spoken, events = run_agent(["Quel temps fait-il ?"], PlannerLLM(reply="Il fait beau."), calls=calls)
    assert ("call", "Appel terminé sur le téléphone") in events and calls.finished == 1
    before_end = events[:events.index(("call", "Appel terminé sur le téléphone"))]
    assert not any(kind == "user" for kind, _ in before_end)  # rien n'est écouté après le raccrochage


# --- Configuration ----------------------------------------------------------------------------------------

def test_calls_are_off_by_default_and_without_device():
    cfg = load_config(ROOT / "config.toml", local=False)
    assert cfg.call.enabled is False and cfg.call.device == ""
    assert cfg.call.gateway.startswith("http://127.0.0.1:")


def test_flush_during_a_call_also_drops_what_the_phone_heard_while_orion_spoke():
    world = World()
    world.connect()
    before = world.phone_mic.flushed
    world.source.flush()  # ORION vient de parler : ce que le téléphone a capté pendant ce temps est oublié
    assert world.phone_mic.flushed == before + 1
    world.calls.finish()


# --- Le téléphone appelle ORION ----------------------------------------------------------------------------

def test_paired_phone_can_call_orion_and_is_answered_at_once():
    world = World()
    call = world.calls.dialed("tel-1", "pixel")
    assert call.state == ACCEPTED and call.origin == "user" and call.reason == ""
    world.calls.connected(call.id)
    assert world.source.mode == CALL and world.calls.take_connected() is call
    world.calls.finish()
    world.wait(5)
    assert world.calls.dialed("tel-2", "pixel").state == ACCEPTED  # pas de délai entre deux appels du téléphone


def test_phone_call_refused_when_busy_or_from_another_device():
    world = World()
    with pytest.raises(CallRefused, match="non jumelé"):
        world.calls.dialed("x", "autre-appareil")
    world.calls.request("test", "routine")
    with pytest.raises(CallRefused, match="occupé"):
        world.calls.dialed("y", "pixel")


def test_phone_calls_are_limited_against_loops():
    from jarvis.call.manager import MAX_DIALS_PER_HOUR

    world = World()
    for i in range(MAX_DIALS_PER_HOUR):
        call = world.calls.dialed(f"t{i}", "pixel")
        world.calls.hung_up(call.id)
        world.calls.finish()
    with pytest.raises(CallRefused, match="dernière heure"):
        world.calls.dialed("encore", "pixel")


def test_phone_call_is_greeted_like_a_wake_word():
    calls = FakeCalls(reason="")
    calls.call = CallSession("pixel", "", "user", 0.0)
    spoken, events = run_agent(["Quelle heure est-il ?"], PlannerLLM(reply="Il est midi."), calls=calls)
    wake = [PERSONALITY.render(t) for t in PERSONALITY.phrases["wake"]]
    assert spoken[0] in wake


def test_admin_test_call_goes_through_the_core_and_its_limits():
    from jarvis.api.server import CoreApi

    api = CoreApi.__new__(CoreApi)
    api.calls = None
    assert api.call_action({"reason": "Essai"})[0] == 404  # appels désactivés
    world = World()
    api.calls = world.calls
    status, body = api.call_action({"reason": "Essai du vrai ORION."})
    assert status == 202 and world.phone.calls[0][0] == "ring" and world.calls.current.origin == "admin"
    assert world.calls.current.reason == "Essai du vrai ORION."
    assert api.call_action({"reason": "Encore"})[0] == 409  # un appel à la fois, limites respectées


# --- Messages Linphone -------------------------------------------------------------------------------------

class FakeMessages:
    """Gestionnaire d'appels factice côté messages : vos messages arrivent un par un, les réponses sont notées."""

    def __init__(self, messages):
        self.inbox, self.replies = list(messages), []

    def take_connected(self):
        return None

    def active(self):
        return False

    def finish(self):
        pass

    def take_message(self):
        return self.inbox.pop(0) if self.inbox else None

    def reply(self, text):
        self.replies.append(text)
        return True


def test_message_is_answered_in_writing_without_speaking_at_home():
    messages = FakeMessages(["Que penses-tu de la musique classique ?"])
    spoken, events = run_agent([], PlannerLLM(reply="Elle me plaît beaucoup."), calls=messages)
    assert messages.replies == ["Elle me plaît beaucoup."]
    assert spoken == []  # rien n'est prononcé à la maison
    assert ("message", "Que penses-tu de la musique classique ?") in events
    assert ("message_reply", "Elle me plaît beaucoup.") in events


def test_sensitive_action_by_message_needs_a_written_yes():
    from test_tools import FakeVolume, Locker, make_core

    locker = Locker()
    messages = FakeMessages(["Verrouille le PC", "Oui."])
    run_agent([], PlannerLLM(), make_core(volume=FakeVolume(), locker=locker), fast_path=True, calls=messages)
    assert messages.replies[0].startswith("Voulez-vous") and locker.calls == 1
    locker = Locker()
    messages = FakeMessages(["Verrouille le PC", "Non."])
    run_agent([], PlannerLLM(), make_core(volume=FakeVolume(), locker=locker), fast_path=True, calls=messages)
    assert locker.calls == 0  # refusé par message : rien n'est fait


def test_manager_keeps_pending_messages_and_sends_replies():
    world = World()
    world.calls.message_received("Bonjour ORION")
    world.calls.message_received("Quelle heure est-il ?")
    assert world.calls.take_message() == "Bonjour ORION" and world.calls.take_message() == "Quelle heure est-il ?"
    assert world.calls.take_message() is None
    sent = []
    world.calls._notifier.send_message = sent.append
    assert world.calls.reply("Il est midi.") and sent == ["Il est midi."]
