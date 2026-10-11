"""Compte SIP d'ORION (jarvis/call/sip.py) face à un faux serveur SIP local (TCP) : authentification, appel sortant
(sonnerie, décroché, raccrochage), refus, annulation, appel entrant, appelant inconnu, raccrochage par l'iPhone.
Aucun réseau extérieur, aucun téléphone."""

from __future__ import annotations

import hashlib
import queue
import re
import socket
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.call.sip import Message, SipAccount, digest, parse, uri_of  # noqa: E402

PASSWORD = "mot-de-passe-de-test"
PEER = "sip:jules@sip.test"
REALM = "sip.test"
NONCE = "abc123"


def expected_response(header: str, method: str) -> bool:
    """L'empreinte reçue correspond-elle au mot de passe ? (recalcul côté serveur, MD5, qop=auth)"""
    f = dict(re.findall(r'(\w+)="?([^",]*)"?', header))
    h = lambda t: hashlib.md5(t.encode()).hexdigest()  # noqa: E731
    ha1, ha2 = h(f"{f['username']}:{REALM}:{PASSWORD}"), h(f"{method}:{f['uri']}")
    return f["response"] == h(f"{ha1}:{f['nonce']}:{f['nc']}:{f['cnonce']}:auth:{ha2}")


class FakeProxy:
    """Faux sip.linphone.org : une connexion, messages lus un par un, réponses écrites par le test."""

    def __init__(self):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.conn = None
        self.incoming: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        self.conn, _ = self.server.accept()
        self._ready.set()
        buffer = b""
        while True:
            try:
                chunk = self.conn.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while True:
                buffer = buffer.lstrip(b"\r\n")
                end = buffer.find(b"\r\n\r\n")
                if end < 0:
                    break
                length = re.search(rb"(?im)^content-length\s*:\s*(\d+)", buffer[:end])
                total = end + 4 + (int(length.group(1)) if length else 0)
                if len(buffer) < total:
                    break
                self.incoming.put(parse(buffer[:total]))
                buffer = buffer[total:]

    def connect(self, host, port):
        return socket.create_connection(("127.0.0.1", self.port))

    def next(self, timeout=5) -> Message:
        return self.incoming.get(timeout=timeout)

    def send(self, text: str, body: str = ""):
        self._ready.wait(5)
        # Content-Length en octets (et non en caractères : « É », « è » prennent deux octets en UTF-8).
        self.conn.sendall((text.replace("\n", "\r\n") + f"Content-Length: {len(body.encode())}\r\n\r\n").encode()
                          + body.encode())

    def reply(self, request: Message, code: int, reason: str, extra: str = "", body: str = "", to_tag: str = "t1"):
        to = request.header("to") + ("" if "tag=" in request.header("to") or code < 101 else f";tag={to_tag}")
        self.send(f"SIP/2.0 {code} {reason}\nVia: {request.header('via')}\nFrom: {request.header('from')}\n"
                  f"To: {to}\nCall-ID: {request.header('call-id')}\nCSeq: {request.header('cseq')}\n{extra}", body)

    def challenge(self, request: Message, proxy: bool):
        name = "Proxy-Authenticate" if proxy else "WWW-Authenticate"
        self.reply(request, 407 if proxy else 401, "Auth",
                   f'{name}: Digest realm="{REALM}", nonce="{NONCE}", algorithm=MD5, qop="auth"\n')


@pytest.fixture
def setup():
    proxy = FakeProxy()
    events = {"incoming": queue.Queue(), "ended": queue.Queue()}
    account = SipAccount("orion", PASSWORD, PEER, domain=REALM, connect=proxy.connect, transport="TCP",
                         on_incoming=events["incoming"].put, on_ended=lambda d, why: events["ended"].put(why))
    account.start()
    first = proxy.next()
    assert first.method == "REGISTER" and "authorization" not in first.headers
    proxy.challenge(first, proxy=False)
    second = proxy.next()
    assert second.method == "REGISTER" and expected_response(second.header("authorization"), "REGISTER")
    proxy.reply(second, 200, "OK", "Expires: 600\n")
    assert account.wait_registered(5)
    yield proxy, account, events
    account._stopping.set()
    account._close()


def test_digest_follows_rfc_2617():
    # Vecteur de la RFC 2617 (section 3.5) : même réponse avec le même cnonce.
    header = digest('Digest realm="testrealm@host.com", qop="auth", nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", '
                    'opaque="5ccc069c403ebaf9f0171e9517f40e41"', "GET", "/dir/index.html", "Mufasa", "Circle Of Life")
    f = dict(re.findall(r'(\w+)="?([^",]*)"?', header))
    h = lambda t: hashlib.md5(t.encode()).hexdigest()  # noqa: E731
    ha1 = h("Mufasa:testrealm@host.com:Circle Of Life")
    assert f["response"] == h(f"{ha1}:dcd98b7102dd2f0e8b11d0f600bfb0c093:00000001:{f['cnonce']}:auth:{h('GET:/dir/index.html')}")
    assert f["opaque"] == "5ccc069c403ebaf9f0171e9517f40e41" and "Circle" not in header


