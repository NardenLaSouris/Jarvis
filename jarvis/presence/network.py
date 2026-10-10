"""Pilote « network » : présence d'un téléphone d'après le réseau local (Wi-Fi de la maison).

Toutes les ``interval`` secondes, chaque appareil suivi (``address``, et ``mac`` pour s'assurer que c'est bien lui)
est sondé : un ping, puis, faute de réponse, l'état ARP du voisin (un téléphone en veille ignore souvent le ping
mais répond encore à l'ARP tant qu'il est connecté au Wi-Fi). Vu : « home » aussitôt. Plus vu depuis ``away_after``
secondes : « away » (un téléphone endormi se réveille régulièrement : quelques minutes de silence ne sont pas un
départ). Seuls les changements sont signalés ; le moteur de présence décide ensuite (jarvis/presence/engine.py).

Commandes fixes (``ping``, ``ip neigh``) sur une adresse validée à la lecture de la configuration : aucun texte
venu d'une demande, aucun shell.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import subprocess
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
ARP_WAIT = 8.0  # s : entrée ARP périmée -> DELAY (5 s) -> PROBE -> REACHABLE si l'appareil répond


def valid_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.version == 4 and address.is_private and not address.is_loopback


def valid_mac(value: str) -> bool:
    return bool(MAC.match(value))


def _ping(address: str) -> bool:
    try:
        done = subprocess.run(["ping", "-c", "1", "-W", "1", "-q", address], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _neighbour(address: str) -> tuple[str, str]:
    """(état ARP, adresse MAC) du voisin, vide s'il est inconnu."""
    try:
        done = subprocess.run(["ip", "-j", "neigh", "show", "to", address], capture_output=True, timeout=5)
        entries = json.loads(done.stdout or b"[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return "", ""
    for entry in entries:
        if entry.get("dst") == address:
            return " ".join(entry.get("state") or []), str(entry.get("lladdr", "")).lower()
    return "", ""


def _nudge(address: str) -> None:
    """Un datagramme vers le port « discard » : le noyau doit résoudre l'adresse, donc interroger l'appareil (ARP)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(b"", (address, 9))
    except OSError:
        pass


def probe(address: str, mac: str = "", sleep: Callable[[float], None] = time.sleep) -> bool:
    """L'appareil répond-il en ce moment (ping, ou ARP confirmé) ? Avec ``mac``, l'adresse doit être la sienne."""
    if _ping(address):
        state, lladdr = _neighbour(address)
        return not mac or lladdr in ("", mac)
    _nudge(address)
    waited = 0.0
    while waited < ARP_WAIT:
        sleep(1.0)
        waited += 1.0
        state, lladdr = _neighbour(address)
        if "REACHABLE" in state:
            return not mac or lladdr == mac
        if "FAILED" in state:
            return False
    return False


class NetworkDriver:
    def __init__(self, sensors, interval: float = 20.0, away_after: float = 600.0,
                 probe: Callable[[str, str], bool] = probe, clock: Callable[[], float] = time.monotonic):
        self.sensors = list(sensors)
        self.interval, self.away_after = interval, away_after
        self._probe, self._clock = probe, clock
        self._emit = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        started = clock()
        self._seen: dict[str, float] = {s.id: started for s in self.sensors}  # absent depuis le démarrage : délai
        self._value: dict[str, str] = {}

    def start(self, emit) -> None:
        self._emit = emit
        self._thread = threading.Thread(target=self._run, name="presence-network", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception:  # noqa: BLE001 - le fil ne doit pas mourir sur une erreur passagère
                log.exception("Présence réseau : sondage en échec")
            self._stop.wait(self.interval)

    def poll(self) -> None:
        for sensor in self.sensors:
            seen = self._probe(sensor.address, sensor.mac)
            now = self._clock()
            if seen:
                self._seen[sensor.id] = now
                self._change(sensor, "home")
            elif now - self._seen[sensor.id] >= self.away_after:
                self._change(sensor, "away")

    def _change(self, sensor, value: str) -> None:
        if self._value.get(sensor.id) == value:
            return
        self._value[sensor.id] = value
        log.info("Présence réseau : %s %s", sensor.id, "connecté" if value == "home" else "absent du réseau")
        if self._emit is not None:
            try:
                self._emit(sensor.id, value)
            except Exception:  # noqa: BLE001 - signal refusé (capteur, horloge) : noté, le sondage continue
                log.exception("Présence réseau : signal %s refusé", value)
