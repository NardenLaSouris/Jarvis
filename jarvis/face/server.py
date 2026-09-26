"""Petit serveur local du visage : sert la page et diffuse l'état visuel (Server-Sent Events).

Lecture seule : aucune commande ne peut être envoyée à JARVIS par cette interface.
Bibliothèque standard uniquement ; si le port est pris, JARVIS continue sans visage.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jarvis.face.state import VisualState

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/face.css": ("face.css", "text/css; charset=utf-8"),
    "/face.js": ("face.js", "text/javascript; charset=utf-8"),
}


class _ExclusiveServer(ThreadingHTTPServer):
    """Port réservé en exclusivité : sous Windows, la réutilisation d'adresse laisserait deux
    serveurs écouter le même port sans erreur."""

    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


class FaceServer:
    def __init__(self, visual: VisualState, host: str = "127.0.0.1", port: int = 8765, rate_hz: float = 30.0):
        self.visual = visual
        self._host = host
        self._port = port
        self._period = 1.0 / rate_hz
        self._stopping = threading.Event()
        self._httpd: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str | None:
        if self._httpd is None:
            return None
        host, port = self._httpd.server_address[:2]
        host = "127.0.0.1" if host in ("0.0.0.0", "") else host
        return f"http://{host}:{port}/"

    def start(self) -> str | None:
        try:
            self._httpd = _ExclusiveServer((self._host, self._port), self._handler())
        except OSError as exc:
            log.warning("Visage indisponible (%s:%s) : %s. JARVIS continue sans interface.", self._host, self._port, exc)
            return None
        threading.Thread(target=self._httpd.serve_forever, name="visage", daemon=True).start()
        return self.url

    def stop(self) -> None:
        self._stopping.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/events":
                    return self._events()
                if path == "/state":
                    return self._send(json.dumps(server.visual.snapshot()).encode(), "application/json")
                if path in FILES:
                    name, kind = FILES[path]
                    return self._send((STATIC_DIR / name).read_bytes(), kind)
                self.send_error(404)

            def _send(self, body: bytes, kind: str):
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def _events(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                try:
                    while not server._stopping.is_set():
                        data = json.dumps(server.visual.snapshot())
                        self.wfile.write(f"data: {data}\n\n".encode())
                        self.wfile.flush()
                        server._stopping.wait(server._period)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    return

            def log_message(self, fmt, *args):
                log.debug("Visage : " + fmt, *args)

        return Handler
