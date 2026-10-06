"""Outils minuteurs et rappels : ils valident la demande et délèguent tout au gestionnaire.

Durées : le LLM transmet la durée telle qu'elle a été dite (« 10 minutes », « une heure et demie ») ;
elle est convertie par ``parse_duration`` et doit figurer dans la demande. Annulation : par numéro,
par durée / message, ou sans précision s'il n'y a qu'une seule échéance en cours.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Callable

from jarvis.personality import MONTHS, WEEKDAYS, normalize, second_person, with_de
from jarvis.scheduling.clock import parse_clock
from jarvis.scheduling.durations import (
    DurationError, duration_in_text, parse_duration, spoken_duration, spoken_remaining,
)
from jarvis.scheduling.manager import REMINDER_NOT_FOUND, TIMER_NOT_FOUND, SchedulingError, TimerManager
from jarvis.scheduling.models import Reminder, Scheduled, Timer
from jarvis.tools.base import INVALID_PARAMETERS, Param, Risk, Tool, ToolError

AMBIGUOUS = "ambiguous_target"
ID = re.compile(r"^\d{1,6}$")
COMMON_WORDS = {"de", "d", "le", "la", "les", "l", "un", "une", "des", "du", "a", "mon", "ma", "mes"}


def _duration(value: str) -> int:
    try:
        return parse_duration(value)
    except DurationError as exc:
        raise ToolError(INVALID_PARAMETERS, f"Je n'ai pas compris la durée « {value[:40]} ».") from exc


DAY_OFFSETS = (("apres demain", 2), ("apres-demain", 2), ("demain", 1), ("aujourd hui", 0), ("ce soir", 0),
               ("cet apres midi", 0), ("ce matin", 0))
OTHER_DAY = "other"


def _reminder_day(text: str) -> str | None:
    """Jour dit dans la demande d'un rappel : « 0 », « 1 » (demain), « 2 » (après-demain), « other » pour une date ou
    un jour de semaine (« le 31 février », « lundi ») que les rappels ne gèrent pas, None sinon. Jamais ignoré : sans
    cela, « demain à 15 heures » sonnait aujourd'hui."""
    norm = f" {normalize(text)} "
    # Seulement autour de l'heure : « à 18 h, de préparer la réunion de demain » vise aujourd'hui.
    clock = re.search(r" (?:\d{1,2} (?:h|heures?)(?: \d{1,2})?|midi|minuit)(?= )", norm)
    if clock is not None:
        norm = norm[:clock.end()] + " " + " ".join(norm[clock.end():].split()[:2]) + " "
    for words, offset in DAY_OFFSETS:
        if f" {words} " in norm:
            return str(offset)
    if any(f" {normalize(w)} " in norm for w in (*MONTHS, *WEEKDAYS)) or re.search(r" le \d{1,2} ", norm):
        return OTHER_DAY
    return None


def _clock_text(value: str) -> str:
    if parse_clock(value) is None:
        raise ToolError(INVALID_PARAMETERS, f"Je n'ai pas compris l'heure « {value[:40]} ».")
    return value


def _clock_said(value: str, text: str) -> bool:
    return parse_clock(value) is not None and parse_clock(value) == parse_clock(text)


def _identifier(value: str) -> str:
    if not ID.match(value):
        raise ToolError(INVALID_PARAMETERS, "Numéro invalide.")
    return value


def _call(action: Callable[[], Scheduled]) -> Scheduled:
    try:
        return action()
    except SchedulingError as exc:
        raise ToolError(exc.code, exc.message) from exc


def _duration_param(description: str, required: bool = True) -> Param:
    return Param(str, description, required=required, max_length=60, check=_duration, evidence=duration_in_text)


def _words(text: str) -> set[str]:
    return set(normalize(text).split()) - COMMON_WORDS


def _pick(active: list, item_id: str | None, matches: Callable[[Scheduled], bool] | None, label: str,
          missing: str, describe: Callable[[Scheduled], str]) -> str:
    if item_id:
        return item_id
    candidates = [item for item in active if matches(item)] if matches else active
    if not candidates:
        raise ToolError(missing, f"Aucun {label} correspondant n'est en cours." if matches else f"Aucun {label} n'est en cours.")
    if len(candidates) > 1:
        listing = " ; ".join(describe(item) for item in candidates)
        raise ToolError(AMBIGUOUS, f"Plusieurs {label}s sont en cours ({listing}). Lequel dois-je annuler ?")
    return candidates[0].id


