"""Appels vocaux sortants d'ORION vers l'appareil mobile jumelé : le Core décide, la passerelle transporte.

Le gestionnaire est la seule porte d'entrée d'un appel. Il refuse :
- une origine qui n'est pas du Core (contenu d'un mail, page Web...) ;
- tout appel tant qu'aucun appareil n'est jumelé ; le destinataire n'est jamais un paramètre : c'est l'appareil
  de la configuration ;
- un deuxième appel pendant le premier, et ce que les limites interdisent (orion/call/limits.py).
La passerelle (``notifier`` : faire sonner, annuler, raccrocher ; ``open_terminal`` : micro et sortie de l'appel)
signale l'acceptation, le refus, la connexion audio et la fin ; le gestionnaire bascule alors le micro et la sortie
d'ORION (orion/audio/switch.py) et la boucle de conversation existante répond (``take_connected``, ``active``,
``finish``). Les délais (sonnerie, connexion, durée maximale) sont tenus par ``tick``, appelé chaque seconde.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Protocol

from orion.call.limits import CallLimits, CallLimitSettings
from orion.call.session import ACCEPTED, CONNECTED, ENDED, EXPIRED, FAILED, REFUSED, RINGING, CallError, CallSession

log = logging.getLogger(__name__)

CORE_ORIGINS = ("user", "routine", "admin")
MAX_DIALS_PER_HOUR = 30  # appels du téléphone vers ORION : seulement contre une boucle (application qui s'emballe)
MAX_PENDING_MESSAGES = 10
MAX_REASON = 2400  # ce qu'ORION dit au décroché (une présentation Flex entière : jamais coupée en plein mot)  # messages en attente pendant une conversation ou un appel
END_EVENTS = {ENDED: "call.ended", REFUSED: "call.refused", EXPIRED: "call.expired", FAILED: "call.failed"}


@dataclass(frozen=True)
class CallSettings:
    device: str = ""  # identifiant de l'appareil jumelé ; vide : aucun appel possible
    ring_timeout: float = 40.0  # s sans réponse : expiré
    connect_timeout: float = 15.0  # s entre « accepté » et l'audio établi : échec
    max_duration: float = 900.0  # s : l'appel est terminé par ORION au-delà
    limits: CallLimitSettings = CallLimitSettings()


class CallRefused(Exception):
    pass


class CallNotifier(Protocol):
    def ring(self, call: CallSession) -> None: ...

    def cancel(self, call: CallSession) -> None: ...

    def hang_up(self, call: CallSession) -> None: ...

    def answer(self, call: CallSession) -> None: ...

    def send_message(self, text: str) -> None: ...


class LogNotifier:
    """Sans passerelle : rien ne sonne ; l'appel expirera (utile pour tester le Core seul)."""

    def ring(self, call: CallSession) -> None:
        log.info("Appel %s : aucune passerelle d'appel, rien ne sonne", call.id)

    def cancel(self, call: CallSession) -> None:
        pass

    def hang_up(self, call: CallSession) -> None:
        pass

    def send_message(self, text: str) -> None:
        log.info("Message : aucune passerelle d'appel, réponse non envoyée")

    def answer(self, call: CallSession) -> None:
        pass


