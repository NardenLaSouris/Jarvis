"""Moteur des routines : déclenchement (heure, intervalle, à la demande) et exécution pas à pas.

Les outils passent par le ToolCore (registre, validation, permissions) ; une phrase passe par les
notifications vocales (ORION la dit dès qu'il est libre). Chaque étape publie un événement : l'historique
est le journal d'activité. Une étape en échec arrête la routine.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Callable

from jarvis.events import Event, EventBus
from jarvis.routines.events import ROUTINE_FINISHED, ROUTINE_STARTED, ROUTINE_STEP
from jarvis.routines.model import EVENT_TRIGGERS, Routine, RoutineError, parse_routine
from jarvis.scheduling.clock import spoken_clock
from jarvis.tools import DONE, ToolRegistry

log = logging.getLogger(__name__)

MAX_LATE = 600  # une action à date précise manquée de plus de 10 minutes (ORION arrêté) n'est plus exécutée
WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
ANNOUNCE_LABELS = {"time": "l'heure", "date": "la date", "weather": "la météo", "day": "la journée",
                   "welcome": "le retour (bon retour et résumé de l'absence)"}


def describe(action: dict) -> str:
    """Étape lisible dans l'historique : « light_on (room=chambre, brightness=30) », « dire : ... »."""
    if action["type"] == "say":
        return f"dire : {action['text']}"
    if action["type"] == "wait":
        return f"attendre {action['seconds']} s"
    if action["type"] == "alarm":
        return "sonnerie du réveil"
    if action["type"] == "announce":
        return f"annoncer {ANNOUNCE_LABELS[action['what']]}"
    params = ", ".join(f"{k}={v}" for k, v in action["parameters"].items())
    return f"{action['tool']} ({params})" if params else action["tool"]


def next_time(trigger: dict, now: datetime) -> datetime | None:
    if trigger["type"] == "at":
        when = datetime.fromisoformat(trigger["at"])
        return when if when > now else None
    if trigger["type"] != "time":
        return None
    hour, minute = map(int, trigger["time"].split(":"))
    for offset in range(8):
        day = now + timedelta(days=offset)
        candidate = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > now and (not trigger["days"] or candidate.weekday() in trigger["days"]):
            return candidate
    return None


