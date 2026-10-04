"""Actions programmées par la voix : « Dans 10 minutes, allume la chambre. », « Tous les jours à 21 h, mets la
lumière à 30 %. », « Éteins tout à 23 h. », « Demain à 7 h, allume l'entrée. »

``split_schedule`` sépare l'expression de temps (au début ou à la fin de la phrase) de la commande, et en déduit le
déclencheur d'une routine : une date précise (``at``, une seule fois) ou une heure récurrente (``time`` + jours).
La commande elle-même est ensuite comprise comme d'habitude (commandes simples ou LLM) puis enregistrée comme
routine : à l'heure dite, elle passe par le même Core (validation, permissions) qu'une demande vocale. Les rappels,
minuteurs et réveils ont leurs propres outils et ne sont jamais traités ici.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from jarvis.personality import normalize
from jarvis.scheduling.clock import parse_clock, spoken_clock
from jarvis.scheduling.durations import SMALL, TENS, DurationError, parse_duration, spoken_duration, tokens

EXCLUDED = ("rappelle", "rappelez", "rappel", "minuteur", "timer", "minuterie", "reveil", "reveille", "alarme",
            "fais moi penser", "previens moi")
CLOCK_WORDS = {"h", "heure", "heures", "et", "demie", "demi", "quart", "moins", "le", "du", "soir", "matin", "apres",
               "midi", "minuit", "pile", *SMALL, *TENS}
WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
EVERY_DAY = ("tous les jours", "chaque jour", "tous les soirs", "chaque soir", "tous les matins", "chaque matin")
WEEKDAYS_ONLY = ("en semaine", "du lundi au vendredi", "les jours de semaine")
WEEKEND = ("le week end", "le weekend", "les week ends", "chaque week end", "tous les week ends")


@dataclass(frozen=True)
class Schedule:
    trigger: dict
    command: str  # la commande, telle qu'elle a été dite
    said: str  # « dans 10 minutes », « tous les jours à 21 heures »


def _words(text: str) -> list[tuple[int, int, str]]:
    """Mots de la phrase d'origine : (début, fin, mot normalisé)."""
    return [(m.start(), m.end(), normalize(m.group())) for m in re.finditer(r"[^\s,;:.!?]+", text)]


def _is_clock(segment: str) -> bool:
    words = tokens(segment)
    return bool(words) and parse_clock(segment) is not None and all(w.isdigit() or w in CLOCK_WORDS for w in words)


def _leads(now: datetime) -> list[tuple[tuple[str, ...], str, object]]:
    """(mots d'introduction, nature, jours) : nature « duration », « daily » (jours), « once » (décalage en jours)."""
    leads = [(("dans",), "duration", None)]
    for phrase in EVERY_DAY:
        leads.append((tuple(phrase.split()) + ("a",), "daily", []))
    for phrase in WEEKDAYS_ONLY:
        leads.append((tuple(phrase.split()) + ("a",), "daily", [0, 1, 2, 3, 4]))
    for phrase in WEEKEND:
        leads.append((tuple(phrase.split()) + ("a",), "daily", [5, 6]))
    for index, day in enumerate(WEEKDAYS):
        for phrase in (f"tous les {day}s", f"chaque {day}", f"le {day} soir", f"tous les {day}s soir"):
            leads.append((tuple(phrase.split()) + ("a",), "daily", [index]))
    leads += [(("demain", "soir", "a"), "once", 1), (("demain", "matin", "a"), "once", 1), (("demain", "a"), "once", 1),
              (("ce", "soir", "a"), "once", 0), (("cet", "apres", "midi", "a"), "once", 0), (("ce", "matin", "a"), "once", 0),
              (("a",), "once", None)]
    return sorted(leads, key=lambda lead: -len(lead[0]))


def _trigger(kind: str, extra, segment: str, lead: tuple[str, ...], now: datetime) -> tuple[dict, str] | None:
    if kind == "duration":
        try:
            seconds = parse_duration(segment)
        except DurationError:
            return None
        when = (now + timedelta(seconds=seconds)).replace(microsecond=0)
        return {"type": "at", "at": when.isoformat()}, f"dans {spoken_duration(seconds)}"
    if not _is_clock(segment):
        return None
    hour, minute = parse_clock(segment)
    if "soir" in lead and hour < 12 or "apres" in lead and hour < 12:
        hour += 12
    clock = spoken_clock(hour, minute)
    if kind == "daily":
        days = sorted(extra)
        label = "tous les jours" if not days else "en semaine" if days == [0, 1, 2, 3, 4] else \
            "le week-end" if days == [5, 6] else f"tous les {WEEKDAYS[days[0]]}s"
        return {"type": "time", "time": f"{hour:02d}:{minute:02d}", "days": days}, f"{label} à {clock}"
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=extra or 0)
    if extra is None and when <= now:
        when += timedelta(days=1)
    if when <= now:
        return None
    day = "demain" if when.date() > now.date() else "aujourd'hui"
    return {"type": "at", "at": when.isoformat()}, f"{day} à {clock}"


def split_schedule(text: str, now: datetime) -> Schedule | None:
    """Expression de temps + commande, ou None (pas d'expression de temps, ou rappel/minuteur/réveil)."""
    norm = f" {normalize(text)} "
    if any(f" {w} " in norm for w in EXCLUDED):
        return None
    words = _words(text)
    while words and words[0][2] in ("jarvis", "s", "il", "te", "plait"):
        words = words[1:]
    if len(words) < 3:
        return None
    names = [w for _, _, w in words]
    for lead, kind, extra in _leads(now):
        n = len(lead)
        # Au début : « dans 10 minutes, allume la chambre ».
        if tuple(names[:n]) == lead:
            for end in range(min(len(words), n + 6), n, -1):
                found = _trigger(kind, extra, text[words[n][0]:words[end - 1][1]], lead, now)
                command = text[words[end][0]:].strip(" ,;:.") if end < len(words) else ""
                if found and command:
                    return Schedule(found[0], command, found[1])
        # À la fin : « éteins tout à 23 h », « allume la chambre dans 10 minutes ».
        for start in range(len(words) - n - 1, 0, -1):
            if tuple(names[start:start + n]) != lead:
                continue
            found = _trigger(kind, extra, text[words[start + n][0]:words[-1][1]], lead, now)
            command = text[:words[start][0]].strip(" ,;:.")
            if found and command:
                return Schedule(found[0], command, found[1])
    return None
