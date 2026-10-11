"""Cœur d'ORION : la boucle veille -> écoute -> réflexion -> réponse.

L'agent ne connaît que les interfaces de ``jarvis.interfaces`` ; il ignore quels
moteurs (openWakeWord, Whisper, Ollama, NeuTTS...) sont utilisés.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

from jarvis.audio.endpointing import UtteranceRecorder
from jarvis.context import ConversationContext
from jarvis.interfaces import (
    AudioSink, AudioSource, LanguageModel, Message, SpeechToText, TextToSpeech, WakeWordDetector,
)
from jarvis.call.rule_tools import NEEDS_DETAILS
from jarvis.call.rules import is_acknowledgement
from jarvis.presence.declaration import parse_declaration
from jarvis.router import IntentRouter, Route
from jarvis.flex import FLEX_SWITCH, PHONE_OPENING, is_flex
from jarvis.learning import instruction_of
from jarvis.scheduling.actions import split_event, split_schedule
from jarvis.streaming import SpeechPipeline, TextPipeline, sentences_from_llm
from jarvis.personality import normalize
from jarvis.tools.base import Risk
from jarvis.tools.core import CANCELLED, CONFIRM, DONE
from jarvis.tools.request import MALFORMED_MESSAGES
from jarvis.tools.planner import named, plan, quoted, tool_request
from jarvis.tools.quick import quick_plan
from jarvis.weather.cities import mentioned_city
from jarvis.web.research import names_pattern, web_request, without_name

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
    barge_in: bool = False
    min_confidence: float | None = None
    # Transcription peu sûre qui redemande exactement l'action d'information faite à l'instant (« tous mes mails
    # comme lui » relisait le résumé des mails) : ORION demande de reformuler au lieu de la refaire. None : jamais.
    repeat_confidence: float | None = None
    wake_patience: int = 1
    wake_sure_score: float = 0.0
    wake_sure_patience: int = 3


REPEAT_WINDOW = 120.0  # secondes : au-delà, redemander la même information est normal
MESSAGE_THREAD = 900.0  # secondes : fil des messages Linphone (contexte, confirmation « oui » d'une action)
WAKE_WINDOW = 2.0  # secondes d'audio gardées avant la détection (vérification, captures)
# Audio écouté après le déclenchement avant la vérification : le détecteur réagit pendant le mot (« Ori... »), Whisper
# doit entendre « Orion » en entier. Ces blocs sont ensuite rendus à l'enregistrement de la demande.
WAKE_TAIL = 0.32
# Reprise après une erreur imprévue : au-delà de MAX_CRASHES erreurs en CRASH_WINDOW secondes, le service s'arrête
# (systemd le relance) plutôt que de tourner en boucle sur la même panne.
MAX_CRASHES = 3
CRASH_WINDOW = 60
# Après un rejet : un pic dans la seconde qui suit est le même son (le score reste haut quelques images) et est
# ignoré ; un nouvel appel entre 1 et 8 s plus tard est accepté sans vérification (« Jarvis » redit).
WAKE_SAME_SOUND = 1.0
WAKE_RETRY_WINDOW = 8.0


class WakeTrigger:
    """Wake word retenu quand le score reste au-dessus du seuil pendant ``patience`` images d'affilée ; les pics
    non retenus sont journalisés (pic, durée) pour régler seuil et patience sur la vraie voix."""

    def __init__(self, threshold: float, patience: int = 1, notice: float = 0.3):
        self.threshold, self.patience, self._notice = threshold, max(1, patience), notice
        self.reset()

    def reset(self) -> None:
        self._run, self._peak, self._frames = 0, 0.0, 0

    def update(self, score: float) -> bool:
        self._run = self._run + 1 if score >= self.threshold else 0
        if score >= self._notice:
            self._peak, self._frames = max(self._peak, score), self._frames + 1
        elif self._frames:
            log.info("Pic du wake word non retenu : score %.2f, %d image(s) ≥ %.2f", self._peak, self._frames,
                     self._notice)
            self._peak, self._frames = 0.0, 0
        return self._run >= self.patience

    @property
    def detail(self) -> str:
        return f"score {self._peak:.2f}, {self._run} image(s)"

    @property
    def peak(self) -> float:
        return self._peak

    @property
    def run(self) -> int:
        return self._run


class WakeWatcher:
    """Pendant qu'ORION réfléchit ou parle : écoute le micro et appelle ``on_wake`` si le wake word est dit."""

    def __init__(self, source: AudioSource, detector: WakeWordDetector, threshold: float,
                 on_wake: Callable[[float], None], patience: int = 1):
        self._source, self._detector, self._on_wake = source, detector, on_wake
        self._trigger = WakeTrigger(threshold, patience)
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, name="interruption", daemon=True)

    def start(self) -> "WakeWatcher":
        self._detector.reset()
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stopping.is_set():
            frame = self._source.read()
            if frame is None:
                return
            score = self._detector.process(frame)
            if self._trigger.update(score) and not self._stopping.is_set():
                self._on_wake(score)
                return

    def stop(self) -> None:
        self._stopping.set()
        self._thread.join(timeout=1.0)


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


# Acceptation seule (« oui », « vas-y ») ou demande de lecture explicite ; jamais « dis-moi la météo ».
AGREE_ALONE = {"oui", "ouais", "vas y", "allez", "d accord", "ok", "okay", "oui vas y", "oui merci", "volontiers",
               "je veux bien", "oui je veux bien"}
AGREE_START = re.compile(r"^(?:oui )?(?:lis|lit|lire|resume|qu est ce qu il dit|qu est ce qu elle dit|"
                         r"qu est ce que ca dit|de quoi ca parle|c est quoi ce mail|il dit quoi)(?: |$)")


def agrees(text: str) -> bool:
    """« Oui », « lis-le », « qu'est-ce qu'il dit ? » : acceptation de la suite proposée par une annonce."""
    norm = re.sub(r"^(?:(?:orion|jarvis|monsieur|euh|bon|alors|eh|he)\s+)+", "", normalize(text)).strip()
    return norm in AGREE_ALONE or bool(AGREE_START.match(norm))


