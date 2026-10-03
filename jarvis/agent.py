"""Cœur de JARVIS : la boucle veille -> écoute -> réflexion -> réponse.

L'agent ne connaît que les interfaces de ``jarvis.interfaces`` ; il ignore quels
moteurs (openWakeWord, Whisper, Ollama, NeuTTS...) sont utilisés.
"""

from __future__ import annotations

import json
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
from jarvis.router import IntentRouter, Route
from jarvis.streaming import SpeechPipeline, sentences_from_llm
from jarvis.personality import normalize
from jarvis.tools.core import CANCELLED, CONFIRM, DONE
from jarvis.tools.planner import plan, tool_request
from jarvis.weather.cities import mentioned_city
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


YOURS = re.compile(r"\b([Mm]on) (minuteur|rappel|ordinateur|PC)\b")


def polish_web_sentence(sentence: str, question: str, first: bool, assistant_name: str = "JARVIS") -> str:
    """Phrase d'une réponse Web ou d'outil prête à dire : sans URL, sans « JARVIS : » en tête, et sans la
    question répétée en guise de première phrase ("" si la phrase est à taire)."""
    sentence = without_urls(sentence)
    sentence = re.sub(rf"^\s*{re.escape(assistant_name)}\s*[:.,]\s*", "", sentence, flags=re.IGNORECASE)
    if first and normalize(sentence) == normalize(without_name(question, assistant_name)):
        return ""
    return YOURS.sub(lambda m: ("Votre" if m.group(1)[0].isupper() else "votre") + " " + m.group(2), sentence)


WEATHER_TOOL = "get_weather"

