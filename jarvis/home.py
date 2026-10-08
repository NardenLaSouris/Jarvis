"""État centralisé de la maison : ce qu'ORION sait réellement des appareils connus.

Chaque valeur est rangée sous un chemin (« lights.chambre.power ») avec son heure et sa provenance :
``confirmed`` vaut True quand elle vient d'une lecture de l'appareil (ou de son accusé de réception), False quand
elle n'est que la commande envoyée. ORION répond ainsi « est-ce que la lumière est allumée ? » d'après une
lecture réelle, et l'API d'administration montre l'état connu sans interroger chaque appareil.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable


class HomeState:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._values: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def set(self, path: str, value: Any, confirmed: bool, source: str = "") -> None:
        with self._lock:
            self._values[path] = {"value": value, "confirmed": confirmed, "at": self._clock(), "source": source}

    def update(self, prefix: str, values: dict, confirmed: bool, source: str = "") -> None:
        for key, value in values.items():
            self.set(f"{prefix}.{key}", value, confirmed, source)

    def get(self, path: str) -> dict | None:
        with self._lock:
            entry = self._values.get(path)
            return dict(entry) if entry else None

    def value(self, path: str, default: Any = None) -> Any:
        entry = self.get(path)
        return entry["value"] if entry else default

    def snapshot(self, prefix: str = "") -> dict[str, dict]:
        """{chemin: {value, confirmed, age_s, source}} ; seulement sous ``prefix`` s'il est donné."""
        now = self._clock()
        with self._lock:
            return {path: {"value": e["value"], "confirmed": e["confirmed"], "age_s": round(now - e["at"], 1),
                           "source": e["source"]}
                    for path, e in sorted(self._values.items()) if path.startswith(prefix)}
