"""Compte SIP d'ORION (Linphone, sip.linphone.org) : la passerelle appelle votre adresse Linphone et décroche vos
appels. L'iPhone sonne par l'application Linphone (notification d'appel, écran d'appel natif), même verrouillé ou
application fermée ; le son est chiffré (SDES-SRTP, orion/call/rtp.py).

- Connexion TLS permanente, sortante (aucun port ouvert) : enregistrement renouvelé avant expiration, petits signaux
  de maintien (CRLF, RFC 5626), reconnexion automatique.
- Un seul correspondant (``peer``, votre adresse) : seul appelé possible, seul appelant accepté (403 sinon, avant
  même le Core).
- Rien de secret dans les journaux : ni mot de passe, ni clé SRTP, ni adresse d'appareil.
Bibliothèque standard pour la signalisation ; le son demande l'environnement de la passerelle (rtp.py).
"""

from __future__ import annotations

import hashlib
import logging
import queue
import re
import secrets
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

DEVICE = "linphone"  # nom de l'appareil de la passerelle pour votre adresse Linphone ([call] device du Core)
REGISTER_EXPIRES = 600
KEEPALIVE = 30.0
RECONNECT = 10.0
INVITE_TIMEOUT = 45.0  # s de sonnerie au plus (le Core arrête avant : [call] ring_timeout)
URI = re.compile(r"<?(sips?:[^>;\s]+)")


def uri_of(header: str) -> str:
    """« "Nom" <sip:user@domaine>;tag=… » -> « sip:user@domaine » (minuscules, sans paramètres)."""
    match = URI.search(header or "")
    return match.group(1).lower() if match else ""


def target_of(header: str) -> str:
    """Adresse exacte où joindre l'autre côté (en-tête Contact), paramètres compris et casse gardée :
    « <sip:jules@sip.linphone.org;gr=urn:uuid:…> » -> « sip:jules@sip.linphone.org;gr=urn:uuid:… ». Le serveur
    Linphone a besoin de ce « gr » pour remettre l'ACK et le BYE au bon appareil (sans lui, l'iPhone ne reçoit
    jamais l'ACK de son « décroché » et raccroche au bout de 32 s)."""
    header = (header or "").strip()
    bracketed = re.search(r"<([^<>]+)>", header)
    if bracketed:
        return bracketed.group(1).strip()
    return header.split(";", 1)[0].split(",", 1)[0].strip()  # sans <> : les paramètres sont ceux de l'en-tête


def tag_of(header: str) -> str:
    match = re.search(r";\s*tag=([^;>\s]+)", header or "")
    return match.group(1) if match else ""


@dataclass
class Message:
    first: str
    headers: dict[str, list[str]]
    body: str = ""

    @property
    def is_request(self) -> bool:
        return not self.first.startswith("SIP/2.0")

    @property
    def method(self) -> str:
        return self.first.split()[0] if self.is_request else self.header("cseq").split()[-1]

    @property
    def status(self) -> int:
        return 0 if self.is_request else int(self.first.split()[1])

    def header(self, name: str) -> str:
        values = self.headers.get(name.lower())
        return values[0] if values else ""

    def all(self, name: str) -> list[str]:
        return self.headers.get(name.lower(), [])


COMPACT = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact", "l": "content-length",
           "c": "content-type", "k": "supported"}


def parse(raw: bytes) -> Message:
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode(errors="replace").split("\r\n")
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        name = COMPACT.get(name.strip().lower(), name.strip().lower())
        headers.setdefault(name, []).append(value.strip())
    return Message(lines[0], headers, body.decode(errors="replace"))


