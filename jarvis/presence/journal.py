"""Journal de la maison et résumé d'absence.

Le journal écoute le bus d'événements et ne garde que ce qui compte pour l'utilisateur, avec une priorité :

    CRITICAL   fuite, fumée
    IMPORTANT  appareil terminé, porte restée ouverte, mouvement pendant l'absence, grand écart de température
    NORMAL     porte ouverte, lumière allumée ou éteinte, départ et retour, rappel, température (si elle a changé)
    IGNORE     mouvements, fermetures, signaux de présence, événements techniques : jamais enregistrés

Stockage : JSONL (même format et même rotation que le journal d'activité), donc conservé au redémarrage.
Le résumé d'absence ne lit que l'absence, ne cite que l'important (critique d'abord, trois éléments au plus) et
ne transforme jamais un événement ordinaire en alerte : « Rien de particulier pendant votre absence. » sinon.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from typing import Callable

from jarvis.activity import JsonlActivityStore
from jarvis.events import ALL, SYSTEM_ERROR, TOOL_EXECUTED, Event, EventBus
from jarvis.presence.engine import (
    ARRIVAL_CONFIRMED, DEPARTURE_CONFIRMED, DOOR_LEFT_OPEN, MOTION_WHILE_AWAY,
)

log = logging.getLogger(__name__)


class Priority(IntEnum):
    IGNORE = 0
    NORMAL = 1
    IMPORTANT = 2
    CRITICAL = 3


LIGHT_TOOLS = {"light_on": "allumée", "light_off": "éteinte"}
TEMPERATURE_STEP = 0.5  # une mesure n'est gardée que si elle s'écarte d'au moins 0,5 °C de la précédente
TEMPERATURE_ALERT = 2.0  # écart pendant l'absence cité dans le résumé


def of(room: str) -> str:
    """« de l'entrée », « du salon », « des chambres », « de la cuisine »."""
    room = room.strip()
    if not room:
        return ""
    if room.startswith("le "):
        return f" du {room[3:]}"
    if room.startswith("les "):
        return f" des {room[4:]}"
    return f" de {room}"


def in_room(room: str) -> str:
    return f" dans {room.strip()}" if room.strip() else ""


class HouseJournal:
    def __init__(self, path: Path, max_bytes: int = 1_000_000, clock: Callable[[], float] = time.time):
        self._store = JsonlActivityStore(path, max_bytes)
        self._clock = clock
        self._last_temperature: dict[str, float] = {}

    def attach(self, bus: EventBus) -> Callable[[], None]:
        return bus.subscribe(ALL, self.record)

    def classify(self, event: Event) -> tuple[Priority, str]:
        p = event.payload
        room = str(p.get("room", ""))
        kind = event.type
        if kind == "leak.detected":
            return Priority.CRITICAL, f"Une fuite d'eau a été détectée{in_room(room)}."
        if kind == "smoke.detected":
            return Priority.CRITICAL, f"De la fumée a été détectée{in_room(room)}."
        if kind == "mail.received":  # nouveau mail important (jarvis/mail/watch.py) : cité au retour
            subject = str(p.get("subject") or "")
            return Priority.IMPORTANT, (f"Nouveau mail de {str(p.get('sender') or 'un expéditeur')[:40]}"
                                        + (f" : « {subject[:80]} »." if subject else "."))
        if kind == "appliance.finished":
            name = str(p.get("name") or "Un appareil")
            return Priority.IMPORTANT, f"{name[:1].upper()}{name[1:]} a terminé son cycle."
        if kind == DOOR_LEFT_OPEN:
            return Priority.IMPORTANT, f"La porte{of(room)} est restée ouverte {p.get('minutes', '?')} minutes."
        if kind == MOTION_WHILE_AWAY:
            return Priority.IMPORTANT, f"Un mouvement a été détecté{in_room(room)} pendant votre absence."
        if kind == "door.open":
            return Priority.NORMAL, f"Porte{of(room)} ouverte."
        if kind == "temperature.measured" and isinstance(p.get("celsius"), (int, float)):
            last = self._last_temperature.get(room)
            if last is not None and abs(p["celsius"] - last) < TEMPERATURE_STEP:
                return Priority.IGNORE, ""
            self._last_temperature[room] = p["celsius"]
            return Priority.NORMAL, f"Température{of(room)} : {p['celsius']} °C."
        if kind == "device.offline":
            return Priority.NORMAL, f"Capteur {p.get('sensor', '?')} hors ligne."
        if kind == DEPARTURE_CONFIRMED:
            return Priority.NORMAL, "Départ détecté."
        if kind == ARRIVAL_CONFIRMED:
            return Priority.NORMAL, "Retour détecté."
        if kind == TOOL_EXECUTED and p.get("tool") in LIGHT_TOOLS and p.get("success"):
            target = (p.get("parameters") or {}).get("room") or "toutes"
            where = "" if target in ("all", "toutes") else f" ({target})"
            return Priority.NORMAL, f"Lumière {LIGHT_TOOLS[p['tool']]}{where}."
        if kind in ("timer.finished", "reminder.finished"):
            return Priority.NORMAL, "Minuteur terminé." if kind == "timer.finished" else \
                f"Rappel : {str(p.get('message', ''))[:80]}."
        if kind == SYSTEM_ERROR:
            return Priority.IGNORE, ""  # technique : affiché sur le visage, pas dans le journal de la maison
        return Priority.IGNORE, ""

    def record(self, event: Event) -> None:
        try:
            priority, text = self.classify(event)
        except Exception:  # le journal ne doit jamais gêner le bus
            log.exception("Journal de la maison : %s non classé", event.type)
            return
        if priority is Priority.IGNORE:
            return
        payload = {k: v for k, v in event.payload.items() if isinstance(v, (str, int, float, bool)) or v is None}
        self._store.append({"type": event.type, "timestamp": event.timestamp,
                            "time": datetime.fromtimestamp(event.timestamp).isoformat(timespec="seconds"),
                            "source": event.source, "priority": priority.name, "text": text,
                            "room": str(event.payload.get("room", "")), "user": str(event.payload.get("user", "")),
                            "payload": payload})

    def entries(self, since: float | None = None, until: float | None = None, limit: int = 5000) -> list[dict]:
        out = []
        for record in self._store.read(limit):
            ts = record.get("timestamp")
            if not isinstance(ts, (int, float)) or record.get("priority") not in Priority.__members__:
                continue
            if (since is None or ts >= since) and (until is None or ts <= until):
                out.append(record)
        return out

    def recent(self, limit: int = 30) -> list[dict]:
        return self.entries(limit=limit)