LATENCY_LABELS = (
    ("stt", "STT"),
    ("web", "Web search"),
    ("tool_plan", "Tool choice"),
    ("tool", "Tool"),
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
        tools=None,
        corrector=None,
        notifications=None,
        services: tuple = (),
        devices=None,
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
        self._tools = tools
        self._devices = devices
        self._last_tool_request = ""
        self._place: str | None = None
        self._corrector = corrector
        self._notifications = notifications
        self._services = services
        self.last_sources: list[dict] = []
        self._pipeline = SpeechPipeline(tts, sink, stream_audio, merge_under)
        self._on_event = on_event or (lambda kind, text: None)
        self._acks = [tts.synthesize(text) for text in settings.acknowledgements]

    def close(self) -> None:
        """Arrête les services de fond (minuteurs...) ; JARVIS peut alors quitter proprement."""
        for service in self._services:
            try:
                service.stop()
            except Exception:
                log.exception("Arrêt de %s impossible", type(service).__name__)

    def _deliver_notifications(self) -> bool:
        """Laisse le canal vocal prononcer ses notifications en attente. Rend True s'il y en avait."""
        if self._notifications is None:
            return False
        return self._notifications.deliver(self._say_notification) > 0

    def _say_notification(self, text: str) -> None:
        self._event("notification", text)
        self._pipeline.speak([text])
        self._source.flush()

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
            if self._deliver_notifications():
                self._wake_word.reset()
                continue
            score = self._wake_word.process(frame)
            if score >= self.settings.wake_threshold:
                self._event("wake", f"Wake word détecté (score {score:.2f})")
                return True
        return False

    def _conversation(self) -> None:
        self._router.start_conversation()
        self._play(*random.choice(self._acks))
        history: list[Message] = []
        self._place = None
        timeout = self.settings.listen_timeout
        while True:
            self._deliver_notifications()
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
            if self._corrector is not None:
                corrected = self._corrector.correct(text)
                if corrected != text:
                    self._event("correction", f"« {text} » -> « {corrected} »")
                    text = corrected
            self._event("user", text)
            self._event("timing", f"STT {latency['stt']:.1f} s")
            self._place = mentioned_city(text) or self._place

            outcome = self._tools.answer(text) if self._tools is not None else None
            if outcome is not None:
                latency["route"] = "tool:confirmation"
                self._event("routing", latency["route"])
                reply, end = self._after_tool(history, text, outcome, latency), False
            else:
                follow_up = self._tool_follow_up(text)
                self._last_tool_request = ""
                route = Route("tool", "tool.follow_up") if follow_up else self._router.route(text)
                latency["route"] = route.label
                self._event("routing", route.label)
                reply, end = self._answer(history, follow_up or text, route, latency), route.end_conversation
            self._event("assistant", reply)
            self._event("latency", format_latency(latency, speech_ended_at))
            if end:
                break
        if self._tools is not None:
            self._tools.cancel_pending()
        self._event("sleep", "Retour en veille")

    def _answer(self, history: list[Message], text: str, route, latency: dict) -> str:
        if route.source == "tool" and self._tools is not None:
            return self._use_tool(history, text, route, latency)
        if route.source == "web.search":
            return self._search_and_answer(history, text, latency)
        if route.reply is not None:
            reply = self._speak([route.reply], latency)
            self._remember(history, Message("user", text))
            self._remember(history, Message("assistant", reply))
            return reply
        return self._ask(history, text, latency)

    # --- Outils ------------------------------------------------------------

    def _tool_follow_up(self, text: str) -> str:
        """« Et à Lyon ? » juste après « Quel temps fera-t-il demain ? » : la demande d'outil précédente,
        complétée ("" si ce n'est pas une suite courte d'une demande d'outil réussie)."""
        words = normalize(text).split()
        if not self._last_tool_request or not words or words[0] != "et" or len(words) > 8:
            return ""
        rest = re.sub(r"^\W*et\b\W*", "", text, flags=re.IGNORECASE)
        return f"{self._last_tool_request.rstrip(' ?.!')} {rest}"

    def _use_tool(self, history: list[Message], text: str, route, latency: dict) -> str:
        """Demande d'outil (directe ou proposée par le LLM) -> Core : validation, permission, confirmation, exécution."""
        if route.tool:
            data = {"type": "tool_call", "tool": route.tool, "parameters": {}}
        else:
            started = time.perf_counter()
            data = plan(self._llm, text, self._tools.registry)
            latency["tool_plan"] = time.perf_counter() - started
            if data is None:
                fallback = self._router.route(text, tools=False)
                latency["route"] = fallback.label
                self._event("routing", f"{fallback.label} (aucun outil)")
                return self._answer(history, text, fallback, latency)
        data = self._with_device(self._with_place(data), text)
        self._event("tool", json.dumps(data, ensure_ascii=False)[:200])
        started = time.perf_counter()
        outcome = self._tools.submit(data)
        latency["tool"] = time.perf_counter() - started
        reply = self._after_tool(history, text, outcome, latency)
        if outcome.status == DONE and outcome.result.success:
            self._last_tool_request = "" if route.tool else text
            place = outcome.result.result.get("location") if isinstance(outcome.result.result, dict) else None
            self._place = place if data.get("tool") == WEATHER_TOOL and place else self._place
        return reply

    def _with_device(self, data: dict, text: str) -> dict:
        """Appareil visé, d'après les mots de la demande (« mon pc portable »), sinon l'appareil par défaut."""
        name, parameters = data.get("tool"), data.get("parameters")
        if self._devices is None or not isinstance(parameters, dict) or not self._tools.registry.exists(name)                 or "device" not in self._tools.registry.get(name).parameters:
            return data
        device = self._devices.find(text) or self._devices.default
        return {**data, "parameters": {**parameters, "device": device.key}}

    def _with_place(self, data: dict) -> dict:
        """Météo sans ville dite : la dernière ville de la conversation, sinon la ville par défaut de l'outil."""
        parameters = data.get("parameters")
        if data.get("tool") != WEATHER_TOOL or not self._place or not isinstance(parameters, dict) \
                or parameters.get("location"):
            return data
        return {**data, "parameters": {**parameters, "location": self._place}}

    def _after_tool(self, history: list[Message], text: str, outcome, latency: dict) -> str:
        """Exécution réussie : phrase de l'outil, ou à défaut réponse du LLM à partir du résultat. Échec, refus,
        annulation ou question de confirmation : phrase du Core, sans LLM (qui pourrait inventer le résultat)."""
        if outcome.status == DONE:
            self._event("tool", json.dumps(outcome.result.as_dict(), ensure_ascii=False)[:200])
            if outcome.result.success and not outcome.result.message:
                return self._ask(history, text, latency, tool_result=outcome.result)
        if outcome.status == CONFIRM:
            spoken = outcome.question
        elif outcome.status == CANCELLED:
            spoken = self._router.phrase("tool_cancelled")
        else:
            spoken = outcome.result.message
        reply = self._speak([spoken], latency)
        self._remember(history, Message("user", text))
        self._remember(history, Message("assistant", reply))
        return reply

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

    def _ask(self, history: list[Message], text: str, latency: dict, web_context=None, tool_result=None) -> str:
        """Réponse du LLM en flux : chaque phrase est prononcée dès qu'elle est complète.

        Avec ``web_context``, les résultats de recherche (données non fiables) accompagnent la demande
        dans le message de l'utilisateur envoyé au LLM ; avec ``tool_result``, le résultat structuré de
        l'outil exécuté. L'historique ne garde que la demande.
        """
        history.append(Message("user", text))
        del history[: max(0, len(history) - 2 * self.settings.max_history_turns + 1)]
        searched = web_context is not None
        system = Message("system", self._router.system_prompt(ongoing=len(history) > 1))
        messages = [system, *history]
        if searched:
            messages[-1] = Message("user", web_request(text, web_context))
        if tool_result is not None:
            messages[-1] = Message("user", tool_request(text, tool_result))
        succeeded = tool_result is not None and tool_result.success
        fallback = tool_result.message if tool_result is not None and not tool_result.success else None
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
                    self._router.reply_filter(user_text=text, tools_used=succeeded, fallback=fallback),
                    marks,
                    done_reason=lambda: getattr(self._llm, "last_done_reason", ""),
                ):
                    sentence = clean_for_speech(sentence)
                    if searched or tool_result is not None:
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
