"""Cœur de JARVIS : la boucle veille -> écoute -> réflexion -> réponse.

L'agent ne connaît que les interfaces de ``jarvis.interfaces`` ; il ignore quels
moteurs (openWakeWord, Whisper, Ollama, NeuTTS...) sont utilisés.
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
from jarvis.interfaces import (
    AudioSink, AudioSource, LanguageModel, Message, SpeechToText, TextToSpeech, WakeWordDetector,
)
from jarvis.router import IntentRouter
from jarvis.streaming import SpeechPipeline, sentences_from_llm
from jarvis.personality import normalize
from jarvis.web.research import web_request, without_name

log = logging.getLogger(__name__)

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


URL = re.compile(r"\(?\b(?:https?://|www\.)\S+?(?=[.,;:!?)]*(?:\s|$))\)?", re.IGNORECASE)


def without_urls(text: str) -> str:
    """Retire les adresses Web d'une phrase : elles ne sont jamais lues à voix haute."""
    if not URL.search(text):
        return text
    return re.sub(r"\s+([.,;:!?])", r"\1", " ".join(URL.sub("", text).split()))


def polish_web_sentence(sentence: str, question: str, first: bool, assistant_name: str = "JARVIS") -> str:
    """Phrase d'une réponse Web prête à dire : sans URL, sans « JARVIS : » en tête, et sans la
    question répétée en guise de première phrase ("" si la phrase est à taire)."""
    sentence = without_urls(sentence)
    sentence = re.sub(rf"^\s*{re.escape(assistant_name)}\s*:\s*", "", sentence, flags=re.IGNORECASE)
    if first and normalize(sentence) == normalize(without_name(question, assistant_name)):
        return ""
    return sentence


LATENCY_LABELS = (
    ("stt", "STT"),
    ("web", "Web search"),
    ("llm_first_token", "LLM first token"),
    ("llm_first_sentence", "LLM first sentence"),
    ("tts_first", "TTS first sentence"),
    ("audio_first", "Audio first chunk"),
    ("llm_total", "LLM total"),
    ("tts_total", "TTS total"),
    ("total_response", "Total response"),
)


