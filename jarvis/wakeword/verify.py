"""Seconde vérification du wake word : les deux secondes d'audio qui ont déclenché la détection sont transcrites
par un petit Whisper (« tiny », sur le Core) ; la conversation ne s'ouvre que si l'on y entend bien le mot de
réveil ([wake_word] phrase : « Orion », ou « Jarvis » pour l'ancien modèle).

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
# Forme sonore de « Orion » : orion, oryon, aurion, o'rion, orillon, et dit vite sans le r : ouyon, oyon (« Ouyon »
# entendu sur un vrai « Orion » le 9 octobre 2026) ; jamais « horizon », « oreille », « avion », « oignon », « Marion »
# (« arion » sans o initial). « Nous aurions » s'entend pareil : Whisper l'écrit avec son sens, écarté.
SHAPES = {"jarvis": WAKE_SHAPE, "orion": re.compile(r"(?:o|au|ou)r?(?:i|y|ill)+[oa]n")}


def heard_wake(text: str, phrase: str = "Jarvis") -> bool:
    """La transcription contient-elle le mot de réveil (à l'oreille) ? Espaces et apostrophes ignorés (« J'arvisse »,
    « O'Rion »)."""
    norm = normalize(text)
    if normalize(phrase) == "orion":
        # Mot par mot : « nous aurions », « aurions-nous » ne sont pas « Orion ».
        words = re.findall(r"[a-z]+", norm.replace(" rion", "rion"))
        return any(SHAPES["orion"].fullmatch(w) for w in words)
    return bool(WAKE_SHAPE.search(re.sub(r"[^a-z]", "", norm)))


class WakeVerifier:
    def __init__(self, model: str = "tiny", download_root: Path | None = None, language: str = "fr",
                 phrase: str = "Jarvis", threads: int = 0):
        from faster_whisper import WhisperModel

        root = str(download_root) if download_root else None
        started = time.perf_counter()
        try:
            self._model = WhisperModel(model, device="cpu", compute_type="int8", download_root=root,
                                       local_files_only=True, cpu_threads=threads)
        except Exception:
            self._model = WhisperModel(model, device="cpu", compute_type="int8", download_root=root,
                                       cpu_threads=threads)
        self._language = language
        self._phrase = phrase
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
        return heard_wake(text, self._phrase), text