def _timer_view(timer: Timer, manager: TimerManager) -> dict:
    return {"timer_id": timer.id, "duration": spoken_duration(timer.seconds),
            "remaining": spoken_remaining(timer.remaining(manager.now())), "ends_at": f"{timer.expires_at:%H:%M}"}


def _reminder_view(reminder: Reminder, manager: TimerManager) -> dict:
    now = manager.now()
    return {"reminder_id": reminder.id, "message": second_person(reminder.message),
            "remaining": spoken_remaining(reminder.remaining(now)), "at": f"{reminder.expires_at:%H:%M}",
            "day": (reminder.expires_at.date() - now.date()).days,
            "far": reminder.remaining(now) >= 2 * 3600}


DAY_NAMES = {0: "aujourd'hui", 1: "demain", 2: "après-demain"}


def _when(r: dict) -> str:
    """« dans 20 minutes » ; au-delà de deux heures ou un autre jour : « demain à 9 heures » (session QA : « dans 29
    heures 28 minutes »). Moins de deux heures : toujours « dans 20 minutes », même passé minuit (à 23 h 58,
    « demain à 0 h 18 » pour un rappel dans 20 minutes)."""
    if not r.get("far"):
        return f"dans {r['remaining']}"
    day = DAY_NAMES.get(r.get("day", 0), "")
    return f"{day} à {_at(r['at'])}".strip() if day else f"dans {r['remaining']}"


def _at(hhmm: str) -> str:
    hours, minutes = hhmm.split(":")
    return f"{int(hours)} h {minutes}" if minutes != "00" else f"{int(hours)} heures"


def _timers_said(result: dict) -> str:
    timers = result["timers"]
    if not timers:
        return "Aucun minuteur n'est en cours."
    if len(timers) == 1:
        return f"Il reste {timers[0]['remaining']} sur votre minuteur de {timers[0]['duration']}."
    listing = " ; ".join(f"{t['duration']}, encore {t['remaining']}" for t in timers)
    return f"Vous avez {len(timers)} minuteurs : {listing}."


def _reminders_said(result: dict) -> str:
    reminders = result["reminders"]
    if not reminders:
        return "Aucun rappel n'est prévu."
    if len(reminders) == 1:
        r = reminders[0]
        return f"Je dois vous rappeler {with_de(r['message'])} {_when(r)}."
    listing = " ; ".join(f"{r['message']} {_when(r)}" for r in reminders)
    return f"Vous avez {len(reminders)} rappels : {listing}."


