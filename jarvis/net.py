"""Briques HTTP communes aux petits serveurs d'ORION (agent Windows, API du Core) : IP du client, jeton
partagé comparé à temps constant, réponses JSON."""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)


def client_ip(address: str) -> str:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    mapped = getattr(ip, "ipv4_mapped", None)
    return str(mapped or ip)


def bearer_ok(header: str, token: str) -> bool:
    scheme, _, given = header.partition(" ")
    return bool(token) and scheme == "Bearer" and hmac.compare_digest(given.encode(), token.encode())


class JsonHandler(BaseHTTPRequestHandler):
    sys_version = ""

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, error: str, message: str = "") -> None:
        self._drain()
        self._json(status, {"status": "error", "error": error, **({"message": message} if message else {})})

    def _drain(self, limit: int = 1_000_000) -> None:
        """Corps non lu (refus, adresse inconnue) : lu et jeté avant de répondre, sinon le client qui l'envoie encore
        voit sa connexion coupée (Windows : « connexion abandonnée ») au lieu de recevoir l'erreur."""
        if getattr(self, "_body_read", False) or self.command not in ("POST", "PUT", "PATCH"):
            return
        self._body_read = True
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            return
        if 0 < length <= limit:
            self.rfile.read(length)

    def _body(self, limit: int) -> bytes | None:
        """Corps de la requête (au plus ``limit`` octets) ; None après avoir répondu 400 ou 413."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(400, "bad_request")
            return None
        if not 0 <= length <= limit:
            self._body_read = True  # trop gros : non lu, la connexion sera fermée
            self._error(413, "body_too_large")
            return None
        self._body_read = True
        return self.rfile.read(length)

    def log_message(self, fmt, *args):
        log.debug(f"{self.server_version} : " + fmt, *args)


class ExclusiveServer(ThreadingHTTPServer):
    """Port réservé en exclusivité : sous Windows, la réutilisation d'adresse laisserait deux
    serveurs écouter le même port sans erreur ; ailleurs, elle permet seulement de rouvrir le port
    juste après un arrêt (TIME_WAIT)."""

    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True
    # File d'attente des connexions : 5 par défaut ; au-delà, des connexions étaient réinitialisées dès une vingtaine
    # de clients simultanés (ORION Control, visages, routines). Mesuré lors du test de charge.
    request_queue_size = 128

    def handle_error(self, request, client_address):
        """Client parti en pleine réponse (navigateur fermé, visage rechargé) : une ligne au lieu d'une trace complète."""
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionResetError, BrokenPipeError, TimeoutError)):
            log.debug("Client %s : connexion interrompue (%s)", client_address[0], type(error).__name__)
            return
        super().handle_error(request, client_address)

    def server_bind(self):
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()
