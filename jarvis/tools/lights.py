"""Lumières connectées (ampoules Tuya, par exemple LSC Smart Connect), pilotées en local par le Core.

La pièce visée vient des mots de la demande (alias), jamais du LLM ; sans pièce nommée (« allume la lumière »),
toutes les lumières sont visées.
Le LLM ne parle jamais aux ampoules : seul le pilote, appelé par ces outils, le fait (TinyTuya, réseau local).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Protocol

from jarvis.tools.base import INVALID_PARAMETERS, Param, Risk, Tool, ToolError
from jarvis.tools.devices import AliasIndex

log = logging.getLogger(__name__)

ALL = "all"
ALL_ALIASES = ("les lumieres", "toutes les lumieres", "toute la maison", "partout", "tout eteindre",
               "les lampes", "toutes les lampes", "les ampoules", "toutes les ampoules")
LIGHT_UNREACHABLE = "light_unreachable"
KELVIN_MIN, KELVIN_MAX = 2700, 6500
HUES = {"rouge": 0, "orange": 30, "jaune": 55, "vert": 120, "cyan": 180, "bleu": 240, "violet": 275,
        "magenta": 300, "rose": 330}
WHITES = {"blanc": 4000, "blanc chaud": 2700, "blanc neutre": 4000, "blanc froid": 6500}
COLORS = (*HUES, *WHITES)


def of(name: str) -> str:
    """« la chambre » -> « de la chambre » ; « l'entrée » -> « de l'entrée » ; « le salon » -> « du salon »."""
    for article, contracted in (("le ", "du "), ("les ", "des ")):
        if name.startswith(article):
            return contracted + name[len(article):]
    return f"de {name}"


@dataclass(frozen=True)
class Room:
    key: str
    name: str  # « la chambre », « l'entrée », « le salon »
    device_id: str
    ip: str
    version: float
    local_key: str
    aliases: tuple[str, ...] = ()



class LightDriver(Protocol):
    def state(self, room: Room) -> dict: ...  # {"on": bool, "brightness": %, "mode": "white" | "colour"}
    def power(self, room: Room, on: bool) -> None: ...
    def brightness(self, room: Room, percent: int) -> None: ...
    def colour(self, room: Room, hue: int) -> None: ...
    def white(self, room: Room, kelvin: int) -> None: ...


class TuyaDriver:
    """Ampoules Tuya en local (protocole 3.3 à 3.5) : clé locale, IP et identifiant de chaque ampoule."""

    def __init__(self, timeout: float = 3.0):
        self._timeout = timeout
        self._bulbs: dict[str, object] = {}

    def _bulb(self, room: Room):
        if room.key not in self._bulbs:
            import tinytuya

            bulb = tinytuya.BulbDevice(room.device_id, room.ip, room.local_key, version=room.version)
            bulb.set_socketTimeout(self._timeout)
            bulb.set_socketRetryLimit(1)
            self._bulbs[room.key] = bulb
        return self._bulbs[room.key]

    def _call(self, room: Room, action: Callable[[object], object]):
        try:
            result = action(self._bulb(room))
        except Exception as exc:
            log.warning("Lumière %s : %s", room.key, type(exc).__name__)
            raise ToolError(LIGHT_UNREACHABLE, f"La lumière {of(room.name)} ne répond pas.") from exc
        if isinstance(result, dict) and result.get("Error"):
            log.warning("Lumière %s : %s", room.key, result.get("Error"))
            raise ToolError(LIGHT_UNREACHABLE, f"La lumière {of(room.name)} ne répond pas.")
        return result

    def state(self, room: Room) -> dict:
        dps = self._call(room, lambda b: b.status()).get("dps", {})
        return {"on": bool(dps.get("20")), "brightness": round(int(dps.get("22", 1000)) / 10),
                "mode": dps.get("21", "white")}

    def power(self, room: Room, on: bool) -> None:
        self._call(room, lambda b: b.turn_on() if on else b.turn_off())

    def brightness(self, room: Room, percent: int) -> None:
        self._call(room, lambda b: b.set_brightness_percentage(percent))

    def colour(self, room: Room, hue: int) -> None:
        self._call(room, lambda b: b.set_hsv(hue / 360, 1.0, 1.0))

    def white(self, room: Room, kelvin: int) -> None:
        percent = self.state(room)["brightness"]
        warmth = round(100 * (kelvin - KELVIN_MIN) / (KELVIN_MAX - KELVIN_MIN))
        self._call(room, lambda b: b.set_white_percentage(max(1, percent), warmth))


class Rooms:
    def __init__(self, rooms: list[Room]):
        if not rooms:
            raise ValueError("[lights.rooms] : aucune pièce")
        self._rooms = {room.key: room for room in rooms}
        self._aliases = AliasIndex([*((a, r.key) for r in rooms for a in (r.name, r.key, *r.aliases)),
                                    *((a, ALL) for a in ALL_ALIASES)])

    def __getitem__(self, key: str) -> Room:
        return self._rooms[key]

    def keys(self) -> tuple[str, ...]:
        return (*self._rooms, ALL)

    def resolve(self, text: str) -> str:
        """Pièce nommée dans la demande ; sinon toutes (« all »), ou la seule pièce s'il n'y en a qu'une."""
        found = self._aliases.find(text)
        if found is None:
            return next(iter(self._rooms)) if len(self._rooms) == 1 else ALL
        return found

    def targets(self, key: str | None) -> list[Room]:
        if key is None:
            raise ToolError(INVALID_PARAMETERS, "Dans quelle pièce ?")
        return list(self._rooms.values()) if key == ALL else [self._rooms[key]]