def digest(challenge: str, method: str, uri: str, user: str, password: str) -> str:
    """En-tête d'authentification par empreinte (RFC 2617/7616), MD5 ou SHA-256, qop=auth si demandé."""
    fields = dict(re.findall(r'(\w+)="?([^",]*)"?', challenge))
    realm, nonce = fields.get("realm", ""), fields.get("nonce", "")
    algorithm = fields.get("algorithm", "MD5").upper()
    hash_ = hashlib.sha256 if algorithm.startswith("SHA-256") else hashlib.md5
    h = lambda text: hash_(text.encode()).hexdigest()  # noqa: E731
    ha1, ha2 = h(f"{user}:{realm}:{password}"), h(f"{method}:{uri}")
    parts = [f'username="{user}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"', f"algorithm={algorithm}"]
    if "auth" in [q.strip() for q in fields.get("qop", "").split(",")]:
        nc, cnonce = "00000001", secrets.token_hex(8)
        parts += ["qop=auth", f"nc={nc}", f'cnonce="{cnonce}"', f'response="{h(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")}"']
    else:
        parts.append(f'response="{h(f"{ha1}:{nonce}:{ha2}")}"')
    if "opaque" in fields:
        parts.append(f'opaque="{fields["opaque"]}"')
    return "Digest " + ", ".join(parts)


def tls_connect(host: str, port: int) -> socket.socket:
    address = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4]  # IPv4 : même famille que le son
    raw = socket.create_connection(address, timeout=10)
    return ssl.create_default_context().wrap_socket(raw, server_hostname=host)


@dataclass
class Dialog:
    """Un appel SIP : identifiants du dialogue, chemin par les serveurs, SDP de l'autre côté."""
    call_id: str
    local_tag: str
    remote_uri: str  # Contact de l'autre côté (cible des requêtes du dialogue)
    to_header: str  # en-tête de l'autre côté, avec sa balise
    route: list[str] = field(default_factory=list)
    remote_sdp: str = ""
    outgoing: bool = True
    cseq: int = 1
    ended: bool = False
    invite: Message | None = None  # INVITE reçu (appel entrant), pour y répondre
    local_sdp: str = ""  # son d'ORION (renvoyé si l'autre côté relance l'appel en cours)


