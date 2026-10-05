"""Calendrier : événements du jour, de demain, prochains, recherche, créneaux libres, ajout et suppression.

Fournisseurs (interface ``CalendarProvider``, sans dépendance externe) :
- ``LocalCalendar`` : événements créés par la voix, dans data/calendar.json (lecture et écriture) ;
- ``IcsCalendar`` : fichier .ics ou adresse iCal privée (lecture seule). Google Agenda et Outlook fournissent une
  « adresse secrète au format iCal » : la renseigner dans [calendar] ics suffit pour lire leurs événements.
  Répétitions (RRULE quotidienne, hebdomadaire, mensuelle, annuelle, exceptions EXDATE) et fuseaux (TZID) gérés.
``Calendar`` réunit plusieurs fournisseurs ; les ajouts vont au calendrier local.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

from jarvis.memory import keywords
from jarvis.persist import load_json_list
from jarvis.personality import MONTHS, WEEKDAYS, normalize
from jarvis.scheduling.clock import parse_clock, spoken_clock
from jarvis.scheduling.durations import DurationError, duration_in_text, parse_duration
from jarvis.tools.base import INVALID_PARAMETERS, Param, Risk, Tool, ToolError

log = logging.getLogger(__name__)

EVENT_NOT_FOUND = "event_not_found"
CALENDAR_UNAVAILABLE = "calendar_unavailable"
DAYS = {"today": 0, "tomorrow": 1, "day_after_tomorrow": 2}


@dataclass(frozen=True)
class CalendarEvent:
    id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str = ""
    source: str = "local"
    rule: str = ""  # RRULE iCalendar (répétition), développée à la lecture
    exdates: tuple[datetime, ...] = ()

    def as_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "start": self.start.isoformat(), "end": self.end.isoformat(),
                "all_day": self.all_day, "location": self.location, "source": self.source}


class CalendarProvider(Protocol):
    name: str
    writable: bool

    def events(self, start: datetime, end: datetime) -> list[CalendarEvent]: ...


class LocalCalendar:
    name, writable = "local", True

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> list[dict]:
        # Fichier abîmé : mis de côté (le prochain ajout ne l'écrase pas), calendrier local vide.
        return load_json_list(self._path, "Événements du calendrier local")

    def _save(self, items: list[dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self._path)

    @staticmethod
    def _event(item: dict) -> CalendarEvent | None:
        try:
            return CalendarEvent(str(item["id"]), str(item["title"]), datetime.fromisoformat(item["start"]),
                                 datetime.fromisoformat(item["end"]), bool(item.get("all_day")), str(item.get("location", "")))
        except (KeyError, TypeError, ValueError):
            return None

    def events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        with self._lock:
            events = [e for e in map(self._event, self._load()) if e is not None]
        return [e for e in events if e.end > start and e.start < end]

    def add(self, title: str, start: datetime, end: datetime, location: str = "") -> CalendarEvent:
        event = CalendarEvent(uuid.uuid4().hex[:8], title, start, end, False, location)
        with self._lock:
            items = self._load()
            items.append(event.as_dict())
            self._save(items)
        return event

    def remove(self, event_id: str) -> bool:
        with self._lock:
            items = self._load()
            kept = [i for i in items if str(i.get("id")) != event_id]
            if len(kept) == len(items):
                return False
            self._save(kept)
            return True


def _ics_datetime(value: str, tzid: str = "") -> tuple[datetime, bool]:
    """Valeur DTSTART/DTEND -> (date et heure locales, journée entière) : UTC (« Z ») et fuseaux nommés (TZID)
    convertis à l'heure de la machine (Europe/Paris pour le Core)."""
    value = value.strip()
    if re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value, "%Y%m%d"), True
    moment = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return _to_local(moment.replace(tzinfo=timezone.utc)), False
    if tzid:
        try:
            from zoneinfo import ZoneInfo

            return _to_local(moment.replace(tzinfo=ZoneInfo(tzid.strip('"')))), False
        except Exception:  # fuseau inconnu (identifiant Windows...) : heure prise telle quelle
            pass
    return moment, False


