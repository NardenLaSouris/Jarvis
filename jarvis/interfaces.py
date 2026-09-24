"""Contrats des composants du pipeline vocal.

Le cœur de l'agent ne dépend que de ces interfaces : chaque moteur (wake word,
STT, LLM, TTS, entrée/sortie audio) peut être remplacé sans toucher à l'agent.
L'audio circule en blocs numpy int16 mono.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

import numpy as np


class AudioSource(Protocol):
    sample_rate: int
    frame_samples: int

    def read(self) -> np.ndarray | None:
        """Bloc suivant (int16, ``frame_samples`` échantillons) ou None si la source est épuisée."""

    def flush(self) -> None:
        """Ignore l'audio capturé pendant que JARVIS parlait ou réfléchissait."""


class AudioSink(Protocol):
    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        """Joue l'audio (int16) et bloque jusqu'à la fin de la lecture."""


class WakeWordDetector(Protocol):
    def process(self, frame: np.ndarray) -> float:
        """Score de détection (0-1) pour ce bloc audio."""

    def reset(self) -> None: ...


class SpeechToText(Protocol):
    def transcribe(self, audio: np.ndarray, sample_rate: int) -> str: ...


class TextToSpeech(Protocol):
    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        """Retourne (audio int16, fréquence d'échantillonnage)."""


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant"]
    content: str


class LanguageModel(Protocol):
    def chat(self, messages: list[Message]) -> str: ...

    def warm_up(self) -> None:
        """Charge le modèle en mémoire pour éviter la latence de la première requête."""
