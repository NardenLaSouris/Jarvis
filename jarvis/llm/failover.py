"""LLM principal sur une autre machine (worker GPU), LLM local en secours.

Avant chaque appel, une connexion TCP rapide vérifie que le principal répond : s'il est éteint, JARVIS
ne reste pas bloqué jusqu'au délai d'attente du LLM et répond aussitôt avec le LLM local. Le principal
est retenté après ``retry_after`` secondes.
"""

from __future__ import annotations

import logging
import socket
import time
from typing import Callable, Iterator
from urllib.parse import urlsplit

from jarvis.interfaces import Message

log = logging.getLogger(__name__)


def tcp_reachable(url: str, timeout: float = 1.0) -> bool:
    parts = urlsplit(url)
    try:
        with socket.create_connection((parts.hostname, parts.port or 80), timeout=timeout):
            return True
    except OSError:
        return False


class FailoverLLM:
    def __init__(self, primary, fallback, primary_url: str, retry_after: float = 30.0,
                 reachable: Callable[[str], bool] = tcp_reachable, clock: Callable[[], float] = time.monotonic):
        self._primary, self._fallback = primary, fallback
        self._url = primary_url
        self._retry_after = retry_after
        self._reachable = reachable
        self._clock = clock
        self._down_since: float | None = None
        self._last = primary

    @property
    def last_stats(self) -> dict:
        return getattr(self._last, "last_stats", {})

    @property
    def last_done_reason(self) -> str:
        return getattr(self._last, "last_done_reason", "")

    def chat(self, messages: list[Message]) -> str:
        return self._call(lambda llm: llm.chat(messages))

    def chat_json(self, messages: list[Message], schema: dict) -> dict:
        return self._call(lambda llm: llm.chat_json(messages, schema))

    def stream(self, messages: list[Message]) -> Iterator[str]:
        """Bascule sur le secours si le principal échoue avant le premier fragment (jamais au milieu d'une phrase)."""
        if self._primary_usable():
            self._last = self._primary
            started = False
            try:
                for piece in self._primary.stream(messages):
                    started = True
                    yield piece
                return
            except Exception as exc:
                if started:
                    raise
                self._primary_failed(exc)
        self._last = self._fallback
        yield from self._fallback.stream(messages)

    def warm_up(self) -> None:
        self._each("préchargement", lambda llm: llm.warm_up())

    def prime(self, prompts: list[list[Message]]) -> None:
        self._each("préparation des prompts", lambda llm: llm.prime(prompts))

    def _call(self, action):
        if self._primary_usable():
            try:
                self._last = self._primary
                return action(self._primary)
            except Exception as exc:
                self._primary_failed(exc)
        self._last = self._fallback
        return action(self._fallback)

    def _primary_usable(self) -> bool:
        if self._down_since is not None and self._clock() - self._down_since < self._retry_after:
            return False
        if not self._reachable(self._url):
            self._primary_failed(ConnectionError("injoignable"))
            return False
        if self._down_since is not None:
            log.info("LLM principal de retour (%s)", self._url)
            self._down_since = None
        return True

    def _primary_failed(self, exc: Exception) -> None:
        if self._down_since is None:
            log.warning("LLM principal indisponible (%s : %s) : LLM local utilisé", self._url, exc)
        self._down_since = self._clock()

    def _each(self, label: str, action) -> None:
        """Les deux LLM : le principal d'abord (rapide), puis le secours, pour qu'une bascule soit aussitôt rapide."""
        for name, llm, usable in (("principal", self._primary, self._reachable(self._url)), ("local", self._fallback, True)):
            if not usable:
                log.info("LLM %s injoignable : %s ignoré(e)", name, label)
                continue
            try:
                action(llm)
            except Exception as exc:
                log.warning("LLM %s : %s impossible (%s)", name, label, exc)