def polish_web_sentence(sentence: str, question: str, first: bool, assistant_name: str = "ORION") -> str:
    """Phrase d'une réponse Web ou d'outil prête à dire : sans URL, sans « ORION : » en tête, et sans la
    question répétée en guise de première phrase ("" si la phrase est à taire)."""
    sentence = without_urls(sentence)
    sentence = re.sub(rf"^\s*{names_pattern(assistant_name)}\s*[:.,]\s*", "", sentence, flags=re.IGNORECASE)
    if first and normalize(sentence) == normalize(without_name(question, assistant_name)):
        return ""
    return YOURS.sub(lambda m: ("Votre" if m.group(1)[0].isupper() else "votre") + " " + m.group(2), sentence)


WEATHER_TOOL = "get_weather"
# Réponses « pas encore disponible » de la personnalité et outils qui les rendent fausses quand ils existent.
STALE_UNAVAILABLE = {
    "unavailable_reminders": ("create_timer", "create_reminder", "create_alarm", "list_events"),
    "unavailable_home": ("light_on", "light_off"),
    "unavailable_realtime": ("get_weather",),
    "unavailable_messages": ("check_mail", "read_mail"),
}
# Demande qui désigne un objet d'ORION (« mes mails », « le minuteur », « mon réveil ») : sans outil retenu, une
# question plutôt qu'une réponse libre, qui inventerait le contenu d'un mail ou le temps restant.
ORION_OBJECTS = re.compile(r"\b(mon|ma|mes|le|la|les|ce|cette|ces|du|des)\s+(mails?|courriers?|minuteurs?|rappels?|"
                           r"reveils?|alarmes?)\b")
CLARIFY_WINDOW = 180.0  # s pour répondre à une question de précision
# « Je sors, appelle-moi dans 3 minutes », « je pars et éteins tout » : départ annoncé, puis une autre demande.
COMPOUND_DEPARTURE = re.compile(
    r"^\s*((?:(?:orion|jarvis)\W+)?je (?:sors|pars|m['’]en vais|quitte la maison|vais sortir|vais partir)"
    r"[^,.;!?]*?)\s*(?:[,.;!?]+|\set\s)\s*(\S.+)$", re.IGNORECASE)
CANCEL = re.compile(r"^(?:non|annule|laisse tomber|oublie|rien|stop|pas grave)\b")
# « Appelle-moi quand… », « préviens-moi par message si… » : une règle d'appel, jamais un rappel (« rappelle-moi »).
CALL_RULE = re.compile(r"^(?:(?:orion|jarvis) )?(?:appelle|appelez|telephone|telephonez|previens|previenez|ecris|"
                       r"ecrivez|envoie|envoyez)(?: moi)?(?: un message| par message| un sms)? "
                       r"(?:quand|si|lorsque|lorsqu|des que|au cas ou|demain|ce soir|dans|a \d|vers)\b")
