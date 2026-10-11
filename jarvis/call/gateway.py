"""Passerelle d'appel d'ORION (orion-call) : fait sonner le téléphone jumelé et transporte l'appel. Processus à part
du Core, sans logique de conversation :

    python -m jarvis.call.gateway            # sert la passerelle ([call] de config.toml)
    python -m jarvis.call.gateway --pair     # code de jumelage à usage unique (5 min), à saisir dans l'application

Deux faces :
- application mobile (réseaux de [call] app_networks : la maison, le VPN) :
    POST /app/pair {code, name}            -> {device_id, token} (une seule fois)
    GET  /app/events?timeout=25            -> {"events": [...]} (attente longue : appel entrant, fin d'appel)
    POST /app/calls/<id>/accept | refuse | hangup
    POST /app/calls/<id>/offer {sdp, type}   -> réponse SDP (audio WebRTC, après « accept »)
    POST /app/dial                           -> {call_id} : le téléphone appelle ORION ; l'événement « answered »
                                                suit quand le Core décroche (sinon « ended », ORION ne répond pas)
  authentification : en-têtes « X-Device » (identifiant) et « Authorization: Bearer <jeton> » ;
- Core (127.0.0.1 seulement, jeton CALL_GATEWAY_TOKEN) :
    POST /core/ring {call_id, device, reason} ; POST /core/cancel | /core/hangup {call_id[, reason]}
    POST /core/answer {call_id} (appel du téléphone décroché) ; POST /core/pairing-code
    GET  /core/events?timeout=25           -> signaux de l'appareil (accepted, refused, connected, hung_up)
    GET  /audio/input ; POST /audio/output | /audio/drain | /audio/stop : le protocole des agents audio d'ORION
  (jarvis/audio/network.py). Audio par WebRTC (jarvis/call/media.py) quand aiortc est installé ; sinon SIMULÉ
  (silence en entrée, voix d'ORION jetée), pour les essais de signalisation.
Aucun jeton, code de jumelage ni contenu d'appel n'est journalisé.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from jarvis.call.devices import DeviceRegistry, PairingError
from jarvis.call.sip import DEVICE as LINPHONE_DEVICE
from jarvis.net import ExclusiveServer, JsonHandler, bearer_ok, client_ip

log = logging.getLogger(__name__)

MAX_BODY = 16 * 1024
MAX_AUDIO = 8 * 1024 * 1024
MAX_WAIT = 30.0
CORE_TIMEOUT = 40.0  # s sans que le Core vienne chercher ses signaux : ORION est injoignable
DIAL_TIMEOUT = 10.0  # s pour que le Core décroche un appel du téléphone
MAX_CALL_SECONDS = 20 * 60  # durée maximale de secours (le Core arrête à [call] max_minutes)
APP_ACTIONS = ("accept", "refuse", "hangup")


class Mailbox:
    """File d'événements avec attente longue (le client revient chercher la suite)."""

    def __init__(self, limit: int = 50):
        self._items: list[dict] = []
        self._limit = limit
        self._cond = threading.Condition()

    def put(self, item: dict) -> None:
        with self._cond:
            self._items = (self._items + [item])[-self._limit:]
            self._cond.notify_all()

    def take(self, timeout: float) -> list[dict]:
        with self._cond:
            if not self._items:
                self._cond.wait(timeout)
            items, self._items = self._items, []
            return items


@dataclass
class GatewayCall:
    id: str
    device: str
    reason: str
    state: str = "sonnerie"  # sonnerie | composé (le téléphone appelle, le Core n'a pas encore décroché),
    #                          accepté, connecté, terminé
    created: float = field(default_factory=time.monotonic)
    media: object = None  # son propre à l'appel (Linphone : SipMedia) ; None : le son WebRTC de la passerelle
    sip: object = None  # appel SIP en cours (jarvis/call/sip.py), pour l'appareil « linphone »
    cancel: object = None  # annulation de la sonnerie d'un appel Linphone sortant


