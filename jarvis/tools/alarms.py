"""Réveils par la voix (« réveille-moi à 7 h 30 », « quels réveils ? », « annule mon réveil »).

Un réveil est une routine ponctuelle (``once``) : sonnerie, puis, si ``briefing``, l'annonce de la journée.
Il est enregistré avec les routines (il survit à un redémarrage) et visible dans ORION Control.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from jarvis.personality import normalize
from jarvis.routines.engine import next_time
from jarvis.scheduling.clock import parse_clock, spoken_clock
from jarvis.tools.base import INVALID_PARAMETERS, Param, Risk, Tool, ToolError

WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
ALARM_NOT_FOUND = "alarm_not_found"


def _clock(value: str) -> tuple[int, int]:
    clock = parse_clock(value)
    if clock is None:
        raise ToolError(INVALID_PARAMETERS, "Cette heure n'existe pas, ou je ne l'ai pas comprise : dites par exemple "
                                            "« 7 heures 30 ».")
    return clock


def _clock_said(value: str, text: str) -> bool:
    return parse_clock(value) is not None and parse_clock(value) == parse_clock(text)


def _day(when: datetime, now: datetime) -> str:
    if when.date() == now.date():
        return "aujourd'hui"
    if when.date() == (now + timedelta(days=1)).date():
        return "demain"
    return WEEKDAYS[when.weekday()]


def _said_alarm_day(text: str) -> str | None:
    """Jour d'un réveil à annuler, sous la forme de _day : « aujourd hui », « demain » ou le nom du jour."""
    norm = f" {normalize(text)} "
    if " apres demain " in norm:
        return None
    for said, day in ((" demain ", "demain"), (" aujourd hui ", "aujourd hui"), (" ce matin ", "aujourd hui")):
        if said in norm:
            return day
    return next((d for d in WEEKDAYS if f" {d} " in norm), None)


def alarm_tools(engine, briefing: bool = True) -> list[Tool]:
    def upcoming() -> list[tuple[datetime, object]]:
        now = engine.clock()
        alarms = [(next_time(r.trigger, now), r) for r in engine.routines() if r.is_alarm and r.enabled]
        return sorted((a for a in alarms if a[0] is not None), key=lambda a: a[0])

    def create_alarm(time: str, day: str | None = None) -> dict:
        hour, minute = _clock(time)
        now = engine.clock()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if day == "tomorrow":
            target += timedelta(days=1)
        elif target <= now:
            target += timedelta(days=1)
        actions = [{"type": "alarm"}] + ([{"type": "announce", "what": "day"}] if briefing else [])
        routine = engine.create({"name": f"Réveil {_day(target, now)} à {spoken_clock(hour, minute)}",
                                 "description": "Réveil programmé par la voix", "once": True,
                                 "trigger": {"type": "time", "time": f"{hour:02d}:{minute:02d}", "days": [target.weekday()]},
                                 "actions": actions})
        return {"alarm_id": routine["id"], "time": f"{hour:02d}:{minute:02d}", "day": _day(target, now),
                "spoken": spoken_clock(hour, minute)}

    def list_alarms() -> dict:
        now = engine.clock()
        return {"count": len(alarms := upcoming()),
                "alarms": [{"alarm_id": r.id, "day": _day(when, now), "time": f"{when:%H:%M}",
                            "spoken": spoken_clock(when.hour, when.minute), "repeat": not r.once} for when, r in alarms]}

    def cancel_alarm(time: str | None = None, day: str | None = None) -> dict:
        alarms = upcoming()
        if day is not None:  # « annule le réveil de demain » (session QA : « demain » proposé comme heure, écarté)
            alarms = [(when, r) for when, r in alarms if normalize(_day(when, engine.clock())) == day]
        if time is not None:
            wanted = _clock(time)
            alarms = [(when, r) for when, r in alarms if (when.hour, when.minute) == wanted]
        if not alarms:
            raise ToolError(ALARM_NOT_FOUND, "Aucun réveil correspondant n'est programmé.")
        if len(alarms) > 1:
            listing = " ; ".join(f"{_day(w, engine.clock())} à {spoken_clock(w.hour, w.minute)}" for w, _ in alarms)
            raise ToolError("ambiguous_target", f"Plusieurs réveils sont programmés ({listing}). Lequel dois-je annuler ?")
        when, routine = alarms[0]
        if routine.once:
            engine.delete(routine.id)
        else:
            engine.set_enabled(routine.id, False)
        return {"alarm_id": routine.id, "spoken": spoken_clock(when.hour, when.minute), "cancelled": True}

    def said_list(r: dict) -> str:
        if not r["alarms"]:
            return "Aucun réveil n'est programmé."
        items = [f"{a['day']} à {a['spoken']}" + (" (récurrent)" if a["repeat"] else "") for a in r["alarms"]]
        if len(items) == 1:
            return f"Vous avez un réveil {items[0]}."
        return f"Vous avez {len(items)} réveils : {' ; '.join(items)}."

    time_param = Param(str, "heure telle qu'elle a été dite, par exemple « 7 heures 30 »", max_length=40,
                       evidence=_clock_said)
    return [
        Tool("create_alarm", "Programme un réveil (sonnerie) à une heure précise, aujourd'hui ou demain.",
             {"time": time_param,
              "day": Param(str, "jour", required=False, choices=("tomorrow",), hidden=True,
                           resolve=lambda text: "tomorrow" if " demain " in f" {normalize(text)} " else None)},
             {"time": "HH:MM", "day": "jour"}, Risk.SAFE, create_alarm,
             say=lambda r: f"Réveil programmé {r['day']} à {r['spoken']}."),
        Tool("list_alarms", "Liste les réveils programmés.", {}, {"count": "nombre", "alarms": "liste"}, Risk.SAFE,
             list_alarms, say=said_list),
        Tool("cancel_alarm", "Annule un réveil (le seul, ou celui de l'heure dite).",
             {"time": Param(str, "heure du réveil à annuler, si elle a été dite", required=False, max_length=40,
                            evidence=_clock_said),
              "day": Param(str, "jour", required=False, max_length=20, hidden=True, resolve=_said_alarm_day)},
             {"cancelled": "true"}, Risk.SAFE, cancel_alarm,
             say=lambda r: f"Le réveil de {r['spoken']} est annulé."),
    ]
