"""LLM principal sur une autre machine (worker GPU, le Katana), LLM local en secours.

État du worker, toujours explicite :
- ONLINE : joignable et ses dernières réponses sont arrivées à temps ;
- DEGRADED : joignable, mais ses dernières demandes ont échoué (délai dépassé, erreur d'Ollama) ou ont été lentes ;
- OFFLINE : injoignable (machine éteinte, réseau, service arrêté).

Avant chaque appel, une connexion TCP courte (``connect_timeout``) vérifie que le worker répond : s'il est éteint,
JARVIS ne reste pas bloqué et passe aussitôt en mode dégradé. Tant qu'il est hors ligne, il n'est plus sollicité ;
une sonde de fond le reteste toutes les ``probe_interval`` secondes et le reprend dès qu'il revient (reconnexion
automatique, sans attendre une demande).

Mode dégradé : la conversation passe au LLM local avec un prompt court (``compact_system``) — le prompt complet
prendrait plus d'une minute sur processeur — et le choix d'outil n'est pas confié au LLM local (``json_fallback``) :
JARVIS utilise alors ses commandes déterministes (voir jarvis.tools.quick).
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator
from urllib.parse import urlsplit

from jarvis.interfaces import Message

log = logging.getLogger(__name__)

ONLINE, DEGRADED, OFFLINE = "ONLINE", "DEGRADED", "OFFLINE"


class LLMUnavailable(RuntimeError):
    """Aucun LLM utilisable pour cet appel (worker hors ligne et pas de secours prévu pour ce type d'appel)."""

    kind = "unavailable"


def tcp_reachable(url: str, timeout: float = 1.0) -> bool:
    parts = urlsplit(url)
    try:
        with socket.create_connection((parts.hostname, parts.port or 80), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass
class WorkerStats:
    """Métriques du worker : nombre de réussites et d'échecs par cause, latences (dernière et moyenne lissée)."""

    ok: int = 0
    errors: dict = field(default_factory=lambda: {"unreachable": 0, "timeout": 0, "error": 0})
    last_latency: float | None = None
    average_latency: float | None = None
    last_error: str = ""
    last_error_kind: str = ""

    def success(self, seconds: float) -> None:
        self.ok += 1
        self.last_latency = seconds
        self.average_latency = seconds if self.average_latency is None else 0.8 * self.average_latency + 0.2 * seconds

    def failure(self, kind: str, message: str) -> None:
        self.errors[kind] = self.errors.get(kind, 0) + 1
        self.last_error, self.last_error_kind = message[:200], kind


def _kind(exc: BaseException) -> str:
    return getattr(exc, "kind", "error") if getattr(exc, "kind", "") in ("unreachable", "timeout") else "error"


class FailoverLLM:
    def __init__(self, primary, fallback, primary_url: str, retry_after: float = 30.0,
                 reachable: Callable[[str], bool] = tcp_reachable, clock: Callable[[], float] = time.monotonic,
                 *, slow_after: float = 8.0, probe_interval: float = 10.0, json_fallback: bool = False,
                 compact_system: Callable[[], str] | None = None):
        self._primary, self._fallback = primary, fallback
        self._url = primary_url
        self._retry_after = retry_after
        self._reachable = reachable
        self._clock = clock
        self._slow_after = slow_after
        self._probe_interval = probe_interval
        self._json_fallback = json_fallback
        self.compact_system = compact_system
        self._down_since: float | None = None
        self._state = ONLINE
        self._since = clock()
        self._last = primary
        self.stats = WorkerStats()
        self.fallback_stats = WorkerStats()
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self.on_state: Callable[[str, bool], None] | None = None  # (état, utilisable) à chaque changement

    # --- État ----------------------------------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def degraded(self) -> bool:
        """Vrai quand le worker n'est pas utilisable maintenant : JARVIS doit éviter ce qui exige le LLM principal."""
        return self._state == OFFLINE or self._down_since is not None

    @property
    def last_stats(self) -> dict:
        return getattr(self._last, "last_stats", {})

    @property
    def last_done_reason(self) -> str:
        return getattr(self._last, "last_done_reason", "")

    def status(self) -> dict:
        s = self.stats
        return {"state": self._state, "since_s": round(self._clock() - self._since, 1), "url": self._url,
                "degraded": self.degraded, "ok": s.ok, "errors": dict(s.errors),
                "last_latency_s": None if s.last_latency is None else round(s.last_latency, 2),
                "average_latency_s": None if s.average_latency is None else round(s.average_latency, 2),
                "last_error": s.last_error, "last_error_kind": s.last_error_kind,
                "fallback": {"ok": self.fallback_stats.ok, "errors": dict(self.fallback_stats.errors)}}

    def _set_state(self, state: str, reason: str = "") -> None:
        with self._lock:
            if state == self._state:
                return
            previous, self._state, self._since = self._state, state, self._clock()
        level = logging.INFO if state == ONLINE else logging.WARNING
        log.log(level, "Worker LLM %s : %s -> %s%s", self._url, previous, state, f" ({reason})" if reason else "")
        if self.on_state is not None:
            try:
                self.on_state(state, state == ONLINE)
            except Exception:
                log.debug("Notification d'état du worker impossible", exc_info=True)

    # --- Appels --------------------------------------------------------------------------------------

    def chat(self, messages: list[Message]) -> str:
        return self._call(lambda llm, msgs: llm.chat(msgs), messages)

    def chat_json(self, messages: list[Message], schema: dict) -> dict:
        return self._call(lambda llm, msgs: llm.chat_json(msgs, schema), messages, fallback=self._json_fallback)

    def stream(self, messages: list[Message]) -> Iterator[str]:
        """Bascule sur le secours si le principal échoue avant le premier fragment (jamais au milieu d'une phrase)."""
        if self._primary_usable():
            self._last = self._primary
            started, first = self._clock(), False
            try:
                for piece in self._primary.stream(messages):
                    if not first:
                        first = True
                        self._succeeded(self._clock() - started)
                    yield piece
                return
            except Exception as exc:
                if first:
                    raise
                self._primary_failed(exc)
        self._last = self._fallback
        yield from self._fallback_call(lambda llm, msgs: llm.stream(msgs), messages, stream=True)

    def warm_up(self) -> None:
        self._each("préchargement", lambda llm: llm.warm_up(), lambda llm: llm.warm_up())

    def prime(self, prompts: list[list[Message]]) -> None:
        """Le principal lit les prompts complets ; le secours seulement le prompt court qu'il utilisera."""
        compact = [[Message("system", self.compact_system()), Message("user", "Bonjour.")]] if self.compact_system \
            else prompts
        self._each("préparation des prompts", lambda llm: llm.prime(prompts), lambda llm: llm.prime(compact))

    def _call(self, action, messages, fallback: bool = True):
        if self._primary_usable():
            started = self._clock()
            try:
                self._last = self._primary
                result = action(self._primary, messages)
                self._succeeded(self._clock() - started)
                return result
            except Exception as exc:
                self._primary_failed(exc)
        if not fallback:
            raise LLMUnavailable(f"LLM principal {self._state.lower()} : pas de secours pour cet appel")
        self._last = self._fallback
        return self._fallback_call(action, messages)

    def _fallback_call(self, action, messages, stream: bool = False):
        messages = self._compact(messages)
        if not stream:
            try:
                result = action(self._fallback, messages)
            except Exception as exc:
                self.fallback_stats.failure(_kind(exc), str(exc))
                raise
            self.fallback_stats.ok += 1
            return result
        return self._fallback_stream(action, messages)

    def _fallback_stream(self, action, messages):
        try:
            yield from action(self._fallback, messages)
        except Exception as exc:
            self.fallback_stats.failure(_kind(exc), str(exc))
            raise
        self.fallback_stats.ok += 1

    def _compact(self, messages: list[Message]) -> list[Message]:
        if self.compact_system is None or not messages or messages[0].role != "system":
            return messages
        return [Message("system", self.compact_system()), *messages[1:]]

    # --- Santé ---------------------------------------------------------------------------------------

    def _primary_usable(self) -> bool:
        if self._down_since is not None and self._clock() - self._down_since < self._retry_after:
            return False
        if not self._reachable(self._url):
            self._primary_failed(ConnectionError("injoignable"), kind="unreachable")
            return False
        if self._down_since is not None:
            log.info("LLM principal de retour (%s)", self._url)
            self._down_since = None
        return True

    def _succeeded(self, seconds: float) -> None:
        self.stats.success(seconds)
        self._down_since = None
        self._set_state(DEGRADED if seconds > self._slow_after else ONLINE,
                        f"réponse lente : {seconds:.1f} s" if seconds > self._slow_after else "")

    def _primary_failed(self, exc: Exception, kind: str | None = None) -> None:
        kind = kind or _kind(exc)
        self.stats.failure(kind, str(exc))
        if self._down_since is None:
            log.warning("LLM principal indisponible (%s, %s : %s) : mode dégradé", self._url, kind, exc)
        self._down_since = self._clock()
        self._set_state(OFFLINE if kind == "unreachable" else DEGRADED, kind)

    def probe(self) -> str:
        """Vérifie le worker (connexion TCP courte) et met l'état à jour ; rend l'état."""
        if self._reachable(self._url):
            if self._state == OFFLINE or self._down_since is not None:
                self._down_since = None
                self._set_state(ONLINE, "de nouveau joignable")
        else:
            self._primary_failed(ConnectionError("injoignable"), kind="unreachable")
        return self._state

    def start(self) -> None:
        """Sonde de fond : reconnexion automatique dès que le worker revient."""
        if self._thread is None and self._probe_interval > 0:
            self._stopping.clear()
            self._thread = threading.Thread(target=self._probe_loop, name="llm-worker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _probe_loop(self) -> None:
        while not self._stopping.wait(self._probe_interval):
            try:
                self.probe()
            except Exception:  # la sonde ne doit jamais arrêter JARVIS
                log.exception("Sonde du worker LLM en erreur")

    def _each(self, label: str, primary_action, fallback_action) -> None:
        """Les deux LLM : le principal d'abord (rapide), puis le secours, pour qu'une bascule soit aussitôt rapide."""
        for name, llm, usable, action in (("principal", self._primary, self._reachable(self._url), primary_action),
                                          ("local", self._fallback, True, fallback_action)):
            if not usable:
                log.info("LLM %s injoignable : %s ignoré(e)", name, label)
                if name == "principal":
                    self._primary_failed(ConnectionError("injoignable"), kind="unreachable")
                continue
            try:
                action(llm)
            except Exception as exc:
                log.warning("LLM %s : %s impossible (%s)", name, label, exc)
