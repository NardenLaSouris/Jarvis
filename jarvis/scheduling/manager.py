"""Gestion des minuteurs et des rappels : création, annulation, liste, expiration.

Le gestionnaire ne connaît ni le LLM, ni la voix : il publie timer.* / reminder.* sur le bus
d'événements, et d'autres composants (notifications, journal, visage) réagissent. Une annulation
et une expiration simultanées sont arbitrées sous verrou : un seul des deux événements est publié.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol

from jarvis.events import Event, EventBus
from jarvis.persist import load_json_list
from jarvis.scheduling.durations import spoken_duration
from jarvis.scheduling.models import Reminder, Scheduled, Status, Timer
from jarvis.scheduling.scheduler import Scheduler

TIMER_CREATED, TIMER_CANCELLED, TIMER_FINISHED = "timer.created", "timer.cancelled", "timer.finished"
REMINDER_CREATED, REMINDER_CANCELLED, REMINDER_FINISHED = "reminder.created", "reminder.cancelled", "reminder.finished"
REMINDER_MOVED = "reminder.moved"  # reporté par l'utilisateur, ou échéance d'origine changée
EVENT_TYPES = (TIMER_CREATED, TIMER_CANCELLED, TIMER_FINISHED, REMINDER_CREATED, REMINDER_CANCELLED, REMINDER_FINISHED,
               REMINDER_MOVED)
MAX_LINKED = 60  # rappels créés pour des objets suivis (mails), en plus des échéances dites
MAX_LINKED_SECONDS = 120 * 86400

INVALID_DURATION = "invalid_duration"
INVALID_MESSAGE = "invalid_message"
LIMIT_REACHED = "limit_reached"
TIMER_NOT_FOUND = "timer_not_found"
REMINDER_NOT_FOUND = "reminder_not_found"
NOT_ACTIVE = "not_active"
MAX_MESSAGE = 200
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class SchedulingError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


log = logging.getLogger(__name__)


class ScheduleStore(Protocol):
    """Rangement des éléments planifiés (en mémoire, ou dans un fichier JSON pour survivre aux redémarrages)."""

    def add(self, item: Scheduled) -> None: ...

    def get(self, kind: str, item_id: str) -> Scheduled | None: ...

    def items(self, kind: str) -> list[Scheduled]: ...

    def changed(self) -> None: ...


class MemoryScheduleStore:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], Scheduled] = {}

    def add(self, item: Scheduled) -> None:
        self._items[(item.kind, item.id)] = item
        self.changed()

    def get(self, kind: str, item_id: str) -> Scheduled | None:
        return self._items.get((kind, item_id))

    def items(self, kind: str) -> list[Scheduled]:
        return [item for (k, _), item in self._items.items() if k == kind]

    def changed(self) -> None:
        pass


class JsonScheduleStore(MemoryScheduleStore):
    """Minuteurs et rappels en cours enregistrés dans un fichier JSON (écriture atomique) : ils survivent à un
    redémarrage d'ORION. Seules les échéances actives sont gardées."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = Path(path)
        raw = load_json_list(self._path, "Échéances")  # fichier abîmé : mis de côté, jamais écrasé
        for entry in raw:
            try:
                cls = Reminder if entry["kind"] == Reminder.kind else Timer
                fields = {"message": entry.get("message", ""), "key": str(entry.get("key", "")),
                          "important": bool(entry.get("important", False)),
                          "link": entry.get("link") if isinstance(entry.get("link"), dict) else {},
                          "adjusted": bool(entry.get("adjusted", False))} if cls is Reminder else {}
                item = cls(str(entry["id"]), datetime.fromisoformat(entry["created_at"]),
                           datetime.fromisoformat(entry["expires_at"]), float(entry["seconds"]), **fields)
            except (KeyError, TypeError, ValueError):
                continue
            self._items[(item.kind, item.id)] = item

    def changed(self) -> None:
        active = [{"kind": i.kind, "id": i.id, "created_at": i.created_at.isoformat(), "expires_at": i.expires_at.isoformat(),
                   "seconds": i.seconds, **({"message": i.message, "key": i.key, "important": i.important,
                                             "link": i.link, "adjusted": i.adjusted}
                                            if isinstance(i, Reminder) else {})}
                  for i in self._items.values() if i.status is Status.ACTIVE]
        self._items = {key: item for key, item in self._items.items() if item.status is Status.ACTIVE}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(active, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self._path)


