"""Moment dit en français -> date et heure, sans LLM : « dans une heure », « demain à 18 h », « ce soir »,
« vendredi », « vendredi prochain », « le 20 octobre », « sous 48 heures », « d'ici la fin de la semaine ».

``parse_when`` rend un ``When`` (date, heure précise ou non, forme reconnue) ou None si aucun moment clair n'est
dit (« au plus vite ») : jamais de date inventée. Une date sans heure vaut la fin de la journée (``DAY_END``) et
``precise`` est faux. Les dates sont « naïves », dans le fuseau local de ``now`` (celui d'ORION : TZ du service).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from jarvis.personality import normalize
from jarvis.scheduling.clock import parse_clock
from jarvis.scheduling.durations import _number, tokens

WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MONTHS = ("janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout", "septembre", "octobre",
          "novembre", "decembre")
DAY_END = (23, 59)
PERIODS = (("apres midi", (15, 0)), ("matin", (9, 0)), ("midi", (12, 0)), ("soir", (19, 0)), ("nuit", (22, 0)))
UNIT_SECONDS = {"seconde": 1, "secondes": 1, "minute": 60, "minutes": 60, "min": 60, "heure": 3600,
                "heures": 3600, "h": 3600, "jour": 86400, "jours": 86400, "semaine": 604800, "semaines": 604800}
FRACTIONS = (("trois quarts d heure", 2700), ("quart d heure", 900), ("demi heure", 1800), ("demie heure", 1800))


@dataclass(frozen=True)
class When:
    at: datetime
    precise: bool  # heure dite (ou délai) ; sinon seulement le jour
    said: str  # forme reconnue, pour le journal


def _day_end(day: datetime) -> datetime:
    return day.replace(hour=DAY_END[0], minute=DAY_END[1], second=0, microsecond=0)


def _with_clock(day: datetime, norm: str) -> tuple[datetime, bool]:
    clock = parse_clock(norm)
    if clock is not None:
        return day.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0), True
    for word, (hour, minute) in PERIODS:
        if re.search(rf"\b{word}\b", norm):
            return day.replace(hour=hour, minute=minute, second=0, microsecond=0), False
    return _day_end(day), False


def delay(text: str) -> tuple[int, str] | None:
    """« dans 20 minutes », « sous 48 heures », « d'ici deux jours », « dans une demi-heure », « dans 1 h 30 »
    -> (secondes, unité dite) ; None sans délai clair."""
    words = tokens(text)
    for i, word in enumerate(words):
        if word not in ("dans", "sous", "ici", "en"):
            continue
        rest = " ".join(w for w in words[i + 1:i + 6] if w not in ("un", "une"))
        for phrase, seconds in FRACTIONS:
            if rest.startswith(phrase):
                return seconds, "minutes"
        j = i + 1
        parsed = _number(words, j) if j < len(words) else None
        if parsed is None:
            continue
        value, used = parsed
        j += used
        if j >= len(words) or words[j] not in UNIT_SECONDS or value <= 0:
            continue
        unit = UNIT_SECONDS[words[j]]
        seconds = value * unit
        if j + 2 < len(words) and words[j + 1] == "et" and words[j + 2] in ("demi", "demie"):
            seconds += unit // 2
        elif unit == 3600 and j + 1 < len(words) and words[j + 1].isdigit() and int(words[j + 1]) < 60:
            seconds += int(words[j + 1]) * 60
        return seconds, words[j]
    return None


def _weekday(norm: str, now: datetime) -> tuple[datetime, str] | None:
    for index, day in enumerate(WEEKDAYS):
        if not re.search(rf"\b{day}\b", norm):
            continue
        ahead = (index - now.weekday()) % 7 or 7  # le jour même : celui de la semaine suivante
        if re.search(rf"\b{day} prochain\b|\bprochain {day}\b", norm) and ahead <= 2:
            ahead += 7  # « vendredi prochain » dit un mercredi ou un jeudi : celui de la semaine suivante
        return now + timedelta(days=ahead), day
    return None


def parse_when(text: str, now: datetime) -> When | None:
    norm = f" {normalize(text)} "
    found = delay(norm)
    if found is not None:
        seconds, unit = found
        at = (now + timedelta(seconds=seconds)).replace(microsecond=0)
        return When(at, True, f"dans {seconds // 60} min")
    if re.search(r"\bfin de (?:la |cette )?semaine\b", norm):
        friday = now + timedelta(days=(4 - now.weekday()) % 7)
        return When(_day_end(friday), False, "fin de la semaine")
    if re.search(r"\bfin (?:du|de ce) mois\b", norm):
        first_next = (now.replace(day=1) + timedelta(days=32)).replace(day=1)
        return When(_day_end(first_next - timedelta(days=1)), False, "fin du mois")
    match = re.search(r"\b(\d{1,2}|1er|premier)\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?\b", norm)
    if match:
        day = 1 if match.group(1) in ("1er", "premier") else int(match.group(1))
        year = int(match.group(3)) if match.group(3) else now.year
        try:
            date = now.replace(year=year, month=MONTHS.index(match.group(2)) + 1, day=day)
        except ValueError:
            return None
        if not match.group(3) and date.date() < now.date() - timedelta(days=60):
            date = date.replace(year=year + 1)  # « le 5 janvier » dit en décembre : l'an prochain
        at, precise = _with_clock(date, norm)
        return When(at, precise, f"{day} {match.group(2)}")
    match = re.search(r"\b(\d{1,2}) (\d{1,2})(?: (\d{4}|\d{2}))?\b", normalize(re.sub(r"(\d)[/.](\d)", r"\1 \2", text)))
    if match and re.search(r"\d[/.]\d", text):
        year = int(match.group(3)) if match.group(3) else now.year
        year += 2000 if year < 100 else 0
        try:
            date = now.replace(year=year, month=int(match.group(2)), day=int(match.group(1)))
        except ValueError:
            return None
        at, precise = _with_clock(date, norm)
        return When(at, precise, f"{date:%d/%m}")
    for pattern, offset, label in ((r"\bapres demain\b", 2, "après-demain"), (r"\bdemain\b", 1, "demain"),
                                   (r"\b(?:aujourd hui|ce soir|ce matin|cet apres midi|cette nuit)\b", 0,
                                    "aujourd'hui")):
        if re.search(pattern, norm):
            at, precise = _with_clock(now + timedelta(days=offset), norm)
            return When(at, precise, label)
    weekday = _weekday(norm, now)
    if weekday is not None:
        at, precise = _with_clock(weekday[0], norm)
        return When(at, precise, weekday[1])
    clock = parse_clock(norm)
    if clock is not None and re.search(r"\b(?:a|vers|avant|pour|des)\s+(?:\d|midi|minuit)", norm) or \
            clock is not None and re.search(r"\b(?:a|vers|avant|pour|des)\s+[a-z]+\s+heures?\b", norm):
        at = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        if at <= now:
            at += timedelta(days=1)
        return When(at, True, f"{clock[0]:02d}:{clock[1]:02d}")
    return None
