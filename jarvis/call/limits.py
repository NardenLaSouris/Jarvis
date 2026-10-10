"""Limites des appels sortants : jamais de harcèlement ni de boucle d'appels, quoi qui les demande.

- au plus ``max_per_hour`` appels sur une heure glissante ;
- au moins ``min_interval`` secondes entre deux appels ;
- après un refus (ou un appel resté sans réponse), pause de ``refusal_pause`` secondes ;
- un seul appel à la fois (vérifié par le gestionnaire).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from jarvis.call.session import EXPIRED, REFUSED


@dataclass(frozen=True)
class CallLimitSettings:
    max_per_hour: int = 3
    min_interval: float = 600.0
    refusal_pause: float = 1800.0


class CallLimits:
    def __init__(self, settings: CallLimitSettings = CallLimitSettings()):
        self.settings = settings
        self._starts: deque[float] = deque()
        self._paused_until = 0.0

    def refusal(self, now: float) -> str | None:
        """Raison du refus d'un nouvel appel maintenant, None s'il est permis."""
        s = self.settings
        while self._starts and now - self._starts[0] >= 3600:
            self._starts.popleft()
        if now < self._paused_until:
            return "appel refusé ou sans réponse il y a peu"
        if self._starts and now - self._starts[-1] < s.min_interval:
            return "dernier appel trop récent"
        if len(self._starts) >= s.max_per_hour:
            return "trop d'appels sur la dernière heure"
        return None

    def started(self, now: float) -> None:
        self._starts.append(now)

    def finished(self, state: str, now: float) -> None:
        if state in (REFUSED, EXPIRED):
            self._paused_until = max(self._paused_until, now + self.settings.refusal_pause)
