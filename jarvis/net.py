"""Briques HTTP communes aux petits serveurs de JARVIS (agent Windows, API du Core) : IP du client, jeton
partagé comparé à temps constant, réponses JSON."""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
from http.server import BaseHTTPRequestHandler

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
        self._json(status, {"status": "error", "error": error, **({"message": message} if message else {})})

    def _body(self, limit: int) -> bytes | None:
        """Corps de la requête (au plus ``limit`` octets) ; None après avoir répondu 400 ou 413."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(400, "bad_request")
            return None
        if not 0 <= length <= limit:
            self._error(413, "body_too_large")
            return None
        return self.rfile.read(length)

    def log_message(self, fmt, *args):
        log.debug(f"{self.server_version} : " + fmt, *args)
