"""Messages d'ORION par Linphone sur l'état de la maison (en plus des réponses à vos messages).

- ``PcOnlineNotice`` : « Votre PC est allumé. » quand le micro du PC (agent Windows) revient après une vraie absence
  (PC éteint), pas après une coupure courte (agent relancé, réseau) ; au plus un message toutes les MIN_GAP.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

PC_ONLINE = "Votre PC est allumé."
MIN_OFFLINE = 120.0  # s d'absence avant qu'un retour compte comme un allumage (un PC met plus longtemps à démarrer)
MIN_GAP = 300.0  # s entre deux messages


class PcOnlineNotice:
    def __init__(self, send: Callable[[str], object], clock: Callable[[], float] = time.monotonic,
                 text: str = PC_ONLINE):
        self._send = send
        self._clock = clock
        self._text = text
        self._lost_at: float | None = None
        self._sent_at: float | None = None

    def status(self, connected: bool) -> None:
        """Micro du PC connecté (True) ou perdu (False), comme ``NetworkSource.on_status``."""
        now = self._clock()
        if not connected:
            if self._lost_at is None:
                self._lost_at = now
            return
        lost, self._lost_at = self._lost_at, None
        if lost is None or now - lost < MIN_OFFLINE:
            return
        if self._sent_at is not None and now - self._sent_at < MIN_GAP:
            return
        self._sent_at = now
        log.info("PC de retour après %.0f s : message envoyé", now - lost)
        threading.Thread(target=self._send, args=(self._text,), name="message-pc", daemon=True).start()
