"""Capteurs de la maison : définitions, événements normalisés et pilotes (« drivers »).

Le moteur de présence (jarvis/presence/engine.py) et le journal ne connaissent que des événements normalisés,
publiés sur le bus d'événements d'ORION :

    door.open / door.closed            porte (``entrance`` : porte d'entrée de la maison)
    motion.detected                    mouvement dans une pièce
    presence.home / presence.away      présence d'un appareil (téléphone) associé à un utilisateur
    leak.detected / smoke.detected     fuite, fumée
    temperature.measured               {"value": °C}
    appliance.finished                 {"name": « la machine à laver »}
    device.online / device.offline     le capteur lui-même

payload : {"sensor", "kind", "room", "user" (capteur de présence), "value", ...} ; source : « sensor:<pilote> ».

Un pilote ne fait qu'une chose : appeler ``emit(capteur, valeur)`` quand son matériel signale quelque chose.
Le pilote « simulated » n'écoute rien : la simulation (python -m jarvis --simulate, API) appelle ``emit`` comme le
ferait un vrai pilote, par le même chemin. Brancher un vrai capteur (MQTT, Zigbee, Tuya, ESP32, Home Assistant...)
= écrire un pilote qui appelle ``emit`` et l'ajouter à ``DRIVERS`` ; rien d'autre ne change.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from jarvis.events import Event, EventBus

log = logging.getLogger(__name__)

# Valeurs admises par sorte de capteur -> type d'événement normalisé.
KINDS: dict[str, dict[str, str]] = {
    "door": {"open": "door.open", "closed": "door.closed"},
    "motion": {"detected": "motion.detected"},
    "presence": {"home": "presence.home", "away": "presence.away"},
    "leak": {"detected": "leak.detected"},
    "smoke": {"detected": "smoke.detected"},
    "temperature": {"measured": "temperature.measured"},
    "appliance": {"finished": "appliance.finished"},
}
STATUS = {"online": "device.online", "offline": "device.offline"}
SENSOR_EVENTS = tuple(sorted({t for values in KINDS.values() for t in values.values()} | set(STATUS.values())))
SENSOR_ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
SOURCE_PREFIX = "sensor:"


class SensorError(ValueError):
    pass


@dataclass(frozen=True)
class Sensor:
    id: str
    kind: str
    room: str = ""
    user: str = ""  # capteur de présence : l'utilisateur dont l'appareil est suivi
    entrance: bool = False  # porte d'entrée de la maison (ou mouvement juste derrière elle)
    driver: str = "simulated"
    name: str = ""  # appareil : « la machine à laver »


def load_sensors(table: dict, known_users: set[str]) -> dict[str, Sensor]:
    """[presence.sensors.<id>] -> capteurs validés (SensorError avec la clé fautive sinon)."""
    sensors = {}
    for sensor_id, spec in (table or {}).items():
        where = f"[presence.sensors.{sensor_id}]"
        if not SENSOR_ID.match(str(sensor_id)):
            raise SensorError(f"{where} : identifiant en minuscules, chiffres et _ attendu")
        if not isinstance(spec, dict):
            raise SensorError(f"{where} : section attendue")
        unknown = set(spec) - {"kind", "room", "user", "entrance", "driver", "name"}
        if unknown:
            raise SensorError(f"{where} : clés inconnues {sorted(unknown)}")
        kind = spec.get("kind")
        if kind not in KINDS:
            raise SensorError(f"{where} kind : {', '.join(KINDS)} attendu, pas {kind!r}")
        driver = spec.get("driver", "simulated")
        if driver not in DRIVERS:
            raise SensorError(f"{where} driver : {', '.join(DRIVERS)} attendu, pas {driver!r}")
        user = str(spec.get("user", ""))
        if kind == "presence" and user not in known_users:
            known = ", ".join(sorted(known_users))
            raise SensorError(f"{where} user : utilisateur inconnu {user!r} (profils : {known})")
        if kind != "presence" and user:
            raise SensorError(f"{where} user : seulement pour un capteur de présence")
        entrance = spec.get("entrance", False)
        if not isinstance(entrance, bool):
            raise SensorError(f"{where} entrance : true ou false attendu")
        sensors[sensor_id] = Sensor(sensor_id, kind, str(spec.get("room", ""))[:40], user, entrance, driver,
                                    str(spec.get("name", ""))[:60])
    return sensors


class SensorHub:
    """Entrée unique des signaux des capteurs : validation, normalisation, publication sur le bus.

    Refusé (journalisé, jamais publié) : capteur inconnu, valeur invalide, horodatage absurde, trop ancien ou dans
    le futur, doublon exact, et toute simulation sur un capteur réel."""

    def __init__(self, sensors: dict[str, Sensor], bus: EventBus, clock: Callable[[], float] = time.time,
                 max_age: float = 120.0, max_future: float = 5.0):
        self.sensors = sensors
        self._bus = bus
        self._clock = clock
        self._max_age, self._max_future = max_age, max_future
        self._recent: dict[tuple, float] = {}
        self._lock = threading.Lock()

    def emit(self, sensor_id: str, value: str, timestamp: float | None = None, extra: dict | None = None,
             source: str = "") -> Event:
        """Événement normalisé publié (SensorError si refusé). ``source`` : pilote qui parle (par défaut, celui du
        capteur) ; « simulated » n'est accepté que pour un capteur simulé."""
        sensor = self.sensors.get(str(sensor_id))
        if sensor is None:
            raise SensorError(f"capteur inconnu : {str(sensor_id)[:40]!r}")
        source = source or sensor.driver
        if source != sensor.driver:
            raise SensorError(f"{sensor.id} : signal de « {source[:20]} » refusé (pilote du capteur : {sensor.driver})")
        event_type = KINDS[sensor.kind].get(value) or STATUS.get(value)
        if event_type is None:
            allowed = ", ".join([*KINDS[sensor.kind], *STATUS])
            raise SensorError(f"{sensor.id} : valeur {str(value)[:20]!r} invalide ({allowed})")
        now = self._clock()
        when = now if timestamp is None else timestamp
        if not isinstance(when, (int, float)) or isinstance(when, bool) or not math.isfinite(when):
            raise SensorError(f"{sensor.id} : horodatage invalide")
        if when > now + self._max_future:
            raise SensorError(f"{sensor.id} : horodatage dans le futur")
        if when < now - self._max_age:
            raise SensorError(f"{sensor.id} : événement trop ancien ({now - when:.0f} s)")
        details = _extra(sensor, extra)
        payload = {"sensor": sensor.id, "kind": sensor.kind, "room": sensor.room, "value": value, **details}
        if sensor.user:
            payload["user"] = sensor.user
        if sensor.entrance:
            payload["entrance"] = True
        if sensor.name:
            payload.setdefault("name", sensor.name)
        key = (sensor.id, value, round(when, 1))
        with self._lock:
            if key in self._recent:
                raise SensorError(f"{sensor.id} : doublon ignoré")
            self._recent = {k: t for k, t in self._recent.items() if now - t < 10}
            self._recent[key] = now
        event = Event(event_type, SOURCE_PREFIX + sensor.driver, payload, timestamp=float(when))
        self._bus.publish(event)
        return event


