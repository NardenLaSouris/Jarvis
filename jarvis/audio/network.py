"""Audio d'un autre PC via son agent JARVIS (jarvis.winagent) : micro distant et sortie distante.

Mêmes contrats que le micro et le haut-parleur locaux. Si l'agent est injoignable (PC éteint, agent
arrêté), la source attend et se reconnecte seule ; la sortie ignore l'audio sans faire tomber JARVIS.
"""

from __future__ import annotations

import logging
import queue
import threading
import urllib.error
import urllib.request

import numpy as np

log = logging.getLogger(__name__)


class FrameQueue(queue.Queue):
    """Blocs audio en attente. Plein (JARVIS occupé à réfléchir ou à parler) : le bloc le plus ancien est oublié,
    avec un seul message tant que le débordement dure."""

    def __init__(self, maxsize: int, label: str):
        super().__init__(maxsize)
        self._label = label
        self._overflowing = False

    def put_latest(self, frame: np.ndarray) -> None:
        if not self.full():
            self._overflowing = False
        elif not self._overflowing:
            log.info("Tampon du micro %s plein : l'audio le plus ancien est ignoré", self._label)
            self._overflowing = True
        while True:
            try:
                self.put_nowait(frame)
                return
            except queue.Full:
                try:
                    self.get_nowait()
                except queue.Empty:
                    pass

    def clear(self) -> None:
        while True:
            try:
                self.get_nowait()
            except queue.Empty:
                return


class _Agent:
    def __init__(self, url: str, token: str):
        token = token.strip()
        if not token:
            raise ValueError("JARVIS_AGENT_TOKEN est requis pour l'audio distant ([audio] remote).")
        if not token.isascii() or not token.isprintable() or " " in token:
            raise ValueError("JARVIS_AGENT_TOKEN contient des caractères invalides.")
        self.url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}

    def open(self, path: str, data: bytes | None = None, timeout: float = 5.0):
        headers = dict(self._headers, **({"Content-Type": "application/octet-stream"} if data is not None else {}))
        request = urllib.request.Request(self.url + path, data=data, headers=headers,
                                         method="POST" if data is not None else "GET")
        return urllib.request.urlopen(request, timeout=timeout)


def _reason(exc: Exception) -> str:
    """Cause lisible, sans jamais reprendre le texte d'une exception qui pourrait citer le jeton."""
    if isinstance(exc, urllib.error.HTTPError):
        return "jeton refusé" if exc.code == 401 else f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, OSError):
        return exc.reason.strerror or type(exc.reason).__name__
    return type(exc).__name__


class NetworkSource:
    """Micro distant, découpé en blocs de ``frame_samples`` à ``sample_rate`` par l'agent."""

    def __init__(self, url: str, token: str, sample_rate: int, frame_samples: int,
                 max_buffered_s: float = 30.0, retry_s: float = 3.0, timeout: float = 5.0):
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self._agent = _Agent(url, token)
        self._retry = retry_s
        self._timeout = timeout
        self._queue = FrameQueue(int(max_buffered_s * sample_rate / frame_samples), "distant")
        self._stopping = threading.Event()
        self._response = None
        self._thread = threading.Thread(target=self._run, name="micro-distant", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        path = f"/audio/input?rate={self.sample_rate}&frame={self.frame_samples}"
        size = 2 * self.frame_samples
        failing = False
        while not self._stopping.is_set():
            try:
                with self._agent.open(path, timeout=self._timeout) as response:
                    self._response = response
                    log.info("Micro distant connecté (%s)", self._agent.url)
                    failing = False
                    while not self._stopping.is_set():
                        data = response.read(size)
                        if len(data) < size:
                            break
                        self._queue.put_latest(np.frombuffer(data, dtype="<i2").astype(np.int16))
            except (OSError, ValueError) as exc:
                if not failing and not self._stopping.is_set():
                    log.warning("Micro distant injoignable (%s) : nouvelle tentative toutes les %.0f s",
                                _reason(exc), self._retry)
                failing = True
            finally:
                self._response = None
            self._stopping.wait(self._retry)

    def read(self) -> np.ndarray | None:
        return self._queue.get()

    def flush(self) -> None:
        self._queue.clear()

    def close(self) -> None:
        self._stopping.set()
        response = self._response
        if response is not None:
            response.close()


class NetworkSink:
    """Sortie distante : chaque morceau est joué sur le PC de l'agent ; ``play`` rend la main une fois
    l'audio accepté par sa carte son, comme le haut-parleur local."""

    def __init__(self, url: str, token: str, timeout: float = 120.0):
        self._agent = _Agent(url, token)
        self._timeout = timeout
        self._failing = False

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        if audio.size:
            self._post(f"/audio/output?rate={int(sample_rate)}", audio.astype("<i2").tobytes())

    def drain(self) -> None:
        self._post("/audio/drain", b"")

    def close(self) -> None:
        pass

    def _post(self, path: str, data: bytes) -> None:
        try:
            with self._agent.open(path, data, timeout=self._timeout) as response:
                response.read()
            if self._failing:
                log.info("Sortie audio distante rétablie (%s)", self._agent.url)
            self._failing = False
        except OSError as exc:
            if not self._failing:
                log.warning("Sortie audio distante injoignable (%s) : audio ignoré", _reason(exc))
            self._failing = True
