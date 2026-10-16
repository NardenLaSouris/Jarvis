"""Mode fond d'écran : page « tableau de bord » d'ORION (statistiques de tout l'environnement, horloge, météo,
agenda...), avec le visage d'ORION intégré au centre (sa propre page, sur son propre port).

Pour Wallpaper Engine (fond d'écran « Web », adresse http://<mini-PC>:8767/) ou n'importe quel navigateur.
Réservée aux adresses de [wallpaper] allowed_ips (elle affiche l'agenda et les rappels) ; bibliothèque standard.

Interactions (POST) :
- ``/action`` : commandes rapides, liste fermée d'outils sans risque (lumières, lecture Spotify), exécutées par le
  Core des outils (permissions, journal) au nom du propriétaire ; rien d'autre n'est accepté ;
- ``/say`` : demande écrite à ORION, traitée par sa boucle comme vos messages Linphone (jarvis.wallpaper.inbox).
Chaque POST exige le jeton de la page (``X-Orion-Token``, lu par /config, illisible depuis un autre site) et une
origine identique à la page : un site ouvert sur le PC ne peut pas agir à votre place.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from orion.net import ExclusiveServer
from orion.wallpaper.collector import Collector

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
# Commandes rapides permises : réversibles, sans confirmation, sans effet hors de la maison.
ACTIONS = {"light_on", "light_off", "light_toggle", "set_brightness", "spotify_play", "spotify_pause",
           "spotify_next", "spotify_previous"}
MAX_BODY = 4096
PER_MINUTE = 30  # actions et demandes par minute au plus (clics en rafale, page qui s'emballe)


class WallpaperServer:
    def __init__(self, collector: Collector, host: str = "0.0.0.0", port: int = 8767, face_port: int = 8765,
                 allowed_ips: tuple[str, ...] = (), tools=None, user: str = "", inbox=None):
        self.collector = collector
        self._host, self._port, self._face_port = host, port, face_port
        self._allowed = frozenset(allowed_ips) | {"127.0.0.1", "::1"}
        self._tools, self._user, self._inbox = tools, user, inbox
        self.token = secrets.token_urlsafe(24)  # nouveau à chaque démarrage, lu par la page elle-même
        self._recent: deque[float] = deque()
        self._lock = threading.Lock()
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

    def _throttled(self) -> bool:
        now = time.monotonic()
        with self._lock:
            while self._recent and now - self._recent[0] > 60:
                self._recent.popleft()
            if len(self._recent) >= PER_MINUTE:
                return True
            self._recent.append(now)
            return False

    def action(self, data: dict) -> tuple[int, dict]:
        tool, parameters = data.get("tool"), data.get("parameters") or {}
        if self._tools is None or tool not in ACTIONS or not isinstance(parameters, dict) \
                or not self._tools.registry.exists(tool):
            return 403, {"error": "action non permise depuis le fond d'écran"}
        if self._tools.registry.get(tool).requires_confirmation:
            # Rendue « à confirmer » par la configuration : jamais depuis cette page (la question remplacerait celle
            # d'une conversation en cours).
            return 409, {"error": "confirmation requise : demandez-le à ORION"}
        from orion import trace

        with trace.request():  # une action du fond d'écran : un identifiant de demande (journal)
            outcome = self._tools.submit({"type": "tool_call", "tool": tool, "parameters": parameters},
                                         user=self._user or None, origin="user")
        result = outcome.result
        if result is None:
            return 409, {"error": "confirmation requise : demandez-le à ORION"}
        log.info("Fond d'écran : %s %s -> %s", tool, parameters, "fait" if result.success else result.error)
        return 200, {"success": result.success, "message": result.message}

    def say(self, data: dict) -> tuple[int, dict]:
        if self._inbox is None:
            return 403, {"error": "conversation indisponible"}
        reply = self._inbox.ask(str(data.get("text") or ""))
        if reply is None:
            return 503, {"error": "ORION est occupée : réessayez dans un instant."}
        return 200, {"reply": reply}

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if not server.allowed(self.client_address[0]):
                    return self.send_error(403)
                path = self.path.split("?", 1)[0]
                if path == "/stats":
                    return self._json(200, server.collector.snapshot())
                if path == "/config":
                    return self._json(200, {"face_port": server._face_port, "token": server.token,
                                            "actions": sorted(ACTIONS) if server._tools is not None else [],
                                            "chat": server._inbox is not None})
                if path == "/chat":
                    return self._json(200, server._inbox.recent() if server._inbox is not None else [])
                if path in FILES:
                    file, kind = FILES[path]
                    return self._send(file.read_bytes(), kind)
                self.send_error(404)

            def do_POST(self):  # noqa: N802
                if not server.allowed(self.client_address[0]):
                    return self.send_error(403)
                host = self.headers.get("Host", "")
                origin = self.headers.get("Origin")
                token = self.headers.get("X-Orion-Token", "")
                if (origin is not None and origin != f"http://{host}") or \
                        not hmac.compare_digest(token.encode(), server.token.encode()):
                    return self._json(403, {"error": "refusé"})
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= MAX_BODY or "application/json" not in self.headers.get("Content-Type", ""):
                    return self._json(400, {"error": "demande invalide"})
                try:
                    data = json.loads(self.rfile.read(length))
                except ValueError:
                    return self._json(400, {"error": "demande invalide"})
                if not isinstance(data, dict):
                    return self._json(400, {"error": "demande invalide"})
                if server._throttled():
                    return self._json(429, {"error": "trop de demandes, un instant"})
                path = self.path.split("?", 1)[0]
                if path == "/action":
                    return self._json(*server.action(data))
                if path == "/say":
                    return self._json(*server.say(data))
                self.send_error(404)

            def _json(self, status: int, data) -> None:
                self._send(json.dumps(data, default=str, ensure_ascii=False).encode(), "application/json", status)

            def _send(self, body: bytes, kind: str, status: int = 200):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                log.debug("Fond d'écran : " + fmt, *args)

        return Handler
