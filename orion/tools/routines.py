"""Routines par la voix : les lister, en lancer une, la désactiver ou la réactiver, en supprimer une (avec
confirmation). « Désactive » ne supprime jamais : la routine reste, sans s'exécuter, jusqu'à « réactive ».

La création passe par ORION Control ou par une phrase programmée (« Tous les jours à 21 h, ... », voir
jarvis.scheduling.actions). Le nom d'une routine est retrouvé par ses mots ; plusieurs correspondances -> question.
"""

from __future__ import annotations

from datetime import datetime

from orion.memory import keywords
from orion.routines.engine import WEEKDAYS, next_time
from orion.scheduling.clock import spoken_clock
from orion.tools.base import Param, Risk, Tool, ToolError
from orion.tools.core import request_user

ROUTINE_NOT_FOUND = "routine_not_found"
ROUTINE_DISABLED = "routine_disabled"
AMBIGUOUS = "ambiguous_target"


def _when(trigger: dict, now: datetime) -> str:
    upcoming = next_time(trigger, now)
    if upcoming is None:
        return "manuelle" if trigger["type"] == "manual" else "sans prochaine exécution"
    day = "aujourd'hui" if upcoming.date() == now.date() else \
        "demain" if (upcoming.date() - now.date()).days == 1 else WEEKDAYS[upcoming.weekday()]
    return f"{day} à {spoken_clock(upcoming.hour, upcoming.minute)}"


def routine_tools(engine) -> list[Tool]:
    def find(name: str):
        wanted = keywords(name)
        routines = engine.routines()
        exact = [r for r in routines if keywords(r.name) == wanted]
        found = exact or [r for r in routines if wanted and wanted & keywords(f"{r.name} {r.description}")]
        if not found:
            raise ToolError(ROUTINE_NOT_FOUND, f"Je ne trouve pas de routine « {name[:40]} ».")
        if len(found) > 1:
            names = ", ".join(f"« {r.name} »" for r in found[:4])
            raise ToolError(AMBIGUOUS, f"Plusieurs routines correspondent : {names}. Laquelle ?")
        return found[0]

    def list_routines() -> dict:
        now = engine.clock()
        items = [{"name": r.name, "enabled": r.enabled, "when": _when(r.trigger, now) if r.enabled else "désactivée"}
                 for r in engine.routines() if not r.is_alarm]
        return {"count": len(items), "routines": items}

    def run_routine(name: str) -> dict:
        routine = find(name)
        if not routine.enabled:
            # Désactivée = suspendue : jamais lancée implicitement. Activer et lancer restent deux demandes distinctes
            # (« réactive la routine soir », puis « lance-la »).
            raise ToolError(ROUTINE_DISABLED, f"La routine « {routine.name} » est désactivée. Dites « réactive la "
                                              f"routine {routine.name} » pour la remettre en service.")
        if not engine.run(routine.id, user=request_user() or None):
            raise ToolError("already_running", f"La routine « {routine.name} » est déjà en cours.")
        return {"name": routine.name}

    def delete_routine(name: str) -> dict:
        routine = find(name)
        engine.delete(routine.id)
        return {"name": routine.name}

    def switch(name: str, enabled: bool) -> dict:
        routine = find(name)
        if routine.enabled != enabled:
            engine.set_enabled(routine.id, enabled)
        return {"name": routine.name, "already": routine.enabled == enabled}

    def disable_routine(name: str) -> dict:
        return switch(name, False)

    def enable_routine(name: str) -> dict:
        return switch(name, True)

    def said_list(r: dict) -> str:
        if not r["routines"]:
            return "Aucune routine n'est programmée."
        items = " ; ".join(f"« {i['name']} », {i['when']}" for i in r["routines"][:6])
        return f"Vous avez {r['count']} routine{'s' if r['count'] > 1 else ''} : {items}."

    name = Param(str, "nom de la routine, avec les mots de l'utilisateur", max_length=60)
    return [
        Tool("list_routines", "Liste les routines et actions programmées, avec leur prochaine exécution.", {},
             {"routines": "liste de {name, enabled, when}"}, Risk.SAFE, list_routines, say=said_list),
        Tool("run_routine", "Lance maintenant une routine existante.", {"name": name}, {"name": "routine"},
             Risk.SAFE, run_routine, say=lambda r: f"Je lance la routine « {r['name']} »."),
        Tool("disable_routine", "Désactive une routine sans la supprimer (elle ne s'exécute plus jusqu'à réactivation).",
             {"name": name}, {"name": "routine"}, Risk.SAFE, disable_routine,
             say=lambda r: f"La routine « {r['name']} » " + ("était déjà désactivée." if r["already"] else
                                                             "est désactivée ; elle reste enregistrée.")),
        Tool("enable_routine", "Réactive une routine désactivée.", {"name": name}, {"name": "routine"},
             Risk.SAFE, enable_routine,
             say=lambda r: f"La routine « {r['name']} » " + ("était déjà active." if r["already"] else "est réactivée.")),
        Tool("delete_routine", "Supprime une routine ou une action programmée.", {"name": name}, {"name": "routine"},
             Risk.CONFIRMATION_REQUIRED, delete_routine,
             question=lambda p: f"Voulez-vous vraiment supprimer la routine « {p['name']} » ?",
             say=lambda r: f"La routine « {r['name']} » est supprimée."),
    ]
