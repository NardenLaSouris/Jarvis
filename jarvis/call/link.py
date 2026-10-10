"""Lien du Core avec la passerelle d'appel : le Core reste client (aucun port ouvert de son côté).

- ``ring``, ``cancel``, ``hang_up`` : ce que le gestionnaire d'appels (jarvis/call/manager.py) demande à la passerelle ;
- un fil suit les signaux du téléphone (``GET /core/events``, attente longue) et les remet au gestionnaire :
  accepté, refusé, connecté, raccroché.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request

from jarvis.call.manager import CallRefused
from jarvis.call.session import CallError

log = logging.getLogger(__name__)


class GatewayLink:
    def __init__(self, url: str, token: str, device: str, timeout: float = 5.0, wait: float = 25.0):
        self._url = url.rstrip("/")
        self._token = token
        self._device = device
        self._timeout = timeout
        self._wait = wait
        self.manager = None  # CallManager, branché après sa création
        self._stopping = threading.Event()

    def _post(self, path: str, payload: dict) -> None:
        request = urllib.request.Request(f"{self._url}{path}", data=json.dumps(payload).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {self._token}",
                                                  "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            response.read()

    # --- Notifier du gestionnaire --------------------------------------------------------------------------

    def ring(self, call) -> None:
        self._post("/core/ring", {"call_id": call.id, "device": self._device, "reason": call.reason})

    def cancel(self, call) -> None:
        self._post("/core/cancel", {"call_id": call.id})

    def hang_up(self, call) -> None:
        self._post("/core/hangup", {"call_id": call.id})

    def answer(self, call) -> None:
        self._post("/core/answer", {"call_id": call.id})

    # --- Signaux du téléphone ------------------------------------------------------------------------------

    def start(self) -> None:
        threading.Thread(target=self._run, name="lien-passerelle", daemon=True).start()

    def stop(self) -> None:
        self._stopping.set()

    def _run(self) -> None:
        failing = False
        while not self._stopping.is_set():
            request = urllib.request.Request(f"{self._url}/core/events?timeout={self._wait:.0f}",
                                             headers={"Authorization": f"Bearer {self._token}"})
            try:
                with urllib.request.urlopen(request, timeout=self._wait + self._timeout) as response:
                    events = json.load(response).get("events", [])
                if failing:
                    log.info("Passerelle d'appel joignable de nouveau")
                failing = False
            except (OSError, ValueError) as exc:
                if not failing:
                    log.warning("Passerelle d'appel injoignable (%s) : nouvel essai toutes les 5 s", type(exc).__name__)
                failing = True
                self._stopping.wait(5)
                continue
            for event in events:
                self.deliver(event)

    def deliver(self, event: dict) -> None:
        manager = self.manager
        handler = {"accepted": "accepted", "refused": "refused", "connected": "connected",
                   "hung_up": "hung_up", "failed": "failed", "dialed": "dialed"}.get(str(event.get("type")))
        if manager is None or handler is None:
            return
        if event.get("type") == "dialed":  # le téléphone appelle ORION : le Core accepte ou raccroche
            call_id = str(event.get("call_id", ""))
            try:
                manager.dialed(call_id, str(event.get("device", "")))
            except CallRefused as exc:
                log.info("Appel du téléphone refusé : %s", exc)
                try:
                    self._post("/core/hangup", {"call_id": call_id, "reason": str(exc)})
                except OSError:
                    pass
            return
        try:
            if handler == "failed":
                manager.failed(str(event.get("call_id", "")), str(event.get("reason", "réseau"))[:80])
            else:
                getattr(manager, handler)(str(event.get("call_id", "")))
        except CallError as exc:
            log.info("Signal de la passerelle ignoré (%s)", exc)
