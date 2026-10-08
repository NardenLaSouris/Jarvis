"""Lumières connectées (ampoules Tuya, par exemple LSC Smart Connect), pilotées en local par le Core.

La pièce visée vient des mots de la demande (alias), jamais du LLM ; sans pièce nommée (« allume la lumière »),
toutes les lumières sont visées.
Le LLM ne parle jamais aux ampoules : seul le pilote, appelé par ces outils, le fait (TinyTuya, réseau local).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Protocol

from jarvis.personality import normalize
from jarvis.tools.base import INVALID_PARAMETERS, Param, Risk, Tool, ToolError
from jarvis.tools.devices import AliasIndex

log = logging.getLogger(__name__)

ALL = "all"
ALL_ALIASES = ("les lumieres", "toutes les lumieres", "toute la maison", "partout", "tout eteindre",
               "les lampes", "toutes les lampes", "les ampoules", "toutes les ampoules")
LIGHT_UNREACHABLE = "light_unreachable"
UNKNOWN_ROOM = "unknown_room"
# Noms de pièces courants : nommer l'une d'elles sans lumière configurée est signalé (« je n'ai pas de lumière
# configurée pour le bureau ») au lieu d'allumer toute la maison.
COMMON_ROOMS = ("salon", "bureau", "cuisine", "salle de bain", "salle de bains", "salle d eau", "chambre d ami",
                "chambre d amis", "chambre des enfants", "garage", "couloir", "entree", "toilettes", "wc", "cave",
                "grenier", "jardin", "terrasse", "balcon", "salle a manger", "buanderie", "chambre", "veranda",
                "cellier", "dressing", "bibliotheque", "salle de jeux", "atelier", "mezzanine", "escalier")
# Scènes : réglage d'ambiance appliqué à chaque lumière visée (brightness en %, temperature en kelvins ou color).
SCENES = {
    "cinema": {"name": "cinéma", "brightness": 10, "temperature": 2700, "aliases": ["film", "mode film"]},
    "lecture": {"name": "lecture", "brightness": 100, "temperature": 4000, "aliases": ["mode lecture"]},
    "detente": {"name": "détente", "brightness": 40, "temperature": 2700,
                "aliases": ["relax", "relaxation", "tamisee", "lumiere tamisee", "ambiance douce"]},
    "nuit": {"name": "nuit", "brightness": 5, "color": "orange", "aliases": ["veilleuse", "mode nuit"]},
    "travail": {"name": "travail", "brightness": 100, "temperature": 6500, "aliases": ["concentration"]},
    "reveil": {"name": "réveil", "brightness": 80, "temperature": 4000, "aliases": ["mode matin"]},
}
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


def _colour_value(colour: str | None) -> int | None:
    """Luminosité (0 à 1000) d'une couleur Tuya « hhhhssssvvvv » (hexadécimal)."""
    try:
        return int(str(colour)[8:12], 16)
    except (TypeError, ValueError):
        return None


class TuyaDriver:
    """Ampoules Tuya en local (protocole 3.3 à 3.5) : clé locale, IP et identifiant de chaque ampoule."""

    def __init__(self, timeout: float = 3.0):
        self._timeout = timeout
        self._bulbs: dict[str, object] = {}
        self._locks: dict[str, threading.Lock] = {}

    def _bulb(self, room: Room):
        if room.key not in self._bulbs:
            import tinytuya

            bulb = tinytuya.BulbDevice(room.device_id, room.ip, room.local_key, version=room.version)
            bulb.set_socketTimeout(self._timeout)
            bulb.set_socketRetryLimit(1)
            self._bulbs[room.key] = bulb
        return self._bulbs[room.key]

    def _call(self, room: Room, action: Callable[[object], object]):
        """Une seule commande à la fois par ampoule (sa connexion n'est pas partagée entre fils : la voix, une
        routine et le tableau de bord d'ORION Control peuvent la viser en même temps) ; un second essai sur
        une erreur passagère."""
        with self._locks.setdefault(room.key, threading.Lock()):
            for attempt in (1, 2):
                try:
                    result = action(self._bulb(room))
                except Exception as exc:
                    error = type(exc).__name__
                    result = None
                else:
                    error = result.get("Error") if isinstance(result, dict) else None
                if not error:
                    return result
                log.warning("Lumière %s : %s%s", room.key, error, " ; nouvel essai" if attempt == 1 else "")
                if attempt == 1:
                    time.sleep(0.2)
        raise ToolError(LIGHT_UNREACHABLE, f"La lumière {of(room.name)} ne répond pas.")

    def state(self, room: Room) -> dict:
        dps = self._call(room, lambda b: b.status()).get("dps", {})
        mode = dps.get("21", "white")
        level = _colour_value(dps.get("24")) if mode == "colour" else int(dps.get("22", 1000))
        return {"on": bool(dps.get("20")), "brightness": round((level or 1000) / 10), "mode": mode}

    def power(self, room: Room, on: bool) -> None:
        self._call(room, lambda b: b.turn_on() if on else b.turn_off())

    def brightness(self, room: Room, percent: int) -> None:
        self._call(room, lambda b: b.set_brightness_percentage(percent))

    def colour(self, room: Room, hue: int) -> None:
        """Change la teinte en gardant la luminosité en cours (« 30 % puis bleu » reste à 30 %)."""
        percent = self.state(room)["brightness"]
        self._call(room, lambda b: b.set_hsv(hue / 360, 1.0, max(percent, 1) / 100))

    def white(self, room: Room, kelvin: int) -> None:
        percent = self.state(room)["brightness"]
        warmth = round(100 * (kelvin - KELVIN_MIN) / (KELVIN_MAX - KELVIN_MIN))
        self._call(room, lambda b: b.set_white_percentage(max(1, percent), warmth))


