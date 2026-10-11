"""Outils de présence : « qui est à la maison ? » (l'état décidé par le moteur, jamais deviné) et « je sors »
(déclaration explicite, comprise sans LLM, appliquée par le moteur)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Callable

from jarvis.presence.declaration import LATER, NOW, SHORT, STAY, parse_declaration
from jarvis.presence.engine import AWAY, POSSIBLE_ARRIVAL, PresenceEngine
from jarvis.tools.base import EXECUTION_FAILED, INVALID_PARAMETERS, Param, Risk, Tool, ToolError


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


def _plan(text: str, clock: Callable[[], datetime]) -> str | None:
    """Ce que dit la demande (jamais le LLM) : « now », « later:<horodatage> », « short » ou « stay »."""
    declaration = parse_declaration(text, clock())
    if declaration is None:
        return None
    if declaration.kind == LATER:
        return f"later:{declaration.at.timestamp():.0f}"
    return declaration.kind


def _check_plan(value: str) -> str:
    if value in (NOW, SHORT, STAY) or re.fullmatch(r"later:\d{9,11}", value):
        return value
    raise ToolError(INVALID_PARAMETERS, "Je n'ai pas compris quand vous partez.")


def departure_tool(engine: PresenceEngine, user: Callable[[], str], clock: Callable[[], datetime] = datetime.now) -> Tool:
    """« Je sors » : vous êtes absent tout de suite (ou à l'heure dite), sans attendre les capteurs ; le moteur de
    présence garde la main sur tout le reste (retour détecté comme d'habitude)."""

    def run(plan: str) -> dict:
        who = user()
        if plan == SHORT:
            return {"result": "short"}
        if plan == STAY:
            return {"result": "stay" if engine.cancel_declared(who) else "nothing"}
        at = float(plan.split(":", 1)[1]) if plan.startswith("later:") else None
        result = engine.declare_departure(who, at)
        if result == "unknown":
            raise ToolError(EXECUTION_FAILED, "Je ne suis pas votre présence : aucun capteur ne vous concerne.")
        return {"result": result, "at": datetime.fromtimestamp(at).strftime("%H:%M") if at else ""}

    def say(r: dict) -> str:
        return {"away": "Entendu, monsieur. Je vous considère comme absent ; je veille sur la maison.",
                "already": "Vous êtes déjà noté absent, monsieur.",
                "planned": f"Entendu. Je vous considérerai comme absent à {r.get('at', '')}.",
                "short": "Entendu, je ne vous compte pas absent pour si peu.",
                "stay": "Très bien, j'annule votre départ prévu.",
                "nothing": "Très bien, monsieur."}[r["result"]]

    plan = Param(str, "départ déclaré", max_length=24, check=_check_plan, hidden=True,
                 resolve=lambda text: _plan(text, clock))
    return Tool("declare_departure", "Note que l'utilisateur sort de la maison (« je sors »), maintenant ou à l'heure "
                "dite, ou qu'il reste finalement.", {"plan": plan}, {"result": "away, planned, already, short, stay"},
                Risk.SAFE, run, say=say)