def format_latency(latency: dict, speech_ended_at: float) -> str:
    total = latency.get("début lecture", speech_ended_at) - speech_ended_at
    llm = latency.get("llm_détail") or {}
    lines = ["Latence de la réponse", f"  Routage : {latency.get('route', 'llm')}",
             f"  Fin de parole : {latency.get('fin de parole', 0):.2f} s (silence attendu avant de conclure)"]
    for key, label in LATENCY_LABELS:
        if key in latency:
            lines.append(f"  {label}: {latency[key]:.2f} s")
    if "llm_total" not in latency:
        lines.append("  LLM : non sollicité (réponse prédéfinie)")
    if llm:
        lines.append(f"  Ollama : chargement {llm['chargement']:.2f} s, prompt {llm['prompt']:.2f} s, "
                     f"génération {llm['génération']:.2f} s pour {llm['jetons']} jetons")
    lines.append(f"  Voix : {latency.get('tts_voix', '')}, {latency.get('phrases', 0)} phrase(s), "
                 f"{latency.get('durée audio', 0):.2f} s d'audio")
    lines.append(f"  TOTAL: {total:.2f} s (fin de parole -> début de lecture)")
    return "\n".join(lines)


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
        router: IntentRouter,
        on_event: EventHandler | None = None,
        stream_audio: bool = False,
        merge_under: int = 0,
        web=None,
    ):
        self.settings = settings
        self._source = source
        self._sink = sink
        self._wake_word = wake_word
        self._recorder = recorder
        self._stt = stt
        self._llm = llm
        self._tts = tts
        self._router = router
        self._web = web
        self.last_sources: list[dict] = []
        self._pipeline = SpeechPipeline(tts, sink, stream_audio, merge_under)
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
        self._router.start_conversation()
        self._play(*random.choice(self._acks))
        history: list[Message] = []
        timeout = self.settings.listen_timeout
        while True:
            self._event("listening", f"À l'écoute ({timeout:.0f} s)")
            audio = self._recorder.record(start_timeout=timeout)
            if audio is None:
                break
            timeout = self.settings.conversation_timeout
            latency = {"fin de parole": self._recorder.endpoint_delay}
            speech_ended_at = self._recorder.speech_ended_at

            started = time.perf_counter()
            text = self._stt.transcribe(audio, self._source.sample_rate)
            latency["stt"] = time.perf_counter() - started
            if not text:
                self._event("stt", "(rien compris)")
                continue
            self._event("user", text)
            self._event("timing", f"STT {latency['stt']:.1f} s")

            route = self._router.route(text)
            latency["route"] = route.label
            self._event("routing", route.label)
            if route.source == "web.search":
                reply = self._search_and_answer(history, text, latency)
            elif route.reply is not None:
                reply = self._speak([route.reply], latency)
                self._remember(history, Message("user", text))
                self._remember(history, Message("assistant", reply))
            else:
                reply = self._ask(history, text, latency)
            self._event("assistant", reply)
            self._event("latency", format_latency(latency, speech_ended_at))
            if route.end_conversation:
                break
        self._event("sleep", "Retour en veille")

    # --- Étapes ------------------------------------------------------------

    def _remember(self, history: list[Message], message: Message) -> None:
        history.append(message)
        del history[: max(0, len(history) - 2 * self.settings.max_history_turns)]

    def _search_and_answer(self, history: list[Message], text: str, latency: dict) -> str:
        """Recherche Web puis réponse du LLM à partir des résultats ; phrase d'excuse si la recherche échoue."""
        context = None
        if self._web is not None:
            started = time.perf_counter()
            context = self._web.run(text)
            latency["web"] = time.perf_counter() - started
            self.last_sources = context.sources
            self._event("web", f"« {context.query} » : {len(context.results)} résultat(s) retenu(s), "
                               f"{len(context.pages)} page(s) lue(s)" + (f" — {context.error}" if context.error else ""))
            self._event("timing", f"Web search {latency['web']:.2f} s")
        if context is None or not context.found:
            key = "web_no_results" if context is not None and context.error is None else "web_unavailable"
            reply = self._speak([self._router.phrase(key)], latency)
            self._remember(history, Message("user", text))
            self._remember(history, Message("assistant", reply))
            return reply
        return self._ask(history, text, latency, web_context=context)

    def _ask(self, history: list[Message], text: str, latency: dict, web_context=None) -> str:
        """Réponse du LLM en flux : chaque phrase est prononcée dès qu'elle est complète.

        Avec ``web_context``, les résultats de recherche (données non fiables) accompagnent la demande
        dans le message de l'utilisateur envoyé au LLM ; l'historique ne garde que la demande.
        """
        history.append(Message("user", text))
        del history[: max(0, len(history) - 2 * self.settings.max_history_turns + 1)]
        searched = web_context is not None
        system = Message("system", self._router.system_prompt(ongoing=len(history) > 1, searched=searched))
        messages = [system, *history]
        if searched:
            messages[-1] = Message("user", web_request(text, web_context))
        marks: dict[str, float] = {}
        failed, spoken = [], []

        def sentences():
            try:
                if hasattr(self._llm, "stream"):
                    fragments = self._llm.stream(messages)
                else:
                    fragments = iter([self._llm.chat(messages)])
                for sentence in sentences_from_llm(
                    fragments,
                    self._router.reply_filter(user_text=text),
                    marks,
                    done_reason=lambda: getattr(self._llm, "last_done_reason", ""),
                ):
                    sentence = clean_for_speech(sentence)
                    if searched:
                        sentence = polish_web_sentence(sentence, text, first=not spoken,
                                                       assistant_name=self.settings.assistant_name)
                    if sentence:
                        spoken.append(sentence)
                        yield sentence
            except Exception:
                log.exception("Échec de l'appel au LLM")
                failed.append(True)
                if "llm_first_sentence" not in marks:
                    yield self._router.phrase("llm_unavailable")

        reply = self._speak(sentences(), latency, marks)
        if not failed:
            latency["llm_détail"] = dict(getattr(self._llm, "last_stats", {}))
            history.append(Message("assistant", reply))
        else:
            history.pop()
        return reply

    def _speak(self, sentences, latency: dict, marks: dict | None = None) -> str:
        stats = self._pipeline.speak(sentences)
        self._source.flush()
        latency.update(marks or {})
        latency["tts_first"] = stats.tts_first or 0.0
        latency["tts_total"] = stats.tts_total
        if stats.first_audio is not None:
            latency["audio_first"] = stats.since_start(stats.first_audio)
            latency["début lecture"] = stats.first_audio
        latency["total_response"] = stats.since_start(stats.finished)
        latency["tts_voix"] = getattr(self._tts, "last_voice", "") or getattr(self._tts, "voice_name", "")
        latency["phrases"] = len(stats.sentences)
        latency["durée audio"] = stats.audio_seconds
        for key, label in LATENCY_LABELS[1:]:
            if key in latency and key != "total_response":
                self._event("timing", f"{label} {latency[key]:.2f} s")
        return " ".join(stats.sentences)

    def _play(self, audio: np.ndarray, rate: int) -> None:
        self._sink.play(audio, rate)
        drain = getattr(self._sink, "drain", None)
        if drain:
            drain()
        # Ne pas s'écouter soi-même : on jette ce que le micro a capté pendant ce temps.
        self._source.flush()

    def _event(self, kind: str, text: str) -> None:
        log.info("[%s] %s", kind, text)
        self._on_event(kind, text)
