"""Moteur de présence : qui est à la maison, départs et arrivées confirmés, à partir des événements normalisés des
capteurs (jarvis/presence/sensors.py). Déterministe, sans LLM.

Un état par utilisateur suivi (celui d'un capteur de présence, son téléphone par exemple) :

    HOME ──(signal de départ)──> POSSIBLE_DEPARTURE ──(confirmé)──> AWAY
     ^                                │ (infirmé, délai)                │
     └────────────────────────────────┘                                │
    HOME <──(confirmé)── POSSIBLE_ARRIVAL <──(signal d'arrivée)────────┘

Chaque hypothèse accumule des signaux dans sa fenêtre de temps ; leur somme est la confiance (0-100+) :

    départ  : téléphone parti 50, porte d'entrée ouverte 30, puis refermée 10 ;
              mouvement à l'intérieur après la fermeture (personne d'autre à la maison) -20
    arrivée : téléphone revenu 50, porte d'entrée ouverte 30, refermée 10, mouvement dans l'entrée 20
              (ailleurs 10)

Confirmé à partir de ``threshold`` (90) : une porte seule (40) ou un téléphone seul (50) ne suffisent jamais.
Infirmé : le téléphone revient pendant un départ (porte ouverte pour sortir les poubelles, récupérer un colis), ou
repart pendant une arrivée. Fenêtre écoulée sans confirmation : départ abandonné (retour à HOME), sauf téléphone
parti sans porte, qui ne devient un départ qu'après ``absence_timeout`` sans aucun mouvement dans la maison ;
arrivée non confirmée : HOME en silence si le téléphone est là (aucune routine), sinon AWAY.

Déclaration explicite (« je sors », jarvis/presence/declaration.py, validée par le Core) : AWAY aussitôt, sans
attendre les signaux ni les délais ci-dessus, qui restent inchangés pour tout le reste. Pendant ``declared_grace``
après elle, la porte, le mouvement et le téléphone encore sur le Wi-Fi sont ceux de votre sortie : ni retour, ni
« mouvement pendant l'absence ». Un départ annoncé pour plus tard (« je sors dans une heure ») attend son heure.

Une seule horloge : ``tick()`` (appelé chaque seconde par le fil du moteur, ou directement dans les tests) gère
toutes les échéances. Les décisions sont publiées sur le bus : presence.departure_confirmed,
presence.arrival_confirmed (routine « Bon retour »), presence.changed, presence.motion_while_away, door.left_open.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from jarvis.events import Event, EventBus
from jarvis.persist import load_json_list
from jarvis.presence.sensors import SOURCE_PREFIX

log = logging.getLogger(__name__)

HOME, POSSIBLE_DEPARTURE, AWAY, POSSIBLE_ARRIVAL = "HOME", "POSSIBLE_DEPARTURE", "AWAY", "POSSIBLE_ARRIVAL"
STATES = (HOME, POSSIBLE_DEPARTURE, AWAY, POSSIBLE_ARRIVAL)
PRESENT = (HOME, POSSIBLE_DEPARTURE)

DEPARTURE_CONFIRMED = "presence.departure_confirmed"
ARRIVAL_CONFIRMED = "presence.arrival_confirmed"
ARRIVAL_UNCONFIRMED = "presence.arrival_unconfirmed"
PRESENCE_CHANGED = "presence.changed"
MOTION_WHILE_AWAY = "presence.motion_while_away"
DOOR_LEFT_OPEN = "door.left_open"
OUTPUT_EVENTS = (DEPARTURE_CONFIRMED, ARRIVAL_CONFIRMED, ARRIVAL_UNCONFIRMED, PRESENCE_CHANGED, MOTION_WHILE_AWAY,
                 DOOR_LEFT_OPEN)

DEPARTURE_WEIGHTS = {"phone_away": 50, "door_open": 30, "door_closed": 10, "motion_inside": -20,
                     "declared": 100}  # « je sors » : déclaré par l'utilisateur lui-même
ARRIVAL_WEIGHTS = {"phone_home": 50, "door_open": 30, "door_closed": 10, "entrance_motion": 20, "motion": 10}


@dataclass(frozen=True)
class PresenceSettings:
    departure_window: float = 180.0  # le téléphone quitte le Wi-Fi jusqu'à quelques minutes après la porte
    arrival_window: float = 300.0  # le téléphone revient sur le Wi-Fi avant d'arriver à la porte
    threshold: int = 90
    absence_timeout: float = 1200.0  # téléphone parti sans porte : départ après ce délai sans mouvement (0 : jamais)
    door_left_open: float = 600.0  # porte d'entrée ouverte plus longtemps : événement (0 : jamais)
    motion_report_every: float = 600.0  # « mouvement pendant l'absence » : au plus une fois par pièce et par délai
    # Sans porte d'entrée connectée : retour confirmé par le seul téléphone, revenu depuis ce délai (il rejoint le
    # Wi-Fi un peu avant la porte : l'accueil attend d'être entendu), après un départ confirmé. < 0 : jamais.
    phone_arrival_delay: float = -1.0
    declared_grace: float = 900.0  # après « je sors » : le temps de partir (porte, mouvement, téléphone encore là)


@dataclass
class Hypothesis:
    kind: str  # "departure" | "arrival"
    started: float
    signals: dict[str, float] = field(default_factory=dict)  # signal -> heure

    def score(self) -> int:
        weights = DEPARTURE_WEIGHTS if self.kind == "departure" else ARRIVAL_WEIGHTS
        return sum(weights[s] for s in self.signals)


@dataclass
class UserPresence:
    user: str
    state: str = HOME
    since: float = 0.0
    away_since: float | None = None
    left_at: float | None = None  # départ confirmé : début du résumé d'absence (après la porte du départ)
    phone: str | None = None  # dernier signal du téléphone : "home" | "away"
    phone_at: float = 0.0
    hypothesis: Hypothesis | None = None
    declared_at: float | None = None  # départ déclaré (« je sors »)
    planned_departure: float | None = None  # départ annoncé pour plus tard (« je sors dans une heure »)


class PresenceEngine:
    def __init__(self, users: list[str], bus: EventBus, settings: PresenceSettings = PresenceSettings(),
                 clock: Callable[[], float] = time.time, state_path: Path | None = None, home=None):
        self._bus = bus
        self.settings = settings
        self._clock = clock
        self._path = Path(state_path) if state_path else None
        self._home = home
        self._lock = threading.RLock()
        now = clock()
        self.users: dict[str, UserPresence] = {u: UserPresence(u, since=now) for u in users}
        self._doors: dict[str, dict] = {}  # capteur -> {"open", "at", "entrance", "room", "reported"}
        self._motion_at: float | None = None
        self._motion_reported: dict[str, float] = {}
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._restore()

    # --- Bus -------------------------------------------------------------------------------------------

    def attach(self) -> None:
        for event_type in ("door.open", "door.closed", "presence.home", "presence.away", "motion.detected"):
            self._bus.subscribe(event_type, self.on_event)

    def on_event(self, event: Event) -> None:
        """Seuls les événements normalisés des capteurs (source « sensor:… ») sont pris en compte."""
        if not event.source.startswith(SOURCE_PREFIX):
            log.warning("Présence : événement %s de source %r ignoré", event.type, event.source)
            return
        with self._lock:
            handler = {"door.open": self._door_open, "door.closed": self._door_closed,
                       "presence.home": self._phone_home, "presence.away": self._phone_away,
                       "motion.detected": self._motion}[event.type]
            handler(event.payload, event.timestamp)
            self.tick(event.timestamp)

    # --- Signaux ---------------------------------------------------------------------------------------

    def _door_open(self, p, t: float) -> None:
        door = self._doors.setdefault(p["sensor"], {"open": False})
        if door["open"]:
            return  # ouverture répétée : la première compte
        door.update(open=True, at=t, entrance=bool(p.get("entrance")), room=p.get("room", ""), reported=False)
        if not door["entrance"]:
            return
        for u in self.users.values():
            if u.state in PRESENT:
                self._signal(u, "departure", "door_open", t)
            elif not self._leaving(u, t):  # la porte de la sortie annoncée n'est pas un retour
                self._signal(u, "arrival", "door_open", t)

    def _door_closed(self, p, t: float) -> None:
        door = self._doors.setdefault(p["sensor"], {"open": False})
        if not door["open"]:
            door.update(open=False, at=t)
            return  # fermeture sans ouverture connue (ordre inversé, doublon) : aucun effet
        door.update(open=False, at=t)
        if not door.get("entrance"):
            return
        for u in self.users.values():
            h = u.hypothesis
            if h is not None and "door_open" in h.signals:
                h.signals["door_closed"] = t

    def _phone_home(self, p, t: float) -> None:
        u = self.users.get(p.get("user", ""))
        if u is None:
            return
        if self._leaving(u, t) and u.phone != "away":
            u.phone, u.phone_at = "home", t
            return  # téléphone encore sur le Wi-Fi juste après « je sors » : pas un retour
        u.phone, u.phone_at = "home", t
        if u.state == POSSIBLE_DEPARTURE:
            self._cancel(u, t, "téléphone toujours à la maison")
        elif u.state in (AWAY, POSSIBLE_ARRIVAL):
            self._signal(u, "arrival", "phone_home", t)

    def _phone_away(self, p, t: float) -> None:
        u = self.users.get(p.get("user", ""))
        if u is None:
            return
        u.phone, u.phone_at = "away", t
        if u.state == POSSIBLE_ARRIVAL:
            self._cancel(u, t, "téléphone reparti")
        elif u.state in PRESENT:
            self._signal(u, "departure", "phone_away", t)

    def _motion(self, p, t: float) -> None:
        self._motion_at = t
        entrance = bool(p.get("entrance"))
        for u in self.users.values():
            h = u.hypothesis
            if h is None:
                continue
            if h.kind == "arrival":
                h.signals["entrance_motion" if entrance else "motion"] = t
            elif "door_closed" in h.signals and not self._someone_else_home(u):
                h.signals["motion_inside"] = t  # quelqu'un bouge encore à l'intérieur : pas parti
        if self.users and all(u.state in (AWAY, POSSIBLE_ARRIVAL) for u in self.users.values()) \
                and not any(u.hypothesis for u in self.users.values()) \
                and not any(self._leaving(u, t) for u in self.users.values()):
            room = p.get("room", "")
            if t - self._motion_reported.get(room, -1e18) >= self.settings.motion_report_every:
                self._motion_reported[room] = t
                self._publish(MOTION_WHILE_AWAY, {"room": room, "sensor": p.get("sensor")}, t)

    def _leaving(self, u: UserPresence, t: float) -> bool:
        """Juste après un départ déclaré : la porte, le mouvement et le téléphone sont ceux de la sortie."""
        return u.declared_at is not None and u.state == AWAY and t - u.declared_at < self.settings.declared_grace

    # --- Déclaration explicite (« je sors ») ----------------------------------------------------------------

    def declare_departure(self, user: str, at: float | None = None) -> str:
        """Départ déclaré par l'utilisateur ; ``at`` : plus tard (« dans une heure »), sinon maintenant.
        Rend « away » (absent dès maintenant), « planned », « already » (déjà absent) ou « unknown »."""
        now = self._clock()
        with self._lock:
            u = self.users.get(user)
            if u is None:
                return "unknown"
            if at is not None and at > now + 60:
                u.planned_departure = at
                self._save()
                log.info("Présence %s : départ annoncé dans %.0f min", user, (at - now) / 60)
                return "planned"
            u.planned_departure = None
            if u.state in (AWAY, POSSIBLE_ARRIVAL):
                self._save()
                return "already"
            self._declared(u, now, "départ déclaré")
            return "away"

    def _declared(self, u: UserPresence, now: float, reason: str) -> None:
        u.hypothesis, u.declared_at = None, now
        self._confirm(u, Hypothesis("departure", now, {"declared": now}), now, reason)

    def cancel_declared(self, user: str) -> bool:
        """« Finalement je reste » : le départ annoncé pour plus tard n'aura pas lieu. True s'il y en avait un."""
        with self._lock:
            u = self.users.get(user)
            if u is None or u.planned_departure is None:
                return False
            u.planned_departure = None
            self._save()
            return True

    def planned_departure(self, user: str) -> float | None:
        u = self.users.get(user)
        return u.planned_departure if u is not None else None

    def _someone_else_home(self, user: UserPresence) -> bool:
        return any(u.state == HOME and u is not user for u in self.users.values())

    # --- Hypothèses ------------------------------------------------------------------------------------

    def _signal(self, u: UserPresence, kind: str, signal: str, t: float) -> None:
        h = u.hypothesis
        if h is None or h.kind != kind:
            h = u.hypothesis = Hypothesis(kind, t)
            self._set_state(u, POSSIBLE_DEPARTURE if kind == "departure" else POSSIBLE_ARRIVAL, t)
        h.signals.setdefault(signal, t)

    def _cancel(self, u: UserPresence, t: float, reason: str) -> None:
        kind = u.hypothesis.kind if u.hypothesis else ""
        log.info("Présence %s : %s infirmé (%s)", u.user, "départ" if kind == "departure" else "arrivée", reason)
        u.hypothesis = None
        self._set_state(u, HOME if kind == "departure" else AWAY, t)

    def tick(self, now: float | None = None) -> None:
        """Décisions et échéances (fenêtres, absence prolongée, porte restée ouverte), à l'heure ``now``."""
        now = self._clock() if now is None else now
        with self._lock:
            for u in self.users.values():
                if u.planned_departure is not None and now >= u.planned_departure:
                    u.planned_departure = None
                    if u.state in PRESENT:
                        self._declared(u, now, "départ annoncé")  # « je sors dans une heure » : l'heure est venue
                    else:
                        self._save()
                self._evaluate(u, now)
            self._doors_left_open(now)

    def _evaluate(self, u: UserPresence, now: float) -> None:
        h = u.hypothesis
        if h is None:
            if u.state == POSSIBLE_DEPARTURE:  # état restauré sans hypothèse
                self._set_state(u, HOME, now)
            return
        window = self.settings.departure_window if h.kind == "departure" else self.settings.arrival_window
        if h.score() >= self.settings.threshold:
            self._confirm(u, h, now, "signaux concordants")
            return
        delay = self.settings.phone_arrival_delay
        if h.kind == "arrival" and delay >= 0 and u.phone == "home" and u.away_since is not None                 and "phone_home" in h.signals and now - h.signals["phone_home"] >= delay:
            self._confirm(u, h, now, "téléphone revenu sur le réseau")
            return
        if now - h.started <= window:
            return
        if h.kind == "departure":
            if u.phone == "away" and self.settings.absence_timeout > 0:
                # Téléphone parti sans porte : départ seulement après une longue absence sans aucun mouvement ;
                # avec du mouvement entre-temps, le téléphone a seulement décroché : de nouveau à la maison.
                if now - u.phone_at < self.settings.absence_timeout:
                    return
                if self._motion_at is None or self._motion_at < u.phone_at:
                    self._confirm(u, h, now, "absence prolongée sans mouvement")
                    return
            u.hypothesis = None
            self._set_state(u, HOME, now)
        else:
            u.hypothesis = None
            if u.phone == "home":
                self._set_state(u, HOME, now)
                self._publish(ARRIVAL_UNCONFIRMED, {"user": u.user, "confidence": h.score()}, now)
            else:
                self._set_state(u, AWAY, now)

    def _confirm(self, u: UserPresence, h: Hypothesis, now: float, reason: str) -> None:
        u.hypothesis = None
        signals = sorted(h.signals)
        if h.kind == "departure":
            u.away_since, u.left_at = h.started, now
            if "declared" not in h.signals:
                u.declared_at = None
            self._set_state(u, AWAY, now)
            self._publish(DEPARTURE_CONFIRMED, {"user": u.user, "confidence": h.score(), "signals": signals,
                                                "reason": reason}, now)
        else:
            away_since, left_at = u.away_since, u.left_at
            u.away_since = u.left_at = u.declared_at = None
            self._set_state(u, HOME, now)
            self._publish(ARRIVAL_CONFIRMED, {"user": u.user, "confidence": h.score(), "signals": signals,
                                              "away_since": away_since, "left_at": left_at or away_since,
                                              "arrival_started": h.started,
                                              "away_seconds": round(now - away_since) if away_since else None},
                          now)

    def _doors_left_open(self, now: float) -> None:
        limit = self.settings.door_left_open
        for sensor, door in self._doors.items():
            if limit > 0 and door.get("open") and not door.get("reported") and now - door["at"] >= limit:
                door["reported"] = True
                self._publish(DOOR_LEFT_OPEN, {"sensor": sensor, "room": door.get("room", ""),
                                               "minutes": round((now - door["at"]) / 60)}, now)

    # --- État ------------------------------------------------------------------------------------------

    def _set_state(self, u: UserPresence, state: str, t: float) -> None:
        if state == u.state:
            return
        previous, u.state, u.since = u.state, state, t
        if state == AWAY and u.away_since is None:
            u.away_since = t
        self._publish(PRESENCE_CHANGED, {"user": u.user, "state": state, "previous": previous}, t)
        if self._home is not None:
            self._home.set(f"presence.{u.user}", state, True, "presence")
            self._home.set("presence.house", self.occupancy(), True, "presence")
        if state in (HOME, AWAY):
            self._save()

    def occupancy(self) -> str:
        """« occupied » si un utilisateur suivi est à la maison, « empty » s'ils sont tous partis, « unknown » sans
        utilisateur suivi. Un utilisateur absent n'est jamais « rentré » parce que la maison est occupée."""
        if not self.users:
            return "unknown"
        return "occupied" if any(u.state in PRESENT for u in self.users.values()) else "empty"

    def is_home(self, user: str) -> bool | None:
        u = self.users.get(user)
        return None if u is None else u.state in PRESENT

    def snapshot(self) -> dict:
        with self._lock:
            return {"house": self.occupancy(),
                    "users": {u.user: {"state": u.state, "since": u.since, "away_since": u.away_since,
                                       "phone": u.phone,
                                       "confidence": u.hypothesis.score() if u.hypothesis else None}
                              for u in self.users.values()},
                    "doors": {s: {"open": d.get("open"), "since": d.get("at")} for s, d in self._doors.items()}}

    def _publish(self, event_type: str, payload: dict, t: float) -> None:
        log.info("Présence : %s %s", event_type, payload)
        self._bus.publish(Event(event_type, "presence", payload, timestamp=t))

    # --- Persistance (états stables seulement) ------------------------------------------------------------

    def _save(self) -> None:
        if self._path is None:
            return
        import json
        import os

        records = [{"user": u.user, "state": u.state if u.state in (HOME, AWAY) else
                    (HOME if u.state == POSSIBLE_DEPARTURE else AWAY), "since": u.since,
                    "away_since": u.away_since, "left_at": u.left_at, "declared_at": u.declared_at,
                    "planned_departure": u.planned_departure} for u in self.users.values()]
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(records), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            log.warning("Présence : état non enregistré (%s)", exc)

    def _restore(self) -> None:
        """Au redémarrage : dernier état stable de chacun (une hypothèse en cours n'est pas reprise)."""
        if self._path is None:
            return
        for record in load_json_list(self._path, "États de présence"):
            if not isinstance(record, dict):
                continue
            u = self.users.get(record.get("user"))
            if u is None or record.get("state") not in (HOME, AWAY):
                continue
            u.state = record["state"]
            since, away, left = record.get("since"), record.get("away_since"), record.get("left_at")
            u.since = float(since) if isinstance(since, (int, float)) else u.since
            u.away_since = float(away) if isinstance(away, (int, float)) and u.state == AWAY else None
            u.left_at = float(left) if isinstance(left, (int, float)) and u.state == AWAY else u.away_since
            declared, planned = record.get("declared_at"), record.get("planned_departure")
            u.declared_at = float(declared) if isinstance(declared, (int, float)) and u.state == AWAY else None
            u.planned_departure = float(planned) if isinstance(planned, (int, float)) else None

    # --- Fil -------------------------------------------------------------------------------------------

    def start(self, interval: float = 1.0) -> None:
        if self._thread is None:
            self._stopping.clear()
            self._thread = threading.Thread(target=self._loop, args=(interval,), name="presence", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _loop(self, interval: float) -> None:
        while not self._stopping.wait(interval):
            try:
                self.tick()
            except Exception:
                log.exception("Présence : échéances en erreur")
