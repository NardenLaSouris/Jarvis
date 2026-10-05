"""Assemblage de la présence : capteurs et pilotes, moteur, journal ; simulation et accueil au retour.

La simulation (CLI ``python -m jarvis --simulate``, API ``POST /api/presence/simulate``, tests) passe par
``SensorHub.emit`` exactement comme un vrai pilote : même validation, mêmes événements, même moteur.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from jarvis.events import Event, EventBus
from jarvis.presence.engine import PresenceEngine, PresenceSettings
from jarvis.presence.journal import HouseJournal, absence_summary
from jarvis.presence.sensors import DRIVERS, KINDS, Sensor, SensorDriver, SensorError, SensorHub, load_sensors

log = logging.getLogger(__name__)

ALIASES = {"close": "closed", "ferme": "closed", "fermee": "closed", "ouvert": "open", "ouverte": "open",
           "ouvre": "open", "motion": "detected", "mouvement": "detected", "present": "home", "parti": "away",
           "absent": "away", "finish": "finished", "termine": "finished", "fuite": "detected", "fumee": "detected"}


class PresenceSystem:
    def __init__(self, hub: SensorHub, engine: PresenceEngine, journal: HouseJournal,
                 drivers: list[SensorDriver], clock: Callable[[], float] = time.time, first_start: bool = False):
        self.hub, self.engine, self.journal, self.drivers = hub, engine, journal, drivers
        self._clock = clock
        self.first_start = first_start  # première mise en service (aucun état enregistré) : routine d'accueil créée

    def start(self) -> None:
        for driver in self.drivers:
            driver.start(self.hub.emit)
        self.engine.start()

    def stop(self) -> None:
        self.engine.stop()
        for driver in self.drivers:
            try:
                driver.stop()
            except Exception:
                log.exception("Présence : arrêt d'un pilote en erreur")

    def resolve(self, name: str) -> Sensor:
        """Capteur par son identifiant, ou le premier (par ordre alphabétique) de cette sorte (« door »)."""
        sensors = self.hub.sensors
        if name in sensors:
            return sensors[name]
        if name in KINDS:
            for sensor_id in sorted(sensors):
                if sensors[sensor_id].kind == name:
                    return sensors[sensor_id]
        raise SensorError(f"aucun capteur « {str(name)[:40]} » (capteurs : {', '.join(sorted(sensors)) or 'aucun'})")

    def simulate(self, name: str, value: str, extra: dict | None = None) -> Event:
        """Signal simulé, injecté comme celui d'un vrai pilote (refusé pour un capteur réel)."""
        sensor = self.resolve(str(name))
        value = ALIASES.get(str(value).lower(), str(value).lower())
        return self.hub.emit(sensor.id, value, extra=extra, source="simulated")

    def welcome(self, context: dict, greeting: Callable[[str], str], summary: bool = True) -> str:
        """« Bon retour, monsieur. » puis, si demandé, ce qui s'est passé pendant l'absence."""
        text = greeting(str(context.get("user", "")))
        left_at, now = context.get("left_at") or context.get("away_since"), self._clock()
        if summary and isinstance(left_at, (int, float)):
            started = context.get("arrival_started")
            quiet = now - started + 5 if isinstance(started, (int, float)) else 0.0
            text += " " + absence_summary(self.journal.entries(since=left_at), left_at, now, quiet_before=quiet)
        return text

    def snapshot(self, journal: int = 20) -> dict:
        return {**self.engine.snapshot(), "sensors": {s.id: {"kind": s.kind, "room": s.room, "user": s.user,
                                                             "driver": s.driver, "entrance": s.entrance}
                                                      for s in self.hub.sensors.values()},
                "journal": self.journal.recent(journal)}


def build_presence(config, bus: EventBus, known_users: set[str], home=None,
                   clock: Callable[[], float] = time.time) -> PresenceSystem:
    """Système de présence d'après [presence] (SensorError / ValueError pour une configuration invalide)."""
    sensors = load_sensors(config.sensors, known_users)
    users = sorted({s.user for s in sensors.values() if s.kind == "presence"})
    settings = PresenceSettings(departure_window=config.departure_window, arrival_window=config.arrival_window,
                                threshold=config.threshold, absence_timeout=config.absence_minutes * 60,
                                door_left_open=config.door_left_open_minutes * 60)
    first_start = not Path(config.state_path).exists()
    engine = PresenceEngine(users, bus, settings, clock=clock, state_path=config.state_path, home=home)
    engine.attach()
    journal = HouseJournal(config.journal_path, config.journal_max_kb * 1024, clock=clock)
    journal.attach(bus)
    hub = SensorHub(sensors, bus, clock=clock, max_age=config.max_event_age)
    by_driver: dict[str, list[Sensor]] = {}
    for sensor in sensors.values():
        by_driver.setdefault(sensor.driver, []).append(sensor)
    drivers = [DRIVERS[name](group) for name, group in sorted(by_driver.items())]
    system = PresenceSystem(hub, engine, journal, drivers, clock, first_start)
    if not users:
        log.warning("Présence : aucun capteur de présence (téléphone) : arrivées et départs jamais confirmés")
    return system