@dataclass(frozen=True)
class Group:
    key: str
    name: str  # « l'étage »
    rooms: tuple[str, ...]
    aliases: tuple[str, ...] = ()


class Rooms:
    def __init__(self, rooms: list[Room], groups: list[Group] | None = None):
        if not rooms:
            raise ValueError("[lights.rooms] : aucune pièce")
        self._rooms = {room.key: room for room in rooms}
        self._groups = {group.key: group for group in groups or []}
        for group in self._groups.values():
            if group.key in self._rooms or group.key == ALL or not group.rooms or not set(group.rooms) <= set(self._rooms):
                raise ValueError(f"[lights.groups.{group.key}] invalide (pièces connues uniquement)")
        self._aliases = AliasIndex([*((a, r.key) for r in rooms for a in (r.name, r.key, *r.aliases)),
                                    *((a, g.key) for g in self._groups.values() for a in (g.name, g.key, *g.aliases)),
                                    *((a, ALL) for a in ALL_ALIASES)])
        known = {normalize(a) for r in rooms for a in (r.name, r.key, *r.aliases)}
        known |= {normalize(a) for g in self._groups.values() for a in (g.name, g.key, *g.aliases)}
        self._unknown = [name for name in COMMON_ROOMS if not any(name in k or k in name for k in known if k)]

    def __getitem__(self, key: str) -> Room:
        return self._rooms[key]

    def rooms(self) -> list[Room]:
        return list(self._rooms.values())

    def keys(self) -> tuple[str, ...]:
        return (*self._rooms, *self._groups, ALL)

    def resolve(self, text: str) -> str:
        """Pièce (ou groupe) nommée dans la demande ; sinon toutes (« all »), ou la seule pièce s'il n'y en a qu'une.
        Une pièce sans lumière configurée (« le bureau ») donne « ?bureau », refusé ensuite avec une phrase claire."""
        found = self._aliases.find(text)
        if found is not None:
            return found
        norm = f" {normalize(text)} "
        for name in self._unknown:
            if re.search(rf" (?:du|de la|de l|des|dans le|dans la|dans l|au|a la|a l|le|la|l) {name} ", norm):
                return f"?{name}"
        return next(iter(self._rooms)) if len(self._rooms) == 1 else ALL

    def check(self, value: str) -> str:
        if value.startswith("?"):
            raise ToolError(UNKNOWN_ROOM, f"Je n'ai pas de lumière configurée pour « {value[1:]} ».")
        if value not in self.keys():
            raise ToolError(INVALID_PARAMETERS, "Pièce inconnue.")
        return value

    def targets(self, key: str | None) -> list[Room]:
        if key is None:
            raise ToolError(INVALID_PARAMETERS, "Dans quelle pièce ?")
        if key == ALL:
            return list(self._rooms.values())
        if key in self._groups:
            return [self._rooms[k] for k in self._groups[key].rooms]
        return [self._rooms[self.check(key)]]


def _said(names: list[str], state: str) -> str:
    """« La lumière de la chambre est allumée. » ; « Toutes les lumières sont allumées. »"""
    if len(names) > 1:
        head, space, tail = state.partition(" ")
        return f"Toutes les lumières sont {head}{'s' if head in ('allumée', 'éteinte') else ''}{space}{tail}."
    return f"La lumière {of(names[0])} est {state}."


def _failed_said(result: dict) -> str:
    """Lumières visées qui n'ont pas répondu (les autres ont été réglées)."""
    failed = result.get("failed") or []
    if not failed:
        return ""
    if len(failed) == 1:
        return f" Mais la lumière {of(failed[0])} ne répond pas."
    return f" Mais {len(failed)} lumières ne répondent pas."


