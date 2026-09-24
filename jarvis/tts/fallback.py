"""Moteur TTS principal avec repli automatique sur un moteur local."""

from __future__ import annotations

import logging

import numpy as np

from jarvis.interfaces import TextToSpeech

log = logging.getLogger(__name__)


class FallbackTTS:
    def __init__(self, primary: TextToSpeech, fallback: TextToSpeech):
        self._primary = primary
        self._fallback = fallback
        self.voice_name = f"{getattr(primary, 'voice_name', 'principal')} (secours : {getattr(fallback, 'voice_name', 'local')})"
        self.last_voice = ""

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        try:
            audio, rate = self._primary.synthesize(text)
            if audio.size:
                self.last_voice = getattr(self._primary, "voice_name", "principal")
                return audio, rate
            log.warning("Synthèse principale vide : repli sur la voix locale")
        except Exception as exc:
            log.warning("Synthèse principale indisponible (%s) : repli sur la voix locale", exc)
        self.last_voice = getattr(self._fallback, "voice_name", "local")
        return self._fallback.synthesize(text)
