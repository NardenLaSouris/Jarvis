"""Musique Spotify jouée comme la voix de JARVIS : librespot (appareil Spotify Connect « JARVIS » sur le Core) écrit
le son dans un tube nommé ; ``MusicRelay`` le lit et l'envoie à l'agent du PC (``/audio/music``), qui le joue à côté
de la voix sur la même sortie.

- PCM int16 stéréo 44,1 kHz, morceaux de 100 ms ; l'agent bloque l'envoi quand sa file est pleine, ce qui cale la
  lecture du tube (et donc librespot) sur le temps réel ;
- la musique baisse pendant une conversation et pendant que JARVIS parle (``DuckingSink``), puis remonte ;
- « pause » vide aussitôt la file de l'agent (``stop``).
"""

from __future__ import annotations

import http.client
import logging
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np

log = logging.getLogger(__name__)

RATE, CHANNELS = 44100, 2
CHUNK = RATE * CHANNELS * 2 // 10  # 100 ms


class MusicRelay:
    def __init__(self, pipe: Path, agent_url: str, token: str, duck_percent: int = 15, restore_after: float = 0.8):
        self._pipe = Path(pipe)
        parts = urlsplit(agent_url)
        self._host, self._port = parts.hostname, parts.port or 80
        self._headers = {"Authorization": f"Bearer {token.strip()}", "Content-Type": "application/octet-stream"}
        self._duck_percent = duck_percent
        self._restore_after = restore_after
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._conversation = False
        self._speaking_until = 0.0
        self._level = 100
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    # --- Commandes vers l'agent ------------------------------------------------------------------------

    def _post(self, path: str, body: bytes = b"", timeout: float = 3.0, connection=None) -> None:
        conn = connection or http.client.HTTPConnection(self._host, self._port, timeout=timeout)
        try:
            conn.request("POST", path, body=body, headers=self._headers)
            response = conn.getresponse()
            response.read()
            if response.status >= 400:
                raise OSError(f"agent : {response.status}")
        finally:
            if connection is None:
                conn.close()

    def _command(self, path: str) -> None:
        try:
            self._post(path)
        except OSError as exc:
            log.debug("Musique : %s impossible (%s)", path, exc)

    def stop(self) -> None:
        """Pause demandée : la musique déjà envoyée à l'agent s'arrête aussitôt."""
        self._command("/audio/music/stop")

    def _apply(self) -> None:
        with self._lock:
            ducked = self._conversation or time.monotonic() < self._speaking_until
            level = self._duck_percent if ducked else 100
            if level == self._level:
                return
            self._level = level
        self._command(f"/audio/music/duck?percent={level}")

    def on_event(self, kind: str) -> None:
        """Événements de l'agent : la musique baisse dès le wake word, remonte au retour en veille."""
        if kind in ("wake", "listening"):
            self._conversation = True
        elif kind == "sleep":
            self._conversation = False
        else:
            return
        self._apply()

    def speaking(self, seconds: float) -> None:
        """JARVIS va parler ``seconds`` secondes (réponse, annonce) : musique baissée jusqu'à la fin."""
        with self._lock:
            self._speaking_until = max(self._speaking_until, time.monotonic()) + seconds + self._restore_after
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._speaking_until - time.monotonic() + 0.05, self._apply)
            self._timer.daemon = True
            self._timer.start()
        self._apply()

    # --- Relais du tube --------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            if not self._pipe.exists():
                self._pipe.parent.mkdir(parents=True, exist_ok=True)
                os.mkfifo(self._pipe)
            self._stopping.clear()
            self._thread = threading.Thread(target=self._run, name="musique", daemon=True)
            self._thread.start()
            log.info("Musique Spotify : tube %s relayé vers l'agent %s:%s", self._pipe, self._host, self._port)

    def stop_service(self) -> None:
        self._stopping.set()

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                with open(self._pipe, "rb", buffering=0) as pipe:  # attend que librespot ouvre le tube
                    self._relay(pipe)
            except OSError as exc:
                log.warning("Musique : tube illisible (%s) ; nouvel essai dans 5 s", exc)
                self._stopping.wait(5)

    def _relay(self, pipe) -> None:
        conn = None
        pending = b""
        while not self._stopping.is_set():
            data = pipe.read(CHUNK - len(pending))
            if not data:
                return  # librespot a fermé le tube : on le rouvre
            pending += data
            if len(pending) < CHUNK:
                continue
            chunk, pending = pending, b""
            try:
                conn = conn or http.client.HTTPConnection(self._host, self._port, timeout=10)
                self._post(f"/audio/music?rate={RATE}&channels={CHANNELS}", chunk, connection=conn)
            except (OSError, http.client.HTTPException) as exc:
                log.debug("Musique : morceau perdu (%s)", exc)
                if conn is not None:
                    conn.close()
                conn = None


class DuckingSink:
    """Sortie de la voix inchangée ; prévient le relais musical avant chaque phrase pour baisser la musique."""

    def __init__(self, sink, relay: MusicRelay):
        self._sink = sink
        self._relay = relay

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        self._relay.speaking(len(audio) / max(1, sample_rate))
        self._sink.play(audio, sample_rate)

    def __getattr__(self, name):
        return getattr(self._sink, name)
