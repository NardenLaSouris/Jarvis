"""Statistiques du fond d'écran : chaque source est relue en arrière-plan à son rythme (machine du Core chaque
seconde, appareils toutes les 15 s, météo tous les quarts d'heure...), la page ne lit que la dernière valeur connue.

Lecture seule partout : aucune source n'agit sur la maison, n'interroge la boîte mail ni ne vide une file (le
résumé des mails n'est jamais consommé ici). Une source en erreur garde sa dernière valeur, marquée périmée, sans
jamais gêner les autres ni ORION.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

HISTORY = 90  # points gardés pour les courbes (90 s de processeur et de réseau)


@dataclass
class Source:
    name: str
    every: float
    read: Callable[[], Any]
    value: Any = None
    at: float | None = None
    ok: bool = False
    running: bool = False
    failures: int = field(default=0)


class Collector:
    def __init__(self, sources: list[Source], clock: Callable[[], float] = time.monotonic, workers: int = 4):
        self._sources = {s.name: s for s in sources}
        self._clock = clock
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fond-ecran")
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="fond-ecran", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def tick(self) -> None:
        """Lance la lecture des sources dues (chacune dans le groupe de fils, jamais deux fois en même temps)."""
        now = self._clock()
        with self._lock:
            due = [s for s in self._sources.values() if not s.running and (s.at is None or now - s.at >= s.every)]
            for source in due:
                source.running = True
        for source in due:
            self._pool.submit(self._read, source)

    def _read(self, source: Source) -> None:
        try:
            value, ok = source.read(), True
        except Exception as exc:  # noqa: BLE001 - une source en panne ne gêne ni les autres, ni ORION
            value, ok = None, False
            if source.failures == 0:
                log.info("Fond d'écran : source « %s » indisponible (%s)", source.name, exc)
        with self._lock:
            if ok:
                source.value, source.failures = value, 0
            else:
                source.failures += 1
            source.ok, source.at, source.running = ok, self._clock(), False

    def _loop(self) -> None:
        while not self._stopping.is_set():
            self.tick()
            self._stopping.wait(1.0)

    def snapshot(self) -> dict:
        with self._lock:
            return {name: {"value": s.value, "ok": s.ok} for name, s in self._sources.items()}


# --- Machine du Core (Linux, /proc), sans dépendance ---------------------------------------------------------

class CoreMachine:
    """Processeur, mémoire, disque, température, charge, réseau et durée de fonctionnement du mini-PC."""

    def __init__(self, proc: Path = Path("/proc"), sys_dir: Path = Path("/sys"), disk: str = "/"):
        self._proc, self._sys, self._disk = proc, sys_dir, disk
        self._cpu: tuple[int, int] | None = None
        self._net: tuple[float, int, int] | None = None
        self.cpu_history: deque = deque(maxlen=HISTORY)
        self.rx_history: deque = deque(maxlen=HISTORY)
        self.tx_history: deque = deque(maxlen=HISTORY)

    def _cpu_percent(self) -> float | None:
        fields = (self._proc / "stat").read_text().splitlines()[0].split()[1:]
        values = [int(v) for v in fields]
        idle, total = values[3] + (values[4] if len(values) > 4 else 0), sum(values)
        previous, self._cpu = self._cpu, (idle, total)
        if previous is None or total == previous[1]:
            return None
        return round(100.0 * (1 - (idle - previous[0]) / (total - previous[1])), 1)

    def _memory(self) -> tuple[float, float]:
        info = {}
        for line in (self._proc / "meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            info[key] = int(rest.split()[0]) * 1024
        total = info.get("MemTotal", 0)
        return total, total - info.get("MemAvailable", 0)

    def _temperature(self) -> float | None:
        temps = []
        for path in self._sys.glob("class/thermal/thermal_zone*/temp"):
            try:
                temps.append(int(path.read_text().strip()) / 1000)
            except (OSError, ValueError):
                continue
        return round(max(temps), 1) if temps else None

    def _network(self) -> tuple[float | None, float | None]:
        rx = tx = 0
        for line in (self._proc / "net" / "dev").read_text().splitlines()[2:]:
            name, _, data = line.partition(":")
            if name.strip() == "lo" or not data.split():
                continue
            parts = data.split()
            rx, tx = rx + int(parts[0]), tx + int(parts[8])
        now = time.monotonic()
        previous, self._net = self._net, (now, rx, tx)
        if previous is None or now <= previous[0]:
            return None, None
        seconds = now - previous[0]
        return round((rx - previous[1]) / seconds), round((tx - previous[2]) / seconds)

    def read(self) -> dict:
        cpu = self._cpu_percent()
        total, used = self._memory()
        disk = shutil.disk_usage(self._disk)
        rx, tx = self._network()
        if cpu is not None:
            self.cpu_history.append(cpu)
        if rx is not None:
            self.rx_history.append(rx)
            self.tx_history.append(tx)
        load = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
        uptime = float((self._proc / "uptime").read_text().split()[0])
        return {"cpu": cpu, "cores": os.cpu_count() or 1, "ram_total": total, "ram_used": used,
                "disk_total": disk.total, "disk_used": disk.used, "temperature": self._temperature(),
                "load": [round(x, 2) for x in load], "uptime": round(uptime), "rx": rx, "tx": tx,
                "cpu_history": list(self.cpu_history), "rx_history": list(self.rx_history),
                "tx_history": list(self.tx_history)}