def timer_tools(manager: TimerManager) -> list[Tool]:
    limit = spoken_duration(manager.max_seconds)
    reminder_limit = spoken_duration(getattr(manager, "max_reminder_seconds", manager.max_seconds))

    def create_timer(duration: int) -> dict:
        return _timer_view(_call(lambda: manager.create_timer(duration)), manager)

    def cancel_timer(timer_id: str | None = None, duration: int | None = None) -> dict:
        matches = (lambda t: abs(t.seconds - duration) < 1) if duration else None
        chosen = _pick(manager.timers(), timer_id, matches, "minuteur", TIMER_NOT_FOUND,
                       lambda t: f"n° {t.id} de {spoken_duration(t.seconds)}")
        timer = _call(lambda: manager.cancel_timer(chosen))
        return {"timer_id": timer.id, "duration": spoken_duration(timer.seconds), "cancelled": True}

    def list_timers() -> dict:
        timers = [_timer_view(t, manager) for t in manager.timers()]
        return {"count": len(timers), "timers": timers}

    def create_reminder(message: str, delay: int | None = None, time: str | None = None,
                        day: str | None = None) -> dict:
        if day == OTHER_DAY and delay is None:
            raise ToolError(INVALID_PARAMETERS, f"Je ne programme les rappels que pour les prochaines "
                                                f"{reminder_limit} : pour une date précise, ajoutez plutôt un "
                                                "événement au calendrier.")
        if delay is None and time is None:
            raise ToolError(INVALID_PARAMETERS, "Dans combien de temps, ou à quelle heure ?")
        if delay is None:
            hour, minute = parse_clock(time)
            now = manager.now()
            when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if day in ("1", "2"):
                when += timedelta(days=int(day))
            elif when <= now:
                if day == "0":
                    raise ToolError(INVALID_PARAMETERS, "Cette heure est déjà passée aujourd'hui.")
                when += timedelta(days=1)
            delay = max(1, round((when - now).total_seconds()))
        return _reminder_view(_call(lambda: manager.create_reminder(delay, message)), manager)

    def cancel_reminder(reminder_id: str | None = None, message: str | None = None) -> dict:
        wanted = _words(message) if message else set()
        matches = (lambda r: bool(wanted) and wanted <= _words(r.message)) if message else None
        chosen = _pick(manager.reminders(), reminder_id, matches, "rappel", REMINDER_NOT_FOUND,
                       lambda r: f"n° {r.id} : {r.message}")
        reminder = _call(lambda: manager.cancel_reminder(chosen))
        return {"reminder_id": reminder.id, "message": second_person(reminder.message), "cancelled": True}

    def list_reminders() -> dict:
        reminders = [_reminder_view(r, manager) for r in manager.reminders()]
        return {"count": len(reminders), "reminders": reminders}

    return [
        Tool("create_timer", f"Lance un minuteur (jusqu'à {limit}).",
             {"duration": _duration_param("durée telle qu'elle a été dite, par exemple « 10 minutes »")},
             {"timer_id": "numéro", "duration": "durée", "remaining": "temps restant", "ends_at": "HH:MM"},
             Risk.SAFE, create_timer,
             say=lambda r: f"Minuteur de {r['duration']} lancé, il sonnera à {_at(r['ends_at'])}."),
        Tool("cancel_timer", "Annule un minuteur en cours (le seul, ou celui désigné par son numéro ou sa durée).",
             {"timer_id": Param(str, "numéro du minuteur, s'il a été dit", required=False, max_length=6,
                                check=_identifier),
              "duration": _duration_param("durée du minuteur à annuler, si elle a été dite", required=False)},
             {"timer_id": "numéro", "duration": "durée", "cancelled": "true"}, Risk.SAFE, cancel_timer,
             say=lambda r: f"Votre minuteur de {r['duration']} est annulé."),
        Tool("list_timers", "Liste les minuteurs en cours et leur temps restant.", {},
             {"count": "nombre", "timers": "liste de {timer_id, duration, remaining, ends_at}"}, Risk.SAFE, list_timers,
             say=_timers_said),
        Tool("create_reminder", f"Programme un rappel dans un délai donné (jusqu'à {reminder_limit}) ou à une heure "
             "précise.",
             {"delay": _duration_param("délai tel qu'il a été dit, par exemple « 20 minutes »", required=False),
              "time": Param(str, "heure précise telle qu'elle a été dite (« à 18 heures »), au lieu d'un délai",
                            required=False, max_length=40, check=_clock_text, evidence=_clock_said),
              "message": Param(str, "ce qu'il faudra rappeler, par exemple « sortir le linge »", max_length=200),
              "day": Param(str, "jour dit", required=False, hidden=True, max_length=8, resolve=_reminder_day,
                           choices=("0", "1", "2", OTHER_DAY))},
             {"reminder_id": "numéro", "message": "message", "remaining": "délai", "at": "HH:MM"},
             Risk.SAFE, create_reminder,
             say=lambda r: f"Entendu, je vous rappellerai {with_de(r['message'])} {_when(r)}."),
        Tool("cancel_reminder", "Annule un rappel prévu (le seul, ou celui désigné par son numéro ou son message).",
             {"reminder_id": Param(str, "numéro du rappel, s'il a été dit", required=False, max_length=6,
                                   check=_identifier),
              "message": Param(str, "mots du rappel à annuler, s'ils ont été dits", required=False, max_length=200)},
             {"reminder_id": "numéro", "message": "message", "cancelled": "true"}, Risk.SAFE, cancel_reminder,
             say=lambda r: f"Le rappel {with_de(r['message'])} est annulé."),
        Tool("list_reminders", "Liste les rappels prévus.", {},
             {"count": "nombre", "reminders": "liste de {reminder_id, message, remaining, at}"}, Risk.SAFE,
             list_reminders, say=_reminders_said),
    ]
