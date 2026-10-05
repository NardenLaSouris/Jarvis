"""Petit serveur local du visage : sert la page et diffuse l'état visuel (Server-Sent Events).

Lecture seule : aucune commande ne peut être envoyée à JARVIS par cette interface.
Bibliothèque standard uniquement ; si le port est pris, JARVIS continue sans visage.
"""

from __future__ import annotations

import json
import logging
import socket
import sys
import threading
from datetime import datetime, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from jarvis.face.state import VisualState
from jarvis.face.themes import COLORS, SEASONS, FaceThemeStore, jarvis_age, season_of

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/face.css": ("face.css", "text/css; charset=utf-8"),
    "/face.js": ("face.js", "text/javascript; charset=utf-8"),
}


def night_hours(start: str, end: str) -> tuple[time, time] | None:
    """« 22:00 », « 07:00 » -> plage du thème nuit ; None si l'un des deux est vide (jamais de nuit)."""
    if not start or not end:
        return None
    try:
        return datetime.strptime(start, "%H:%M").time(), datetime.strptime(end, "%H:%M").time()
    except ValueError as exc:
        raise ValueError(f"Horaire du thème nuit invalide (HH:MM attendu) : {start!r}, {end!r}") from exc


def is_night(now: time, hours: tuple[time, time] | None) -> bool:
    if hours is None:
        return False
    start, end = hours
    return start <= now or now < end if start > end else start <= now < end


class ExclusiveServer(ThreadingHTTPServer):
    """Port réservé en exclusivité : sous Windows, la réutilisation d'adresse laisserait deux
    serveurs écouter le même port sans erreur ; ailleurs, elle permet seulement de rouvrir le port
    juste après un arrêt (TIME_WAIT)."""

    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True
    # File d'attente des connexions : 5 par défaut ; au-delà, des connexions étaient réinitialisées dès une vingtaine
    # de clients simultanés (JARVIS Control, visages, routines). Mesuré lors du test de charge.
    request_queue_size = 128

    def server_bind(self):
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


class FaceServer:
    def __init__(self, visual: VisualState, host: str = "127.0.0.1", port: int = 8765, rate_hz: float = 30.0,
                 night: tuple[time, time] | None = None, clock: Callable[[], datetime] = datetime.now,
                 themes: FaceThemeStore | None = None, birthday: tuple[int, int] | None = None,
                 seasons: bool = True, born=None):
        self.visual = visual
        self._themes = themes
        self._birthday, self._seasons, self._born = birthday, seasons, born
        self._night = night
        self._clock = clock
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
            self._httpd = ExclusiveServer((self._host, self._port), self._handler())
        except OSError as exc:
            log.warning("Visage indisponible (%s:%s) : %s. JARVIS continue sans interface.", self._host, self._port, exc)
            return None
        threading.Thread(target=self._httpd.serve_forever, name="visage", daemon=True).start()
        return self.url

    def payload(self) -> dict:
        """État visuel diffusé aux pages, avec le thème choisi par l'horloge du Core (même thème partout)."""
        snapshot = self.visual.snapshot()
        chosen = self._themes.get() if self._themes is not None else "auto"
        now = self._clock()
        if chosen == "auto":
            # Jour de fête : son thème ; sinon bleu le jour, noir et blanc la nuit.
            base = (season_of(now.date(), self._birthday, self._born) if self._seasons else None) \
                or ("night" if is_night(now.time(), self._night) else "day")
        else:
            base = chosen
        season = SEASONS.get(base, {})
        greeting = season.get("greeting", "")
        age = jarvis_age(now.date(), self._born)
        if base == "naissancejarvis" and age:
            greeting = f"Joyeux anniversaire JARVIS : {age} an{'s' if age > 1 else ''}"
        # En erreur : thème rouge ; la page revient d'elle-même au thème de base quand l'erreur cesse. Pour une
        # couleur choisie, la page construit la palette à partir de sa teinte (« hue ») ; pour une fête, des
        # teintes qui alternent (« hues »), un message (« greeting ») et un effet animé (« effect »).
        return {**snapshot, "theme": "error" if snapshot.get("error") else base, "base_theme": base,
                "hue": COLORS.get(base), "hues": season.get("hues"), "palette": season.get("palette"),
                "greeting": greeting,
                "effect": season.get("effect", "")}

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
                    return self._send(json.dumps(server.payload()).encode(), "application/json")
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
                        data = json.dumps(server.payload())
                        self.wfile.write(f"data: {data}\n\n".encode())
                        self.wfile.flush()
                        server._stopping.wait(server._period)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    return

            def log_message(self, fmt, *args):
                log.debug("Visage : " + fmt, *args)

        return Handler
