"""Transcription sur le GPU du Katana (jarvis.stt.server), Whisper local du mini-PC en secours.

Mesuré le 15 octobre 2026 (bench/stt, 60 phrases) : large-v3-turbo sur le Katana, 3,1 % d'erreurs par mot en
0,20 s médian, contre 7,5 % en 3,19 s pour small sur le mini-PC. Le Katana éteint ou muet : connexion TCP courte
refusée, ou pas de réponse en ``timeout`` secondes -> Whisper local aussitôt, puis nouvel essai du Katana après
``retry_after`` secondes (pas de délai payé à chaque phrase tant qu'il est absent).
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.request
from typing import Callable

import numpy as np

from jarvis.llm.failover import tcp_reachable

log = logging.getLogger(__name__)


class RemoteSTT:
    def __init__(self, url: str, token: str, local, language: str, beam_size: int = 1, vad_filter: bool = False,
                 temperature_fallback: bool = True, timeout: float = 6.0, retry_after: float = 30.0,
                 reachable: Callable[[str], bool] = lambda url: tcp_reachable(url, 0.5),
                 clock: Callable[[], float] = time.monotonic):
        self.url = url.rstrip("/")
        self._token = token
        self.local = local
        self._language = language
        self._beam_size = beam_size
        self._vad_filter = vad_filter
        self._temperature_fallback = temperature_fallback
        self._timeout = timeout
        self._retry_after = retry_after
        self._reachable = reachable
        self._clock = clock
        self._down_since: float | None = None
        self.hotwords = getattr(local, "hotwords", "")
        self.last_confidence: float | None = None
        self.last_source = ""
        # Attributs du Whisper local (diagnostic, --stt-test).
        self.model_name, self.device = getattr(local, "model_name", ""), getattr(local, "device", "cpu")
        self.compute_type, self.load_time = getattr(local, "compute_type", ""), getattr(local, "load_time", 0.0)

    def _remote(self, audio: np.ndarray) -> dict:
        body = json.dumps({"audio": base64.b64encode(audio.astype(np.int16).tobytes()).decode("ascii"),
                           "language": self._language, "hotwords": self.hotwords, "beam_size": self._beam_size,
                           "vad_filter": self._vad_filter, "temperature_fallback": self._temperature_fallback})
        request = urllib.request.Request(f"{self.url}/transcribe", data=body.encode("utf-8"), method="POST",
                                         headers={"Authorization": f"Bearer {self._token}",
                                                  "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            return json.loads(response.read())

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        if sample_rate != 16000:
            raise ValueError("la transcription attend de l'audio à 16 kHz")
        waiting = self._down_since is not None and self._clock() - self._down_since < self._retry_after
        if not waiting:
            try:
                if not self._reachable(self.url):
                    raise ConnectionError("injoignable")
                data = self._remote(audio)
                if self._down_since is not None:
                    log.info("Transcription sur le GPU (%s) de nouveau disponible", self.url)
                self._down_since = None
                self.last_confidence, self.last_source = data.get("confidence"), "gpu"
                return str(data.get("text") or "").strip()
            except Exception as exc:  # noqa: BLE001 - Katana éteint, réseau, erreur : Whisper local
                if self._down_since is None:
                    log.warning("Transcription sur le GPU (%s) indisponible (%s) : Whisper local", self.url, exc)
                self._down_since = self._clock()
        self.local.hotwords = self.hotwords
        text = self.local.transcribe(audio, sample_rate)
        self.last_confidence, self.last_source = getattr(self.local, "last_confidence", None), "local"
        return text