SOURCES = re.compile(r"\b(tes|vos|quelles sont les|quelle est la|cite moi les|cite tes) sources?\b|\bd ou vient (cette|l) info")

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
        alarm=None,
        fast_path: bool = False,
        routines=None,
        profiles=None,
        terminal: str = "main",
        wake_verifier=None,
        wake_captures=None,
        content_guard=None,
        calls=None,
        learning=None,
        call_rules=None,
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
        self._alarm = alarm
        self._fast_path = fast_path
        self._routines = routines
        self._profiles = profiles
        self._guard = content_guard  # jarvis.llm.guard : ce qu'un modèle non éprouvé peut dire
        self.request_context = profiles.context(terminal) if profiles is not None else None
        self._context = ConversationContext(tools.registry, settings.assistant_name) if tools is not None else None
        self._interrupted = False
        self._last_outcome = None
        self._action_unclear = False
        self._last_tool_request = ""
        self._last_done: tuple[str, str, float] | None = None
        self._previous_request = ""
        # Seconde vérification du wake word (jarvis.wakeword.verify) et audio des réveils (jarvis.wakeword.captures).
        self._wake_verifier = wake_verifier
        self._wake_captures = wake_captures
        self._wake_capture: Path | None = None
        # Ce qu'ORION apprend (jarvis.learning) : vocabulaire, seuil et règles du mot de réveil.
        self._learning = learning
        self._last_wake: dict | None = None  # fiche du réveil qui a ouvert la conversation en cours
        self._false_wake = False
        self._since_rejection: float | None = None  # secondes d'audio écoutées depuis le dernier rejet
        self._lead: list[np.ndarray] = []  # audio lu après le mot de réveil, début de la demande
        # Actions restantes d'une demande enchaînée arrêtée sur une confirmation (« ferme Chrome et ouvre le
        # bloc-notes ») : (demande, actions, paramètres précédents), reprises si la confirmation est acceptée.
        self._after_confirmation: tuple[str, list[dict], dict] | None = None
        self._place: str | None = None
        self._corrector = corrector
        self._notifications = notifications
        self._follow_up: dict | None = None  # suite proposée par une annonce (écoute sans mot de réveil)
        self._calls = calls  # appels vers le téléphone jumelé (jarvis/call) ; None : désactivés
        self._call_rules = call_rules  # règles d'appel (jarvis/call/rules.py) : accusé « c'est noté »
        self._clarify: dict | None = None  # outil en attente d'une précision (règle d'appel incomplète)
        self._in_call = None  # appel décroché, pris par la prochaine conversation
        # Messages Linphone : réponse écrite (rien n'est prononcé), fil gardé MESSAGE_THREAD secondes.
        self._texting = False
        self._message_history: list[Message] = []
        self._message_at = 0.0
        self._services = services
        self.last_sources: list[dict] = []
        self._pipeline = SpeechPipeline(tts, sink, stream_audio, merge_under)
        self._on_event = on_event or (lambda kind, text: None)
        self._acks = [tts.synthesize(text) for text in settings.acknowledgements]

    def close(self) -> None:
        """Arrête les services de fond (minuteurs...) ; ORION peut alors quitter proprement."""
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

    def _take_follow_up(self) -> dict | None:
        take = getattr(self._notifications, "take_follow_up", None)
        return take() if take is not None else None

    def _say_notification(self, text: str) -> None:
        self._event("notification", text)
        self._pipeline.speak([text])
        self._source.flush()

    # --- Boucle principale -------------------------------------------------

    def run(self) -> None:
        """Tourne jusqu'à épuisement de la source audio (ou Ctrl+C). Une erreur imprévue pendant une conversation
        est journalisée et ORION se remet en veille ; des erreurs en rafale (plus de MAX_CRASHES en
        CRASH_WINDOW secondes) arrêtent le service, que systemd redémarre (Restart=on-failure)."""
        crashes: list[float] = []
        while self._wait_for_wake_word():
            try:
                self._conversation()
            except Exception:
                now = time.monotonic()
                crashes = [t for t in crashes if now - t < CRASH_WINDOW] + [now]
                log.exception("Erreur imprévue pendant la conversation : retour en veille (%d en %d s)",
                              len(crashes), CRASH_WINDOW)
                if len(crashes) > MAX_CRASHES:
                    raise
                self._recover()
        log.info("Source audio épuisée, arrêt.")

    def _recover(self) -> None:
        """Après une erreur : rien d'une demande à moitié traitée ne doit survivre (confirmation, contexte)."""
        for cleanup in (lambda: self._tools.cancel_pending() if self._tools is not None else None,
                        lambda: self._context.clear() if self._context is not None else None,
                        lambda: self._calls.finish() if self._calls is not None else None,
                        lambda: self._event("sleep", "Retour en veille (après une erreur)")):
            try:
                cleanup()
            except Exception:  # noqa: BLE001 - la reprise ne doit jamais échouer à son tour
                log.exception("Reprise après erreur incomplète")
        self._follow_up = None
        self._in_call = None
        self._lead = []

    def _wait_for_wake_word(self) -> bool:
        self._event("sleep", f"En veille — dites « {self.settings.wake_phrase} »")
        self._wake_word.reset()
        trigger = WakeTrigger(self._wake_threshold(), self.settings.wake_patience)
        rate = getattr(self._source, "sample_rate", 16000)
        recent: deque = deque(maxlen=max(1, round(WAKE_WINDOW * rate / max(1, getattr(self._source, "frame_samples",
                                                                                       1280)))))
        frame_seconds = getattr(self._source, "frame_samples", 1280) / rate
        sure_run = 0
        while (frame := self._source.read()) is not None:
            recent.append(frame)
            if self._since_rejection is not None:
                self._since_rejection += frame_seconds
                if self._since_rejection > WAKE_RETRY_WINDOW:
                    self._since_rejection = None
            if self._deliver_notifications():
                self._wake_word.reset()
                trigger.reset()
                offer = self._take_follow_up()
                if offer is not None:  # « nouveau mail... » : ORION écoute la réponse sans qu'on redise son nom
                    self._follow_up = offer
                    self._wake_capture = None
                    self._event("wake", "Écoute après l'annonce")
                    return True
                continue
            call = self._calls.take_connected() if self._calls is not None else None
            if call is not None:  # le téléphone a décroché : la conversation commence sans mot de réveil
                self._in_call = call
                self._wake_capture = None
                self._event("wake", "Appel décroché")
                return True
            take_message = getattr(self._calls, "take_message", None)  # gestionnaire sans messages : aucun
            message = take_message() if take_message is not None else None
            if message is not None:  # votre message Linphone : réponse écrite, puis retour à la veille
                self._handle_message(message)
                self._wake_word.reset()
                trigger.reset()
                continue
            score = self._wake_word.process(frame)
            sure_run = self._sure_run(sure_run, score)
            if trigger.update(score):
                if self._since_rejection is not None and self._since_rejection < WAKE_SAME_SOUND:
                    trigger.reset()  # fin du même son que celui qui vient d'être écarté
                    continue
                tail, sure_run = self._read_tail(frame_seconds, sure_run)
                recent.extend(tail)
                sure = sure_run >= self.settings.wake_sure_patience
                sure_run = 0
                if not self._wake_confirmed(np.concatenate(recent), rate, trigger, sure):
                    self._wake_word.reset()
                    trigger.reset()
                    recent.clear()
                    continue
                self._lead = tail
                self._event("wake", f"Wake word détecté ({trigger.detail})")
                self._stop_alarm()
                return True
        return False

    def _sure_run(self, run: int, score: float) -> int:
        """Images consécutives au-dessus de sure_score (la plus longue série est gardée une fois la patience atteinte)."""
        sure = self.settings.wake_sure_score
        if sure <= 0:
            return 0
        if run >= self.settings.wake_sure_patience:
            return run
        return run + 1 if score >= sure else 0

    def _read_tail(self, frame_seconds: float, sure_run: int) -> tuple[list[np.ndarray], int]:
        tail: list[np.ndarray] = []
        if self._wake_verifier is None:
            return tail, sure_run
        while len(tail) * frame_seconds < WAKE_TAIL - 1e-9 and (frame := self._source.read()) is not None:
            tail.append(frame)
            sure_run = self._sure_run(sure_run, self._wake_word.process(frame))
        return tail, sure_run

    def _wake_confirmed(self, audio: np.ndarray, rate: int, trigger: WakeTrigger, sure: bool = False) -> bool:
        """Seconde vérification (« Jarvis » bien entendu dans l'audio du déclenchement) ; un réveil franc (``sure``)
        et un second appel peu après un rejet sont acceptés sans elle (le mot écarté à tort se rattrape en le
        redisant)."""
        meta = {"score": round(trigger.peak, 3), "frames": trigger.run, "threshold": trigger.threshold}
        retry = self._since_rejection is not None
        always_verify = self._learning is not None and self._learning.always_verify  # règle apprise
        if sure and self._wake_verifier is not None and not always_verify:
            meta["sure"] = True
        elif self._wake_verifier is not None and not retry:
            started = time.perf_counter()
            try:
                confirmed, heard = self._wake_verifier.check(audio, rate)
            except Exception:
                log.exception("Vérification du wake word impossible : réveil accepté")
                confirmed, heard = True, ""
            meta.update(verified=confirmed, heard=heard[:120], verify_s=round(time.perf_counter() - started, 2))
            if confirmed and self._learning is not None and self._learning.blocked(heard):
                confirmed = False  # déjà signalé comme faux réveil (« ce n'était pas pour toi »)
                meta["blocked"] = True
            if not confirmed:
                self._since_rejection = 0.0
                self._event("wake_rejected", f"Wake word écarté ({trigger.detail}, entendu « {heard[:60]} »)")
                if self._wake_captures is not None:
                    self._wake_captures.finish(self._wake_captures.save(audio, rate, meta), "rejected")
                return False
        elif retry:
            meta["retry"] = True
        self._since_rejection = None
        self._wake_capture = self._wake_captures.save(audio, rate, meta) if self._wake_captures is not None else None
        self._last_wake = meta
        return True

    def _wake_threshold(self) -> float:
        """Seuil du mot de réveil : celui du réglage, relevé après des faux réveils (jarvis.learning)."""
        base = self.settings.wake_threshold
        return self._learning.wake_threshold(base) if self._learning is not None else base

    def _conversation(self) -> None:
        understood = self._listen_and_answer()
        outcome = "false" if self._false_wake else "used" if understood else "silent"
        if self._wake_captures is not None:
            self._wake_captures.finish(self._wake_capture, outcome)
            self._wake_capture = None
        if outcome == "silent" and self._last_wake is not None and self._learning is not None:
            self._learning.silent_wake()
        self._last_wake, self._false_wake = None, False

    def _listen_and_answer(self) -> bool:
        """Conversation ouverte par le wake word, une annonce ou un appel décroché ; True si au moins une demande a
        été comprise."""
        self._router.start_conversation()
        offer, self._follow_up = self._follow_up, None
        call, self._in_call = self._in_call, None
        if call is not None:
            self._greet_call(call)
        elif offer is None:
            self._play(*random.choice(self._acks))
        history: list[Message] = []
        self._place = None
        # Rien ne passe d'une conversation à l'autre (« Et demain ? » reprenait la météo de Lyon de la précédente).
        self._last_tool_request = self._previous_request = ""
        self._after_confirmation = None
        if self.request_context is not None and self._tools is not None and hasattr(self._tools, "set_user"):
            self._tools.set_user(self.request_context.user_id)
        if self._context is not None:
            self._context.clear()
        ongoing = offer is not None or call is not None
        timeout = self.settings.conversation_timeout if ongoing else self.settings.listen_timeout
        try:
            return self._converse(history, offer, call, timeout)
        finally:
            if call is not None:
                self._calls.finish()  # ORION raccroche s'il est encore en ligne ; retour au terminal de la maison

    def _handle_message(self, text: str) -> None:
        """Votre message Linphone : traité comme une demande dite à ORION (mêmes outils, permissions, confirmations
        et garde-fous, vous comme propriétaire), dans le fil des messages des MESSAGE_THREAD dernières secondes ; la
        réponse repart par message, rien n'est prononcé ni écouté à la maison."""
        now = time.monotonic()
        if now - self._message_at > MESSAGE_THREAD:  # nouveau fil : rien de l'ancien (« oui » ne confirme rien)
            self._message_history = []
            self._router.start_conversation()
        self._message_at = now
        if self.request_context is not None and self._tools is not None and hasattr(self._tools, "set_user"):
            self._tools.set_user(self.request_context.user_id)
        self._event("message", text)
        if self._learning is not None:
            text = self._learning.rewrite(text)
        saved, pipeline = self._pipeline, TextPipeline()
        self._pipeline, self._texting = pipeline, True
        try:
            reply, _ = self._reply_to(self._message_history, text, None, {})
        except Exception:  # noqa: BLE001 - un message mal traité ne fait pas tomber ORION
            log.exception("Message non traité")
            reply = self._router.phrase("not_understood")
        finally:
            self._pipeline, self._texting = saved, False
        answer = " ".join(pipeline.spoken).strip() or (reply or "").strip() or self._router.phrase("not_understood")
        self._event("message_reply", answer)
        self._calls.reply(answer)

    def _greet_call(self, call) -> None:
        if call.origin == "user":  # vous appelez ORION : il répond comme à son nom (« Oui, monsieur ? »)
            greeting = self._router.phrase("wake")
        elif call.reason.startswith(PHONE_OPENING):  # mode Flex : la présentation fait office de salut
            greeting = call.reason
        else:
            greeting = " ".join(t for t in (self._router.phrase("call_greeting"), call.reason) if t)
            if self._call_rules is not None and self._call_rules.awaiting_ack(call.id):
                greeting += " Dites « c'est noté » pour que je ne vous rappelle pas à ce sujet."
        self._event("assistant", greeting)
        self._pipeline.speak([greeting])
        self._source.flush()

    def _converse(self, history: list[Message], offer, call, timeout: float) -> bool:
        understood = False
        while True:
            if call is not None and not self._calls.active():
                self._event("call", "Appel terminé sur le téléphone")
                break
            self._deliver_notifications()
            self._event("listening", f"À l'écoute ({timeout:.0f} s)")
            lead, self._lead = self._lead, []
            audio = self._recorder.record(start_timeout=timeout, lead=lead)
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
            if self._learning is not None and (learned := self._learning.rewrite(text)) != text:
                self._event("correction", f"« {text} » -> « {learned} » (vocabulaire appris)")
                text = learned
            self._event("user", text)
            understood = True
            confidence = getattr(self._stt, "last_confidence", None)
            self._event("timing", f"STT {latency['stt']:.1f} s" + (f" (confiance {confidence:.2f})" if confidence is not None else ""))
            self._place = mentioned_city(text) or self._place

            accepted_offer, offer = offer, None  # l'offre ne vaut que pour la première phrase
            reply, end = self._reply_to(history, text, accepted_offer, latency, speech_ended_at)
            if reply is None:  # commande promue : déjà dite
                continue
            self._event("assistant", reply)
            self._event("latency", format_latency(latency, speech_ended_at))
            if self._interrupted:
                self._interrupted = False
                timeout = self.settings.listen_timeout
                continue
            if end:
                break
        if self._tools is not None:
            self._tools.cancel_pending()
        self._event("sleep", "Retour en veille")
        return understood

    def _reply_to(self, history: list[Message], text: str, offer, latency: dict, speech_ended_at=None):
        """Une demande comprise, dite ou écrite (message Linphone) -> (réponse, fin de conversation) : confirmation
        en attente, annonce acceptée, sources, contexte, outils ou conversation. Réponse None : déjà dite (commande
        promue). Mêmes permissions, confirmations et garde-fous dans les deux cas."""
        outcome = self._tools.answer(text) if self._tools is not None else None
        rest, self._after_confirmation = self._after_confirmation, None
        accepted = offer is not None and agrees(text)
        if accepted and outcome is None:
            latency["route"] = "tool:annonce"
            self._event("routing", latency["route"])
            data = {"type": "tool_call", "tool": offer["tool"], "parameters": dict(offer.get("parameters") or {})}
            return self._run_call(history, text, data, latency), False
        if outcome is not None:
            latency["route"] = "tool:confirmation"
            self._event("routing", latency["route"])
            reply = self._after_tool(history, text, outcome, latency)
            if rest is not None and outcome.status == DONE and outcome.result.success:
                # Confirmation acceptée : la suite de la demande enchaînée n'est plus perdue.
                reply = f"{reply} {self._use_tools(history, rest[0], rest[1], latency, rest[2])}"
            return reply, False
        if self.last_sources and SOURCES.search(normalize(text)):
            latency["route"] = "web:sources"
            self._event("routing", latency["route"])
            return self._say_sources(history, text, latency), False
        if (contextual := self._context.resolve(text) if self._context is not None else None) is not None:
            latency["route"] = "tool:context"
            self._event("routing", latency["route"])
            if contextual.get("type") == "tool_calls":
                return self._use_tools(history, text, contextual["calls"], latency), False
            return self._run_call(history, text, contextual, latency), False
        clarify, self._clarify = self._clarify, None
        if (clarify is not None and time.monotonic() - clarify["at"] < CLARIFY_WINDOW and self._tools is not None
                and not CANCEL.search(normalize(text))):
            latency["route"] = "tool:précision"
            self._event("routing", latency["route"])
            return self._run_call(history, f"{clarify['text']} ; précision : {text}",
                                  {"type": "tool_call", "tool": clarify["tool"], "parameters": {}}, latency), False
        if (self._call_rules is not None and self._in_call is not None and is_acknowledgement(text)
                and self._call_rules.acknowledge(self._in_call.id)):
            return self._say_text(history, text, "C'est noté, monsieur. Je ne vous rappellerai pas à ce sujet.",
                                  latency), True
        if (self._call_rules is not None and self._texting and is_acknowledgement(text)
                and self._call_rules.acknowledge()):
            return self._say_text(history, text, "C'est noté, monsieur.", latency), False
        if self._tools is not None and self._tools.registry.exists("flex"):
            norm = normalize(text)
            flex = ("set_flex_enabled" if FLEX_SWITCH.search(norm) and self._tools.registry.exists("set_flex_enabled")
                    else "flex" if is_flex(text) else None)
            if flex is not None:  # « Flex », « fais ta présentation » : reconnu sans LLM, décidé par les règles d'appel
                latency["route"] = f"tool:{flex}"
                self._event("routing", latency["route"])
                # Fin de la conversation : la présentation (canal vocal) commence dès qu'ORION est libre.
                return self._run_call(history, text, {"type": "tool_call", "tool": flex, "parameters": {}},
                                      latency), flex == "flex"
        compound = COMPOUND_DEPARTURE.match(text)
        if compound and self._tools is not None and self._tools.registry.exists("declare_departure"):
            # « Je sors, appelle-moi dans 3 minutes » : le départ, puis la suite comme une demande à part entière.
            declaration = parse_declaration(compound.group(1), datetime.now())
            if declaration is not None and declaration.kind in ("now", "later", "short"):
                first = ""
                if declaration.kind != "short":
                    latency["route"] = "tool:départ"
                    self._event("routing", latency["route"])
                    first = self._run_call(history, compound.group(1),
                                           {"type": "tool_call", "tool": "declare_departure", "parameters": {}},
                                           latency)
                rest, end = self._reply_to(history, compound.group(2), None, latency, speech_ended_at)
                return " ".join(part for part in (first, rest) if part) or None, end
        reaction = split_event(text) if self._routines is not None and self._tools is not None else None
        if reaction is not None:  # « Quand je rentre, allume l'entrée » : routine à chaque retour (ou départ)
            return self._schedule(history, text, reaction, latency), False
        if (self._tools is not None and self._tools.registry.exists("add_instruction")
                and instruction_of(text) is not None and split_schedule(text, datetime.now()) is None):
            latency["route"] = "tool:consigne"  # « Désormais, réponds-moi plus brièvement »
            self._event("routing", latency["route"])
            return self._run_call(history, text, {"type": "tool_call", "tool": "add_instruction", "parameters": {}},
                                  latency), False
        if (self._tools is not None and self._tools.registry.exists("create_call_rule")
                and CALL_RULE.search(normalize(text))):
            latency["route"] = "tool:règle d'appel"
            self._event("routing", latency["route"])
            return self._run_call(history, text, {"type": "tool_call", "tool": "create_call_rule", "parameters": {}},
                                  latency), False
        if self._tools is not None and self._tools.registry.exists("declare_departure") and \
                parse_declaration(text, datetime.now()) is not None:
            # « Je sors » : reconnu sans LLM, appliqué par le Core (outil declare_departure, moteur de présence).
            latency["route"] = "tool:départ"
            self._event("routing", latency["route"])
            return self._run_call(history, text, {"type": "tool_call", "tool": "declare_departure",
                                                  "parameters": {}}, latency), False
        follow_up = self._tool_follow_up(text)
        self._previous_request, self._last_tool_request = self._last_tool_request, ""
        route = Route("tool", "tool.follow_up") if follow_up else self._router.route(text)
        if route.source == "unavailable" and self._tools is not None and any(
                self._tools.registry.exists(t) for t in STALE_UNAVAILABLE.get(route.name, ())):
            route = Route("tool", "tool.action")  # « pas encore disponible » alors que l'outil existe
        promoted = self._promote(text, route)
        if promoted is not None:
            reply = self._try_promoted(history, text, promoted, latency)
            if reply is not None:
                self._event("assistant", reply)
                self._event("latency", format_latency(latency, speech_ended_at))
                return None, False
        if route.name == "stop":
            self._stop_alarm()
        if route.name == "false_wake":
            self._report_false_wake()
        if route.source == "llm" and self._unsure():
            route = Route("unsure", reply=self._router.phrase("not_understood"))
        latency["route"] = route.label
        self._event("routing", route.label)
        return self._answer(history, follow_up or text, route, latency), route.end_conversation

    def _report_false_wake(self) -> None:
        """« Ce n'était pas pour toi » juste après un réveil : le mot de réveil se durcit (jarvis.learning)."""
        if self._texting or self._in_call is not None or self._last_wake is None or self._learning is None:
            return
        learned = self._learning.false_wake(self._last_wake)
        self._false_wake = True
        self._event("learning", f"Faux réveil : seuil relevé de {learned['raise']:.2f}"
                                + (f", nouvelle règle ({learned['rule']})" if learned["rule"] else ""))

    def _answer(self, history: list[Message], text: str, route, latency: dict) -> str:
        if route.source == "tool" and self._tools is not None:
            return self._use_tool(history, text, route, latency)
        if route.reply is not None:
            reply = self._speak([route.reply], latency)
            self._remember(history, Message("user", text))
            self._remember(history, Message("assistant", reply))
            return reply
        if route.source == "llm" and self._tools is not None and not self._degraded():
            reply = self._open_plan(history, text, latency)
            if reply is not None:
                return reply
        refused = self._guarded(text, latency)
        if refused is not None:
            return self._say_text(history, text, refused, latency)
        if route.source == "web.search":
            if self._degraded():
                return self._say_phrase(history, text, "degraded_web", latency)
            return self._search_and_answer(history, text, latency)
        return self._ask(history, text, latency)

    def _open_plan(self, history: list[Message], text: str, latency: dict) -> str | None:
        """Demande qu'aucune règle du routeur n'a reconnue (« on n'y voit rien », « annule-le », « lis le premier ») :
        le planificateur décide s'il s'agit d'une action, avec la demande d'outil précédente pour contexte. Sans
        outil, None : la conversation libre reprend (jamais d'action ou de donnée inventée faute d'outil)."""
        started = time.perf_counter()
        previous, report = self._previous_request, {}
        data = plan(self._llm, text, self._tools.registry, previous=previous, report=report)
        latency["tool_plan"] = time.perf_counter() - started
        if data is None and report.get("proposed") and previous and not named(report["proposed"], text,
                                                                               self._tools.registry):
            # Action proposée seulement d'après la demande précédente (« c'est joli » après « passe en cyan ») :
            # une remarque, pas une demande ; la conversation libre y répond.
            self._event("routing", f"tool:plan (remarque, pas d'action : {report['proposed']})")
            return None
        if data is None and report.get("proposed"):
            # Une action était voulue mais pas assez précise (niveau non dit...) : on demande, sans rien inventer.
            self._event("routing", f"tool:plan (à préciser : {report['proposed']})")
            return self._say_text(history, text, self._router.phrase("action_unclear"), latency)
        if data is None and ORION_OBJECTS.search(normalize(text)):
            self._event("routing", "tool:plan (objet d'ORION sans outil : à préciser)")
            return self._say_text(history, text, self._router.phrase("action_unclear"), latency)
        if data is None:
            return None
        latency["route"] = "tool:plan"
        self._event("routing", "tool:plan" + (" (suite)" if previous else ""))
        said = f"{previous.rstrip(' .!?')}. {text}" if previous else text
        if data.get("type") == "tool_calls":
            return self._use_tools(history, said, data["calls"], latency)
        reply = self._run_call(history, said, data, latency)
        if self._last_outcome is not None and self._last_outcome.status == DONE and self._last_outcome.result.success:
            self._last_tool_request = text
        return reply

    def _guarded(self, text: str, latency: dict) -> str | None:
        """Phrase de refus si le garde-fou écarte la demande avant le modèle (recherche comprise), sinon None."""
        if self._guard is None:
            return None
        user = self.request_context.user_id if self.request_context is not None else ""
        profile = self._profiles.user(user) if self._profiles is not None else None
        verdict = self._guard.check(text, profile.role if profile is not None else "guest")
        latency["garde-fou"] = verdict.seconds
        if verdict.allowed:
            return None
        self._event("guard", f"Demande écartée par le garde-fou ({verdict.category})")
        return self._router.phrase(f"guard_{verdict.category}") or self._router.phrase("guard_dangerous")

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
        schedule = split_schedule(text, self._routines.clock()) if self._routines is not None and not route.tool \
            else None
        if schedule is not None:
            return self._schedule(history, text, schedule, latency)
        if route.tool:
            data = {"type": "tool_call", "tool": route.tool, "parameters": {}}
        else:
            started = time.perf_counter()
            data = self._quick(text, latency) if self._fast_path or self._degraded() else None
            if data is None and self._degraded():
                return self._say_phrase(history, text, "degraded_unknown", latency)
            if data is None:
                data = plan(self._llm, text, self._tools.registry)
                if data is None and self._degraded():  # le worker vient de tomber pendant le choix d'outil
                    data = self._quick(text, latency)
            latency["tool_plan"] = time.perf_counter() - started
            if data is None:
                self._action_unclear = True  # demande d'action sans outil reconnu : pas de « fonction indisponible »
                fallback = self._router.route(text, tools=False)
                latency["route"] = fallback.label
                self._event("routing", f"{fallback.label} (aucun outil)")
                if fallback.source == "unavailable" and any(
                        self._tools.registry.exists(t) for t in STALE_UNAVAILABLE.get(fallback.name, ())):
                    # Ces réponses (« les rappels ne sont pas encore disponibles ») valent pour une fonction absente ;
                    # ici l'outil existe, la demande n'était simplement pas assez claire : on demande de préciser.
                    return self._say_text(history, text, self._router.phrase("action_unclear"), latency)
                return self._answer(history, text, fallback, latency)
            if data.get("type") == "tool_calls":
                return self._use_tools(history, text, data["calls"], latency)
        reply = self._run_call(history, text, data, latency)
        if self._last_outcome is not None and self._last_outcome.status == DONE and self._last_outcome.result.success:
            self._last_tool_request = "" if route.tool else text
        return reply

    def _run_call(self, history: list[Message], text: str, data: dict, latency: dict) -> str:
        """Une demande d'outil -> Core ; une action réussie devient le contexte des compléments (« À 30 %. »)."""
        data = self._with_hidden(self._with_place(data), text)
        if self._unsure_repeat(data):
            self._last_outcome = None
            latency["route"] = "unsure (même action)"
            self._event("routing", latency["route"])
            return self._say_phrase(history, text, "unsure_repeat", latency)
        self._event("tool", json.dumps(data, ensure_ascii=False)[:200])
        started = time.perf_counter()
        outcome = self._last_outcome = self._tools.submit(data)
        latency["tool"] = time.perf_counter() - started
        error = outcome.result.error if outcome.result is not None else None
        # Règle d'appel incomplète : la question est posée, la réponse suivante complète la demande.
        self._clarify = ({"tool": data.get("tool"), "text": text, "at": time.monotonic()}
                         if error == NEEDS_DETAILS else None)
        reply = self._after_tool(history, text, outcome, latency)
        if outcome.status == DONE and outcome.result.success:
            self._record(data, outcome.result.result)
            self._last_done = (data.get("tool"), self._call_key(data), time.monotonic())
            place = outcome.result.result.get("location") if isinstance(outcome.result.result, dict) else None
            self._place = place if data.get("tool") == WEATHER_TOOL and place else self._place
        return reply

    def _record(self, data: dict, result=None) -> None:
        if self._context is not None:
            self._context.record(data["tool"], data.get("parameters") or {}, result)

    def _schedule(self, history: list[Message], text: str, schedule, latency: dict) -> str:
        """« Dans 10 minutes, allume la chambre. » : la commande est comprise maintenant, puis enregistrée comme
        routine ; à l'heure dite, elle passe par le Core comme une demande vocale. Seules les actions sans
        confirmation peuvent être programmées (personne ne sera là pour répondre « oui »)."""
        latency["route"] = "tool:schedule"
        self._event("routing", f"{latency['route']} ({schedule.said})")
        started = time.perf_counter()
        data = self._quick(schedule.command, latency) if self._fast_path or self._degraded() else None
        if data is None and not self._degraded():
            data = plan(self._llm, schedule.command, self._tools.registry)
        latency["tool_plan"] = time.perf_counter() - started
        if data is None:
            return self._say_text(history, text, "Je n'ai pas compris quelle action programmer.", latency)
        calls = data["calls"] if data.get("type") == "tool_calls" else [data]
        actions, previous = [], {}
        for call in calls:
            resolved = self._with_hidden({"type": "tool_call", "tool": call["tool"],
                                          "parameters": dict(call.get("parameters") or {})},
                                         schedule.command, call.get("segment", schedule.command), previous)
            previous = resolved["parameters"]
            if self._tools.registry.exists(resolved["tool"]):
                decision = self._tools.permissions.decide(self._tools.user, self._tools.registry.get(resolved["tool"]),
                                                          resolved["parameters"])
                if decision.decision.value == "deny":
                    return self._say_text(history, text, "Je n'ai pas l'autorisation de programmer cela.", latency)
            actions.append({"type": "tool", "tool": resolved["tool"], "parameters": resolved["parameters"]})
        from jarvis.routines.model import RoutineError

        try:
            self._routines.create({"name": schedule.command[:60], "description": f"Programmé par la voix : {text}"[:200],
                                   "enabled": True, "once": schedule.trigger["type"] == "at",
                                   "trigger": schedule.trigger, "actions": actions})
        except RoutineError as exc:
            message = str(exc)
            if "confirmation" in message:
                message = "cette action demande une confirmation, je ne peux pas la programmer"
            return self._say_text(history, text, f"Je ne peux pas programmer cela : {message[:1].lower()}{message[1:]}"
                                  .rstrip(".") + ".", latency)
        command = schedule.command[:1].lower() + schedule.command[1:]
        return self._say_text(history, text, f"C'est programmé {schedule.said} : {command}.", latency)

    def _promote(self, text: str, route) -> dict | None:
        """Demande partie vers la conversation mais qui est une commande simple certaine (« Mais Without Me
        d'Eminem », « mets » mal transcrit) : la commande, sinon None."""
        if route.source != "llm" or self._tools is None:
            return None
        data = quick_plan(text, self._tools.registry, ignored=(self.settings.assistant_name, self.settings.wake_phrase))
        return data if data is not None and data.get("tool") == "spotify_play" else None

    def _try_promoted(self, history: list[Message], text: str, data: dict, latency: dict) -> str | None:
        """Exécute la commande ; si elle échoue (rien de cohérent trouvé), None : la conversation reprend la main."""
        data = self._with_hidden(data, text)
        outcome = self._tools.submit(data)
        if outcome.status != DONE or not outcome.result.success:
            self._event("routing", "llm (commande non confirmée)")
            return None
        latency["route"] = "tool:quick (promue)"
        self._event("routing", latency["route"])
        self._record(data, outcome.result.result)
        return self._after_tool(history, text, outcome, latency)

    def _say_text(self, history: list[Message], text: str, reply: str, latency: dict) -> str:
        reply = self._speak([reply], latency)
        self._remember(history, Message("user", text))
        self._remember(history, Message("assistant", reply))
        return reply

    def _say_sources(self, history: list[Message], text: str, latency: dict) -> str:
        """« Quelles sont tes sources ? » : les sites de la dernière recherche (jamais les adresses, illisibles)."""
        sites = list(dict.fromkeys(s.get("source", "") for s in self.last_sources if s.get("source")))[:4]
        reply = "D'après " + (", ".join(sites[:-1]) + " et " + sites[-1] if len(sites) > 1 else sites[0]) + "." \
            if sites else "Je n'ai pas de source à vous citer."
        return self._say_text(history, text, reply, latency)

    def _degraded(self) -> bool:
        """Worker LLM hors ligne ou défaillant : seules les commandes simples (sans LLM) sont tentées."""
        return bool(getattr(self._llm, "degraded", False))

    def _quick(self, text: str, latency: dict) -> dict | None:
        if quoted(text):  # « Répète après moi : verrouille le PC » n'est jamais une commande
            return None
        data = quick_plan(text, self._tools.registry, ignored=(self.settings.assistant_name, self.settings.wake_phrase))
        if data is not None:
            latency["route"] = "tool:quick" + (" (mode dégradé)" if self._degraded() else "")
            self._event("routing", latency["route"])
        return data

    def _say_phrase(self, history: list[Message], text: str, key: str, latency: dict) -> str:
        reply = self._speak([self._router.phrase(key)], latency)
        self._remember(history, Message("user", text))
        self._remember(history, Message("assistant", reply))
        return reply

    def _with_hidden(self, data: dict, text: str, segment: str | None = None, previous: dict | None = None) -> dict:
        """Paramètres masqués au LLM (appareil, pièce...), déduits des mots de la demande. Dans une demande à
        plusieurs actions, chacune prend ceux de sa partie de phrase (``segment``) ; si elle n'en nomme aucun,
        ceux de l'action précédente (« allume la chambre à 30 %, en bleu » : le bleu vise aussi la chambre)."""
        name, parameters = data.get("tool"), data.get("parameters")
        if not isinstance(parameters, dict) or not self._tools.registry.exists(name):
            return data
        resolved = {}
        for key, param in self._tools.registry.get(name).parameters.items():
            if not param.hidden or param.resolve is None or key in parameters:
                continue
            value = param.resolve(text)
            if segment is not None:
                own = param.resolve(segment)
                value = own if own != param.resolve("") else (previous or {}).get(key, value)
            if value is not None:
                resolved[key] = value
        return {**data, "parameters": {**parameters, **resolved}} if resolved else data

    def _use_tools(self, history: list[Message], text: str, calls: list[dict], latency: dict,
                   previous: dict | None = None) -> str:
        """Plusieurs actions dans l'ordre ; arrêt à la première qui échoue ou demande une confirmation (la suite
        attend alors la réponse)."""
        spoken, previous = [], dict(previous or {})
        started = time.perf_counter()
        for index, call in enumerate(calls):
            data = self._with_hidden({"type": "tool_call", "tool": call["tool"], "parameters": call["parameters"]},
                                     text, call.get("segment", text), previous)
            previous = data["parameters"]
            self._event("tool", json.dumps(data, ensure_ascii=False)[:200])
            outcome = self._tools.submit(data)
            if outcome.status == CONFIRM:
                spoken.append(outcome.question)
                if calls[index + 1:]:
                    self._after_confirmation = (text, calls[index + 1:], previous)
                break
            if outcome.result is not None and outcome.result.message:
                spoken.append(outcome.result.message)
            if outcome.status != DONE or not outcome.result.success:
                break
            self._record(data, outcome.result.result)
        latency["tool"] = time.perf_counter() - started
        reply = self._speak(spoken or [self._router.phrase("action_unavailable")], latency)
        self._remember(history, Message("user", text))
        self._remember(history, Message("assistant", reply))
        return reply

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
            if outcome.result.success and outcome.result.tool == "forget":
                # Ce qui vient d'être oublié ne doit plus pouvoir être redit à partir de la conversation en cours.
                history.clear()
            if outcome.result.success and not outcome.result.message:
                return self._ask(history, text, latency, tool_result=outcome.result)
        if outcome.status == CONFIRM:
            spoken = outcome.question
        elif outcome.status == CANCELLED:
            spoken = self._router.phrase("tool_cancelled")
        elif outcome.result is not None and outcome.result.message in MALFORMED_MESSAGES:
            # Proposition du LLM mal formée (champ intrus...) : jamais lue telle quelle à l'utilisateur.
            spoken = self._router.phrase("action_unclear")
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
            sites = ", ".join(dict.fromkeys(s.get("source", "") for s in context.sources if s.get("source")))
            self._event("web", f"« {context.query} » : {len(context.results)} résultat(s) retenu(s), "
                               f"{len(context.pages)} page(s) lue(s)" + (f" — {context.error}" if context.error else "")
                        + (f" — sources : {sites}" if sites else ""))
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
        elif not searched and self._learning is not None:
            reminders = self._learning.matching(text)
            if reminders:  # consigne durable qui concerne cette demande : rappelée juste avant elle
                notes = " ; ".join(f"« {r} »" for r in reminders[:3])
                messages[-1] = Message("user", f"{text}\n\n(Consigne durable de l'utilisateur qui s'applique ici, à "
                                               f"suivre directement : {notes})")
        succeeded = tool_result is not None and tool_result.success
        fallback = tool_result.message if tool_result is not None and not tool_result.success else None
        if fallback is None and self._action_unclear:
            fallback = self._router.phrase("action_unclear")
        self._action_unclear = False
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
                self._event("error", "LLM indisponible")
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
        watcher = (WakeWatcher(self._source, self._wake_word, self._wake_threshold(), self._interrupt,
                               self.settings.wake_patience).start()
                   if self.settings.barge_in and not self._texting else None)
        try:
            stats = self._pipeline.speak(sentences)
        finally:
            if watcher is not None:
                watcher.stop()
        if not self._texting:  # message écrit : le micro de la maison n'est pas touché
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

    def _stop_alarm(self) -> None:
        if self._alarm is not None and self._alarm.stop():
            self._event("alarm", "Réveil arrêté")

    def _interrupt(self, score: float) -> None:
        """Wake word dit pendant la réponse : elle est abandonnée et le son coupé ; ORION écoute la suite."""
        self._interrupted = True
        self._event("wake", f"Interruption (score {score:.2f})")
        self._pipeline.cancel()
        stop = getattr(self._sink, "stop", None)
        if stop is not None:
            try:
                stop()
            except Exception:
                log.exception("Arrêt de la lecture impossible")

    @staticmethod
    def _call_key(data: dict) -> str:
        return json.dumps(data.get("parameters") or {}, ensure_ascii=False, sort_keys=True, default=str)

    def _unsure_repeat(self, data: dict) -> bool:
        if self._texting:  # message écrit : aucune transcription douteuse
            return False
        threshold, confidence = self.settings.repeat_confidence, getattr(self._stt, "last_confidence", None)
        if threshold is None or confidence is None or confidence >= threshold or self._last_done is None:
            return False
        tool, key, at = self._last_done
        registry = self._tools.registry
        return (data.get("tool") == tool and self._call_key(data) == key and time.monotonic() - at < REPEAT_WINDOW
                and registry.exists(tool) and registry.get(tool).risk is Risk.SAFE)

    def _unsure(self) -> bool:
        """Transcription trop peu sûre pour être confiée au LLM (« ta gueule » entendu « regarde-moi le gul »).
        Jamais pour un message écrit (rien n'a été transcrit)."""
        if self._texting:
            return False
        confidence = getattr(self._stt, "last_confidence", None)
        return self.settings.min_confidence is not None and confidence is not None \
            and confidence < self.settings.min_confidence

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
