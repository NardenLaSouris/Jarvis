"""Micro et sortie commutables : terminal de la maison, ou appareil mobile pendant un appel.

La boucle de conversation d'ORION ne lit qu'une source et ne joue que sur une sortie ; pendant un appel, elles
basculent vers le téléphone, puis reviennent à la maison. Trois modes :
- « maison » : le micro et la sortie habituels ;
- « appel » : le micro et la sortie du téléphone (via la passerelle d'appel) ; le micro de la maison est vidé au
  fur et à mesure (rien de ce qui s'y dit n'est transcrit) ;
- « attente » : l'appel vient de se terminer, la conversation n'a pas encore rendu la main : du silence, au rythme
  normal, jusqu'au retour à la maison (jamais l'audio de la maison pris pour la suite de l'appel).
Le micro du téléphone est lu par un fil à part : s'il se tait (réseau coupé), ORION reçoit du silence au lieu de
rester bloqué.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

import numpy as np

log = logging.getLogger(__name__)

HOME, CALL, HOLD = "maison", "appel", "attente"


class _Pump:
    """Lit une source bloquante dans un fil et garde les derniers blocs (ceux qui débordent sont oubliés)."""

    def __init__(self, source, max_frames: int = 64):
        self.source = source
        self._queue: queue.Queue = queue.Queue(max_frames)
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, name="micro-appel", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stopping.is_set():
            frame = self.source.read()
            if frame is None:
                return
            while not self._stopping.is_set():
                try:
                    self._queue.put_nowait(frame)
                    break
                except queue.Full:
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        pass

    def get(self, timeout: float) -> np.ndarray | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def clear(self) -> None:
        """Oublie l'audio en attente : le sien et celui de la source (sinon ce que le téléphone a capté pendant
        qu'ORION parlait ressortirait juste après, avec plusieurs secondes de retard)."""
        flush = getattr(self.source, "flush", None)
        if flush is not None:
            flush()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def close(self) -> None:
        self._stopping.set()
        close = getattr(self.source, "close", None)
        if close is not None:
            try:
                close()
            except Exception:  # noqa: BLE001 - fermeture au mieux
                log.debug("Fermeture du micro de l'appel impossible", exc_info=True)


class SwitchableSource:
    def __init__(self, home, sleep=time.sleep):
        self.home = home
        self.sample_rate = home.sample_rate
        self.frame_samples = home.frame_samples
        self._sleep = sleep
        self._lock = threading.Lock()
        self._mode = HOME
        self._call: _Pump | None = None

    @property
    def mode(self) -> str:
        return self._mode

    def use_call(self, source) -> None:
        with self._lock:
            if self._call is not None:
                self._call.close()
            self._call = _Pump(source)
            self._mode = CALL

    def hold(self) -> None:
        """Appel terminé : silence jusqu'à ``use_home`` (la conversation de l'appel se referme)."""
        with self._lock:
            if self._call is not None:
                self._call.close()
                self._call = None
            if self._mode == CALL:
                self._mode = HOLD

    def use_home(self) -> None:
        self.hold()
        with self._lock:
            self._mode = HOME
        self.home.flush()  # ce qui s'est dit à la maison pendant l'appel n'est jamais transcrit

    def _silence(self) -> np.ndarray:
        self._sleep(self.frame_samples / self.sample_rate)
        return np.zeros(self.frame_samples, np.int16)

    def read(self) -> np.ndarray | None:
        mode, call = self._mode, self._call
        if mode == HOME:
            return self.home.read()
        self.home.flush()
        if mode == HOLD or call is None:
            return self._silence()
        frame = call.get(timeout=2 * self.frame_samples / self.sample_rate)
        return frame if frame is not None else np.zeros(self.frame_samples, np.int16)

    def flush(self) -> None:
        call = self._call
        if self._mode == HOME:
            self.home.flush()
        elif call is not None:
            call.clear()

    def close(self) -> None:
        self.hold()
        close = getattr(self.home, "close", None)
        if close is not None:
            close()


class SwitchableSink:
    def __init__(self, home):
        self.home = home
        self._call = None

    @property
    def active(self):
        return self._call if self._call is not None else self.home

    def use_call(self, sink) -> None:
        self._call = sink

    def use_home(self) -> None:
        call, self._call = self._call, None
        close = getattr(call, "close", None)
        if close is not None:
            try:
                close()
            except Exception:  # noqa: BLE001
                log.debug("Fermeture de la sortie de l'appel impossible", exc_info=True)

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        self.active.play(audio, sample_rate)

    def drain(self) -> None:
        drain = getattr(self.active, "drain", None)
        if drain is not None:
            drain()

    def stop(self) -> None:
        stop = getattr(self.active, "stop", None)
        if stop is not None:
            stop()

    def close(self) -> None:
        self.use_home()
        close = getattr(self.home, "close", None)
        if close is not None:
            close()
