"""Sonde SIP d'ORION vers Linphone (prototype isolé, hors production) : enregistrement, puis appel sans son.

    python research/sip/probe.py register                 # ORION s'enregistre sur sip.linphone.org (TLS)
    python research/sip/probe.py ring sip:vous@sip.linphone.org [--seconds 25]
                                                          # fait sonner l'adresse, puis raccroche ; affiche la
                                                          # réponse SDP (chiffrement du son, adresses : NAT)

Compte d'ORION : LINPHONE_ORION_USER (défaut « orionproject ») et LINPHONE_ORION_PASSWORD dans .env. Le mot de passe
n'est jamais affiché ni journalisé : seules les lignes de statut et les en-têtes sans secret le sont.
Bibliothèque standard seulement (socket, ssl, hashlib) : SIP sur TLS, authentification par empreinte (RFC 2617/7616).
"""

from __future__ import annotations

import argparse
import hashlib
import re
import secrets
import socket
import ssl
import sys
import time
from pathlib import Path

DOMAIN = "sip.linphone.org"
PORT = 5061
SECRET_HEADERS = ("authorization", "proxy-authorization")


def env(name: str, default: str = "") -> str:
    path = Path(__file__).resolve().parents[2] / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.strip() == name:
                return value.strip().strip('"').strip("'")
    return default


class SipTls:
    """Connexion SIP sur TLS : envoi de requêtes, lecture des messages (en-têtes + corps, Content-Length)."""

    def __init__(self, host: str = DOMAIN, port: int = PORT):
        # IPv4 d'abord (le son passera par la même famille d'adresses ; IPv6 reste possible, entre crochets).
        address = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4]
        raw = socket.create_connection(address, timeout=10)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        ip, self.local_port = self.sock.getsockname()[:2]
        self.local_ip = f"[{ip}]" if ":" in ip else ip  # SIP : une adresse IPv6 s'écrit entre crochets
        self._buffer = b""

    def send(self, message: str) -> None:
        self.sock.sendall(message.encode())

    def receive(self, timeout: float = 10.0) -> tuple[str, dict, str] | None:
        self.sock.settimeout(timeout)
        while b"\r\n\r\n" not in self._buffer:
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, TimeoutError):
                return None
            if not chunk:
                return None
            self._buffer += chunk
        head, _, rest = self._buffer.partition(b"\r\n\r\n")
        lines = head.decode(errors="replace").split("\r\n")
        headers: dict[str, list[str]] = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers.setdefault(name.strip().lower(), []).append(value.strip())
        length = int((headers.get("content-length") or headers.get("l") or ["0"])[0])
        while len(rest) < length:
            rest += self.sock.recv(65536)
        body, self._buffer = rest[:length], rest[length:]
        return lines[0], headers, body.decode(errors="replace")