class CallManager:
    def __init__(self, settings: CallSettings, source, sink, open_terminal: Callable[[CallSession], tuple],
                 notifier: CallNotifier | None = None, clock: Callable[[], float] = time.time,
                 on_event: Callable[[str, dict], None] | None = None):
        self.settings = settings
        self._source, self._sink = source, sink  # SwitchableSource, SwitchableSink
        self._open_terminal = open_terminal
        self._notifier = notifier or LogNotifier()
        self._clock = clock
        self._on_event = on_event or (lambda kind, data: None)
        self._limits = CallLimits(settings.limits)
        self._dials: deque[float] = deque()  # appels composés par le téléphone (limite anti-boucle à part)
        self._inbox: deque[str] = deque(maxlen=MAX_PENDING_MESSAGES)  # vos messages pas encore traités
        self._lock = threading.RLock()
        self.current: CallSession | None = None
        self._handed_over = False  # appel connecté déjà pris par la conversation
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    # --- Demande (Core seulement) ------------------------------------------------------------------------

    def request(self, reason: str, origin: str) -> CallSession:
        """Appelle l'appareil jumelé ; CallRefused avec la raison sinon (rien ne sonne)."""
        now = self._clock()
        with self._lock:
            if origin not in CORE_ORIGINS:
                raise CallRefused(f"origine non autorisée ({origin})")
            if not self.settings.device:
                raise CallRefused("aucun appareil jumelé")
            if self.current is not None and not self.current.final:
                raise CallRefused("un appel est déjà en cours")
            refusal = self._limits.refusal(now)
            if refusal:
                raise CallRefused(refusal)
            call = self.current = CallSession(self.settings.device, str(reason or "").strip()[:MAX_REASON], origin,
                                              now)
            self._handed_over = False
            self._limits.started(now)
            self._event("call.requested", call)
            try:
                self._notifier.ring(call)
            except Exception as exc:  # noqa: BLE001 - passerelle absente ou en panne : l'appel échoue proprement
                log.warning("Appel %s : impossible de faire sonner (%s)", call.id, type(exc).__name__)
                self._end(call, FAILED, "sonnerie impossible", now)
                return call
            call.move(RINGING, now)
            self._event("call.ringing", call)
            return call

    def dialed(self, call_id: str, device: str) -> CallSession:
        """Le téléphone jumelé appelle ORION : décroché aussitôt (origine « user », comme une demande à la maison).
        CallRefused si ce n'est pas l'appareil jumelé, si ORION est déjà en ligne ou au-delà de MAX_DIALS_PER_HOUR."""
        now = self._clock()
        with self._lock:
            if not self.settings.device or device != self.settings.device:
                raise CallRefused("appareil non jumelé au Core")
            if self.current is not None and not self.current.final:
                raise CallRefused("occupé")
            while self._dials and now - self._dials[0] >= 3600:
                self._dials.popleft()
            if len(self._dials) >= MAX_DIALS_PER_HOUR:
                raise CallRefused("trop d'appels sur la dernière heure")
            self._dials.append(now)
            call = self.current = CallSession(device, "", "user", now, id=str(call_id)[:64])
            self._handed_over = False
            call.move(RINGING, now)
            call.move(ACCEPTED, now)
            self._event("call.accepted", call)
            try:
                self._notifier.answer(call)  # la passerelle laisse maintenant le téléphone ouvrir l'audio
            except Exception as exc:  # noqa: BLE001
                log.warning("Appel %s : passerelle non prévenue du décroché (%s)", call.id, type(exc).__name__)
                self._end(call, FAILED, "décroché impossible", now, notify=False)
            return call

    # --- Signaux de la passerelle --------------------------------------------------------------------------

    def _call(self, call_id: str) -> CallSession:
        call = self.current
        if call is None or call.id != call_id or call.final:
            raise CallError("appel inconnu ou terminé")
        return call

    def accepted(self, call_id: str) -> None:
        with self._lock:
            call = self._call(call_id)
            call.move(ACCEPTED, self._clock())
            self._event("call.accepted", call)

    def refused(self, call_id: str) -> None:
        with self._lock:
            self._end(self._call(call_id), REFUSED, "refusé sur le téléphone", self._clock(), notify=False)

    def connected(self, call_id: str) -> None:
        """Audio établi : le micro et la sortie d'ORION passent sur le téléphone."""
        with self._lock:
            call = self._call(call_id)
            if call.state != ACCEPTED:
                raise CallError(f"appel {call.state} : connexion audio inattendue")
            try:
                source, sink = self._open_terminal(call)
            except Exception as exc:  # noqa: BLE001 - micro ou sortie de l'appel indisponibles
                log.warning("Appel %s : terminal audio indisponible (%s)", call.id, type(exc).__name__)
                self._end(call, FAILED, "audio indisponible", self._clock())
                return
            call.move(CONNECTED, self._clock())
            self._sink.use_call(sink)
            self._source.use_call(source)
            self._event("call.connected", call)

    def hung_up(self, call_id: str) -> None:
        with self._lock:
            self._end(self._call(call_id), ENDED, "raccroché sur le téléphone", self._clock(), notify=False)

    def failed(self, call_id: str, why: str = "réseau") -> None:
        with self._lock:
            self._end(self._call(call_id), FAILED, why, self._clock(), notify=False)

    # --- Messages (Linphone) -----------------------------------------------------------------------------

    def message_received(self, text: str) -> None:
        """Votre message (seule votre adresse passe la passerelle) : traité par la conversation quand elle est
        libre ; au plus MAX_PENDING_MESSAGES en attente (les plus anciens sont oubliés)."""
        with self._lock:
            self._inbox.append(str(text)[:2000])
        log.info("Message reçu (%d caractères)", len(text))

    def take_message(self) -> str | None:
        with self._lock:
            return self._inbox.popleft() if self._inbox else None

    def reply(self, text: str) -> bool:
        """Réponse d'ORION à votre message ; False si la passerelle ne l'a pas transmise."""
        try:
            self._notifier.send_message(text)
            return True
        except Exception as exc:  # noqa: BLE001 - passerelle absente : la réponse est perdue, ORION continue
            log.warning("Réponse au message non envoyée (%s)", type(exc).__name__)
            return False

    # --- Boucle de conversation ------------------------------------------------------------------------

    def take_connected(self) -> CallSession | None:
        """Appel connecté pas encore pris par la conversation (une seule fois)."""
        with self._lock:
            call = self.current
            if call is not None and call.state == CONNECTED and not self._handed_over:
                self._handed_over = True
                return call
            return None

    def active(self) -> bool:
        call = self.current
        return call is not None and call.state == CONNECTED

    def finish(self) -> None:
        """La conversation de l'appel est finie : ORION raccroche s'il est encore en ligne, puis revient à la maison."""
        with self._lock:
            call = self.current
            if call is not None and not call.final:
                self._end(call, ENDED, "fin de la conversation", self._clock())
            self._handed_over = False
            self._sink.use_home()
            self._source.use_home()

    # --- Délais ------------------------------------------------------------------------------------------

    def tick(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        with self._lock:
            call = self.current
            if call is None or call.final:
                return
            s = self.settings
            if call.state == RINGING and now - call.since(RINGING) >= s.ring_timeout:
                self._end(call, EXPIRED, "sans réponse", now)
            elif call.state == ACCEPTED and now - call.since(ACCEPTED) >= s.connect_timeout:
                self._end(call, FAILED, "connexion audio non établie", now)
            elif call.state == CONNECTED and now - call.since(CONNECTED) >= s.max_duration:
                self._end(call, ENDED, "durée maximale atteinte", now)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="appels", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        stop = getattr(self._notifier, "stop", None)
        if stop is not None:
            stop()

    def _run(self) -> None:
        while not self._stopping.wait(1.0):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - le fil des délais ne doit pas mourir
                log.exception("Appels : délais non vérifiés")

    # --- Fin ---------------------------------------------------------------------------------------------

    def _end(self, call: CallSession, state: str, why: str, now: float, notify: bool = True) -> None:
        """Fin de l'appel ; ``notify`` : prévenir la passerelle (pas quand c'est elle qui l'annonce)."""
        was = call.state
        call.move(state, now, why)
        self._limits.finished(state, now)
        if notify:
            try:
                if was == RINGING:
                    self._notifier.cancel(call)  # la sonnerie s'arrête sur le téléphone
                elif was in (ACCEPTED, CONNECTED):
                    self._notifier.hang_up(call)
            except Exception as exc:  # noqa: BLE001
                log.warning("Appel %s : passerelle non prévenue de la fin (%s)", call.id, type(exc).__name__)
        if was == CONNECTED:
            self._source.hold()  # silence jusqu'à ce que la conversation rende la main (``finish``)
        self._event(END_EVENTS[state], call)

    def _event(self, kind: str, call: CallSession) -> None:
        log.info("Appel %s : %s%s", call.id, call.state, f" ({call.end_reason})" if call.end_reason else "")
        try:
            self._on_event(kind, call.describe())
        except Exception:  # noqa: BLE001
            log.debug("Événement d'appel non publié", exc_info=True)