class CallGateway:
    def __init__(self, registry: DeviceRegistry, core_token: str, app_networks: list[str],
                 host: str = "0.0.0.0", port: int = 8770, frame_seconds: float = 0.08, media_factory=None):
        self.registry = registry
        self._core_token = core_token
        self._networks = [ipaddress.ip_network(n, strict=False) for n in app_networks]
        self._address = (host, port)
        self._frame_seconds = frame_seconds
        self._lock = threading.Lock()
        self.call: GatewayCall | None = None
        self.core_events = Mailbox()
        self._app_events: dict[str, Mailbox] = {}
        self._httpd: ExclusiveServer | None = None
        self._clock = time.monotonic
        self._core_seen: float | None = None  # dernière attente du Core sur /core/events (il est en vie)
        self._stopping = threading.Event()
        # media_factory(on_connected, on_failed) -> CallMedia ; None : audio simulé.
        self.media = media_factory(self._media_connected, self._media_failed) if media_factory else None
        self.linphone = None  # LinphoneChannel (jarvis/call/linphone.py) : appareil « linphone », votre adresse

    def current_media(self):
        """Son de l'appel en cours : le sien (Linphone) ou celui de la passerelle (WebRTC, ou simulé si None)."""
        call = self.call
        if call is not None and call.media is not None:
            return call.media
        return self.media

    def _is_sip(self, call: GatewayCall) -> bool:
        """Appel de l'appareil « linphone » (seulement si Linphone est configuré : sans lui, rien de SRTP n'est
        importé, la passerelle tourne aussi sans pylibsrtp)."""
        return self.linphone is not None and call.device == LINPHONE_DEVICE

    # --- Logique -------------------------------------------------------------------------------------------

    def app_mailbox(self, device_id: str) -> Mailbox:
        with self._lock:
            return self._app_events.setdefault(device_id, Mailbox())

    def ring(self, call_id: str, device_id: str, reason: str) -> bool:
        call = GatewayCall(str(call_id)[:64], device_id, str(reason or "")[:300])
        if self._is_sip(call):  # votre adresse Linphone : la passerelle la compose
            with self._lock:
                self.call = call
            log.info("Passerelle : appel %s, sonnerie sur Linphone", call.id)
            return self.linphone.ring(call)
        if self.registry.get(device_id) is None:
            return False
        with self._lock:
            self.call = GatewayCall(str(call_id)[:64], device_id, str(reason or "")[:300])
        self.app_mailbox(device_id).put({"type": "incoming", "call_id": self.call.id, "reason": self.call.reason})
        log.info("Passerelle : appel %s, sonnerie sur l'appareil %s", self.call.id, device_id)
        return True

    def core_alive(self) -> bool:
        seen = self._core_seen
        return seen is not None and self._clock() - seen < CORE_TIMEOUT

    def dial(self, device_id: str) -> tuple[GatewayCall | None, str]:
        """Le téléphone appelle ORION : (appel « composé », "") en attendant que le Core décroche (``answer``) ou
        raccroche ; (None, raison) si ORION est injoignable ou déjà en ligne."""
        if not self.core_alive():
            return None, "ORION est injoignable."
        with self._lock:
            if self.call is not None and self.call.state != "terminé":
                return None, "ORION est déjà en ligne."
            call = self.call = GatewayCall(secrets.token_urlsafe(12), device_id, "", state="composé")
        self.core_events.put({"type": "dialed", "call_id": call.id, "device": device_id})
        log.info("Passerelle : appel %s composé par l'appareil %s", call.id, device_id)
        return call, ""

    def answer_from_core(self, call_id: str) -> bool:
        """Le Core décroche un appel du téléphone : le téléphone peut maintenant ouvrir l'audio."""
        call = self._current(call_id)
        if call is None or call.state != "composé":
            return False
        if self._is_sip(call):  # appel venu de Linphone : ORION répond avec le son, connecté aussitôt
            if not self.linphone.answer(call):
                call.state = "terminé"
                return False
            call.state = "connecté"
            self.core_events.put({"type": "connected", "call_id": call.id})
            log.info("Passerelle : appel %s de Linphone décroché par ORION", call.id)
            return True
        call.state = "accepté"
        self.app_mailbox(call.device).put({"type": "answered", "call_id": call.id})
        log.info("Passerelle : appel %s décroché par ORION", call.id)
        return True

    def watch(self, now: float | None = None) -> None:
        """Délais de la passerelle (appelé chaque seconde) : Core qui ne décroche pas, Core disparu en plein appel,
        durée maximale de secours."""
        now = self._clock() if now is None else now
        call = self.call
        if call is None or call.state == "terminé":
            return
        if call.state == "composé" and now - call.created >= DIAL_TIMEOUT:
            self._end_here(call, "ORION ne répond pas", tell_core=False)
        elif not self.core_alive():
            self._end_here(call, "ORION injoignable", tell_core=False)
        elif now - call.created >= MAX_CALL_SECONDS:
            self._end_here(call, "durée maximale atteinte", tell_core=True)

    def _end_here(self, call: GatewayCall, why: str, tell_core: bool) -> None:
        call.state = "terminé"
        if tell_core:
            self.core_events.put({"type": "failed", "call_id": call.id, "reason": why})
        self.app_mailbox(call.device).put({"type": "ended", "call_id": call.id, "reason": why})
        threading.Thread(target=self._close_media, args=(call,), daemon=True).start()
        log.info("Passerelle : appel %s terminé (%s)", call.id, why)

    def end_from_core(self, call_id: str, why: str) -> None:
        call = self._current(call_id)
        if call is None:
            return
        call.state = "terminé"
        self._close_media(call)
        self.app_mailbox(call.device).put({"type": "ended", "call_id": call.id, "reason": why})
        log.info("Passerelle : appel %s %s", call.id, why)

    def offer(self, device_id: str, call_id: str, sdp: str, kind: str) -> dict | None:
        """Offre WebRTC du téléphone (appel accepté) -> réponse SDP ; None si l'appel ne l'attend pas."""
        call = self._current(call_id)
        if self.media is None or call is None or call.device != device_id or call.state != "accepté":
            return None
        return self.media.answer(call.id, sdp, kind)

    def _media_connected(self, call_id: str) -> None:
        call = self._current(call_id)
        if call is not None and call.state == "accepté":
            call.state = "connecté"
            self.core_events.put({"type": "connected", "call_id": call.id})

    def _media_failed(self, call_id: str, why: str) -> None:
        call = self._current(call_id)
        if call is None:
            return
        call.state = "terminé"
        self.core_events.put({"type": "failed", "call_id": call.id, "reason": why})
        self.app_mailbox(call.device).put({"type": "ended", "call_id": call.id, "reason": why})
        threading.Thread(target=self._close_media, args=(call,), daemon=True).start()

    def _close_media(self, call: GatewayCall | None = None) -> None:
        """Fin du son d'un appel : Linphone (annulation ou raccrochage SIP, son de l'appel), sinon WebRTC."""
        call = call if call is not None else self.call
        if call is not None and self._is_sip(call):
            self.linphone.end(call)
        elif self.media is not None:
            self.media.close()

    # --- Linphone (jarvis/call/linphone.py) ----------------------------------------------------------------

    def sip_answered(self, call: GatewayCall) -> None:
        """Vous avez décroché sur Linphone : décroché et son établi pour le Core."""
        if self._current(call.id) is not call:
            self.linphone.end(call)
            return
        call.state = "connecté"
        self.core_events.put({"type": "accepted", "call_id": call.id})
        self.core_events.put({"type": "connected", "call_id": call.id})

    def sip_refused(self, call: GatewayCall) -> None:
        if self._current(call.id) is call:
            call.state = "terminé"
            self.core_events.put({"type": "refused", "call_id": call.id})
            log.info("Passerelle : appel %s refusé sur Linphone", call.id)

    def sip_failed(self, call: GatewayCall, why: str) -> None:
        if self._current(call.id) is call:
            call.state = "terminé"
            self.core_events.put({"type": "failed", "call_id": call.id, "reason": why})
            log.info("Passerelle : appel %s sans suite sur Linphone (%s)", call.id, why)

    def sip_dialed(self, dialog) -> tuple[GatewayCall | None, str]:
        """Vous appelez ORION depuis Linphone : le Core est prévenu et décide (``answer_from_core``)."""
        if not self.core_alive():
            return None, "ORION est injoignable"
        with self._lock:
            if self.call is not None and self.call.state != "terminé":
                return None, "ORION est déjà en ligne"
            call = self.call = GatewayCall(secrets.token_urlsafe(12), LINPHONE_DEVICE, "", state="composé", sip=dialog)
        self.core_events.put({"type": "dialed", "call_id": call.id, "device": LINPHONE_DEVICE})
        log.info("Passerelle : appel %s de Linphone, en attente d'ORION", call.id)
        return call, ""

    def sip_ended(self, dialog, why: str) -> None:
        """Vous avez raccroché (ou annulé) sur Linphone."""
        call = self.call
        if call is None or call.sip is not dialog or call.state == "terminé":
            return
        state, call.state = call.state, "terminé"
        if call.media is not None:
            call.media.close()
        self.core_events.put({"type": "hung_up" if state in ("accepté", "connecté") else "failed", "call_id": call.id,
                              "reason": "annulé sur Linphone"})
        log.info("Passerelle : appel %s %s sur Linphone", call.id, why)

    def from_app(self, device_id: str, call_id: str, action: str) -> bool:
        call = self._current(call_id)
        if call is None or call.device != device_id:
            return False
        if action == "accept" and call.state == "sonnerie":
            call.state = "accepté"
            self.core_events.put({"type": "accepted", "call_id": call.id})
            if self.media is None:  # audio simulé : connecté aussitôt ; en WebRTC, à la connexion audio réelle
                call.state = "connecté"
                self.core_events.put({"type": "connected", "call_id": call.id})
        elif action == "refuse" and call.state == "sonnerie":
            call.state = "terminé"
            self.core_events.put({"type": "refused", "call_id": call.id})
        elif action == "hangup" and call.state in ("composé", "accepté", "connecté"):
            call.state = "terminé"
            self.core_events.put({"type": "hung_up", "call_id": call.id})
            self._close_media()
        else:
            return False
        log.info("Passerelle : appel %s, %s sur le téléphone", call.id, action)
        return True

    def _current(self, call_id: str) -> GatewayCall | None:
        call = self.call
        return call if call is not None and call.id == call_id and call.state != "terminé" else None

    def app_allowed(self, ip: str) -> bool:
        address = ipaddress.ip_address(ip)
        return any(address in network for network in self._networks)

    # --- Serveur -------------------------------------------------------------------------------------------

    def start(self) -> None:
        self._httpd = ExclusiveServer(self._address, self._handler())
        threading.Thread(target=self._httpd.serve_forever, name="passerelle-appel", daemon=True).start()
        threading.Thread(target=self._watch_loop, name="passerelle-delais", daemon=True).start()
        log.info("Passerelle d'appel à l'écoute sur %s:%d (application : %s)", *self._address,
                 ", ".join(str(n) for n in self._networks))

    def _watch_loop(self) -> None:
        while not self._stopping.wait(1.0):
            try:
                self.watch()
            except Exception:  # noqa: BLE001 - le fil des délais ne doit pas mourir
                log.exception("Passerelle : délais non vérifiés")

    def stop(self) -> None:
        self._stopping.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()

    @property
    def port(self) -> int:
        return self._httpd.server_address[1] if self._httpd is not None else self._address[1]

    def _handler(self):
        gateway = self

        class Handler(JsonHandler):
            server_version = "orion-call"

            def _wait(self, query: dict) -> float:
                try:
                    return max(0.0, min(MAX_WAIT, float(query.get("timeout", ["25"])[0])))
                except ValueError:
                    return 25.0

            def _json_body(self) -> dict | None:
                raw = self._body(MAX_BODY)
                if raw is None:
                    return None
                try:
                    data = json.loads(raw or b"{}")
                except ValueError:
                    self._error(400, "bad_json")
                    return None
                if not isinstance(data, dict):
                    self._error(400, "bad_json")
                    return None
                return data

            # Core : local seulement, jeton de la passerelle.
            def _core_ok(self) -> bool:
                if client_ip(self.client_address[0]) not in ("127.0.0.1", "::1"):
                    self._error(403, "forbidden")
                    return False
                if not bearer_ok(self.headers.get("Authorization", ""), gateway._core_token):
                    self._error(401, "unauthorized")
                    return False
                return True

            # Application : réseaux autorisés, puis appareil jumelé.
            def _app_network_ok(self) -> bool:
                if not gateway.app_allowed(client_ip(self.client_address[0])):
                    self._error(403, "forbidden")
                    return False
                return True

            def _device(self):
                header = self.headers.get("Authorization", "")
                scheme, _, token = header.partition(" ")
                device = gateway.registry.authenticate(self.headers.get("X-Device", ""), token) \
                    if scheme == "Bearer" else None
                if device is None:
                    self._error(401, "unauthorized")
                return device

            def do_GET(self):
                url = urlsplit(self.path)
                query = parse_qs(url.query)
                if url.path == "/app/events":
                    if not self._app_network_ok() or (device := self._device()) is None:
                        return
                    self._json(200, {"events": gateway.app_mailbox(device.id).take(self._wait(query))})
                elif url.path == "/app/health":
                    if self._app_network_ok() and (device := self._device()) is not None:
                        self._json(200, {"status": "ok", "device": device.name})
                elif url.path == "/core/events":
                    if self._core_ok():
                        gateway._core_seen = gateway._clock()  # le Core est en vie (il attend ses signaux)
                        events = gateway.core_events.take(self._wait(query))
                        gateway._core_seen = gateway._clock()
                        self._json(200, {"events": events})
                elif url.path == "/audio/input":
                    if self._core_ok():
                        self._silence(query)
                else:
                    self._error(404, "not_found")

            def do_POST(self):
                path = urlsplit(self.path).path
                parts = path.strip("/").split("/")
                if path == "/app/pair":
                    if not self._app_network_ok() or (data := self._json_body()) is None:
                        return
                    try:
                        device, token = gateway.registry.pair(data.get("code", ""), data.get("name", ""))
                    except PairingError as exc:
                        self._error(403, "pairing_refused", str(exc))
                        return
                    log.info("Passerelle : appareil « %s » jumelé (%s)", device.name, device.id)
                    self._json(200, {"device_id": device.id, "token": token, "name": device.name})
                elif len(parts) == 4 and parts[:2] == ["app", "calls"] and parts[3] == "offer":
                    if not self._app_network_ok() or (device := self._device()) is None:
                        return
                    if (data := self._json_body()) is None:
                        return
                    try:
                        answer = gateway.offer(device.id, parts[2], str(data.get("sdp", "")),
                                               str(data.get("type", "offer")))
                    except Exception as exc:  # noqa: BLE001 - offre illisible ou connexion impossible
                        log.warning("Passerelle : offre audio refusée (%s)", type(exc).__name__)
                        answer = None
                    if answer is None:
                        self._error(409, "offer_refused")
                    else:
                        self._json(200, answer)
                elif path == "/app/dial":
                    if not self._app_network_ok() or (device := self._device()) is None:
                        return
                    self._drain()
                    call, refusal = gateway.dial(device.id)
                    if call is None:
                        self._error(409 if "en ligne" in refusal else 503, "unavailable", refusal)
                    else:
                        self._json(200, {"call_id": call.id})
                elif len(parts) == 4 and parts[:2] == ["app", "calls"] and parts[3] in APP_ACTIONS:
                    if not self._app_network_ok() or (device := self._device()) is None:
                        return
                    self._drain()
                    if gateway.from_app(device.id, parts[2], parts[3]):
                        self._json(200, {"status": "ok"})
                    else:
                        self._error(409, "call_not_active")
                elif path in ("/core/ring", "/core/cancel", "/core/hangup", "/core/pairing-code", "/core/answer"):
                    if not self._core_ok() or (data := self._json_body()) is None:
                        return
                    if path == "/core/pairing-code":
                        self._json(200, {"code": gateway.registry.new_code(), "expires_in": 300})
                    elif path == "/core/answer":
                        ok = gateway.answer_from_core(str(data.get("call_id", "")))
                        self._json(200, {"status": "ok"}) if ok else self._error(409, "call_not_dialed")
                    elif path == "/core/ring":
                        ok = gateway.ring(data.get("call_id", ""), data.get("device", ""), data.get("reason", ""))
                        self._json(200, {"status": "ringing"}) if ok else self._error(404, "device_not_paired")
                    else:
                        why = "annulé" if path == "/core/cancel" else "raccroché par ORION"
                        why = str(data.get("reason") or why)[:80]  # « occupé »… : dit sur le téléphone
                        gateway.end_from_core(str(data.get("call_id", "")), why)
                        self._json(200, {"status": "ok"})
                elif path in ("/audio/output", "/audio/drain", "/audio/stop"):
                    if self._core_ok():
                        self._voice(path)
                else:
                    self._error(404, "not_found")

            def _voice(self, path: str) -> None:
                """Voix d'ORION vers le téléphone (WebRTC) ; jetée en audio simulé."""
                body = self._body(MAX_AUDIO)
                if body is None:
                    return
                media = gateway.current_media()
                if media is not None:
                    if path == "/audio/output":
                        import numpy as np

                        query = parse_qs(urlsplit(self.path).query)
                        try:
                            rate = max(8000, min(48000, int(query.get("rate", ["22050"])[0])))
                        except ValueError:
                            rate = 22050
                        media.play(np.frombuffer(body[: len(body) // 2 * 2], dtype="<i2"), rate)
                    elif path == "/audio/drain":
                        media.drain()
                    else:
                        media.stop_voice()
                self._json(200, {"status": "ok"})

            def _silence(self, query: dict) -> None:
                """Micro simulé : des blocs de silence au rythme réel, tant que l'appel dure."""
                try:
                    frame = max(160, min(16000, int(query.get("frame", ["1280"])[0])))
                except ValueError:
                    frame = 1280
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                block = bytes(2 * frame)
                try:
                    while gateway.call is not None and gateway.call.state != "terminé":
                        media = gateway.current_media()
                        if media is None:  # audio simulé
                            chunk = block
                            time.sleep(gateway._frame_seconds)
                        elif media.mic is None:  # en attente de l'audio du téléphone
                            time.sleep(0.05)
                            continue
                        else:
                            chunk = media.mic.read(2 * frame, timeout=0.5)
                            if chunk is None:
                                break
                            if not chunk:
                                continue
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis.call.gateway")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--pair", action="store_true", help="affiche un code de jumelage à usage unique")
    parser.add_argument("--no-media", action="store_true", help="audio simulé, même si aiortc est installé")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")
    from jarvis.config import load_config, secret

    env_file = Path(__file__).resolve().parents[2] / ".env"  # sans importer le Core (environnement minimal)
    cfg = load_config(args.config)
    c = cfg.call
    token = secret("CALL_GATEWAY_TOKEN", env_file)
    if not token:
        print("CALL_GATEWAY_TOKEN absent de .env : la passerelle refuse de démarrer.")
        return 2
    url = urlsplit(c.gateway)
    if args.pair:
        import urllib.request

        request = urllib.request.Request(f"{c.gateway}/core/pairing-code", data=b"{}", method="POST",
                                         headers={"Authorization": f"Bearer {token}",
                                                  "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            code = json.load(response)["code"]
        print(f"Code de jumelage (valable 5 minutes, une seule fois) : {code[:4]}-{code[4:]}")
        return 0
    media_factory = None
    if not args.no_media:
        try:
            from jarvis.call.media import CallMedia as media_factory
        except ImportError:
            log.warning("aiortc absent : audio simulé (pip install aiortc numpy dans l'environnement de la passerelle)")
    gateway = CallGateway(DeviceRegistry(Path(c.devices_path)), token, list(c.app_networks),
                          c.listen_host, url.port or 8770, media_factory=media_factory)
    log.info("Audio des appels : %s", "WebRTC" if gateway.media is not None else "simulé")
    sip_password = secret("LINPHONE_ORION_PASSWORD", env_file)
    if c.sip_user and c.sip_peer and sip_password:  # appareil « linphone » : votre adresse Linphone
        from jarvis.call.linphone import LinphoneChannel
        from jarvis.call.sip import SipAccount

        account = SipAccount(c.sip_user, sip_password, c.sip_peer, domain=c.sip_domain)
        gateway.linphone = LinphoneChannel(account, gateway)
        account.start()
        log.info("Linphone : compte sip:%s@%s, seul correspondant %s", c.sip_user, c.sip_domain, c.sip_peer)
    gateway.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        gateway.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