def _state_said(states: list[dict]) -> str:
    """État lu sur les ampoules : « La lumière de la chambre est allumée à 30 %, en couleur. »"""
    def describe(s: dict) -> str:
        if s.get("online") is False:
            return "ne répond pas"
        if not s["on"]:
            return "est éteinte"
        mode = ", en couleur" if s.get("mode") == "colour" else ""
        return f"est allumée à {s['brightness']} %{mode}"

    if len(states) == 1:
        return f"La lumière {of(states[0]['room'])} {describe(states[0])}."
    if all(s.get("online") is not False and not s["on"] for s in states):
        return "Toutes les lumières sont éteintes."
    parts = []
    for s in states:
        text = describe(s)
        text = text[4:] if text.startswith("est ") else text
        parts.append(f"{s['room'][:1].upper()}{s['room'][1:]} : {text}")
    return " ; ".join(parts) + "."


def load_scenes(table: dict | None) -> dict[str, dict]:
    """Scènes intégrées, complétées ou remplacées par [lights.scenes.<nom>] (brightness, temperature ou color,
    name, aliases)."""
    scenes = {key: dict(value) for key, value in SCENES.items()}
    for key, spec in (table or {}).items():
        if not isinstance(spec, dict) or not key.isidentifier():
            raise ValueError(f"[lights.scenes.{key}] invalide")
        scene = {**scenes.get(key, {"name": key, "aliases": []}), **spec}
        if "temperature" in spec:
            scene.pop("color", None)
        if "color" in spec:
            scene.pop("temperature", None)
        if "brightness" in scene and not (isinstance(scene["brightness"], int) and 1 <= scene["brightness"] <= 100):
            raise ValueError(f"[lights.scenes.{key}] brightness : 1 à 100")
        if "temperature" in scene and not (isinstance(scene["temperature"], int)
                                           and KELVIN_MIN <= scene["temperature"] <= KELVIN_MAX):
            raise ValueError(f"[lights.scenes.{key}] temperature : {KELVIN_MIN} à {KELVIN_MAX}")
        if "color" in scene and scene["color"] not in COLORS:
            raise ValueError(f"[lights.scenes.{key}] color inconnue : {scene['color']}")
        scenes[key] = scene
    return scenes


def scene_aliases(scenes: dict[str, dict]) -> list[tuple[str, str]]:
    """(mot dit, scène) : clé, nom et alias de chaque scène."""
    return [(alias, key) for key, scene in scenes.items()
            for alias in (key, scene.get("name", key), *scene.get("aliases", []))]


