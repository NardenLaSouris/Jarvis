"""Routine : déclencheur et suite d'actions, validés strictement avant tout enregistrement.

Une action est soit un outil du registre de JARVIS (validé comme une demande du LLM, et sans confirmation
requise : une routine s'exécute sans personne pour répondre « oui »), soit une phrase dite par JARVIS, soit
une attente. Aucune autre forme n'existe : pas de commande libre.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from jarvis.tools import Risk, ToolError, ToolRegistry, parse_request

MAX_ACTIONS = 20
MAX_WAIT = 600
MAX_INTERVAL = 1440
TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class RoutineError(ValueError):
    pass


@dataclass(frozen=True)
class Routine:
    id: str
    name: str
    description: str
    enabled: bool
    trigger: dict
    actions: tuple[dict, ...]

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "description": self.description, "enabled": self.enabled,
                "trigger": dict(self.trigger), "actions": [dict(a) for a in self.actions]}


def _text(data: dict, key: str, limit: int, required: bool, label: str) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise RoutineError(f"{label} : texte attendu.")
    value = value.strip()
    if (required and not value) or len(value) > limit or CONTROL.search(value):
        raise RoutineError(f"{label} : {'obligatoire, ' if required else ''}{limit} caractères au plus.")
    return value


def _integer(data: dict, key: str, low: int, high: int, label: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise RoutineError(f"{label} : nombre entier de {low} à {high} attendu.")
    return value


def _only(data: dict, keys: set[str], label: str) -> None:
    if not isinstance(data, dict):
        raise RoutineError(f"{label} : objet attendu.")
    extra = set(data) - keys
    if extra:
        raise RoutineError(f"{label} : champs inconnus {sorted(extra)}.")


def parse_trigger(data) -> dict:
    _only(data, {"type", "time", "days", "minutes"}, "Déclencheur")
    kind = data.get("type")
    if kind == "manual":
        return {"type": "manual"}
    if kind == "time":
        time = data.get("time")
        if not isinstance(time, str) or not TIME.match(time):
            raise RoutineError("Déclencheur : heure au format HH:MM attendue.")
        days = data.get("days", [])
        if not isinstance(days, list) or any(not isinstance(d, int) or isinstance(d, bool) or not 0 <= d <= 6
                                             for d in days):
            raise RoutineError("Déclencheur : jours de 0 (lundi) à 6 (dimanche).")
        return {"type": "time", "time": time, "days": sorted(set(days))}
    if kind == "interval":
        return {"type": "interval", "minutes": _integer(data, "minutes", 1, MAX_INTERVAL, "Intervalle (minutes)")}
    raise RoutineError("Déclencheur : type « time », « interval » ou « manual » attendu.")


def parse_action(data, registry: ToolRegistry) -> dict:
    kind = data.get("type") if isinstance(data, dict) else None
    if kind == "say":
        _only(data, {"type", "text"}, "Action « dire »")
        return {"type": "say", "text": _text(data, "text", 200, True, "Phrase")}
    if kind == "wait":
        _only(data, {"type", "seconds"}, "Action « attendre »")
        return {"type": "wait", "seconds": _integer(data, "seconds", 1, MAX_WAIT, "Attente (secondes)")}
    if kind == "tool":
        _only(data, {"type", "tool", "parameters"}, "Action")
        name = data.get("tool")
        if not registry.exists(name):
            raise RoutineError(f"Action : outil inconnu {str(name)[:40]!r}.")
        if registry.get(name).risk is not Risk.SAFE:
            raise RoutineError(f"Action : « {name} » demande une confirmation, impossible dans une routine.")
        try:
            request = parse_request({"tool": name, "parameters": data.get("parameters") or {}}, registry)
        except ToolError as exc:
            raise RoutineError(f"Action « {name} » : {exc.message}") from exc
        return {"type": "tool", "tool": request.tool, "parameters": dict(request.parameters)}
    raise RoutineError("Action : type « tool », « say » ou « wait » attendu.")


def parse_routine(data, registry: ToolRegistry, routine_id: str | None = None) -> Routine:
    """Routine validée (RoutineError sinon) ; ``routine_id`` conservé lors d'une modification."""
    _only(data, {"id", "name", "description", "enabled", "trigger", "actions"}, "Routine")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise RoutineError("Routine : « enabled » doit valoir true ou false.")
    actions = data.get("actions")
    if not isinstance(actions, list) or not 1 <= len(actions) <= MAX_ACTIONS:
        raise RoutineError(f"Routine : de 1 à {MAX_ACTIONS} actions.")
    return Routine(routine_id or uuid.uuid4().hex[:8], _text(data, "name", 60, True, "Nom"),
                   _text(data, "description", 200, False, "Description"), enabled,
                   parse_trigger(data.get("trigger")), tuple(parse_action(a, registry) for a in actions))