def _timer_payload(timer: Timer) -> dict:
    return {"timer_id": timer.id, "duration_seconds": round(timer.seconds),
            "expires_at": timer.expires_at.isoformat(timespec="seconds"),
            "subject": f"minuteur {timer.id} ({spoken_duration(timer.seconds)})"}


def _reminder_payload(reminder: Reminder) -> dict:
    return {"reminder_id": reminder.id, "message": reminder.message, "delay_seconds": round(reminder.seconds),
            "expires_at": reminder.expires_at.isoformat(timespec="seconds"),
            "subject": f"rappel {reminder.id} : {reminder.message}", "important": reminder.important,
            "key": reminder.key, "link": dict(reminder.link)}


class TimerManager:
    def __init__(self, events: EventBus | None = None, max_seconds: float = 86400, max_active: int = 20,
                 store: ScheduleStore | None = None, clock: Callable[[], datetime] = datetime.now,
                 monotonic: Callable[[], float] = time.monotonic, max_reminder_seconds: float | None = None):
        self._events = events
        self._max_seconds = max_seconds
        # Rappels : horizon propre (« demain à 9 h » dit à 3 h du matin dépasse 24 h).
        self._max_reminder_seconds = max(max_seconds, max_reminder_seconds or max_seconds)
        self._max_active = max_active
        self._store = store or MemoryScheduleStore()
        self._clock = clock
        self._lock = threading.Lock()
        self._numbers = {Timer.kind: itertools.count(1), Reminder.kind: itertools.count(1)}
        self._scheduler = Scheduler(self._expire, monotonic)
        self.last_finished: Reminder | None = None  # dernier rappel annoncé (« reporte-le » juste après)

    @property
    def max_seconds(self) -> float:
        return self._max_seconds

    @property
    def max_reminder_seconds(self) -> float:
        return self._max_reminder_seconds

    def start(self) -> None:
        self._restore()
        self._scheduler.start()

    def _restore(self) -> None:
        """Échéances retrouvées dans le rangement (redémarrage) : replanifiées ; celles passées pendant l'arrêt sont
        annoncées aussitôt pour un rappel (« late »), oubliées pour un minuteur (il a sonné dans le vide)."""
        now = self._clock()
        with self._lock:
            restored = [i for kind in self._numbers for i in self._store.items(kind) if i.status is Status.ACTIVE]
            for kind in list(self._numbers):
                ids = [int(i.id) for i in restored if i.kind == kind and i.id.isdigit()]
                self._numbers[kind] = itertools.count(max(ids, default=0) + 1)
        late = []
        for item in restored:
            remaining = (item.expires_at - now).total_seconds()
            if remaining > 0:
                self._scheduler.schedule(f"{item.kind}:{item.id}", remaining)
            elif isinstance(item, Reminder):
                late.append(item)
            else:
                item.status = Status.COMPLETED
        for reminder in late:
            reminder.status = Status.COMPLETED
            self._publish(REMINDER_FINISHED, {**_reminder_payload(reminder), "late": True})
        if restored:
            log.info("Échéances retrouvées : %d (dont %d rappel(s) en retard)", len(restored), len(late))
            self._store.changed()

    def stop(self) -> None:
        self._scheduler.stop()

    @property
    def running(self) -> bool:
        return self._scheduler.running

    def create_timer(self, seconds: float) -> Timer:
        timer = self._create(Timer, seconds)
        self._publish(TIMER_CREATED, _timer_payload(timer))
        return timer

    def create_reminder(self, seconds: float, message: str) -> Reminder:
        message = self._clean_message(message)
        reminder = self._create(Reminder, seconds, message=message)
        self._publish(REMINDER_CREATED, _reminder_payload(reminder))
        return reminder

    def create_reminder_at(self, when: datetime, message: str, key: str, important: bool = False,
                           link: dict | None = None) -> Reminder:
        """Rappel à une date précise pour un objet suivi (échéance d'un mail) : un seul par ``key`` (rappel actif
        existant rendu tel quel), horizon et nombre propres, hors limite des échéances dites."""
        message = self._clean_message(message)
        if not key or len(key) > 200:
            raise SchedulingError(INVALID_MESSAGE, "Clé de rappel invalide.")
        with self._lock:
            existing = self._keyed_unlocked(key)
            if existing is not None:
                return existing
            now = self._clock()
            seconds = (when - now).total_seconds()
            if not 0 < seconds <= MAX_LINKED_SECONDS:
                raise SchedulingError(INVALID_DURATION, "Cette date de rappel n'est pas valable.")
            if sum(1 for i in self._active_unlocked(Reminder.kind) if i.key) >= MAX_LINKED:
                raise SchedulingError(LIMIT_REACHED, "Trop de rappels de suivi sont déjà programmés.")
            item = Reminder(str(next(self._numbers[Reminder.kind])), now, when.replace(microsecond=0), seconds,
                            message=message, key=key, important=important, link=dict(link or {}))
            self._store.add(item)
            self._scheduler.schedule(f"{Reminder.kind}:{item.id}", seconds)
        self._publish(REMINDER_CREATED, _reminder_payload(item))
        return item

    def postpone(self, reminder: Reminder, when: datetime) -> Reminder:
        """Report demandé par l'utilisateur : rappel actif déplacé, ou rappel qui vient de sonner reprogrammé (même
        clé : jamais de doublon), marqué comme ajusté."""
        if reminder.status is Status.ACTIVE:
            return self.move_reminder(reminder.id, when, by_user=True)
        existing = self.keyed(reminder.key or f"report:{reminder.id}")
        if existing is not None:  # déjà reprogrammé une fois : on le déplace
            return self.move_reminder(existing.id, when, by_user=True)
        again = self.create_reminder_at(when, reminder.message, reminder.key or f"report:{reminder.id}",
                                        reminder.important, reminder.link)
        with self._lock:
            again.adjusted = True
            self._store.changed()
        return again

    def keyed(self, key: str) -> Reminder | None:
        """Rappel actif créé pour ``key``, s'il y en a un."""
        with self._lock:
            return self._keyed_unlocked(key)

    def _keyed_unlocked(self, key: str) -> Reminder | None:
        return next((i for i in self._active_unlocked(Reminder.kind) if i.key == key), None)

    def move_reminder(self, reminder_id: str, when: datetime, by_user: bool = False) -> Reminder:
        """Nouvelle heure d'un rappel actif : report demandé (``by_user``, gardé ensuite) ou échéance d'origine
        changée (ignorée si l'utilisateur l'a déjà reporté lui-même)."""
        with self._lock:
            item = self._store.get(Reminder.kind, str(reminder_id))
            if item is None or item.status is not Status.ACTIVE:
                raise SchedulingError(REMINDER_NOT_FOUND, f"Je ne trouve pas de rappel numéro {str(reminder_id)[:10]}.")
            if item.adjusted and not by_user:
                return item
            seconds = (when - self._clock()).total_seconds()
            if seconds <= 0:
                raise SchedulingError(INVALID_DURATION, "Cette heure est déjà passée.")
            item.expires_at = when.replace(microsecond=0)
            item.adjusted = item.adjusted or by_user
            self._scheduler.cancel(f"{Reminder.kind}:{item.id}")
            self._scheduler.schedule(f"{Reminder.kind}:{item.id}", seconds)
            self._store.changed()
        self._publish(REMINDER_MOVED, {**_reminder_payload(item), "by_user": by_user})
        return item

    def cancel_timer(self, timer_id: str) -> Timer:
        timer = self._cancel(Timer.kind, timer_id, TIMER_NOT_FOUND, "minuteur")
        self._publish(TIMER_CANCELLED, _timer_payload(timer))
        return timer

    def cancel_reminder(self, reminder_id: str) -> Reminder:
        reminder = self._cancel(Reminder.kind, reminder_id, REMINDER_NOT_FOUND, "rappel")
        self._publish(REMINDER_CANCELLED, _reminder_payload(reminder))
        return reminder

    def timers(self) -> list[Timer]:
        return self._active(Timer.kind)

    def reminders(self) -> list[Reminder]:
        return self._active(Reminder.kind)

    def get(self, kind: str, item_id: str) -> Scheduled | None:
        with self._lock:
            return self._store.get(kind, item_id)

    def now(self) -> datetime:
        return self._clock()

    def _create(self, cls: type, seconds: float, **fields) -> Scheduled:
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not seconds > 0:
            raise SchedulingError(INVALID_DURATION, "La durée doit être positive.")
        limit = self._max_reminder_seconds if cls is Reminder else self._max_seconds
        if seconds > limit:
            raise SchedulingError(INVALID_DURATION, f"La durée maximale est de {spoken_duration(limit)}.")
        with self._lock:
            if sum(len(self._active_unlocked(kind)) for kind in self._numbers) >= self._max_active:
                raise SchedulingError(LIMIT_REACHED, f"Je ne peux pas gérer plus de {self._max_active} échéances à la fois.")
            now = self._clock()
            item = cls(str(next(self._numbers[cls.kind])), now, now + timedelta(seconds=seconds), float(seconds), **fields)
            self._store.add(item)
            self._scheduler.schedule(f"{cls.kind}:{item.id}", seconds)
        return item

    def _cancel(self, kind: str, item_id: str, missing: str, label: str) -> Scheduled:
        with self._lock:
            item = self._store.get(kind, str(item_id))
            if item is None:
                raise SchedulingError(missing, f"Je ne trouve pas de {label} numéro {str(item_id)[:10]}.")
            if item.status is not Status.ACTIVE:
                state = "déjà terminé" if item.status is Status.COMPLETED else "déjà annulé"
                raise SchedulingError(NOT_ACTIVE, f"Ce {label} est {state}.")
            item.status = Status.CANCELLED
            self._scheduler.cancel(f"{kind}:{item.id}")
            self._store.changed()
        return item

    def _expire(self, key: str) -> None:
        kind, _, item_id = key.partition(":")
        with self._lock:
            item = self._store.get(kind, item_id)
            if item is None or item.status is not Status.ACTIVE:
                return
            item.status = Status.COMPLETED
            self._store.changed()
        if isinstance(item, Reminder):
            self.last_finished = item
            self._publish(REMINDER_FINISHED, _reminder_payload(item))
        else:
            self._publish(TIMER_FINISHED, _timer_payload(item))

    def _active(self, kind: str) -> list:
        with self._lock:
            return self._active_unlocked(kind)

    def _active_unlocked(self, kind: str) -> list:
        items = [i for i in self._store.items(kind) if i.status is Status.ACTIVE]
        return sorted(items, key=lambda i: i.expires_at)

    @staticmethod
    def _clean_message(message: str) -> str:
        if not isinstance(message, str):
            raise SchedulingError(INVALID_MESSAGE, "Le rappel doit avoir un message.")
        message = " ".join(message.split()).strip(" .")
        if not message or len(message) > MAX_MESSAGE or CONTROL.search(message):
            raise SchedulingError(INVALID_MESSAGE, "Le message du rappel est vide ou trop long.")
        return message

    def _publish(self, event_type: str, payload: dict) -> None:
        if self._events is not None:
            self._events.publish(Event(event_type, "scheduling", payload))