def test_password_never_sent(setup):
    proxy, account, _ = setup
    assert PASSWORD not in repr(account.__dict__.get("_contact")) and account.registered


def test_outgoing_call_rings_is_answered_then_hung_up_by_the_route(setup):
    proxy, account, _ = setup
    ringing, result = threading.Event(), {}
    thread = threading.Thread(target=lambda: result.update(r=account.invite("v=0\r\n", on_ringing=ringing.set)))
    thread.start()
    invite = proxy.next()
    assert invite.method == "INVITE" and uri_of(invite.first.split()[1]) == PEER
    proxy.challenge(invite, proxy=True)
    assert proxy.next().method == "ACK"  # le 407 est acquitté
    invite = proxy.next()
    assert expected_response(invite.header("proxy-authorization"), "INVITE")
    proxy.reply(invite, 180, "Ringing")
    assert ringing.wait(5)
    proxy.reply(invite, 200, "OK", "Contact: <sip:jules@10.0.0.9:5060>\nRecord-Route: <sip:proxy.test;lr>\n",
                body="v=0\r\nm=audio 4000 RTP/SAVP 0\r\n")
    thread.join(5)
    dialog, code = result["r"]
    assert code == 200 and "m=audio 4000" in dialog.remote_sdp
    ack = proxy.next()
    assert ack.method == "ACK" and ack.header("route") == "<sip:proxy.test;lr>" and "tag=t1" in ack.header("to")
    assert ack.header("cseq").split()[0] == invite.header("cseq").split()[0]
    threading.Thread(target=account.hang_up, args=(dialog,)).start()
    bye = proxy.next()
    assert bye.method == "BYE" and bye.header("route") == "<sip:proxy.test;lr>"
    assert int(bye.header("cseq").split()[0]) > int(invite.header("cseq").split()[0])
    proxy.reply(bye, 200, "OK")


def test_refused_call_returns_the_code(setup):
    proxy, account, _ = setup
    result = {}
    thread = threading.Thread(target=lambda: result.update(r=account.invite("v=0\r\n")))
    thread.start()
    invite = proxy.next()
    proxy.reply(invite, 603, "Decline")
    thread.join(5)
    assert result["r"] == (None, 603) and proxy.next().method == "ACK"


def test_ringing_call_can_be_cancelled(setup):
    proxy, account, _ = setup
    cancel, result = threading.Event(), {}
    thread = threading.Thread(target=lambda: result.update(r=account.invite("v=0\r\n", cancel=cancel)))
    thread.start()
    invite = proxy.next()
    proxy.reply(invite, 180, "Ringing")
    cancel.set()  # le Core annule (sans réponse, Core qui raccroche)
    request = proxy.next()
    assert request.method == "CANCEL" and request.header("call-id") == invite.header("call-id")
    assert request.header("cseq").split()[0] == invite.header("cseq").split()[0]
    proxy.reply(request, 200, "OK")
    proxy.reply(invite, 487, "Request Terminated")
    thread.join(5)
    assert result["r"] == (None, 487)


def incoming(proxy, caller=PEER, call_id="in-1"):
    proxy.send(f"INVITE sip:orion@127.0.0.1 SIP/2.0\nVia: SIP/2.0/TCP proxy.test;branch=z9hG4bKx\n"
               f"Record-Route: <sip:proxy.test;lr>\nFrom: <{caller}>;tag=a1\nTo: <sip:orion@{REALM}>\n"
               f"Call-ID: {call_id}\nCSeq: 1 INVITE\nContact: <sip:jules@10.0.0.9:5060>\n"
               "Content-Type: application/sdp\n", "v=0\r\nm=audio 5000 RTP/SAVP 96\r\n")


def test_unknown_caller_is_refused_before_the_core(setup):
    proxy, account, events = setup
    incoming(proxy, caller="sip:inconnu@ailleurs.test")
    reply = proxy.next()
    assert reply.status == 403 and events["incoming"].empty()