class SipAccount:
    """``on_incoming(dialog)`` : appel de votre adresse (à décrocher par ``answer`` ou refuser par ``reject``) ;
    ``on_ended(dialog, why)`` : l'autre côté a raccroché ou annulé."""

    def __init__(self, user: str, password: str, peer: str, domain: str = "sip.linphone.org", port: int = 5061,
                 connect: Callable[[str, int], socket.socket] = tls_connect, transport: str = "TLS",
                 on_incoming: Callable[[Dialog], None] | None = None,
                 on_ended: Callable[[Dialog, str], None] | None = None,
                 on_message: Callable[[str], None] | None = None):
        self.user, self._password, self.domain, self.port = user, password, domain, port
        self.peer = uri_of(peer)
        self.aor = f"sip:{user}@{domain}"
        self._connect, self._transport = connect, transport
        self.on_incoming, self.on_ended, self.on_message = on_incoming, on_ended, on_message
        self.on_update: Callable | None = None  # relance de l'appel par l'autre côté (nouvelle offre de son)
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._unreadable_at = float("-inf")
        # « branche méthode » -> réponses de la transaction. La méthode compte : une annulation (CANCEL) reprend la
        # branche de l'INVITE, et son « 200 OK » ne doit jamais passer pour le décroché de l'appel.
        self._waiters: dict[str, queue.Queue] = {}
        self._dialogs: dict[str, Dialog] = {}  # Call-ID -> appel
        self._registered = threading.Event()
        self._stopping = threading.Event()
        self.local_ip, self.local_port = "0.0.0.0", 0
        self._contact = ""

    # --- Connexion et enregistrement ------------------------------------------------------------------------

    @property
    def registered(self) -> bool:
        return self._registered.is_set()

    def start(self) -> None:
        threading.Thread(target=self._run, name="sip-compte", daemon=True).start()

    def stop(self) -> None:
        self._stopping.set()
        try:
            if self._registered.is_set():
                self._register(expires=0)
        except (OSError, TimeoutError):
            pass
        self._close()

    def wait_registered(self, timeout: float) -> bool:
        return self._registered.wait(timeout)

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                self._open()
                self._register(REGISTER_EXPIRES)
                refresh = time.monotonic() + REGISTER_EXPIRES * 0.8
                keepalive = time.monotonic() + KEEPALIVE
                while not self._stopping.is_set() and self._sock is not None:
                    time.sleep(1.0)
                    now = time.monotonic()
                    if now >= keepalive:
                        self._send_raw(b"\r\n\r\n")  # maintien de la connexion (box, serveur)
                        keepalive = now + KEEPALIVE
                    if now >= refresh:
                        self._register(REGISTER_EXPIRES)
                        refresh = now + REGISTER_EXPIRES * 0.8
            except (OSError, TimeoutError, SipError) as exc:
                if not self._stopping.is_set():
                    log.warning("Compte SIP : connexion perdue (%s) ; nouvel essai dans %.0f s", type(exc).__name__,
                                RECONNECT)
            self._registered.clear()
            self._close()
            self._stopping.wait(RECONNECT)

    def _open(self) -> None:
        sock = self._connect(self.domain, self.port)
        ip, self.local_port = sock.getsockname()[:2]
        self.local_ip = ip
        self._contact = f"<sip:{self.user}@{ip}:{self.local_port};transport={self._transport.lower()}>"
        self._sock = sock
        threading.Thread(target=self._reader, args=(sock,), name="sip-lecture", daemon=True).start()

    def _close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _register(self, expires: int) -> None:
        call_id = getattr(self, "_register_call_id", None) or secrets.token_hex(10)
        self._register_call_id = call_id
        response = self._transaction("REGISTER", f"sip:{self.domain}", self.aor, call_id=call_id,
                                     from_tag=getattr(self, "_register_tag", None) or self._new_register_tag(),
                                     extra=f"Expires: {expires}\r\n")
        if response is None or response.status != 200:
            raise SipError(f"enregistrement refusé ({response.status if response else 'sans réponse'})")
        if expires:
            if not self._registered.is_set():
                log.info("Compte SIP %s enregistré sur %s", self.aor, self.domain)
            self._registered.set()
        else:
            self._registered.clear()

    def _new_register_tag(self) -> str:
        self._register_tag = secrets.token_hex(4)
        self._register_cseq = 0
        return self._register_tag

    # --- Envoi ----------------------------------------------------------------------------------------------

    def _send_raw(self, data: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise SipError("non connecté")
        with self._send_lock:
            sock.sendall(data)

    def _request(self, method: str, uri: str, to: str, call_id: str, from_tag: str, cseq: int, branch: str,
                 body: str = "", extra: str = "", route: list[str] | None = None, auth: tuple[str, bool] | None = None):
        to = to if "<" in to else f"<{to}>"
        lines = [f"{method} {uri} SIP/2.0",
                 f"Via: SIP/2.0/{self._transport} {self.local_ip}:{self.local_port};branch={branch};rport",
                 "Max-Forwards: 70", f"From: <{self.aor}>;tag={from_tag}", f"To: {to}", f"Call-ID: {call_id}",
                 f"CSeq: {cseq} {method}", f"Contact: {self._contact}", "User-Agent: ORION"]
        if route:
            lines.append(f"Route: {', '.join(route)}")
        if auth:
            lines.append(f"{'Proxy-Authorization' if auth[1] else 'Authorization'}: {auth[0]}")
        if body and "content-type:" not in extra.lower():  # SDP par défaut ; un message porte son propre type
            lines.append("Content-Type: application/sdp")
        text = "\r\n".join(lines) + "\r\n" + extra + f"Content-Length: {len(body.encode())}\r\n\r\n" + body
        self._send_raw(text.encode())

    def _transaction(self, method: str, uri: str, to: str, call_id: str, from_tag: str, body: str = "",
                     extra: str = "", route: list[str] | None = None, cseq: int | None = None,
                     on_provisional: Callable[[Message], None] | None = None,
                     timeout: float = 10.0) -> Message | None:
        """Requête et réponse finale ; un défi 401/407 est relevé une fois avec l'empreinte."""
        auth = None
        for _ in range(2):
            if method == "REGISTER":
                self._register_cseq = getattr(self, "_register_cseq", 0) + 1
                number = self._register_cseq
            else:
                number = cseq if cseq is not None else 1
            branch = "z9hG4bK" + secrets.token_hex(8)
            waiter: queue.Queue = queue.Queue()
            self._waiters[f"{branch} {method}"] = waiter
            try:
                self._request(method, uri, to, call_id, from_tag, number, branch, body, extra, route, auth)
                response = self._final(waiter, timeout, on_provisional)
            finally:
                self._waiters.pop(f"{branch} {method}", None)
            if response is None or response.status not in (401, 407) or auth is not None:
                if response is not None and method == "INVITE" and response.status >= 300:
                    self._ack_failure(uri, response, from_tag, number, branch)
                return response
            if method == "INVITE":
                self._ack_failure(uri, response, from_tag, number, branch)
            proxy = response.status == 407
            challenge = response.header("proxy-authenticate" if proxy else "www-authenticate")
            auth = (digest(challenge, method, uri, self.user, self._password), proxy)
            if cseq is not None:
                cseq += 1
        return None

    def _final(self, waiter: queue.Queue, timeout: float, on_provisional) -> Message | None:
        until = time.monotonic() + timeout
        while (left := until - time.monotonic()) > 0:
            try:
                response = waiter.get(timeout=left)
            except queue.Empty:
                return None
            if response.status >= 200:
                return response
            if on_provisional is not None and response.status > 100:
                on_provisional(response)
                until = max(until, time.monotonic() + INVITE_TIMEOUT)  # ça sonne : on attend la décision
        return None

    def _ack_failure(self, uri: str, response: Message, from_tag: str, cseq: int, branch: str) -> None:
        """Un INVITE refusé se termine par un ACK de la même transaction (même branche, même numéro)."""
        lines = [f"ACK {uri} SIP/2.0", f"Via: SIP/2.0/{self._transport} {self.local_ip}:{self.local_port};branch={branch};rport",
                 "Max-Forwards: 70", f"From: <{self.aor}>;tag={from_tag}", f"To: {response.header('to')}",
                 f"Call-ID: {response.header('call-id')}", f"CSeq: {cseq} ACK", "Content-Length: 0", "", ""]
        try:
            self._send_raw("\r\n".join(lines).encode())
        except (OSError, SipError):
            pass

    # --- Appels sortants -----------------------------------------------------------------------------------

    def invite(self, sdp: str, on_ringing: Callable[[], None] | None = None,
               cancel: threading.Event | None = None) -> tuple[Dialog | None, int]:
        """Appelle votre adresse : (appel décroché, 200) ou (None, code) — 486/603 refusé, 487 annulé, 0 sans réponse.
        ``cancel`` levé pendant la sonnerie : l'appel est annulé (CANCEL)."""
        if not self.registered:
            return None, 0
        call_id, tag = secrets.token_hex(10), secrets.token_hex(4)
        state: dict = {}

        def provisional(response: Message) -> None:
            if response.status in (180, 183) and on_ringing is not None and not state.get("ringing"):
                state["ringing"] = True
                on_ringing()
            state["last"] = response

        if cancel is not None:
            threading.Thread(target=self._cancel_when, args=(cancel, call_id, tag, state), daemon=True).start()
        state["sent"] = True
        response = self._transaction("INVITE", self.peer, self.peer, call_id, tag, body=sdp, cseq=1,
                                     on_provisional=provisional, timeout=INVITE_TIMEOUT)
        state["done"] = True
        if response is None:
            self._send_cancel(call_id, tag, state)
            return None, 0
        if response.status >= 300:
            return None, response.status
        record = [r.strip() for value in response.all("record-route") for r in value.split(",") if r.strip()]
        dialog = Dialog(call_id, tag, target_of(response.header("contact")) or self.peer, response.header("to"),
                        list(reversed(record)), response.body, True, int(response.header("cseq").split()[0]),
                        local_sdp=sdp)
        self._dialogs[call_id] = dialog
        self._ack(dialog)
        return dialog, 200

    def _cancel_when(self, cancel: threading.Event, call_id: str, tag: str, state: dict) -> None:
        """Annule dès que le Core le demande, mais seulement une fois la sonnerie connue (une réponse provisoire
        reçue : sans elle, l'annulation n'aurait pas de transaction à viser)."""
        while not state.get("done"):
            if cancel.is_set() and state.get("last") is not None:
                self._send_cancel(call_id, tag, state)
                return
            time.sleep(0.05)

    def _send_cancel(self, call_id: str, tag: str, state: dict) -> None:
        last = state.get("last")
        if last is None:
            return
        branch = re.search(r"branch=([^;,\s]+)", last.header("via"))
        if branch is None:
            return
        lines = [f"CANCEL {self.peer} SIP/2.0", f"Via: SIP/2.0/{self._transport} {self.local_ip}:{self.local_port};branch={branch.group(1)};rport",
                 "Max-Forwards: 70", f"From: <{self.aor}>;tag={tag}", f"To: <{self.peer}>", f"Call-ID: {call_id}",
                 f"CSeq: {last.header('cseq').split()[0]} CANCEL", "Content-Length: 0", "", ""]
        try:
            self._send_raw("\r\n".join(lines).encode())
        except (OSError, SipError):
            pass

    def _ack(self, dialog: Dialog) -> None:
        """ACK d'un 200 : nouvelle transaction, même numéro que l'INVITE, par le chemin du dialogue."""
        branch = "z9hG4bK" + secrets.token_hex(8)
        self._request("ACK", dialog.remote_uri, dialog.to_header, dialog.call_id, dialog.local_tag, dialog.cseq,
                      branch, route=dialog.route)

    def hang_up(self, dialog: Dialog) -> None:
        if dialog.ended:
            return
        dialog.ended = True
        self._dialogs.pop(dialog.call_id, None)
        dialog.cseq += 1
        try:
            if dialog.outgoing:
                self._transaction("BYE", dialog.remote_uri, dialog.to_header, dialog.call_id, dialog.local_tag,
                                  route=dialog.route, cseq=dialog.cseq, timeout=5)
            else:
                self._transaction_in_dialog("BYE", dialog)
        except (OSError, SipError):
            pass

    # --- Appels entrants -----------------------------------------------------------------------------------

    def answer(self, dialog: Dialog, sdp: str) -> None:
        """Décroche un appel entrant (200 avec le SDP d'ORION)."""
        dialog.local_sdp = sdp
        self._respond(dialog.invite, 200, "OK", to_tag=dialog.local_tag, body=sdp, contact=True)

    def reject(self, dialog: Dialog, code: int = 486, reason: str = "Busy Here") -> None:
        dialog.ended = True
        self._dialogs.pop(dialog.call_id, None)
        self._respond(dialog.invite, code, reason, to_tag=dialog.local_tag)

    def _transaction_in_dialog(self, method: str, dialog: Dialog) -> None:
        """Requête de l'appelé (nous) dans un dialogue entrant : From/To inversés par rapport à l'INVITE reçu."""
        invite = dialog.invite
        branch = "z9hG4bK" + secrets.token_hex(8)
        route = [r.strip() for value in invite.all("record-route") for r in value.split(",") if r.strip()]
        lines = [f"{method} {dialog.remote_uri} SIP/2.0",
                 f"Via: SIP/2.0/{self._transport} {self.local_ip}:{self.local_port};branch={branch};rport",
                 "Max-Forwards: 70", f"From: {invite.header('to')};tag={dialog.local_tag}"
                 if "tag=" not in invite.header("to") else f"From: {invite.header('to')}",
                 f"To: {invite.header('from')}", f"Call-ID: {dialog.call_id}", f"CSeq: {dialog.cseq} {method}"]
        if route:
            lines.append(f"Route: {', '.join(route)}")
        lines += ["Content-Length: 0", "", ""]
        self._send_raw("\r\n".join(lines).encode())

    def _respond(self, request: Message, code: int, reason: str, to_tag: str = "", body: str = "",
                 contact: bool = False) -> None:
        to = request.header("to")
        if to_tag and "tag=" not in to:
            to = f"{to};tag={to_tag}"
        lines = [f"SIP/2.0 {code} {reason}"]
        lines += [f"Via: {v}" for v in request.all("via")]
        lines += [f"Record-Route: {r}" for r in request.all("record-route")]
        lines += [f"From: {request.header('from')}", f"To: {to}", f"Call-ID: {request.header('call-id')}",
                  f"CSeq: {request.header('cseq')}", "User-Agent: ORION"]
        if contact:
            lines.append(f"Contact: {self._contact}")
        if body:
            lines.append("Content-Type: application/sdp")
        text = "\r\n".join(lines) + f"\r\nContent-Length: {len(body.encode())}\r\n\r\n" + body
        self._send_raw(text.encode())

    # --- Lecture -------------------------------------------------------------------------------------------

    def _reader(self, sock: socket.socket) -> None:
        buffer = b""
        sock.settimeout(None)
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buffer += chunk
                while True:
                    buffer = buffer.lstrip(b"\r\n")  # signaux de maintien
                    end = buffer.find(b"\r\n\r\n")
                    if end < 0:
                        break
                    length = re.search(rb"(?im)^(?:content-length|l)\s*:\s*(\d+)", buffer[:end])
                    total = end + 4 + (int(length.group(1)) if length else 0)
                    if len(buffer) < total:
                        break
                    raw, buffer = buffer[:total], buffer[total:]
                    try:
                        self._dispatch(parse(raw))
                    except Exception:  # noqa: BLE001 - un message mal formé ne coupe pas la connexion
                        log.exception("Compte SIP : message ignoré")
        except OSError:
            pass
        if self._sock is sock:
            self._close()  # le fil principal se reconnecte

    def _dispatch(self, message: Message) -> None:
        if not message.is_request:
            branch = re.search(r"branch=([^;,\s]+)", message.header("via"))
            waiter = self._waiters.get(f"{branch.group(1)} {message.method}") if branch else None
            if waiter is not None:
                waiter.put(message)
                return
            dialog = self._dialogs.get(message.header("call-id"))
            if message.method == "INVITE" and message.status == 200 and dialog is not None and dialog.outgoing:
                # « Décroché » répété : notre ACK n'est pas arrivé. On le renvoie (sinon le téléphone raccroche).
                log.info("Compte SIP : décroché répété par le téléphone, ACK renvoyé")
                self._ack(dialog)
            return
        method, call_id = message.method, message.header("call-id")
        if method == "INVITE":
            self._incoming(message)
        elif method == "ACK":
            pass  # notre 200 a été reçu : rien à répondre
        elif method == "BYE":
            self._respond(message, 200, "OK")
            dialog = self._dialogs.pop(call_id, None)
            if dialog is not None and not dialog.ended:
                dialog.ended = True
                if self.on_ended is not None:
                    self.on_ended(dialog, "raccroché")
        elif method == "CANCEL":
            self._respond(message, 200, "OK")
            dialog = self._dialogs.pop(call_id, None)
            if dialog is not None and not dialog.ended and dialog.invite is not None:
                dialog.ended = True
                self._respond(dialog.invite, 487, "Request Terminated", to_tag=dialog.local_tag)
                if self.on_ended is not None:
                    self.on_ended(dialog, "annulé")
        elif method == "MESSAGE":
            self._incoming_message(message)
        elif method == "UPDATE" and call_id in self._dialogs:
            self._relaunched(self._dialogs[call_id], message)
        elif method in ("OPTIONS", "UPDATE", "INFO", "NOTIFY"):
            if call_id in self._dialogs:
                log.info("Compte SIP : %s reçu pendant l'appel", method)
            self._respond(message, 200, "OK")
        else:
            self._respond(message, 405, "Method Not Allowed")

    # --- Messages ------------------------------------------------------------------------------------------

    def send_message(self, text: str) -> bool:
        """Message texte vers votre adresse (réponse d'ORION) ; True s'il a été accepté par le serveur."""
        if not self.registered:
            return False
        response = self._transaction("MESSAGE", self.peer, self.peer, secrets.token_hex(10), secrets.token_hex(4),
                                     body=text, extra="Content-Type: text/plain;charset=UTF-8\r\n", cseq=1)
        return response is not None and 200 <= response.status < 300

    def _incoming_message(self, message: Message) -> None:
        if uri_of(message.header("from")) != self.peer:
            log.warning("Compte SIP : message d'un correspondant non autorisé refusé")
            self._respond(message, 403, "Forbidden")
            return
        self._respond(message, 200, "OK")
        text, kind = message_text(message.header("content-type"), message.body)
        if text is None:
            if kind not in ("application/im-iscomposing+xml", "message/imdn+xml"):  # « en train d'écrire », accusés
                log.info("Compte SIP : message de type %s ignoré (illisible pour ORION)", kind)
                self._unreadable_notice()
            return
        if self.on_message is not None and text.strip():
            self.on_message(text.strip())

    def _unreadable_notice(self) -> None:
        """Photo, fichier, message vocal (« application/vnd.gsma.rcs-ft-http+xml » chez Linphone) : ORION le dit au
        lieu de se taire, une fois par minute au plus. Envoi à part : ce fil lit aussi la réponse du serveur."""
        now = time.monotonic()
        if now - self._unreadable_at < 60:
            return
        self._unreadable_at = now
        threading.Thread(target=self.send_message, args=(UNREADABLE,), name="sip-unreadable", daemon=True).start()

    def _relaunched(self, dialog: "Dialog", request: Message) -> None:
        """Ré-INVITE ou UPDATE de l'autre côté pendant l'appel : réponse avec notre son (inchangé) ; sa nouvelle
        offre (adresse, clé) est transmise au son de l'appel (``on_update``)."""
        log.info("Compte SIP : %s reçu pendant l'appel (%s)", request.method,
                 "nouvelle offre de son" if request.body.strip() else "sans offre")
        self._respond(request, 200, "OK", to_tag=dialog.local_tag, body=dialog.local_sdp, contact=True)
        if request.body.strip():
            dialog.remote_sdp = request.body
            if self.on_update is not None:
                try:
                    self.on_update(dialog)
                except Exception:  # noqa: BLE001 - le son garde son ancienne adresse, l'appel continue
                    log.exception("Compte SIP : relance de l'appel non appliquée au son")

    def _incoming(self, invite: Message) -> None:
        call_id = invite.header("call-id")
        if call_id in self._dialogs:  # nouvel INVITE dans un appel en cours (relance, maintien) : même son
            self._relaunched(self._dialogs[call_id], invite)
            return
        caller = uri_of(invite.header("from"))
        if caller != self.peer:
            log.warning("Compte SIP : appel d'un correspondant non autorisé refusé")
            self._respond(invite, 403, "Forbidden", to_tag=secrets.token_hex(4))
            return
        dialog = Dialog(call_id, secrets.token_hex(4), target_of(invite.header("contact")) or caller,
                        invite.header("from"), remote_sdp=invite.body, outgoing=False, invite=invite)
        self._dialogs[call_id] = dialog
        self._respond(invite, 180, "Ringing", to_tag=dialog.local_tag)
        if self.on_incoming is not None:
            self.on_incoming(dialog)
        else:
            self.reject(dialog)


UNREADABLE = ("Je ne lis que le texte, monsieur : les photos, fichiers et messages vocaux ne me parviennent pas. "
              "Écrivez-moi votre demande en quelques mots.")


def message_text(content_type: str, body: str) -> tuple[str | None, str]:
    """(texte, type) d'un message SIP : texte simple, ou enveloppe CPIM de Linphone (RFC 3862) ; (None, type) sinon
    (indication « en train d'écrire », accusé de lecture, message chiffré de bout en bout…)."""
    kind = (content_type or "text/plain").split(";")[0].strip().lower()
    if kind == "text/plain":
        return body, kind
    if kind == "message/cpim":
        _, _, rest = body.replace("\r\n", "\n").partition("\n\n")  # en-têtes CPIM
        mime, _, content = rest.partition("\n\n")  # en-têtes du contenu
        inner = next((line.split(":", 1)[1] for line in mime.split("\n") if line.lower().startswith("content-type:")),
                     "text/plain")
        inner = inner.split(";")[0].strip().lower()
        return (content, inner) if inner == "text/plain" else (None, inner)
    return None, kind


class SipError(Exception):
    pass