def digest(challenge: str, method: str, uri: str, user: str, password: str) -> str:
    fields = dict(re.findall(r'(\w+)="?([^",]*)"?', challenge))
    realm, nonce = fields.get("realm", DOMAIN), fields["nonce"]
    algorithm = fields.get("algorithm", "MD5").upper()
    hash_ = hashlib.sha256 if algorithm.startswith("SHA-256") else hashlib.md5
    h = lambda text: hash_(text.encode()).hexdigest()  # noqa: E731
    ha1, ha2 = h(f"{user}:{realm}:{password}"), h(f"{method}:{uri}")
    parts = [f'username="{user}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"', f"algorithm={algorithm}"]
    if "auth" in fields.get("qop", "").split(","):
        nc, cnonce = "00000001", secrets.token_hex(8)
        response = h(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        parts += ["qop=auth", f"nc={nc}", f'cnonce="{cnonce}"']
    else:
        response = h(f"{ha1}:{nonce}:{ha2}")
    parts.append(f'response="{response}"')
    if "opaque" in fields:
        parts.append(f'opaque="{fields["opaque"]}"')
    return "Digest " + ", ".join(parts)


def show(label: str, message) -> None:
    if message is None:
        print(f"{label} : aucune réponse (délai dépassé)")
        return
    status, headers, body = message
    print(f"{label} : {status}")
    for name in ("www-authenticate", "proxy-authenticate", "contact", "expires", "reason", "warning"):
        for value in headers.get(name, []):
            if name == "contact":  # adresses et jetons de notification des appareils : jamais affichés
                value = re.sub(r";pn-[^;>]*", ";pn-…", re.sub(r"@[^;>]*", "@…", value))
            print(f"    {name} : {value[:160]}")


class Session:
    def __init__(self, user: str, password: str):
        self.user, self.password = user, password
        self.link = SipTls()
        self.tag = secrets.token_hex(4)
        self.call_id = secrets.token_hex(10)
        self.cseq = 0

    def headers(self, method: str, to: str, branch: str, extra: str = "") -> str:
        self.cseq += 1
        via = f"SIP/2.0/TLS {self.link.local_ip}:{self.link.local_port};branch=z9hG4bK{branch};rport"
        return (f"Via: {via}\r\nMax-Forwards: 70\r\nFrom: <sip:{self.user}@{DOMAIN}>;tag={self.tag}\r\n"
                f"To: <{to}>\r\nCall-ID: {self.call_id}\r\nCSeq: {self.cseq} {method}\r\n"
                f"Contact: <sip:{self.user}@{self.link.local_ip}:{self.link.local_port};transport=tls>\r\n"
                f"User-Agent: ORION-sonde\r\n{extra}")

    def request(self, method: str, uri: str, to: str, body: str = "", extra: str = "", auth: str = "") -> None:
        content = f"Content-Type: application/sdp\r\n" if body else ""
        authorization = f"Authorization: {auth}\r\n" if auth else ""
        self.link.send(f"{method} {uri} SIP/2.0\r\n{self.headers(method, to, secrets.token_hex(6), extra)}"
                       f"{authorization}{content}Content-Length: {len(body.encode())}\r\n\r\n{body}")

    def authenticated(self, method: str, uri: str, to: str, body: str = "", extra: str = ""):
        """Requête, puis la même avec l'empreinte si le serveur la demande (401/407)."""
        self.request(method, uri, to, body, extra)
        reply = self.final_or_provisional()
        if reply and reply[0].split()[1] in ("401", "407"):
            show(f"{method} (défi)", reply)
            challenge = (reply[1].get("www-authenticate") or reply[1].get("proxy-authenticate"))[0]
            self.request(method, uri, to, body, extra, auth=digest(challenge, method, uri, self.user, self.password))
            reply = self.final_or_provisional()
        return reply

    def final_or_provisional(self, timeout: float = 10.0):
        message = self.link.receive(timeout)
        while message is not None and message[0].startswith("SIP/2.0 100"):
            message = self.link.receive(timeout)
        return message


def main() -> int:
    parser = argparse.ArgumentParser(prog="python research/sip/probe.py")
    parser.add_argument("action", choices=["register", "ring"])
    parser.add_argument("target", nargs="?", help="adresse appelée (ring)")
    parser.add_argument("--seconds", type=float, default=25.0)
    args = parser.parse_args()
    user, password = env("LINPHONE_ORION_USER", "orionproject"), env("LINPHONE_ORION_PASSWORD")
    if not password:
        print("LINPHONE_ORION_PASSWORD absent de .env")
        return 2
    session = Session(user, password)
    print(f"Connexion TLS à {DOMAIN}:{PORT} établie (local {session.link.local_ip}:{session.link.local_port})")
    reply = session.authenticated("REGISTER", f"sip:{DOMAIN}", f"sip:{user}@{DOMAIN}", extra="Expires: 300\r\n")
    show("REGISTER", reply)
    if not reply or not reply[0].startswith("SIP/2.0 200"):
        return 1
    if args.action == "register":
        show("REGISTER (retrait)", session.authenticated("REGISTER", f"sip:{DOMAIN}", f"sip:{user}@{DOMAIN}",
                                                          extra="Expires: 0\r\n"))
        return 0
    if not args.target:
        print("Adresse à appeler manquante")
        return 2
    sdp = (f"v=0\r\no=orion 1 1 IN IP4 {session.link.local_ip}\r\ns=ORION\r\nc=IN IP4 {session.link.local_ip}\r\n"
           f"t=0 0\r\nm=audio 40000 RTP/AVP 0 101\r\na=rtpmap:0 PCMU/8000\r\na=rtpmap:101 telephone-event/8000\r\n"
           f"a=sendrecv\r\n")
    session.call_id, session.cseq = secrets.token_hex(10), 0
    reply = session.authenticated("INVITE", args.target, args.target, body=sdp)
    invite_cseq = session.cseq
    started = time.monotonic()
    while reply is not None and int(reply[0].split()[1]) < 200 and time.monotonic() - started < args.seconds:
        show("INVITE", reply)
        reply = session.link.receive(timeout=max(1.0, args.seconds - (time.monotonic() - started)))
    if reply is None or int(reply[0].split()[1]) < 200:  # personne n'a décroché : la sonnerie est annulée
        session.cseq = invite_cseq - 1
        session.request("CANCEL", args.target, args.target)
        show("CANCEL", session.final_or_provisional())
        show("INVITE (final)", session.final_or_provisional())
        return 0
    show("INVITE (final)", reply)
    status, headers, body = reply
    to = headers.get("to", [f"<{args.target}>"])[0]
    remote = re.search(r"<([^>]+)>", (headers.get("contact") or [f"<{args.target}>"])[0]).group(1)
    if status.startswith("SIP/2.0 200"):
        session.cseq = invite_cseq - 1
        session.request("ACK", remote, to)
        print("Réponse SDP de l'iPhone (chiffrement du son ; adresses masquées) :")
        print("    " + re.sub(r"\d+\.\d+\.\d+\.\d+", "x.x.x.x", body).replace("\r\n", "\n    "))
        time.sleep(3)
        session.request("BYE", remote, to)
        show("BYE", session.final_or_provisional())
    return 0


if __name__ == "__main__":
    sys.exit(main())