def _said(names: list[str], state: str) -> str:
    """« La lumière de la chambre est allumée. » ; « Toutes les lumières sont allumées. »"""
    if len(names) > 1:
        head, space, tail = state.partition(" ")
        return f"Toutes les lumières sont {head}{'s' if head in ('allumée', 'éteinte') else ''}{space}{tail}."
    return f"La lumière {of(names[0])} est {state}."


def light_tools(rooms: Rooms, driver: LightDriver) -> list[Tool]:
    def room_param() -> Param:
        return Param(str, "pièce", required=False, max_length=32, choices=rooms.keys(), hidden=True,
                     resolve=rooms.resolve)

    def percent_param(required: bool) -> Param:
        return Param(int, "luminosité en pourcentage (1 à 100), seulement si elle a été dite", required=required,
                     minimum=1, maximum=100)

    def each(room: str | None, action: Callable[[Room], None]) -> list[Room]:
        targets = rooms.targets(room)
        for target in targets:
            action(target)
        return targets

    def result(targets: list[Room], **values) -> dict:
        return {"rooms": [t.name for t in targets], **values}

    def light_on(room: str | None = None, brightness: int | None = None) -> dict:
        targets = each(room, lambda r: driver.power(r, True))
        if brightness is not None:
            each(room, lambda r: driver.brightness(r, brightness))
        return result(targets, on=True, brightness=brightness)

    def light_off(room: str | None = None) -> dict:
        return result(each(room, lambda r: driver.power(r, False)), on=False)

    def light_toggle(room: str | None = None) -> dict:
        targets = rooms.targets(room)
        on = not any(driver.state(t)["on"] for t in targets)
        for target in targets:
            driver.power(target, on)
        return result(targets, on=on)

    def set_brightness(room: str | None = None, brightness: int = 100) -> dict:
        targets = each(room, lambda r: (driver.power(r, True), driver.brightness(r, brightness)))
        return result(targets, brightness=brightness)

    def set_color(color: str, room: str | None = None) -> dict:
        if color in WHITES:
            targets = each(room, lambda r: (driver.power(r, True), driver.white(r, WHITES[color])))
        else:
            targets = each(room, lambda r: (driver.power(r, True), driver.colour(r, HUES[color])))
        return result(targets, color=color)

    def set_color_temperature(temperature: int, room: str | None = None) -> dict:
        targets = each(room, lambda r: (driver.power(r, True), driver.white(r, temperature)))
        return result(targets, temperature=temperature)

    def said_power(r: dict) -> str:
        level = f" à {r['brightness']} %" if r.get("brightness") else ""
        return _said(r["rooms"], ("allumée" if r["on"] else "éteinte") + level)

    def said_level(r: dict, value: str) -> str:
        return _said(r["rooms"], value)

    returns = {"rooms": "pièces", "on": "allumée ou non", "brightness": "%"}
    return [
        Tool("light_on", "Allume la lumière d'une pièce (ou toutes), avec une luminosité si elle est dite.",
             {"room": room_param(), "brightness": percent_param(False)}, returns, Risk.SAFE, light_on,
             say=said_power),
        Tool("light_off", "Éteint la lumière d'une pièce (ou toutes).", {"room": room_param()}, returns, Risk.SAFE,
             light_off, say=said_power),
        Tool("light_toggle", "Allume la lumière si elle est éteinte, l'éteint sinon.", {"room": room_param()},
             returns, Risk.SAFE, light_toggle, say=said_power),
        Tool("set_brightness", "Règle la luminosité d'une lumière sur un pourcentage précis.",
             {"room": room_param(), "brightness": percent_param(True)}, returns, Risk.SAFE, set_brightness,
             say=lambda r: said_level(r, f"à {r['brightness']} %")),
        Tool("set_color", "Change la couleur d'une lumière.",
             {"room": room_param(), "color": Param(str, "couleur dite", choices=COLORS, max_length=20)},
             {"rooms": "pièces", "color": "couleur"}, Risk.SAFE, set_color,
             say=lambda r: said_level(r, f"en {r['color']}")),
        Tool("set_color_temperature", f"Règle la température de blanc en kelvins ({KELVIN_MIN} à {KELVIN_MAX}).",
             {"room": room_param(), "temperature": Param(int, "kelvins, seulement s'ils ont été dits",
                                                         minimum=KELVIN_MIN, maximum=KELVIN_MAX)},
             {"rooms": "pièces", "temperature": "kelvins"}, Risk.SAFE, set_color_temperature,
             say=lambda r: said_level(r, f"à {r['temperature']} kelvins")),
    ]


def load_rooms(table: dict, key_of: Callable[[str], str]) -> Rooms | None:
    """[lights.rooms.<pièce>] (name, device_id, ip, version, aliases) ; clé locale par ``key_of(pièce)``."""
    if not table:
        return None
    rooms = []
    for key, spec in table.items():
        if not isinstance(spec, dict) or not key.isidentifier() or key == ALL:
            raise ValueError(f"[lights.rooms.{key}] invalide")
        local_key = key_of(key)
        if not local_key or not spec.get("device_id") or not spec.get("ip"):
            raise ValueError(f"[lights.rooms.{key}] : device_id, ip et clé locale requis")
        rooms.append(Room(key, str(spec.get("name", f"la {key}")), str(spec["device_id"]), str(spec["ip"]),
                          float(spec.get("version", 3.5)), local_key, tuple(str(a) for a in spec.get("aliases", []))))
    return Rooms(rooms)
