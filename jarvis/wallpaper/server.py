"""Mode fond d'écran : page « tableau de bord » d'ORION (statistiques de tout l'environnement, horloge, météo,
agenda...), avec le visage d'ORION intégré tel quel au centre (sa propre page, sur son propre port).

Pour Wallpaper Engine (fond d'écran « Web », adresse http://<mini-PC>:8767/) ou n'importe quel navigateur.
Lecture seule : aucune commande ne peut être envoyée à ORION par cette page. Réservée aux adresses de
[wallpaper] allowed_ips (elle affiche l'agenda et les rappels) ; bibliothèque standard uniquement.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from jarvis.net import ExclusiveServer
from jarvis.wallpaper.collector import Collector

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
FACE_STATIC = Path(__file__).resolve().parent.parent / "face" / "static"
FILES = {
    "/": (STATIC_DIR / "index.html", "text/html; charset=utf-8"),
    "/index.html": (STATIC_DIR / "index.html", "text/html; charset=utf-8"),
    "/wallpaper.css": (STATIC_DIR / "wallpaper.css", "text/css; charset=utf-8"),
    "/wallpaper.js": (STATIC_DIR / "wallpaper.js", "text/javascript; charset=utf-8"),
    "/michroma.woff2": (FACE_STATIC / "michroma.woff2", "font/woff2"),  # police du visage (SIL OFL)
}


class WallpaperServer:
    def __init__(self, collector: Collector, host: str = "0.0.0.0", port: int = 8767, face_port: int = 8765,
                 allowed_ips: tuple[str, ...] = ()):
        self.collector = collector
        self._host, self._port, self._face_port = host, port, face_port
        self._allowed = frozenset(allowed_ips) | {"127.0.0.1", "::1"}
        self._httpd = None

    def allowed(self, ip: str) -> bool:
        return ip.removeprefix("::ffff:") in self._allowed

    def start(self) -> str | None:
        try:
            self._httpd = ExclusiveServer((self._host, self._port), self._handler())
        except OSError as exc:
            log.warning("Fond d'écran indisponible (%s:%s) : %s", self._host, self._port, exc)
            return None
        self.collector.start()
        threading.Thread(target=self._httpd.serve_forever, name="fond-ecran-http", daemon=True).start()
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/"

    def stop(self) -> None:
        self.collector.stop()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if not server.allowed(self.client_address[0]):
                    return self.send_error(403)
                path = self.path.split("?", 1)[0]
                if path == "/stats":
                    return self._send(json.dumps(server.collector.snapshot(), default=str).encode(),
                                      "application/json")
                if path == "/config":
                    return self._send(json.dumps({"face_port": server._face_port}).encode(), "application/json")
                if path in FILES:
                    file, kind = FILES[path]
                    return self._send(file.read_bytes(), kind)
                self.send_error(404)

            def _send(self, body: bytes, kind: str):
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                log.debug("Fond d'écran : " + fmt, *args)

        return Handler