NUMBERS = {1: "un", 2: "deux", 3: "trois", 4: "quatre", 5: "cinq", 6: "six", 7: "sept", 8: "huit", 9: "neuf",
           10: "dix"}


def absence_summary(entries: list[dict], since: float, until: float, quiet_before: float = 0.0,
                    max_items: int = 3) -> str:
    """Ce qui mérite d'être dit d'une absence (de ``since`` à ``until``) ; les ``quiet_before`` dernières secondes
    (l'arrivée elle-même : porte, mouvements) sont ignorées."""
    end = until - quiet_before
    window = [e for e in entries if since <= e["timestamp"] <= end]
    items: list[tuple[int, float, str]] = []
    for e in window:
        priority = Priority[e["priority"]]
        if priority >= Priority.IMPORTANT:
            items.append((-priority, e["timestamp"], e["text"]))
    doors = [e for e in window if e["type"] == "door.open"]
    if doors:  # une porte ouverte pendant l'absence (hors arrivée) mérite d'être signalée
        room = doors[0].get("room", "")
        count = "" if len(doors) == 1 else f" {NUMBERS.get(len(doors), len(doors))} fois"
        items.append((-Priority.IMPORTANT, doors[0]["timestamp"],
                      f"La porte{of(room)} a été ouverte{count} pendant votre absence."))
    for room, change in _temperature_changes(window).items():
        if abs(change) >= TEMPERATURE_ALERT:
            degrees = round(abs(change))
            way = "augmenté" if change > 0 else "baissé"
            items.append((-Priority.IMPORTANT, until,
                          f"La température{of(room)} a {way} de {NUMBERS.get(degrees, degrees)} degrés."))
    if not items:
        return "Rien de particulier pendant votre absence."
    seen, sentences = set(), []
    for _, _, text in sorted(items):
        if text not in seen:
            seen.add(text)
            sentences.append(text)
    extra = len(sentences) - max_items
    sentences = sentences[:max_items]
    if extra > 0:
        sentences.append(f"Et {NUMBERS.get(extra, extra)} autre{'s' if extra > 1 else ''} événement"
                         f"{'s' if extra > 1 else ''} dans le journal.")
    return " ".join(sentences)


def _temperature_changes(window: list[dict]) -> dict[str, float]:
    first: dict[str, float] = {}
    last: dict[str, float] = {}
    for e in window:
        value = (e.get("payload") or {}).get("celsius")
        if e["type"] == "temperature.measured" and isinstance(value, (int, float)):
            first.setdefault(e.get("room", ""), value)
            last[e.get("room", "")] = value
    return {room: last[room] - first[room] for room in first}
