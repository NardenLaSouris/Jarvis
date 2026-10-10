"""Passerelle d'appel (jarvis/call/gateway.py) : jumelage, authentification de l'appareil, réseaux autorisés,
sonnerie et signaux jusqu'au gestionnaire d'appels du Core. Vrai serveur HTTP local (port libre), aucun téléphone.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.call.devices import MAX_FAILURES, PAIRING_TTL, DeviceRegistry, PairingError  # noqa: E402
from jarvis.call.gateway import CallGateway  # noqa: E402
from jarvis.call.link import GatewayLink  # noqa: E402

CORE_TOKEN = "jeton-du-core-pour-les-tests"


# --- Appareils et codes de jumelage --------------------------------------------------------------------------

def test_pairing_code_is_single_use_and_only_the_token_hash_is_stored(tmp_path):
    now = [1000.0]
    registry = DeviceRegistry(tmp_path / "devices.json", clock=lambda: now[0])
    code = registry.new_code()
    assert len(code) == 8
    device, token = registry.pair(code.lower()[:4] + "-" + code.lower()[4:], "Pixel")  # saisie tolérante
    assert registry.authenticate(device.id, token) is device
    assert registry.authenticate(device.id, token + "x") is None and registry.authenticate("autre", token) is None
    stored = (tmp_path / "devices.json").read_text(encoding="utf-8")
    assert token not in stored and device.token_sha256 in stored
    if os.name == "posix":
        assert (tmp_path / "devices.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(PairingError):
        registry.pair(code, "Pixel")  # déjà utilisé
    assert DeviceRegistry(tmp_path / "devices.json").authenticate(device.id, token) is not None  # relu du disque


def test_pairing_code_expires_and_guessing_is_locked_out(tmp_path):
    now = [1000.0]
    registry = DeviceRegistry(tmp_path / "devices.json", clock=lambda: now[0])
    code = registry.new_code()
    now[0] += PAIRING_TTL + 1
    with pytest.raises(PairingError):
        registry.pair(code, "Pixel")
    fresh = registry.new_code()
    for _ in range(MAX_FAILURES):
        with pytest.raises(PairingError):
            registry.pair("ZZZZZZZZ", "Pixel")
    with pytest.raises(PairingError, match="bloqué"):
        registry.pair(fresh, "Pixel")  # même le bon code attend la fin du blocage


# --- Serveur ----------------------------------------------------------------------------------------------

def request(gateway, method, path, payload=None, headers=None, timeout=5):
    data = json.dumps(payload).encode() if payload is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(f"http://127.0.0.1:{gateway.port}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


@pytest.fixture
def gateway(tmp_path):
    gw = CallGateway(DeviceRegistry(tmp_path / "devices.json"), CORE_TOKEN, ["127.0.0.0/8"], "127.0.0.1", 0)
    gw.start()
    yield gw
    gw.stop()


def core(token=CORE_TOKEN):
    return {"Authorization": f"Bearer {token}"}


def pair(gateway):
    status, body = request(gateway, "POST", "/core/pairing-code", {}, core())
    assert status == 200
    status, body = request(gateway, "POST", "/app/pair", {"code": body["code"], "name": "Pixel"})
    assert status == 200
    return body["device_id"], {"X-Device": body["device_id"], "Authorization": f"Bearer {body['token']}"}


def test_core_side_needs_the_gateway_token(gateway):
    assert request(gateway, "POST", "/core/pairing-code", {}, core("faux"))[0] == 401
    assert request(gateway, "POST", "/core/ring", {"call_id": "a", "device": "x"})[0] == 401
    assert request(gateway, "GET", "/core/events?timeout=0", headers=core("faux"))[0] == 401


def test_app_side_needs_a_paired_device_and_an_allowed_network(gateway, tmp_path):
    _, device = pair(gateway)
    assert request(gateway, "GET", "/app/health", headers=device) == (200, {"status": "ok", "device": "Pixel"})
    assert request(gateway, "GET", "/app/health", headers={**device, "Authorization": "Bearer faux"})[0] == 401
    assert request(gateway, "POST", "/app/pair", {"code": "ABCDEFGH", "name": "Intrus"})[0] == 403
    closed = CallGateway(DeviceRegistry(tmp_path / "autres.json"), CORE_TOKEN, ["192.168.1.0/24"], "127.0.0.1", 0)
    closed.start()
    try:
        assert request(closed, "GET", "/app/health", headers=device)[0] == 403  # hors des réseaux autorisés
        assert request(closed, "POST", "/app/pair", {"code": "ABCDEFGH"})[0] == 403
    finally:
        closed.stop()


def test_ring_reaches_the_phone_and_its_answer_reaches_the_core(gateway):
    device_id, device = pair(gateway)
    assert request(gateway, "POST", "/core/ring", {"call_id": "c1", "device": "inconnu"}, core())[0] == 404
    assert request(gateway, "POST", "/core/ring", {"call_id": "c1", "device": device_id, "reason": "Colis"}, core())[0] == 200
    status, body = request(gateway, "GET", "/app/events?timeout=1", headers=device)
    assert body["events"] == [{"type": "incoming", "call_id": "c1", "reason": "Colis"}]
    assert request(gateway, "POST", "/app/calls/autre/accept", {}, device)[0] == 409
    assert request(gateway, "POST", "/app/calls/c1/accept", {}, device)[0] == 200
    assert request(gateway, "POST", "/app/calls/c1/accept", {}, device)[0] == 409  # rejoué : sans effet
    status, body = request(gateway, "GET", "/core/events?timeout=1", headers=core())
    assert [e["type"] for e in body["events"]] == ["accepted", "connected"]
    assert request(gateway, "POST", "/app/calls/c1/hangup", {}, device)[0] == 200
    assert request(gateway, "GET", "/core/events?timeout=1", headers=core())[1]["events"] == \
        [{"type": "hung_up", "call_id": "c1"}]


def test_core_end_is_announced_to_the_phone(gateway):
    device_id, device = pair(gateway)
    request(gateway, "POST", "/core/ring", {"call_id": "c2", "device": device_id}, core())
    request(gateway, "GET", "/app/events?timeout=1", headers=device)
    assert request(gateway, "POST", "/core/cancel", {"call_id": "c2"}, core())[0] == 200
    assert request(gateway, "GET", "/app/events?timeout=1", headers=device)[1]["events"] == \
        [{"type": "ended", "call_id": "c2", "reason": "annulé"}]
    assert request(gateway, "POST", "/app/calls/c2/accept", {}, device)[0] == 409  # trop tard


def test_other_device_cannot_answer_someone_elses_call(gateway):
    device_id, device = pair(gateway)
    _, other = pair(gateway)
    request(gateway, "POST", "/core/ring", {"call_id": "c3", "device": device_id}, core())
    assert request(gateway, "POST", "/app/calls/c3/accept", {}, other)[0] == 409


# --- Lien du Core ---------------------------------------------------------------------------------------------

def test_core_link_rings_and_forwards_the_phone_signals_to_the_manager(gateway):
    from jarvis.audio.switch import SwitchableSink, SwitchableSource
    from jarvis.call import CallManager, CallSettings
    from test_call import Mic, Speaker

    device_id, device = pair(gateway)
    link = GatewayLink(f"http://127.0.0.1:{gateway.port}", CORE_TOKEN, device_id, wait=1)
    phone_mic, phone_speaker = Mic(7), Speaker()
    manager = CallManager(CallSettings(device=device_id), SwitchableSource(Mic(1), sleep=lambda s: None),
                          SwitchableSink(Speaker()), lambda call: (phone_mic, phone_speaker), notifier=link)
    link.manager = manager
    call = manager.request("Colis", "routine")
    assert request(gateway, "GET", "/app/events?timeout=1", headers=device)[1]["events"][0]["call_id"] == call.id
    request(gateway, "POST", f"/app/calls/{call.id}/accept", {}, device)
    for event in request(gateway, "GET", "/core/events?timeout=1", headers=core())[1]["events"]:
        link.deliver(event)
    assert call.state == "connecté" and manager.take_connected() is call
    manager.finish()  # ORION raccroche : le téléphone est prévenu
    assert request(gateway, "GET", "/app/events?timeout=1", headers=device)[1]["events"] == \
        [{"type": "ended", "call_id": call.id, "reason": "raccroché par ORION"}]
    link.deliver({"type": "hung_up", "call_id": call.id})  # signal en retard : ignoré, sans erreur


def test_closing_the_call_mic_during_a_read_is_quiet(gateway):
    import threading

    from jarvis.audio.network import NetworkSource

    device_id, device = pair(gateway)
    request(gateway, "POST", "/core/ring", {"call_id": "c4", "device": device_id}, core())
    request(gateway, "POST", "/app/calls/c4/accept", {}, device)
    errors = []
    previous, threading.excepthook = threading.excepthook, lambda info: errors.append(info.exc_type)
    try:
        source = NetworkSource(f"http://127.0.0.1:{gateway.port}", CORE_TOKEN, 16000, 1280)
        assert len(source.read()) == 1280  # le micro (simulé) de l'appel arrive
        source.close()  # fin d'appel pendant une lecture
        source._thread.join(3)
    finally:
        threading.excepthook = previous
    assert errors == [] and not source._thread.is_alive()


def test_phone_can_dial_orion_and_the_core_can_refuse(gateway):
    device_id, device = pair(gateway)
    status, body = request(gateway, "POST", "/app/dial", {}, device)
    assert status == 200
    call_id = body["call_id"]
    assert request(gateway, "GET", "/core/events?timeout=1", headers=core())[1]["events"] == \
        [{"type": "dialed", "call_id": call_id, "device": device_id}]
    assert request(gateway, "POST", "/app/dial", {}, device)[0] == 409  # déjà en ligne
    request(gateway, "POST", "/core/hangup", {"call_id": call_id, "reason": "occupé"}, core())
    assert request(gateway, "GET", "/app/events?timeout=1", headers=device)[1]["events"] == \
        [{"type": "ended", "call_id": call_id, "reason": "occupé"}]


def test_core_link_answers_or_refuses_a_dialed_call(gateway):
    from jarvis.audio.switch import SwitchableSink, SwitchableSource
    from jarvis.call import CallManager, CallSettings
    from test_call import Mic, Speaker

    device_id, device = pair(gateway)
    link = GatewayLink(f"http://127.0.0.1:{gateway.port}", CORE_TOKEN, device_id, wait=1)
    manager = CallManager(CallSettings(device=device_id), SwitchableSource(Mic(1), sleep=lambda s: None),
                          SwitchableSink(Speaker()), lambda call: (Mic(7), Speaker()), notifier=link)
    link.manager = manager
    call_id = request(gateway, "POST", "/app/dial", {}, device)[1]["call_id"]
    for event in request(gateway, "GET", "/core/events?timeout=1", headers=core())[1]["events"]:
        link.deliver(event)
    assert manager.current.id == call_id and manager.current.state == "accepté"
    other = CallManager(CallSettings(device="un-autre"), SwitchableSource(Mic(1), sleep=lambda s: None),
                        SwitchableSink(Speaker()), lambda call: (Mic(7), Speaker()), notifier=link)
    link.manager = other  # Core configuré pour un autre appareil : il raccroche
    link.deliver({"type": "dialed", "call_id": call_id, "device": device_id})
    assert request(gateway, "GET", "/app/events?timeout=1", headers=device)[1]["events"] == \
        [{"type": "ended", "call_id": call_id, "reason": "appareil non jumelé au Core"}]
