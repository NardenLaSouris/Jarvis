"""Demandes écrites depuis le fond d'écran : déposées ici par la page, traitées par la boucle d'ORION comme vos
messages Linphone (mêmes outils, permissions, confirmations), une à la fois, entre deux écoutes du mot de réveil.

La page attend la réponse ; une demande non traitée à temps (ORION en pleine conversation) est retirée de la file
et signalée comme telle, jamais exécutée plus tard à l'insu de l'utilisateur.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

MAX_PENDING = 3
MAX_TEXT = 500
HISTORY = 12


@dataclass
class Request:
    text: str
    done: threading.Event = field(default_factory=threading.Event)
    reply: str = ""
    taken: bool = False

    def answer(self, text: str) -> None:
        self.reply = text
        self.done.set()


class WallpaperInbox:
    def __init__(self, clock=time.time):
        self._lock = threading.Lock()
        self._queue: deque[Request] = deque()
        self._clock = clock
        self.history: deque[dict] = deque(maxlen=HISTORY)  # derniers échanges, pour la page

    def ask(self, text: str, timeout: float = 90.0) -> str | None:
        """Dépose une demande et attend la réponse d'ORION ; None si elle n'a pas pu être traitée à temps."""
        text = " ".join(str(text).split())[:MAX_TEXT]
        if not text:
            return None
        request = Request(text)
        with self._lock:
            if len(self._queue) >= MAX_PENDING:
                return None
            self._queue.append(request)
        if not request.done.wait(timeout):
            with self._lock:
                if not request.taken:  # jamais lancée : retirée, elle ne s'exécutera pas plus tard
                    self._queue.remove(request)
                    return None
            request.done.wait(timeout)  # en cours de traitement : la réponse arrive
        if not request.done.is_set():
            return None
        with self._lock:
            self.history.append({"at": self._clock(), "you": request.text, "orion": request.reply})
        return request.reply

    def take(self) -> Request | None:
        """Prochaine demande, pour la boucle d'ORION (None si la file est vide)."""
        with self._lock:
            if not self._queue:
                return None
            request = self._queue.popleft()
            request.taken = True
            return request

    def recent(self) -> list[dict]:
        with self._lock:
            return list(self.history)
