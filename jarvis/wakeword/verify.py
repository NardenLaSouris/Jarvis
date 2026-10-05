"""Seconde vérification du wake word : les deux secondes d'audio qui ont déclenché la détection sont transcrites
par un petit Whisper (« tiny », sur le Core) ; la conversation ne s'ouvre que si l'on y entend bien « Jarvis ».

Le détecteur openWakeWord réagit parfois à une voix de jeu, de Discord ou de télévision (environ un réveil sur deux
sans aucune phrase ensuite, relevé sur trois jours). Les faux pics ressemblent au mot par le son, pas par les
syllabes : « service », « j'arrive », « j'avais » sont écartés, « Jarvis », « Jervis », « J'avise » retenus.

Mesuré sur les enregistrements de test (voix de synthèse jamais vues à l'entraînement) : 12 « Jarvis » sur 16
confirmés, 0 mot piège sur 16, 0,43 s par vérification sur le mini-PC. Un « Jarvis » écarté à tort se rattrape en
le redisant : un second appel peu après est accepté sans vérification (voir jarvis.agent).
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import numpy as np

from jarvis.personality import normalize

log = logging.getLogger(__name__)

# Forme sonore de « Jarvis » une fois transcrite : J/G/Dj/Ch, voyelle, r ou n facultatif, « vi » puis s ou c
# (jarvis, jarvisse, gervis, jervice, j'avise, « j'en vis » : un vrai « Jarvis » écarté le 5 octobre 2026) ;
# jamais « j'avais », « j'arrive », « j'en vais », « service », « j'ai revu ».
WAKE_SHAPE = re.compile(r"(?:dj|j|g|ch)[ae]?[rn]?vi[sc]")


def heard_wake(text: str) -> bool:
    """La transcription contient-elle « Jarvis » (à l'oreille) ? Espaces et apostrophes ignorés (« J'arvisse »)."""
    return bool(WAKE_SHAPE.search(re.sub(r"[^a-z]", "", normalize(text))))


class WakeVerifier:
    def __init__(self, model: str = "tiny", download_root: Path | None = None, language: str = "fr"):
        from faster_whisper import WhisperModel

        root = str(download_root) if download_root else None
        started = time.perf_counter()
        try:
            self._model = WhisperModel(model, device="cpu", compute_type="int8", download_root=root,
                                       local_files_only=True)
        except Exception:
            self._model = WhisperModel(model, device="cpu", compute_type="int8", download_root=root)
        self._language = language
        self.check(np.zeros(16000, dtype=np.int16), 16000)  # premier calcul (lent) fait au démarrage
        log.info("Vérification du wake word : whisper %s prêt (%.1f s)", model, time.perf_counter() - started)

    def check(self, audio: np.ndarray, rate: int) -> tuple[bool, str]:
        """(« Jarvis » entendu, transcription) pour l'audio du déclenchement (16 kHz)."""
        if rate != 16000:
            raise ValueError("la vérification attend de l'audio à 16 kHz")
        samples = audio.astype(np.float32) / (32768.0 if audio.dtype == np.int16 else 1.0)
        segments, _ = self._model.transcribe(samples, language=self._language, beam_size=1,
                                             condition_on_previous_text=False, without_timestamps=True,
                                             vad_filter=False)
        text = " ".join(s.text.strip() for s in segments).strip()
        return heard_wake(text), text
