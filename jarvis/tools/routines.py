"""Routines par la voix : les lister, en lancer une, en supprimer une (avec confirmation).

La création passe par JARVIS Control ou par une phrase programmée (« Tous les jours à 21 h, ... », voir
jarvis.scheduling.actions). Le nom d'une routine est retrouvé par ses mots ; plusieurs correspondances -> question.
"""

from __future__ import annotations

from datetime import datetime

from jarvis.memory import keywords
from jarvis.routines.engine import WEEKDAYS, next_time
from jarvis.scheduling.clock import spoken_clock
from jarvis.tools.base import Param, Risk, Tool, ToolError

ROUTINE_NOT_FOUND = "routine_not_found"
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
        if not engine.run(routine.id):
            raise ToolError("already_running", f"La routine « {routine.name} » est déjà en cours.")
        return {"name": routine.name}

    def delete_routine(name: str) -> dict:
        routine = find(name)
        engine.delete(routine.id)
        return {"name": routine.name}

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
        Tool("delete_routine", "Supprime une routine ou une action programmée.", {"name": name}, {"name": "routine"},
             Risk.CONFIRMATION_REQUIRED, delete_routine,
             question=lambda p: f"Voulez-vous vraiment supprimer la routine « {p['name']} » ?",
             say=lambda r: f"La routine « {r['name']} » est supprimée."),
    ]
