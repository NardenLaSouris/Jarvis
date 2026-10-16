"""Planificateur : un seul fil d'exécution pour toutes les échéances (pas un fil par minuteur).

Les échéances sont rangées par date dans un tas ; le fil dort jusqu'à la prochaine, ou jusqu'à ce
qu'une échéance plus proche soit ajoutée, puis appelle le rappel associé. ``stop()`` l'arrête proprement.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, on_due: Callable[[str], None], clock: Callable[[], float] = time.monotonic):
        self._on_due = on_due
        self._clock = clock
        self._heap: list[tuple[float, int, str]] = []
        self._keys: dict[str, float] = {}
        self._order = itertools.count()
        self._condition = threading.Condition()
        self._running = False
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._condition:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(target=self._loop, name="planificateur", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        with self._condition:
            self._running = False
            self._condition.notify_all()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def schedule(self, key: str, delay: float) -> None:
        with self._condition:
            due = self._clock() + max(0.0, delay)
            self._keys[key] = due
            heapq.heappush(self._heap, (due, next(self._order), key))
            self._condition.notify_all()

    def cancel(self, key: str) -> bool:
        with self._condition:
            return self._keys.pop(key, None) is not None

    def pending(self) -> int:
        with self._condition:
            return len(self._keys)

    def _loop(self) -> None:
        while True:
            with self._condition:
                while self._running:
                    if self._heap and self._heap[0][0] <= self._clock():
                        break
                    timeout = self._heap[0][0] - self._clock() if self._heap else None
                    self._condition.wait(timeout)
                if not self._running:
                    return
                due, _, key = heapq.heappop(self._heap)
                if self._keys.get(key) != due:
                    continue
                del self._keys[key]
            try:
                self._on_due(key)
            except Exception:
                log.exception("Échéance %s : erreur pendant le traitement", key)