def _to_local(moment: datetime) -> datetime:
    return moment.astimezone().replace(tzinfo=None)


def _utc_to_local(moment: datetime) -> datetime:
    return _to_local(moment.replace(tzinfo=timezone.utc))


WEEKDAY_CODES = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
MAX_OCCURRENCES = 2000


def _add_months(moment: datetime, months: int) -> datetime | None:
    month = moment.month - 1 + months
    try:
        return moment.replace(year=moment.year + month // 12, month=month % 12 + 1)
    except ValueError:  # 31 février : ce mois-là est sauté
        return None


def occurrences(event: CalendarEvent, start: datetime, end: datetime) -> list[CalendarEvent]:
    """Occurrences d'un événement répété (RRULE : FREQ DAILY / WEEKLY / MONTHLY / YEARLY, INTERVAL, COUNT, UNTIL,
    BYDAY pour les semaines) qui tombent entre ``start`` et ``end`` ; dates exclues (EXDATE) retirées."""
    if not event.rule:
        return [event] if event.end > start and event.start < end else []
    rule = dict(part.split("=", 1) for part in event.rule.split(";") if "=" in part)
    freq = rule.get("FREQ", "").upper()
    try:
        interval = max(1, int(rule.get("INTERVAL", "1")))
        count = int(rule["COUNT"]) if "COUNT" in rule else None
        until = _ics_datetime(rule["UNTIL"])[0] if "UNTIL" in rule else None
    except ValueError:
        return [event] if event.end > start and event.start < end else []
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return [event] if event.end > start and event.start < end else []
    days = sorted(WEEKDAY_CODES[d[-2:]] for d in rule.get("BYDAY", "").split(",") if d[-2:] in WEEKDAY_CODES) \
        if freq == "WEEKLY" else []
    duration = event.end - event.start
    found, produced, step = [], 0, 0
    while produced < MAX_OCCURRENCES and step < MAX_OCCURRENCES * 7:
        if freq == "DAILY":
            candidates = [event.start + timedelta(days=step * interval)]
        elif freq == "WEEKLY":
            week = event.start + timedelta(weeks=step * interval) - timedelta(days=event.start.weekday())
            candidates = [week + timedelta(days=d) for d in (days or [event.start.weekday()])]
        elif freq == "MONTHLY":
            candidates = [c for c in [_add_months(event.start, step * interval)] if c]
        else:
            candidates = [c for c in [_add_months(event.start, 12 * step * interval)] if c]
        step += 1
        for moment in candidates:
            if moment < event.start:
                continue
            if (until is not None and moment > until) or (count is not None and produced >= count) or moment >= end:
                return found
            produced += 1
            if moment in event.exdates or moment + duration <= start:
                continue
            found.append(replace(event, id=f"{event.id}@{moment:%Y%m%dT%H%M}", start=moment, end=moment + duration,
                                 rule=""))
    return found


def parse_ics(text: str, source: str = "ics") -> list[CalendarEvent]:
    """Événements d'un calendrier iCalendar (VEVENT : SUMMARY, DTSTART, DTEND, LOCATION, UID, RRULE, EXDATE,
    fuseaux TZID). Un événement illisible est ignoré, jamais le calendrier entier."""
    unfolded = re.sub(r"\r?\n[ \t]", "", text)
    events, current = [], None
    for line in unfolded.splitlines():
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT" and current is not None:
            try:
                start_value, start_params = current["DTSTART"]
                start, all_day = _ics_datetime(start_value, start_params.get("TZID", ""))
                if "DTEND" in current:
                    end = _ics_datetime(current["DTEND"][0], current["DTEND"][1].get("TZID", ""))[0]
                else:
                    end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
                title = current.get("SUMMARY", ("Sans titre", {}))[0]
                title = title.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ")
                exdates = []
                for value, params in current.get("EXDATE", []):
                    for item in value.split(","):
                        try:
                            exdates.append(_ics_datetime(item, params.get("TZID", ""))[0])
                        except ValueError:
                            continue
                events.append(CalendarEvent(current.get("UID", (uuid.uuid4().hex[:8], {}))[0][:64], title[:120], start,
                                            max(end, start), all_day,
                                            current.get("LOCATION", ("", {}))[0].replace("\\,", ",")[:120], source,
                                            current.get("RRULE", ("", {}))[0][:200], tuple(exdates)))
            except (KeyError, ValueError, TypeError):
                pass
            current = None
        elif current is not None and ":" in line:
            head, _, value = line.partition(":")
            name, *raw = head.split(";")
            params = dict(p.split("=", 1) for p in raw if "=" in p)
            if name.upper() == "EXDATE":
                current.setdefault("EXDATE", []).append((value, params))
            else:
                current[name.upper()] = (value, params)
    return events


class IcsCalendar:
    """Fichier .ics ou adresse iCal (https) en lecture seule ; l'adresse est relue au plus toutes les 10 minutes."""

    writable = False

    def __init__(self, location: str, timeout: float = 5.0, ttl: float = 600.0):
        self.name = "ics"
        self._location, self._timeout, self._ttl = location, timeout, ttl
        self._cache: tuple[float, list[CalendarEvent]] | None = None

    def _fetch(self) -> list[CalendarEvent]:
        if self._cache and time.monotonic() - self._cache[0] < self._ttl:
            return self._cache[1]
        if self._location.startswith(("http://", "https://")):
            with urllib.request.urlopen(self._location, timeout=self._timeout) as response:
                text = response.read(5_000_000).decode("utf-8", "replace")
        else:
            text = Path(self._location).expanduser().read_text(encoding="utf-8", errors="replace")
        events = parse_ics(text)
        self._cache = (time.monotonic(), events)
        return events

    def events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        return [o for e in self._fetch() for o in occurrences(e, start, end)]


class Calendar:
    def __init__(self, providers: list, clock: Callable[[], datetime] = datetime.now, day_start: str = "08:00",
                 day_end: str = "20:00"):
        self._providers = providers
        self._clock = clock
        self.day_start, self.day_end = day_start, day_end

    @property
    def local(self) -> LocalCalendar | None:
        return next((p for p in self._providers if getattr(p, "writable", False)), None)

    def now(self) -> datetime:
        return self._clock()

    def events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        found, failed = [], []
        for provider in self._providers:
            try:
                found += provider.events(start, end)
            except Exception as exc:
                log.warning("Calendrier %s indisponible : %s", getattr(provider, "name", "?"), type(exc).__name__)
                failed.append(provider)
        if failed and len(failed) == len(self._providers):
            raise ToolError(CALENDAR_UNAVAILABLE, "Je n'arrive pas à lire votre calendrier pour le moment.")
        return sorted(found, key=lambda e: (e.start, e.title))

    def day(self, offset: int) -> tuple[date, list[CalendarEvent]]:
        day = (self._clock() + timedelta(days=offset)).date()
        start = datetime.combine(day, datetime.min.time())
        return day, self.events(start, start + timedelta(days=1))

    def today_lines(self) -> list[str]:
        """Événements restants aujourd'hui, pour l'annonce de la journée."""
        now = self._clock()
        _, events = self.day(0)
        return [f"« {e.title} »" + ("" if e.all_day else f" à {spoken_clock(e.start.hour, e.start.minute)}")
                for e in events if e.all_day or e.start >= now]


def _spoken_day(day: date, today: date) -> str:
    if day == today:
        return "aujourd'hui"
    if day == today + timedelta(days=1):
        return "demain"
    return f"{WEEKDAYS[day.weekday()]} {day.day} {MONTHS[day.month - 1]}"


def _said_event(e: CalendarEvent, today: date, with_day: bool = False) -> str:
    when = "toute la journée" if e.all_day else f"à {spoken_clock(e.start.hour, e.start.minute)}"
    day = f"{_spoken_day(e.start.date(), today)} " if with_day else ""
    return f"{day}{when}, {e.title}" + (f" ({e.location})" if e.location else "")


def said_day(text: str) -> str | None:
    norm = f" {normalize(text)} "
    if " apres demain " in norm:
        return "day_after_tomorrow"
    if " demain " in norm:
        return "tomorrow"
    return None


def calendar_tools(calendar: Calendar) -> list[Tool]:
    day_param = Param(str, "jour", required=False, choices=tuple(DAYS), hidden=True, resolve=said_day)

    def list_events(day: str | None = None) -> dict:
        today = calendar.now().date()
        when, events = calendar.day(DAYS.get(day or "today", 0))
        return {"day": _spoken_day(when, today), "count": len(events),
                "events": [_said_event(e, today) for e in events]}

    def next_events(count: int | None = None) -> dict:
        now = calendar.now()
        events = [e for e in calendar.events(now, now + timedelta(days=30)) if e.end > now][:count or 3]
        return {"count": len(events), "events": [_said_event(e, now.date(), with_day=True) for e in events]}

    def search_events(query: str) -> dict:
        now = calendar.now()
        wanted = keywords(query)
        events = [e for e in calendar.events(now - timedelta(days=1), now + timedelta(days=90))
                  if wanted and wanted & keywords(f"{e.title} {e.location}")]
        return {"query": query, "count": len(events), "events": [_said_event(e, now.date(), True) for e in events[:5]]}

    def free_slots(day: str | None = None, duration: int | None = None) -> dict:
        minutes = (duration or 1800) / 60
        now = calendar.now()
        when, events = calendar.day(DAYS.get(day or "today", 0))
        start = datetime.combine(when, datetime.strptime(calendar.day_start, "%H:%M").time())
        end = datetime.combine(when, datetime.strptime(calendar.day_end, "%H:%M").time())
        cursor = max(start, now.replace(second=0, microsecond=0)) if when == now.date() else start
        slots = []
        for e in [e for e in events if not e.all_day] + [CalendarEvent("fin", "", end, end)]:
            if (e.start - cursor).total_seconds() / 60 >= minutes:
                slots.append(f"de {spoken_clock(cursor.hour, cursor.minute)} à {spoken_clock(e.start.hour, e.start.minute)}")
            cursor = max(cursor, e.end)
        return {"day": _spoken_day(when, now.date()), "slots": slots, "count": len(slots)}

    def add_event(title: str, time: str, day: str | None = None, duration: int | None = None) -> dict:
        local = calendar.local
        if local is None:
            raise ToolError(CALENDAR_UNAVAILABLE, "Aucun calendrier modifiable n'est configuré.")
        hour, minute = parse_clock(time)
        now = calendar.now()
        start = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=DAYS.get(day or "today", 0))
        if day is None and start <= now:
            start += timedelta(days=1)
        event = local.add(title.strip(" ."), start, start + timedelta(seconds=duration or 3600))
        return {"title": event.title, "when": _said_event(event, now.date(), True)}

    def delete_event(title: str) -> dict:
        local = calendar.local
        now = calendar.now()
        wanted = keywords(title)
        candidates = [e for e in (local.events(now - timedelta(days=1), now + timedelta(days=365)) if local else [])
                      if wanted & keywords(e.title)]
        if not candidates:
            raise ToolError(EVENT_NOT_FOUND, f"Je ne trouve pas d'événement « {title[:40]} » dans mon calendrier.")
        if len(candidates) > 1:
            listing = " ; ".join(_said_event(e, now.date(), True) for e in candidates[:3])
            raise ToolError("ambiguous_target", f"Plusieurs événements correspondent : {listing}. Lequel ?")
        local.remove(candidates[0].id)
        return {"title": candidates[0].title, "when": _said_event(candidates[0], now.date(), True)}

    def events_said(r: dict) -> str:
        if not r["events"]:
            return f"Rien de prévu {r['day']}."
        lead = f"{r['day'][:1].upper()}{r['day'][1:]}, vous avez"
        return f"{lead} {r['count']} événement{'s' if r['count'] > 1 else ''} : " + " ; ".join(r["events"]) + "."

    def next_said(r: dict) -> str:
        if not r["events"]:
            return "Aucun événement prévu dans les trente prochains jours."
        return "À venir : " + " ; ".join(r["events"]) + "."

    def search_said(r: dict) -> str:
        if not r["events"]:
            return f"Je ne trouve rien concernant « {r['query']} » dans votre calendrier."
        return " ; ".join(r["events"]) + "."

    def slots_said(r: dict) -> str:
        if not r["slots"]:
            return f"Aucun créneau libre {r['day']}."
        return f"{r['day'][:1].upper()}{r['day'][1:]}, vous êtes libre " + ", puis ".join(r["slots"]) + "."

    def duration_check(value: str) -> int:
        try:
            return parse_duration(value)
        except DurationError as exc:
            raise ToolError(INVALID_PARAMETERS, f"Je n'ai pas compris la durée « {value[:30]} ».") from exc

    def clock_check(value: str) -> str:
        if parse_clock(value) is None:
            raise ToolError(INVALID_PARAMETERS, f"Je n'ai pas compris l'heure « {value[:30]} ».")
        return value

    def clock_said(value: str, text: str) -> bool:
        return parse_clock(value) is not None and parse_clock(value) == parse_clock(text)

    duration = Param(str, "durée telle qu'elle a été dite, si elle l'a été", required=False, max_length=40,
                     check=duration_check, evidence=duration_in_text)
    return [
        Tool("list_events", "Donne les événements du calendrier d'aujourd'hui (ou de demain, d'après-demain).",
             {"day": day_param}, {"events": "événements du jour"}, Risk.SAFE, list_events, say=events_said),
        Tool("next_events", "Donne les prochains événements du calendrier.",
             {"count": Param(int, "nombre d'événements, s'il est dit", required=False, minimum=1, maximum=10)},
             {"events": "événements à venir"}, Risk.SAFE, next_events, say=next_said),
        Tool("search_events", "Cherche un événement dans le calendrier (rendez-vous chez le dentiste...).",
             {"query": Param(str, "mots de l'événement cherché", max_length=80)},
             {"events": "événements trouvés"}, Risk.SAFE, search_events, say=search_said),
        Tool("free_slots", "Donne les créneaux libres d'une journée.",
             {"day": day_param, "duration": duration}, {"slots": "créneaux libres"}, Risk.SAFE, free_slots,
             say=slots_said),
        Tool("add_event", "Ajoute un événement au calendrier à une heure précise.",
             {"title": Param(str, "intitulé de l'événement", max_length=120),
              "time": Param(str, "heure telle qu'elle a été dite", max_length=40, check=clock_check, evidence=clock_said),
              "day": day_param, "duration": duration},
             {"title": "événement", "when": "date"}, Risk.SAFE, add_event,
             say=lambda r: f"C'est noté dans votre calendrier : {r['when']}."),
        Tool("delete_event", "Supprime un événement ajouté au calendrier par JARVIS.",
             {"title": Param(str, "intitulé de l'événement à supprimer", max_length=120)},
             {"title": "événement"}, Risk.CONFIRMATION_REQUIRED, delete_event,
             question=lambda p: f"Voulez-vous vraiment supprimer l'événement « {p['title']} » ?",
             say=lambda r: f"L'événement {r['when']} est supprimé."),
    ]
