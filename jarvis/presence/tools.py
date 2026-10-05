"""Outil de lecture « qui est à la maison ? » : l'état décidé par le moteur de présence, jamais deviné."""

from __future__ import annotations

from typing import Callable

from jarvis.presence.engine import AWAY, POSSIBLE_ARRIVAL, PresenceEngine
from jarvis.tools.base import Risk, Tool


def presence_tool(engine: PresenceEngine, name_of: Callable[[str], str]) -> Tool:
    def run() -> dict:
        snapshot = engine.snapshot()
        return {"house": snapshot["house"],
                "users": {user: data["state"] for user, data in snapshot["users"].items()}}

    def say(r: dict) -> str:
        if r["house"] == "unknown":
            return "Je ne suis la présence de personne pour le moment : aucun capteur de présence n'est configuré."
        order = lambda name: (name != "vous", name)  # noqa: E731 - « vous » d'abord
        home = sorted((name_of(u) for u, s in r["users"].items() if s not in (AWAY, POSSIBLE_ARRIVAL)), key=order)
        away = sorted((name_of(u) for u, s in r["users"].items() if s in (AWAY, POSSIBLE_ARRIVAL)), key=order)
        if not home:
            return "La maison est vide."
        sentence = f"À la maison : {', '.join(home)}."
        return sentence + (f" Absent : {', '.join(away)}." if away else "")

    return Tool("presence_status", "Dit qui est à la maison (d'après les capteurs de présence).", {},
                {"house": "occupied, empty ou unknown", "users": "état de chacun"}, Risk.SAFE, run, say=say)