def light_tools(rooms: Rooms, driver: LightDriver, scenes: dict[str, dict] | None = None, home=None) -> list[Tool]:
    scenes = scenes if scenes is not None else load_scenes(None)
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lumiere")

    def room_param() -> Param:
        return Param(str, "pièce", required=False, max_length=40, hidden=True, resolve=rooms.resolve,
                     check=rooms.check)

    def percent_param(required: bool) -> Param:
        return Param(int, "luminosité en pourcentage (1 à 100), seulement si elle a été dite", required=required,
                     minimum=1, maximum=100)

    def remember(target: Room, values: dict, confirmed: bool) -> None:
        if home is not None:
            home.update(f"lights.{target.key}", values, confirmed, "tuya")

    def each(room: str | None, action: Callable[[Room], object],
             values: dict | None = None) -> tuple[list[Room], list[str]]:
        """Action sur chaque lumière visée, en parallèle s'il y en a plusieurs. Une lumière qui ne répond pas
        n'empêche pas les autres ; si aucune ne répond, l'erreur est remontée telle quelle."""
        targets = rooms.targets(room)

        def run(target: Room):
            try:
                action(target)
            except ToolError as exc:
                return exc
            if values:
                remember(target, values, confirmed=False)
            return None

        outcomes = [run(targets[0])] if len(targets) == 1 else list(pool.map(run, targets))
        errors = [(t, e) for t, e in zip(targets, outcomes, strict=True) if e is not None]
        if errors and len(errors) == len(targets):
            raise errors[0][1]
        done = [t for t, e in zip(targets, outcomes, strict=True) if e is None]
        return done, [t.name for t, _ in errors]

    def result(done: tuple[list[Room], list[str]], **values) -> dict:
        targets, failed = done
        data = {"rooms": [t.name for t in targets], **values}
        if failed:
            data["failed"] = failed
        return data

    def light_on(room: str | None = None, brightness: int | None = None) -> dict:
        def action(r: Room) -> None:
            driver.power(r, True)
            if brightness is not None:
                driver.brightness(r, brightness)
        values = {"power": True, **({"brightness": brightness} if brightness is not None else {})}
        return result(each(room, action, values), on=True, brightness=brightness)

    def light_off(room: str | None = None) -> dict:
        return result(each(room, lambda r: driver.power(r, False), {"power": False}), on=False)

    def light_toggle(room: str | None = None) -> dict:
        targets = rooms.targets(room)
        on = not any(driver.state(t)["on"] for t in targets)
        return result(each(room, lambda r: driver.power(r, on), {"power": on}), on=on)

    def set_brightness(room: str | None = None, brightness: int = 100) -> dict:
        done = each(room, lambda r: (driver.power(r, True), driver.brightness(r, brightness)),
                    {"power": True, "brightness": brightness})
        return result(done, brightness=brightness)

    def set_color(color: str, room: str | None = None) -> dict:
        if color in WHITES:
            done = each(room, lambda r: (driver.power(r, True), driver.white(r, WHITES[color])),
                        {"power": True, "mode": "white", "color_temperature": WHITES[color]})
        else:
            done = each(room, lambda r: (driver.power(r, True), driver.colour(r, HUES[color])),
                        {"power": True, "mode": "colour", "color": color})
        return result(done, color=color)

    def set_color_temperature(temperature: int, room: str | None = None) -> dict:
        done = each(room, lambda r: (driver.power(r, True), driver.white(r, temperature)),
                    {"power": True, "mode": "white", "color_temperature": temperature})
        return result(done, temperature=temperature)

    def set_scene(scene: str, room: str | None = None) -> dict:
        spec = scenes[scene]

        def action(r: Room) -> None:
            driver.power(r, True)
            if spec.get("color") in HUES:
                driver.colour(r, HUES[spec["color"]])
            elif spec.get("color") in WHITES:
                driver.white(r, WHITES[spec["color"]])
            elif "temperature" in spec:
                driver.white(r, spec["temperature"])
            if "brightness" in spec:
                driver.brightness(r, spec["brightness"])
        values = {"power": True, "scene": scene, **({"brightness": spec["brightness"]} if "brightness" in spec else {})}
        return result(each(room, action, values), scene=spec.get("name", scene))

    def light_status(room: str | None = None) -> dict:
        targets = rooms.targets(room)

        def read(target: Room) -> dict:
            try:
                state = driver.state(target)
            except ToolError:
                return {"room": target.name, "online": False}
            remember(target, {"power": state["on"], "brightness": state["brightness"], "mode": state.get("mode")},
                     confirmed=True)
            return {"room": target.name, "online": True, **state}

        states = [read(targets[0])] if len(targets) == 1 else list(pool.map(read, targets))
        if all(s["online"] is False for s in states):
            raise ToolError(LIGHT_UNREACHABLE, f"La lumière {of(states[0]['room'])} ne répond pas."
                            if len(states) == 1 else "Aucune lumière ne répond.")
        return {"lights": states}

    def said_power(r: dict) -> str:
        level = f" à {r['brightness']} %" if r.get("brightness") else ""
        return _said(r["rooms"], ("allumée" if r["on"] else "éteinte") + level) + _failed_said(r)

    def said_level(r: dict, value: str) -> str:
        return _said(r["rooms"], value) + _failed_said(r)

    scene_names = tuple(scenes)
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
        Tool("set_scene", f"Applique une ambiance lumineuse ({', '.join(scene_names)}).",
             {"room": room_param(), "scene": Param(str, "ambiance dite", choices=scene_names, max_length=20)},
             {"rooms": "pièces", "scene": "ambiance"}, Risk.SAFE, set_scene,
             say=lambda r: said_level(r, f"en ambiance {r['scene']}")),
        Tool("light_status", "Dit si une lumière est allumée et à quelle luminosité (lecture réelle de l'ampoule).",
             {"room": room_param()}, {"lights": "liste de {room, online, on, brightness, mode}"}, Risk.SAFE,
             light_status, say=lambda r: _state_said(r["lights"])),
    ]


def load_rooms(table: dict, key_of: Callable[[str], str], groups: dict | None = None) -> Rooms | None:
    """[lights.rooms.<pièce>] (name, device_id, ip, version, aliases) ; clé locale par ``key_of(pièce)`` ;
    [lights.groups.<groupe>] (name, rooms, aliases) facultatifs."""
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
    parsed = []
    for key, spec in (groups or {}).items():
        if not isinstance(spec, dict) or not key.isidentifier():
            raise ValueError(f"[lights.groups.{key}] invalide")
        parsed.append(Group(key, str(spec.get("name", key)), tuple(str(r) for r in spec.get("rooms", [])),
                            tuple(str(a) for a in spec.get("aliases", []))))
    return Rooms(rooms, parsed)