def test_peer_call_is_offered_answered_and_ended_by_the_phone(setup):
    proxy, account, events = setup
    incoming(proxy)
    assert proxy.next().status == 180
    dialog = events["incoming"].get(timeout=5)
    assert not dialog.outgoing and "m=audio 5000" in dialog.remote_sdp
    account.answer(dialog, "v=0\r\nm=audio 6000 RTP/SAVP 96\r\n")
    ok = proxy.next()
    assert ok.status == 200 and "m=audio 6000" in ok.body and ok.header("record-route") == "<sip:proxy.test;lr>"
    proxy.send("BYE sip:orion@127.0.0.1 SIP/2.0\nVia: SIP/2.0/TCP proxy.test;branch=z9hG4bKy\n"
               f"From: <{PEER}>;tag=a1\nTo: <sip:orion@{REALM}>;tag={dialog.local_tag}\nCall-ID: in-1\nCSeq: 2 BYE\n")
    assert proxy.next().status == 200 and events["ended"].get(timeout=5) == "raccroché"


def test_orion_hangs_up_an_incoming_call_with_swapped_identities(setup):
    proxy, account, events = setup
    incoming(proxy)
    proxy.next()
    dialog = events["incoming"].get(timeout=5)
    account.answer(dialog, "v=0\r\n")
    proxy.next()
    account.hang_up(dialog)
    bye = proxy.next()
    assert bye.method == "BYE" and uri_of(bye.header("to")) == PEER and f"tag={dialog.local_tag}" in bye.header("from")
    assert bye.header("route") == "<sip:proxy.test;lr>"


def message(proxy, caller=PEER, content_type="text/plain;charset=UTF-8", body="Bonjour ORION"):
    proxy.send(f"MESSAGE sip:orion@127.0.0.1 SIP/2.0\nVia: SIP/2.0/TCP proxy.test;branch=z9hG4bKm\n"
               f"From: <{caller}>;tag=m1\nTo: <sip:orion@{REALM}>\nCall-ID: msg-1\nCSeq: 1 MESSAGE\n"
               f"Content-Type: {content_type}\n", body)


def test_your_messages_reach_orion_in_plain_text_or_cpim(setup):
    proxy, account, _ = setup
    received = []
    account.on_message = received.append
    message(proxy)
    assert proxy.next().status == 200
    cpim = ("From: <sip:jules@sip.test>\r\nTo: <sip:orion@sip.test>\r\nDateTime: 2026-10-11T02:00:00Z\r\n\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\nÉteins la lumière")
    message(proxy, content_type="message/cpim", body=cpim)
    assert proxy.next().status == 200
    message(proxy, content_type="application/im-iscomposing+xml", body="<isComposing/>")  # « en train d'écrire »
    assert proxy.next().status == 200
    assert received == ["Bonjour ORION", "Éteins la lumière"]


def test_message_from_someone_else_is_refused(setup):
    proxy, account, _ = setup
    received = []
    account.on_message = received.append
    message(proxy, caller="sip:inconnu@ailleurs.test", body="Supprime tous les mails")
    assert proxy.next().status == 403 and received == []


def test_orion_reply_is_sent_with_authentication(setup):
    proxy, account, _ = setup
    result = {}
    thread = threading.Thread(target=lambda: result.update(ok=account.send_message("Il est midi.")))
    thread.start()
    first = proxy.next()
    assert first.method == "MESSAGE" and uri_of(first.first.split()[1]) == PEER
    assert first.header("content-type").startswith("text/plain") and first.body == "Il est midi."
    proxy.challenge(first, proxy=True)
    second = proxy.next()
    assert expected_response(second.header("proxy-authorization"), "MESSAGE")
    proxy.reply(second, 202, "Accepted")
    thread.join(5)
    assert result["ok"] is True


@pytest.mark.parametrize("method", ["INVITE", "UPDATE"])
def test_phone_relaunching_the_call_gets_our_audio_and_its_new_offer_is_applied(setup, method):
    """Ré-INVITE ou UPDATE en plein appel (nouvelle adresse du son) : réponse 200 avec notre son, nouvelle offre
    transmise (sinon le téléphone raccroche au bout de 32 s, délai d'une requête sans réponse)."""
    proxy, account, events = setup
    updates = []
    account.on_update = lambda dialog: updates.append(dialog.remote_sdp)
    incoming(proxy)
    proxy.next()
    dialog = events["incoming"].get(timeout=5)
    account.answer(dialog, "v=0\r\nm=audio 6000 RTP/SAVP 96\r\n")
    proxy.next()
    proxy.send(f"{method} sip:orion@127.0.0.1 SIP/2.0\nVia: SIP/2.0/TCP proxy.test;branch=z9hG4bKr\n"
               f"From: <{PEER}>;tag=a1\nTo: <sip:orion@{REALM}>;tag={dialog.local_tag}\nCall-ID: in-1\n"
               f"CSeq: 3 {method}\nContent-Type: application/sdp\n", "v=0\r\nm=audio 7000 RTP/SAVP 96\r\n")
    ok = proxy.next()
    assert ok.status == 200 and "m=audio 6000" in ok.body and ok.header("to").count("tag=") == 1
    assert updates == ["v=0\r\nm=audio 7000 RTP/SAVP 96\r\n"]
