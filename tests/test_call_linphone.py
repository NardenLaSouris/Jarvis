"""Passerelle et appareil « linphone » (jarvis/call/linphone.py) : le Core appelle votre adresse ou décroche vos
appels, avec un faux compte SIP et un faux son (aucun réseau, aucun téléphone). Environnement de la passerelle :
    ~/orion-call-venv/bin/python -m pytest tests/test_call_linphone.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytest.importorskip("pylibsrtp")
pytest.importorskip("av")

import jarvis.call.linphone as linphone  # noqa: E402
from jarvis.call.devices import DeviceRegistry  # noqa: E402
from jarvis.call.gateway import CallGateway  # noqa: E402
from jarvis.call.sip import Dialog  # noqa: E402

IPHONE_SDP = ("v=0\r\nc=IN IP4 203.0.113.5\r\nt=0 0\r\nm=audio 18912 RTP/SAVP 0 101\r\n"
              "a=rtpmap:101 telephone-event/8000\r\n"
              "a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:kPhcb+mOGyLyd6DSSESoxhzsf66dJWNji7jof7I3\r\n")


class FakeMedia:
    def __init__(self, sock, key, remote, payload, codec, rate, **kw):
        self.codec_name, self.remote, self.closed = codec, remote, False
        sock.close()

    def close(self):
        self.closed = True


class FakeAccount:
    """Compte SIP factice : note ce que la passerelle lui demande ; ``result`` : réponse de votre téléphone."""

    def __init__(self, result=(None, 0)):
        self.registered, self.local_ip = True, "192.0.2.1"
        self.result, self.calls = result, []
        self.on_incoming = self.on_ended = None
        self.ringing = threading.Event()

    def invite(self, sdp, on_ringing=None, cancel=None):
        self.calls.append(("invite", sdp))
        if self.result == "wait":  # sonnerie jusqu'à l'annulation
            cancel.wait(5)
            return None, 487
        return self.result

    def answer(self, dialog, sdp):
        dialog.local_sdp = sdp
        self.calls.append(("answer", sdp))

    def reject(self, dialog, code=486, reason=""):
        dialog.ended = True
        self.calls.append(("reject", code))

    def hang_up(self, dialog):
        dialog.ended = True
        self.calls.append(("hang_up", dialog.call_id))


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(linphone, "SipMedia", FakeMedia)

    def make(result=(None, 0)):
        gateway = CallGateway(DeviceRegistry(tmp_path / "devices.json"), "jeton", ["127.0.0.0/8"], "127.0.0.1", 0)
        account = FakeAccount(result)
        gateway.linphone = linphone.LinphoneChannel(account, gateway)
        gateway._core_seen = gateway._clock()  # le Core est en vie
        return gateway, account
    return make


def events(gateway, wait=2.0):
    return [e["type"] for e in gateway.core_events.take(wait)]


def answered_dialog():
    return Dialog("c-1", "tag", "sip:jules@10.0.0.9", "<sip:jules@sip.linphone.org>;tag=t1", remote_sdp=IPHONE_SDP)


def test_core_call_to_linphone_is_answered_with_encrypted_audio(world):
    gateway, account = world((answered_dialog(), 200))
    assert gateway.ring("appel-1", linphone.DEVICE, "Fuite d'eau.")
    assert events(gateway) == ["accepted", "connected"]
    assert gateway.call.state == "connecté" and gateway.current_media().codec_name == "PCMU"
    offer = account.calls[0][1]
    assert "RTP/SAVP" in offer and "opus/48000/2" in offer and "a=crypto:" in offer  # Opus proposé d'abord
    gateway.end_from_core("appel-1", "raccroché par ORION")
    assert ("hang_up", "c-1") in account.calls and gateway.call.media.closed


@pytest.mark.parametrize("code, expected", [(603, "refused"), (486, "refused"), (0, "failed"), (408, "failed")])
def test_refused_or_unanswered_linphone_call(world, code, expected):
    gateway, _ = world((None, code))
    gateway.ring("appel-2", linphone.DEVICE, "Test")
    assert events(gateway) == [expected] and gateway.call.state == "terminé"


def test_core_cancels_while_linphone_rings(world):
    gateway, account = world("wait")
    gateway.ring("appel-3", linphone.DEVICE, "Test")
    time.sleep(0.2)
    gateway.end_from_core("appel-3", "annulé")  # sans réponse dans le délai du Core
    assert gateway.call.cancel.is_set()
    time.sleep(0.3)
    assert events(gateway, 0.5) == []  # aucun signal en retard après l'annulation


def test_unregistered_account_cannot_ring(world):
    gateway, account = world()
    account.registered = False
    assert gateway.ring("appel-4", linphone.DEVICE, "Test") is False


def test_your_linphone_call_is_offered_to_the_core_and_answered(world):
    gateway, account = world()
    dialog = Dialog("in-1", "loc", "sip:jules@10.0.0.9", "<sip:jules@sip.linphone.org>;tag=a1",
                    remote_sdp=IPHONE_SDP.replace("RTP/SAVP 0 101", "RTP/SAVP 0 101"), outgoing=False)
    account.on_incoming(dialog)
    [event] = gateway.core_events.take(1)
    assert event["type"] == "dialed" and event["device"] == linphone.DEVICE
    assert gateway.answer_from_core(event["call_id"])  # le Core décroche
    assert events(gateway) == ["connected"] and account.calls[0][0] == "answer"
    assert "RTP/SAVP 0 101" in account.calls[0][1] and "a=crypto:" in account.calls[0][1]
    account.on_ended(dialog, "raccroché")  # vous raccrochez
    assert events(gateway) == ["hung_up"] and gateway.call.state == "terminé" and gateway.call.media.closed


def test_your_call_is_refused_when_orion_is_unreachable_or_busy(world):
    gateway, account = world()
    gateway._core_seen = None  # Core arrêté
    account.on_incoming(Dialog("in-2", "loc", "x", "y", remote_sdp=IPHONE_SDP, outgoing=False))
    assert account.calls == [("reject", 486)] and gateway.core_events.take(0.2) == []
    gateway._core_seen = gateway._clock()
    gateway.ring("appel-5", linphone.DEVICE, "Test")  # ORION est déjà en train d'appeler
    account.on_incoming(Dialog("in-3", "loc", "x", "y", remote_sdp=IPHONE_SDP, outgoing=False))
    assert ("reject", 486) in account.calls[1:]


def test_unencrypted_offer_is_refused_by_orion(world):
    gateway, account = world()
    account.on_incoming(Dialog("in-4", "loc", "x", "y", remote_sdp=IPHONE_SDP.replace("RTP/SAVP", "RTP/AVP"),
                               outgoing=False))
    [event] = gateway.core_events.take(1)
    assert gateway.answer_from_core(event["call_id"]) is False
    assert ("reject", 488) in account.calls


class Sent(FakeAccount):
    def __init__(self):
        super().__init__()
        self.messages = []

    def send_message(self, text):
        self.messages.append(text)
        return True


def test_your_message_reaches_the_core_and_its_reply_goes_back(world, tmp_path, monkeypatch):
    gateway, _ = world()
    account = Sent()
    gateway.linphone = linphone.LinphoneChannel(account, gateway)
    account.on_message("Quelle heure est-il ?")
    assert gateway.core_events.take(1) == [{"type": "message", "text": "Quelle heure est-il ?"}]
    assert gateway.linphone.send("Il est midi.") and account.messages == ["Il est midi."]


def test_message_while_orion_is_down_gets_an_automatic_reply(world):
    gateway, _ = world()
    account = Sent()
    gateway.linphone = linphone.LinphoneChannel(account, gateway)
    gateway._core_seen = None  # Core arrêté
    account.on_message("Tu es là ?")
    time.sleep(0.3)
    assert account.messages == ["ORION est indisponible pour le moment."] and gateway.core_events.take(0.2) == []


def test_messages_are_limited_per_minute(world):
    from jarvis.call.gateway import MESSAGES_PER_MINUTE

    gateway, _ = world()
    account = Sent()
    gateway.linphone = linphone.LinphoneChannel(account, gateway)
    for i in range(MESSAGES_PER_MINUTE + 5):
        account.on_message(f"message {i}")
    assert len(gateway.core_events.take(1)) == MESSAGES_PER_MINUTE


def test_online_message_when_orion_becomes_available_without_bursts(world):
    from jarvis.call.gateway import NOTICE_GAP, ONLINE_NOTICE

    gateway, _ = world()
    account = Sent()
    gateway.linphone = linphone.LinphoneChannel(account, gateway)
    account.registered = False
    gateway.watch(0.0)  # compte Linphone pas encore enregistré : rien
    account.registered = True
    gateway.watch(1.0)
    gateway.watch(2.0)  # toujours disponible : pas de second message
    time.sleep(0.3)
    assert account.messages == [ONLINE_NOTICE]
    gateway._core_seen = None  # ORION s'arrête puis revient vite : pas de rafale
    gateway.watch(3.0)
    gateway._core_seen = gateway._clock()
    gateway.watch(4.0)
    time.sleep(0.2)
    assert account.messages == [ONLINE_NOTICE]
    gateway._core_seen = None  # nouvel arrêt, retour après le délai : nouveau message
    gateway.watch(NOTICE_GAP)
    gateway._core_seen = gateway._clock()
    gateway.watch(NOTICE_GAP + 2.0)
    time.sleep(0.3)
    assert account.messages == [ONLINE_NOTICE, ONLINE_NOTICE]


def test_no_online_message_while_the_core_is_down(world):
    gateway, _ = world()
    account = Sent()
    gateway.linphone = linphone.LinphoneChannel(account, gateway)
    gateway._core_seen = None
    gateway.watch(0.0)
    time.sleep(0.2)
    assert account.messages == []
