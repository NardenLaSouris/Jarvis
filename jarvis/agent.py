"""Cœur de JARVIS : la boucle veille -> écoute -> réflexion -> réponse.

L'agent ne connaît que les interfaces de ``jarvis.interfaces`` ; il ignore quels
moteurs (openWakeWord, Whisper, Ollama, Piper...) sont utilisés.
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np

from jarvis.audio.endpointing import UtteranceRecorder
from jarvis.capabilities import CapabilityRegistry
from jarvis.interfaces import (
    AudioSink, AudioSource, LanguageModel, Message, SpeechToText, TextToSpeech, WakeWordDetector,
)
from jarvis.prompts import build_system_prompt

log = logging.getLogger(__name__)

LLM_FAILURE_REPLY = "Je suis navré, monsieur, je n'arrive pas à joindre mon module de réflexion."

EventHandler = Callable[[str, str], None]


@dataclass(frozen=True)
class AgentSettings:
    assistant_name: str
    wake_phrase: str
    wake_threshold: float
    acknowledgements: tuple[str, ...]
    listen_timeout: float
    conversation_timeout: float
    max_history_turns: int


def clean_for_speech(text: str) -> str:
    """Retire la mise en forme que certains modèles ajoutent malgré la consigne."""
    text = re.sub(r"[*_`]+", "", text)
    text = re.sub(r"[#>|]+", " ", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.MULTILINE)
    return re.sub(r"\s+", " ", text).strip()


class Agent:
    def __init__(
        self,
        settings: AgentSettings,
        source: AudioSource,
        sink: AudioSink,
        wake_word: WakeWordDetector,
        recorder: UtteranceRecorder,
        stt: SpeechToText,
        llm: LanguageModel,
        tts: TextToSpeech,
        capabilities: CapabilityRegistry,
        on_event: EventHandler | None = None,
    ):
        self.settings = settings
        self._source = source
        self._sink = sink
        self._wake_word = wake_word
        self._recorder = recorder
        self._stt = stt
        self._llm = llm
        self._tts = tts
        self._capabilities = capabilities
        self._on_event = on_event or (lambda kind, text: None)
        self._acks = [tts.synthesize(text) for text in settings.acknowledgements]

    # --- Boucle principale -------------------------------------------------

    def run(self) -> None:
        """Tourne jusqu'à épuisement de la source audio (ou Ctrl+C)."""
        while self._wait_for_wake_word():
            self._conversation()
        log.info("Source audio épuisée, arrêt.")

    def _wait_for_wake_word(self) -> bool:
        self._event("sleep", f"En veille — dites « {self.settings.wake_phrase} »")
        self._wake_word.reset()
        while (frame := self._source.read()) is not None:
            score = self._wake_word.process(frame)
            if score >= self.settings.wake_threshold:
                self._event("wake", f"Wake word détecté (score {score:.2f})")
                return True
        return False

    def _conversation(self) -> None:
        self._play(*random.choice(self._acks))
        history: list[Message] = []
        timeout = self.settings.listen_timeout
        while True:
            self._event("listening", f"À l'écoute ({timeout:.0f} s)")
            audio = self._recorder.record(start_timeout=timeout)
            if audio is None:
                break
            timeout = self.settings.conversation_timeout

            started = time.perf_counter()
            text = self._stt.transcribe(audio, self._source.sample_rate)
            if not text:
                self._event("stt", "(rien compris)")
                continue
            self._event("user", text)
            self._event("timing", f"STT {time.perf_counter() - started:.1f} s")

            reply = self._ask(history, text)
            self._event("assistant", reply)
            self._say(reply)
        self._event("sleep", "Retour en veille")

    # --- Étapes ------------------------------------------------------------

    def _ask(self, history: list[Message], text: str) -> str:
        history.append(Message("user", text))
        del history[: max(0, len(history) - 2 * self.settings.max_history_turns + 1)]
        system = Message("system", build_system_prompt(self.settings.assistant_name, self._capabilities))
        started = time.perf_counter()
        try:
            reply = clean_for_speech(self._llm.chat([system, *history]))
        except Exception:
            log.exception("Échec de l'appel au LLM")
            history.pop()
            return LLM_FAILURE_REPLY
        self._event("timing", f"LLM {time.perf_counter() - started:.1f} s")
        history.append(Message("assistant", reply))
        return reply

    def _say(self, text: str) -> None:
        started = time.perf_counter()
        audio, rate = self._tts.synthesize(text)
        self._event("timing", f"TTS {time.perf_counter() - started:.1f} s")
        self._play(audio, rate)

    def _play(self, audio: np.ndarray, rate: int) -> None:
        self._sink.play(audio, rate)
        # Ne pas s'écouter soi-même : on jette ce que le micro a capté pendant ce temps.
        self._source.flush()

    def _event(self, kind: str, text: str) -> None:
        log.info("[%s] %s", kind, text)
        self._on_event(kind, text)