class RoutineEngine:
    def __init__(self, store, registry: ToolRegistry, run_tool: Callable[[dict], object],
                 say: Callable[[str, str], None], events: EventBus | None = None,
                 clock: Callable[[], datetime] = datetime.now, tick: float = 1.0,
                 sleep: Callable[[float], None] | None = None, alarm=None,
                 announce: Callable[[str], str] | None = None):
        self._store = store
        self._registry = registry
        self._run_tool = run_tool
        self._say = say
        self._events = events
        self._clock = clock
        self._tick = tick
        self.alarm = alarm
        self._announce = announce
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._sleep = sleep or (lambda seconds: self._stopping.wait(seconds))
        self._routines: dict[str, Routine] = {}
        self._invalid: list[dict] = []
        self._state: dict[str, dict] = {}
        self._fired: set[tuple[str, str]] = set()
        self._interval_from: dict[str, float] = {}
        self._thread: threading.Thread | None = None
        self._load()

    def _load(self) -> None:
        for raw in self._store.load():
            try:
                routine = parse_routine(raw, self._registry, str(raw.get("id") or "") or None)
            except RoutineError as exc:
                log.warning("Routine « %s » ignorée : %s", raw.get("name"), exc)
                self._invalid.append(raw)
                continue
            self._routines[routine.id] = routine
            self._interval_from[routine.id] = time.monotonic()

    def _save(self) -> None:
        self._store.save([r.as_dict() for r in self._routines.values()] + self._invalid)

    # --- Gestion -------------------------------------------------------------------------------------

    def view(self, routine: Routine) -> dict:
        state = self._state.get(routine.id, {})
        upcoming = next_time(routine.trigger, self._clock()) if routine.enabled else None
        return {**routine.as_dict(), "running": bool(state.get("running")), "last_run": state.get("last_run"),
                "last_status": state.get("last_status"), "next_run": upcoming.isoformat() if upcoming else None}

    def list(self) -> list[dict]:
        with self._lock:
            return [self.view(r) for r in self._routines.values()]

    def get(self, routine_id: str) -> dict:
        with self._lock:
            return self.view(self._find(routine_id))

    def _future(self, routine: Routine) -> Routine:
        """Une date précise doit être à venir (une routine créée ou modifiée pour le passé ne s'exécuterait jamais)."""
        if routine.trigger["type"] == "at" and datetime.fromisoformat(routine.trigger["at"]) <= self._clock():
            raise RoutineError("Déclencheur : la date est déjà passée.")
        return routine

    def create(self, data: dict) -> dict:
        routine = self._future(parse_routine({k: v for k, v in data.items() if k != "id"}, self._registry))
        with self._lock:
            self._routines[routine.id] = routine
            self._interval_from[routine.id] = time.monotonic()
            self._save()
            return self.view(routine)

    def update(self, routine_id: str, data: dict) -> dict:
        with self._lock:
            self._find(routine_id)
            routine = self._future(parse_routine({k: v for k, v in data.items() if k != "id"}, self._registry,
                                                 routine_id))
            self._routines[routine_id] = routine
            self._interval_from[routine_id] = time.monotonic()
            self._save()
            return self.view(routine)

    def set_enabled(self, routine_id: str, enabled: bool) -> dict:
        with self._lock:
            routine = self._find(routine_id)
            return self.update(routine_id, {**routine.as_dict(), "enabled": enabled})

    def duplicate(self, routine_id: str) -> dict:
        with self._lock:
            source = self._find(routine_id).as_dict()
            return self.create({**source, "name": f"{source['name'][:52]} (copie)", "enabled": False})

    def delete(self, routine_id: str) -> None:
        with self._lock:
            self._find(routine_id)
            del self._routines[routine_id]
            self._state.pop(routine_id, None)
            self._save()

    def clock(self) -> datetime:
        return self._clock()

    def routines(self) -> list[Routine]:
        with self._lock:
            return list(self._routines.values())

    def today(self, now: datetime | None = None, routines: bool = True) -> list[str]:
        """Réveils et routines encore prévus aujourd'hui : « réveil à 7 h 30 », « routine « Soir » à 21 heures ».
        ``routines=False`` : les réveils seulement (le programme de la journée ne cite pas les automatismes)."""
        now = now or self._clock()
        items = []
        for routine in self.routines():
            if not routines and not routine.is_alarm:
                continue
            upcoming = next_time(routine.trigger, now) if routine.enabled else None
            if upcoming is not None and upcoming.date() == now.date():
                label = "réveil" if routine.is_alarm else f"routine « {routine.name} »"
                items.append((upcoming, f"{label} à {spoken_clock(upcoming.hour, upcoming.minute)}"))
        return [text for _, text in sorted(items)]

    def _find(self, routine_id: str) -> Routine:
        routine = self._routines.get(routine_id)
        if routine is None:
            raise KeyError(routine_id)
        return routine

    # --- Exécution -----------------------------------------------------------------------------------

    def run(self, routine_id: str, source: str = "manual", wait: bool = False,
            context: dict | None = None) -> bool:
        """Lance la routine en arrière-plan ; False si elle est déjà en cours. ``context`` : données de
        l'événement déclencheur (arrivée : utilisateur, début de l'absence), pour l'annonce « welcome »."""
        with self._lock:
            routine = self._find(routine_id)
            state = self._state.setdefault(routine_id, {})
            if state.get("running"):
                return False
            state["running"] = True
        thread = threading.Thread(target=self._execute, args=(routine, source, context or {}),
                                  name=f"routine-{routine_id}", daemon=True)
        thread.start()
        if wait:
            thread.join()
        return True

    def _execute(self, routine: Routine, source: str, context: dict | None = None) -> None:
        base = {"routine_id": routine.id, "name": routine.name, "source": source}
        self._publish(ROUTINE_STARTED, {**base, "subject": f"routine « {routine.name} »"})
        success, error = True, None
        for index, action in enumerate(routine.actions, 1):
            ok, message = self._step(routine, action, context or {})
            self._publish(ROUTINE_STEP, {**base, "step": index, "action": describe(action), "success": ok,
                                         "message": message, "subject": f"{routine.name} : {describe(action)}"})
            if not ok:
                success, error = False, message
                break
        with self._lock:
            self._state[routine.id] = {"running": False, "last_run": self._clock().isoformat(timespec="seconds"),
                                       "last_status": "success" if success else "error"}
            if routine.once and source != "manual" and routine.id in self._routines:
                del self._routines[routine.id]
                self._save()
        self._publish(ROUTINE_FINISHED, {**base, "success": success, "error": error,
                                         "subject": f"routine « {routine.name} » {'réussie' if success else 'en échec'}"})

    def _step(self, routine: Routine, action: dict, context: dict | None = None) -> tuple[bool, str]:
        try:
            if action["type"] == "wait":
                self._sleep(action["seconds"])
                return True, ""
            if action["type"] == "say":
                self._say(routine.name, action["text"])
                return True, ""
            if action["type"] == "alarm":
                if self.alarm is None:
                    return False, "Sonnerie indisponible."
                return True, "arrêtée" if self.alarm.ring() else "arrêtée au bout de la durée maximale"
            if action["type"] == "announce":
                if self._announce is None:
                    return False, "Annonce indisponible."
                what = action["what"]
                text = self._announce(what, context or {}) if what == "welcome" else self._announce(what)
                if text:
                    self._say(routine.name, text)
                return bool(text), text or "Rien à annoncer."
            outcome = self._run_tool({"type": "tool_call", "tool": action["tool"], "parameters": action["parameters"]})
            result = getattr(outcome, "result", None)
            if getattr(outcome, "status", None) == DONE and result is not None and result.success:
                return True, result.message
            return False, (result.message if result is not None else "") or "L'action a échoué."
        except Exception:
            log.exception("Routine « %s » : étape en erreur", routine.name)
            return False, "L'action a échoué."

    def _publish(self, event_type: str, payload: dict) -> None:
        if self._events is not None:
            self._events.publish(Event(event_type, "routines", payload))

    # --- Déclenchement -------------------------------------------------------------------------------

    def due(self, now: datetime, monotonic: float) -> list[tuple[str, str]]:
        """(routine, déclencheur) à lancer maintenant ; chaque minute horaire ne déclenche qu'une fois."""
        due, expired = [], []
        with self._lock:
            for routine in self._routines.values():
                trigger = routine.trigger
                if not routine.enabled:
                    continue
                if trigger["type"] == "at":
                    key, when = (routine.id, trigger["at"]), datetime.fromisoformat(trigger["at"])
                    if now >= when and key not in self._fired:
                        self._fired.add(key)
                        if (now - when).total_seconds() > MAX_LATE:
                            expired.append(routine)
                        else:
                            due.append((routine.id, "time"))
                elif trigger["type"] == "time":
                    key = (routine.id, now.strftime("%Y-%m-%d %H:%M"))
                    if (now.strftime("%H:%M") == trigger["time"] and key not in self._fired
                            and (not trigger["days"] or now.weekday() in trigger["days"])):
                        self._fired.add(key)
                        due.append((routine.id, "time"))
                elif trigger["type"] == "interval":
                    started = self._interval_from.setdefault(routine.id, monotonic)
                    if monotonic - started >= trigger["minutes"] * 60:
                        self._interval_from[routine.id] = monotonic
                        due.append((routine.id, "interval"))
            self._fired = {key for key in self._fired if key[1][:10] >= now.strftime("%Y-%m-%d")}
            for routine in expired:  # ORION était arrêté à l'heure prévue : action trop ancienne, abandonnée
                log.warning("Routine « %s » prévue le %s non exécutée (trop tard) : supprimée", routine.name,
                            routine.trigger["at"])
                del self._routines[routine.id]
            if expired:
                self._save()
        return due

    # --- Déclenchement par un événement de la maison --------------------------------------------------

    def attach_events(self, bus: EventBus) -> None:
        """Routines « event » : lancées par un retour ou un départ confirmés (moteur de présence)."""
        for event_type in EVENT_TRIGGERS.values():
            bus.subscribe(event_type, self.on_event)

    def on_event(self, event: Event) -> None:
        if event.source != "presence":  # seul le moteur de présence décide d'un retour ou d'un départ
            log.warning("Routines : %s de source %r ignoré", event.type, event.source)
            return
        with self._lock:
            matching = [r for r in self._routines.values() if r.enabled and r.trigger["type"] == "event"
                        and EVENT_TRIGGERS.get(r.trigger["event"]) == event.type
                        and r.trigger.get("user", event.payload.get("user")) == event.payload.get("user")]
        for routine in matching:
            if not self.run(routine.id, source=routine.trigger["event"], context=dict(event.payload)):
                log.info("Routine %s déjà en cours : %s ignoré", routine.id, event.type)

    def start(self) -> None:
        if self._thread is None:
            self._stopping.clear()
            self._thread = threading.Thread(target=self._loop, name="routines", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _loop(self) -> None:
        while not self._stopping.wait(self._tick):
            for routine_id, source in self.due(self._clock(), time.monotonic()):
                if not self.run(routine_id, source):
                    log.info("Routine %s déjà en cours : déclenchement %s ignoré", routine_id, source)