def _extra(sensor: Sensor, extra: dict | None) -> dict:
    """Données complémentaires admises (et vérifiées) selon la sorte de capteur."""
    if not extra:
        return {}
    if not isinstance(extra, dict):
        raise SensorError(f"{sensor.id} : données complémentaires invalides")
    if sensor.kind == "temperature":
        value = extra.get("celsius")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not -40 <= value <= 80:
            raise SensorError(f"{sensor.id} : température invalide")
        return {"celsius": round(float(value), 1)}
    if sensor.kind == "appliance" and isinstance(extra.get("name"), str):
        return {"name": extra["name"].strip()[:60]}
    return {}


# --- Pilotes --------------------------------------------------------------------------------------------

class SensorDriver(Protocol):
    """Pilote d'une famille de capteurs : ``start(emit)`` puis appelle ``emit(capteur, valeur)`` à chaque signal."""

    def start(self, emit: Callable[..., Event]) -> None: ...

    def stop(self) -> None: ...


class SimulatedDriver:
    """Aucun matériel : les signaux viennent de la simulation (CLI, API, tests), par ``SensorHub.emit``."""

    def __init__(self, sensors: list[Sensor]):
        self.sensors = sensors

    def start(self, emit: Callable[..., Event]) -> None:
        pass

    def stop(self) -> None:
        pass


DRIVERS: dict[str, Callable[[list[Sensor]], SensorDriver]] = {"simulated": SimulatedDriver}
