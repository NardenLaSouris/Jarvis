"""Confirmation d'une action par l'utilisateur, gérée par le Core.

La question est posée par ORION (formulée par l'outil, pas par le LLM) ; la réponse est comparée à
des listes fixes de « oui » et de « non ». Toute autre phrase annule la demande en attente.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable

from jarvis.personality import normalize
from jarvis.tools.base import ToolRequest

YES, NO, OTHER, NONE = "yes", "no", "other", "none"


@dataclass(frozen=True)
class Pending:
    request: ToolRequest
    question: str
    asked_at: float
    user: str = ""  # qui a demandé : seul lui peut confirmer


class ConfirmationManager:
    def __init__(self, yes: Iterable[str], no: Iterable[str], ignored: Iterable[str] = (), ttl: float = 30.0,
                 clock: Callable[[], float] = time.monotonic):
        self._yes = {normalize(w) for w in yes}
        self._no = {normalize(w) for w in no}
        self._ignored = {normalize(w) for w in ignored}
        self._ttl = ttl
        self._clock = clock
        self._pending: Pending | None = None

    @property
    def pending(self) -> Pending | None:
        if self._pending is not None and self._clock() - self._pending.asked_at > self._ttl:
            self._pending = None
        return self._pending

    def ask(self, request: ToolRequest, question: str, user: str = "") -> str:
        self._pending = Pending(request, question, self._clock(), user)
        return question

    def answer(self, text: str) -> tuple[str, Pending | None]:
        pending = self.pending
        if pending is None:
            return NONE, None
        self._pending = None
        words = [w for w in normalize(text).split() if w not in self._ignored]
        said = " ".join(words)
        if said in self._yes:
            return YES, pending
        if said in self._no:
            return NO, pending
        return OTHER, pending

    def clear(self) -> None:
        self._pending = None
