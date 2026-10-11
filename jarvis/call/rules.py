"""Règles d'appel : « appelle-moi quand je suis absent et qu'un rappel arrive à échéance », dites par vous, écrites
par le Core. Le LLM traduit la phrase en règle structurée (jarvis/call/rule_parser.py) ; ce module la valide, la
garde, l'évalue et décide des appels. Aucune règle ne naît d'un mail, d'une page Web ou de la seule parole du LLM :
seul l'outil create_call_rule (confirmation obligatoire) en crée.

Règle (dictionnaire validé par ``validate_rule``) :
    trigger     {"type": "event", "event": <EVENTS>, "ref": <fil d'un mail, pour mail_reply>}
                | {"type": "at", "at": <date ISO>}  (vérification à une heure dite)
    conditions  [{"type": "away"} | {"type": "away_for", "minutes": n} | {"type": "home"}
                 | {"type": "important"} | {"type": "reminder_pending", "reminder": <numéro>}
                 | {"type": "mail_unanswered", "thread": <fil>}]
    until       date ISO de fin de la règle, ou ""          priority  normal | important | urgent
    channel     call | message                             repeat    {"attempts": 1-5, "interval_minutes": 5-120}
    resolve     ["arrival", "acknowledged"]                once      la règle s'arrête après son premier appel

Appel déclenché (« job ») : un par règle et par fait déclencheur (clé de déduplication), jamais deux :

    planifié ──> tentative ──> décroché ──> acquitté          (« c'est noté » pendant l'appel ou par message)
        ^            │            └──────> décroché sans accusé (résumé envoyé par message, plus d'appel)
        └── nouvel   ├──> sans réponse ─┐  (nouvel essai après l'intervalle, au plus « attempts » tentatives,
            essai    ├──> échec technique┘   puis message de secours)
                     └──> refusé           (vous avez refusé : message de secours, plus d'appel)
    annulé / résolu : conditions plus vraies (vous êtes rentré, rappel traité), règle supprimée ou désactivée.

Les limites anti-harcèlement du gestionnaire d'appels (jarvis/call/limits.py) restent la dernière barrière : un
appel qu'elles refusent est reporté (sans compter comme tentative) jusqu'à MAX_JOB_AGE, puis abandonné.
Tout est enregistré (règles, appels, décisions) et repris au redémarrage.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from jarvis.call.manager import CallRefused
from jarvis.events import Event, EventBus
from jarvis.persist import load_json_list
from jarvis.personality import normalize

log = logging.getLogger(__name__)

# Déclencheurs permis : clé -> (type d'événement du bus, sources de confiance, libellé dit à l'utilisateur).
EVENTS = {
    "reminder_due": ("reminder.finished", ("scheduling",), "un rappel arrive à échéance"),
    "mail_reply": ("mail.arrived", ("mail",), "une réponse à ce mail arrive"),
    "important_mail": ("mail.received", ("mail",), "un mail important arrive"),
    "system_down": ("system.error", ("system",), "un composant d'ORION tombe en panne"),
    "leak": ("leak.detected", ("sensor:",), "une fuite d'eau est détectée"),
    "smoke": ("smoke.detected", ("sensor:",), "de la fumée est détectée"),
    "motion_while_away": ("presence.motion_while_away", ("presence",), "un mouvement est détecté en votre absence"),
    "door_left_open": ("door.left_open", ("presence",), "la porte d'entrée reste ouverte"),
}
CONDITIONS = ("away", "away_for", "home", "important", "reminder_pending", "mail_unanswered")
PRIORITIES = ("normal", "important", "urgent")
CHANNELS = ("call", "message", "auto")  # auto : enceintes si vous êtes là, sinon téléphone
ACTIONS = ("notify", "flex")  # notify : dire le motif ; flex : présentation d'ORION (jarvis/flex.py)
RESOLVE = ("arrival", "acknowledged")
MAX_RULES = 30
MAX_SAY = 160
# Règle intégrée ([call] remind_when_away) : un rappel qui sonne pendant votre absence vous est dit au téléphone.
BUILTIN_REMINDERS = "rappels-absence"
BUILTIN_RULE = {"trigger": {"type": "event", "event": "reminder_due"}, "conditions": [{"type": "away"}],
                "repeat": {"attempts": 2, "interval_minutes": 15}}
MAX_ACTIVE_JOBS = 10
MAX_JOB_AGE = 3 * 3600.0  # s : au-delà, un appel encore en attente est abandonné
MAX_LOG = 20  # décisions gardées par règle (« pourquoi m'as-tu appelé ? »)
LIMIT_RETRY = 120.0  # s avant de réessayer un appel refusé par les limites anti-harcèlement
REF = re.compile(r"^[\w.@<>:+/=-]{1,200}$")

PLANNED, TRYING, ANSWERED, ACKNOWLEDGED = "planifié", "tentative", "décroché", "acquitté"
UNACKNOWLEDGED, NO_ANSWER, FAILED, REFUSED = "décroché sans accusé", "sans réponse", "échec technique", "refusé"
SENT, CANCELLED, RESOLVED, GAVE_UP = "message envoyé", "annulé", "résolu", "abandonné"
PRESENTED = "présenté"  # mode Flex : présentation faite (enceintes ou téléphone)
JOB_FINAL = (ACKNOWLEDGED, UNACKNOWLEDGED, REFUSED, SENT, CANCELLED, RESOLVED, GAVE_UP, PRESENTED)
FLEX_MAX_AGE = 600.0  # s : une présentation demandée il y a plus longtemps n'a plus de sens (redémarrage)
FLEX_REPEAT_GUARD = 120.0  # s après une présentation aux enceintes : « Flex » répété n'en relance pas une autre
ACK = re.compile(r"^(?:(?:c est |cest )?(?:note|bien note|bien recu|recu|compris|entendu|ok|okay|d accord|"
                 r"ca marche|je m en occupe|je gere|je sais|merci|oui|vu)(?: \w+){0,3})$")


class RuleError(ValueError):
    pass


def is_acknowledgement(text: str) -> bool:
    """« C'est noté », « bien reçu », « je m'en occupe » : vous avez compris le motif de l'appel."""
    return bool(ACK.match(normalize(text).replace("orion ", "").replace(" monsieur", "")))


# --- Validation (Core) ---------------------------------------------------------------------------------------

def _date(value, now: datetime | None, label: str) -> str:
    try:
        when = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise RuleError(f"{label} : date illisible.") from exc
    if when.tzinfo is not None:
        when = when.astimezone().replace(tzinfo=None)
    if now is not None and (when <= now or when > now + timedelta(days=60)):
        raise RuleError(f"{label} : la date doit être dans les 60 prochains jours.")
    return when.replace(microsecond=0).isoformat()


def validate_rule(data, now: datetime | None) -> dict:
    """Règle propre et cohérente, ou RuleError (jamais corrigée en silence). ``now`` None : dates non bornées
    (règle relue au redémarrage, déjà validée à sa création)."""
    if not isinstance(data, dict):
        raise RuleError("Règle : objet attendu.")
    trigger = data.get("trigger")
    if not isinstance(trigger, dict) or trigger.get("type") not in ("event", "at", "manual"):
        raise RuleError("Déclencheur inconnu.")
    if trigger["type"] == "event":
        event = trigger.get("event")
        if event not in EVENTS:
            raise RuleError(f"Événement non pris en charge : {str(event)[:40]}.")
        clean_trigger = {"type": "event", "event": event}
        if event == "mail_reply":
            ref = str(trigger.get("ref") or "")
            if not REF.match(ref):
                raise RuleError("Quel mail, monsieur ? Ouvrez-le ou lisez-le d'abord, puis redemandez-moi.")
            clean_trigger["ref"] = ref
    elif trigger["type"] == "manual":
        clean_trigger = {"type": "manual"}  # déclenchée sur demande (« Flex »)
    else:
        clean_trigger = {"type": "at", "at": _date(trigger.get("at"), now, "Heure de vérification")}
    conditions = []
    for condition in data.get("conditions") or []:
        if not isinstance(condition, dict) or condition.get("type") not in CONDITIONS:
            raise RuleError("Condition non prise en charge.")
        kind = condition["type"]
        clean = {"type": kind}
        if kind == "away_for":
            minutes = condition.get("minutes")
            if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 7 * 1440:
                raise RuleError("Durée d'absence : de 1 minute à 7 jours.")
            clean["minutes"] = minutes
        elif kind == "reminder_pending":
            reminder = str(condition.get("reminder") or "")
            if not re.fullmatch(r"\d{1,6}", reminder):
                raise RuleError("Quel rappel, monsieur ? Donnez-moi son numéro.")
            clean["reminder"] = reminder
        elif kind == "mail_unanswered":
            thread = str(condition.get("thread") or "")
            if not REF.match(thread):
                raise RuleError("Quel mail, monsieur ? Ouvrez-le ou lisez-le d'abord, puis redemandez-moi.")
            clean["thread"] = thread
        if clean not in conditions:
            conditions.append(clean)
    kinds = {c["type"] for c in conditions}
    if "home" in kinds and kinds & {"away", "away_for"}:
        raise RuleError("Cette règle demande à la fois que vous soyez absent et à la maison.")
    if clean_trigger["type"] == "at" and not conditions:
        conditions = []  # « appelle-moi demain à 18 h » : un simple appel programmé, permis
    until = data.get("until") or ""
    until = _date(until, now, "Fin de la règle") if until else ""
    if until and clean_trigger["type"] == "at" and until < clean_trigger["at"]:
        raise RuleError("La règle s'arrêterait avant de pouvoir se déclencher.")
    priority = data.get("priority", "normal")
    channel = data.get("channel", "call")
    if priority not in PRIORITIES or channel not in CHANNELS:
        raise RuleError("Priorité ou canal inconnu.")
    repeat = data.get("repeat") or {}
    attempts, interval = repeat.get("attempts", 2), repeat.get("interval_minutes", 15)
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (attempts, interval)) or \
            not 1 <= attempts <= 5 or not 5 <= interval <= 120:
        raise RuleError("Répétition : 1 à 5 tentatives, de 5 à 120 minutes d'intervalle.")
    resolve = [r for r in (data.get("resolve") or ["arrival", "acknowledged"]) if r in RESOLVE]
    once = bool(data.get("once", clean_trigger["type"] == "at" or "ref" in clean_trigger
                         or kinds & {"reminder_pending", "mail_unanswered"}))
    say = " ".join(str(data.get("say") or "").split())
    if len(say) > MAX_SAY or re.search(r"[\x00-\x1f]", say):
        raise RuleError("Ce que je dois vous dire est trop long.")
    action = data.get("action", "notify")
    if action not in ACTIONS or (channel == "auto") != (action == "flex"):
        raise RuleError("Action ou canal incohérents.")
    if clean_trigger["type"] == "manual" and action != "flex":
        raise RuleError("Seule la présentation se déclenche sur demande.")
    rule = {"trigger": clean_trigger, "conditions": conditions, "until": until, "priority": priority,
            "channel": channel, "repeat": {"attempts": attempts, "interval_minutes": interval},
            "resolve": resolve, "once": once if action != "flex" else False}
    if action == "flex":
        delay = data.get("delay_seconds", 60)
        if isinstance(delay, bool) or not isinstance(delay, int) or not 10 <= delay <= 600:
            raise RuleError("Délai avant l'appel : de 10 secondes à 10 minutes.")
        rule.update(action="flex", delay_seconds=delay)
    if say:
        rule["say"] = say  # ce qu'ORION dit au décroché, dans vos mots (« que je fais un test »)
    return rule


def describe_rule(rule: dict, labels: dict | None = None) -> str:
    """Phrase dite à l'utilisateur, écrite par le Core (jamais le texte du LLM)."""
    labels = labels or {}
    verb = "vous appeler" if rule["channel"] == "call" else "vous écrire"
    trigger = rule["trigger"]
    if trigger["type"] == "at":
        when = datetime.fromisoformat(trigger["at"])
        head = f"{verb} le {when:%d/%m} à {when:%H:%M}"
    else:
        label = EVENTS[trigger["event"]][2]
        if trigger["event"] == "mail_reply" and labels.get(trigger.get("ref", "")):
            label = f"une réponse au mail « {labels[trigger['ref']]} » arrive"
        head = f"{verb} quand {label}"
    parts = []
    for c in rule["conditions"]:
        parts.append({"away": "si vous êtes absent", "home": "si vous êtes à la maison",
                      "important": "s'il est important",
                      "away_for": f"si vous êtes absent depuis plus de {c.get('minutes')} minutes",
                      "reminder_pending": f"si le rappel numéro {c.get('reminder')} est toujours en attente",
                      "mail_unanswered": "si vous n'avez toujours pas répondu à "
                                         + (f"« {labels[c['thread']]} »" if labels.get(c.get("thread", ""))
                                            else "ce mail")}[c["type"]])
    text = head + (", " + " et ".join(parts) if parts else "")
    if rule.get("action") == "flex":
        return (f"vous présenter mes capacités quand vous me le demandez : sur les enceintes, ou par téléphone si vous "
                f"êtes toujours absent au bout de {rule.get('delay_seconds', 60)} secondes")
    if rule.get("say"):
        said = spoken_say(rule["say"]).rstrip(" .")
        text += f", {said}" if said.startswith(("pour", "au sujet")) else f", pour vous dire : {said}"
    if rule["until"]:
        text += f", jusqu'au {datetime.fromisoformat(rule['until']):%d/%m à %H:%M}"
    repeat = rule["repeat"]
    if rule["channel"] == "call" and repeat["attempts"] > 1:
        text += f" (jusqu'à {repeat['attempts']} essais, toutes les {repeat['interval_minutes']} minutes)"
    return text


# --- Moteur ------------------------------------------------------------------------------------------------

class CallRules:
    def __init__(self, path: Path, calls, presence=None, user: str = "owner", reminders=None,
                 mail_replied: Callable[[str], bool | None] | None = None, bus: EventBus | None = None,
                 clock: Callable[[], datetime] = datetime.now, monotonic: Callable[[], float] = time.time):
        self.path = Path(path)
        self._calls = calls  # CallManager : request, reply (messages Linphone)
        self._presence = presence  # PresenceEngine
        self._user = user
        self._reminders = reminders  # TimerManager
        self._mail_replied = mail_replied or (lambda thread: None)
        self._bus = bus
        self._clock = clock
        self._time = monotonic
        self._lock = threading.RLock()
        self.labels: dict[str, str] = {}  # fil d'un mail -> objet (pour les phrases), gardé avec les règles
        self.removed: set[str] = set()  # règles intégrées supprimées par vous : jamais recréées
        # Mode Flex (jarvis/flex.py), branché par la factory : texte (téléphone ou non), enceintes, sortie prête.
        self.flex_text: Callable[[bool], list[str]] | None = None
        self.flex_present_home: Callable[[list[str]], None] | None = None
        self.speaker_ready: Callable[[], bool] | None = None
        self.rules: list[dict] = []
        self.jobs: list[dict] = []
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._load()

    # --- Stockage ----------------------------------------------------------------------------------------

    def _load(self) -> None:
        records = load_json_list(self.path, "Règles d'appel")
        for record in records:
            if not isinstance(record, dict):
                continue
            if record.get("kind") == "labels" and isinstance(record.get("labels"), dict):
                self.labels = {str(k)[:200]: str(v)[:80] for k, v in record["labels"].items()}
                self.removed = {str(x) for x in record.get("removed", [])}
            elif record.get("kind") == "rule":
                try:
                    rule = validate_rule(record.get("rule"), None)
                except RuleError:
                    log.warning("Règle d'appel %s illisible : ignorée", record.get("id"))
                    continue
                self.rules.append({"id": str(record.get("id")), "text": str(record.get("text", ""))[:300],
                                   "enabled": bool(record.get("enabled", True)), "rule": rule,
                                   "created": str(record.get("created", "")), "log": list(record.get("log", []))[-MAX_LOG:],
                                   "keys": list(record.get("keys", []))[-200:], "done": bool(record.get("done"))})
            elif record.get("kind") == "job":
                job = {k: record.get(k) for k in ("id", "rule", "key", "state", "reason", "attempts", "next_at",
                                                    "created", "call", "history", "max_attempts", "interval",
                                                    "channel", "resolve", "action", "waited")}
                if job["state"] == TRYING:  # ORION arrêté pendant l'appel : la tentative compte comme un échec
                    job["state"] = FAILED
                    job["history"] = (job.get("history") or []) + [[FAILED, self._time(), "redémarrage"]]
                    job["next_at"] = self._time() + 60
                self.jobs.append(job)

    def _save(self) -> None:
        records = [{"kind": "labels", "labels": self.labels, "removed": sorted(self.removed)}]
        records += [{"kind": "rule", **{k: r[k] for k in ("id", "text", "enabled", "rule", "created", "log",
                                                         "keys", "done")}} for r in self.rules]
        records += [{"kind": "job", **job} for job in self.jobs]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("Règles d'appel non enregistrées : %s", exc)

    # --- Règles (outils) ---------------------------------------------------------------------------------

    def add(self, rule: dict, text: str, label: tuple[str, str] | None = None) -> dict:
        rule = validate_rule(rule, self._clock())
        with self._lock:
            if len(self.rules) >= MAX_RULES:
                raise RuleError(f"Vous avez déjà {MAX_RULES} règles d'appel : supprimez-en une d'abord.")
            if any(r["rule"] == rule and r["enabled"] and not r["done"] for r in self.rules):
                raise RuleError("Cette règle existe déjà.")
            if label is not None:
                self.labels[label[0]] = label[1][:80]
            entry = {"id": secrets.token_hex(4), "text": str(text)[:300], "enabled": True, "rule": rule,
                     "created": self._clock().isoformat(timespec="seconds"), "log": [], "keys": [], "done": False}
            self.rules.append(entry)
            self._save()
        log.info("Règle d'appel %s créée : %s", entry["id"], describe_rule(rule))
        return entry

    def replace(self, position: int, rule: dict, text: str) -> dict:
        rule = validate_rule(rule, self._clock())
        with self._lock:
            entry = self.at(position)
            entry.update(rule=rule, text=str(text)[:300], done=False, enabled=True)
            self._log(entry, "règle modifiée")
            self._save()
        return entry

    def at(self, position: int) -> dict:
        with self._lock:
            if not isinstance(position, int) or not 1 <= position <= len(self.rules):
                raise RuleError(f"Je n'ai pas de règle numéro {position}." if self.rules
                                else "Vous n'avez aucune règle d'appel.")
            return self.rules[position - 1]

    def set_enabled(self, position: int, enabled: bool) -> dict:
        with self._lock:
            entry = self.at(position)
            entry["enabled"] = enabled
            if not enabled:
                self._cancel_jobs(entry["id"], CANCELLED, "règle désactivée")
            self._log(entry, "règle activée" if enabled else "règle désactivée")
            self._save()
            return entry

    def ensure_builtin(self, rule_id: str, rule: dict, text: str) -> None:
        """Règle intégrée (réglage du mini) : créée une fois ; désactivée ou supprimée par vous, elle le reste."""
        with self._lock:
            if rule_id in self.removed or any(r["id"] == rule_id for r in self.rules):
                return
            self.rules.insert(0, {"id": rule_id, "text": text, "enabled": True, "rule": validate_rule(rule, None),
                                  "created": self._clock().isoformat(timespec="seconds"), "log": [], "keys": [],
                                  "done": False})
            self._save()
        log.info("Règle d'appel intégrée « %s » ajoutée", rule_id)

    def set_builtin_delay(self, rule_id: str, seconds: int) -> None:
        """Délai de la règle intégrée repris du réglage à chaque démarrage (flex_delay_seconds)."""
        with self._lock:
            entry = next((r for r in self.rules if r["id"] == rule_id), None)
            if entry is not None and entry["rule"].get("delay_seconds") != seconds:
                entry["rule"] = validate_rule({**entry["rule"], "delay_seconds": seconds}, None)
                self._save()

    def remove(self, position: int) -> dict:
        with self._lock:
            entry = self.at(position)
            self.rules.remove(entry)
            if entry["id"] in (BUILTIN_REMINDERS, "flex"):
                self.removed.add(entry["id"])
            self._cancel_jobs(entry["id"], CANCELLED, "règle supprimée")
            self._save()
            return entry

    def describe(self, entry: dict) -> str:
        return describe_rule(entry["rule"], self.labels)

    def explain(self, position: int | None = None) -> list[str]:
        """Dernières décisions (« pourquoi m'as-tu appelé ? », « pourquoi ne m'as-tu pas appelé ? »)."""
        with self._lock:
            entries = [self.at(position)] if position else self.rules
            lines = []
            for entry in entries:
                for when, what in entry["log"][-5:]:
                    lines.append((when, f"{when[11:16]} : {what}"))
            return [text for _, text in sorted(lines)[-8:]]

    def _log(self, entry: dict, what: str) -> None:
        entry["log"] = (entry["log"] + [[self._clock().isoformat(timespec="seconds"), what[:160]]])[-MAX_LOG:]

    # --- Conditions ----------------------------------------------------------------------------------------

    def _state(self) -> tuple[str | None, float | None]:
        if self._presence is None:
            return None, None
        user = self._presence.users.get(self._user)
        return (user.state, user.away_since) if user is not None else (None, None)

    def check(self, rule: dict, payload: dict) -> tuple[bool, str]:
        """Conditions de la règle maintenant : (vraies ?, raison lisible de la décision)."""
        state, away_since = self._state()
        for c in rule["conditions"]:
            kind = c["type"]
            if kind in ("away", "away_for"):
                if state is None:
                    return False, "présence inconnue (aucun capteur)"
                if state != "AWAY":
                    return False, "vous étiez à la maison"
                if kind == "away_for":
                    away = (self._time() - away_since) / 60 if away_since else 0
                    if away < c["minutes"]:
                        return False, f"absent depuis {away:.0f} min seulement (règle : {c['minutes']} min)"
            elif kind == "home" and state != "HOME":
                return False, "vous n'étiez pas à la maison"
            elif kind == "important" and not (payload.get("important") or payload.get("priority") == "important"):
                return False, "ce n'était pas important"
            elif kind == "reminder_pending":
                item = self._reminders.get("reminder", c["reminder"]) if self._reminders is not None else None
                if item is None or getattr(item.status, "value", item.status) != "active":
                    return False, f"le rappel {c['reminder']} n'est plus en attente"
            elif kind == "mail_unanswered" and self._mail_replied(c["thread"]) is not False:
                return False, "vous avez déjà répondu" if self._mail_replied(c["thread"]) else \
                    "je ne peux pas vérifier votre réponse"
        return True, "conditions remplies"

    # --- Déclenchement -------------------------------------------------------------------------------------

    def connect(self, calls, bus: EventBus) -> None:
        """Gestionnaire d'appels (créé après les outils) et bus : la règle peut maintenant appeler."""
        self._calls = calls
        self.attach(bus)

    def attach(self, bus: EventBus) -> None:
        self._bus = bus
        for event_type in {spec[0] for spec in EVENTS.values()}:
            bus.subscribe(event_type, self.on_event)
        for event_type in ("call.connected", "call.ended", "call.refused", "call.expired", "call.failed"):
            bus.subscribe(event_type, self.on_call)
        bus.subscribe("presence.arrival_confirmed", self.on_arrival)

    def on_event(self, event: Event) -> None:
        with self._lock:
            for entry in self.rules:
                trigger = entry["rule"]["trigger"]
                if not entry["enabled"] or entry["done"] or trigger["type"] != "event":
                    continue
                kind, sources, _ = EVENTS[trigger["event"]]
                if event.type != kind:
                    continue
                if not any(event.source == s or (s.endswith(":") and event.source.startswith(s)) for s in sources):
                    log.warning("Règle d'appel : %s de source %r ignoré", event.type, event.source)
                    continue
                if trigger.get("ref") and trigger["ref"] != event.payload.get("thread"):
                    continue
                key = _event_key(event)
                self._fire(entry, key, event.payload)

    def _fire(self, entry: dict, key: str, payload: dict) -> None:
        rule = entry["rule"]
        if f"{entry['id']}:{key}" in entry["keys"]:
            return  # même fait déjà traité (événement rejoué, redémarrage) : jamais deux appels
        entry["keys"] = (entry["keys"] + [f"{entry['id']}:{key}"])[-200:]
        if rule["until"] and self._clock().isoformat() > rule["until"]:
            entry["enabled"] = False
            self._log(entry, "règle arrivée à sa date de fin")
            self._save()
            return
        ok, why = self.check(rule, payload)
        if not ok:
            self._log(entry, f"pas d'appel : {why}")
            self._save()
            return
        if sum(1 for j in self.jobs if j["state"] not in JOB_FINAL) >= MAX_ACTIVE_JOBS:
            self._log(entry, "pas d'appel : trop d'appels en attente")
            self._save()
            return
        reason = _reason(rule, payload)
        job = {"id": secrets.token_hex(4), "rule": entry["id"], "key": key, "state": PLANNED, "reason": reason,
               "attempts": 0, "next_at": self._time(), "created": self._time(), "call": "",
               "history": [[PLANNED, self._time(), why]], "max_attempts": rule["repeat"]["attempts"],
               "interval": rule["repeat"]["interval_minutes"] * 60, "channel": rule["channel"],
               "resolve": list(rule["resolve"])}
        self.jobs.append(job)
        if rule["once"]:
            entry["done"] = True
        self._log(entry, f"appel prévu : {reason}")
        self._save()
        self._advance(job)

    def tick(self) -> None:
        """Échéances : règles à une heure dite, tentatives à relancer, appels trop anciens."""
        now_dt, now = self._clock(), self._time()
        with self._lock:
            for entry in self.rules:
                trigger = entry["rule"]["trigger"]
                if entry["enabled"] and not entry["done"] and trigger["type"] == "at" and \
                        now_dt.isoformat() >= trigger["at"]:
                    late = (now_dt - datetime.fromisoformat(trigger["at"])).total_seconds()
                    if late > 3600:  # ORION arrêté au moment dit : trop tard pour appeler
                        entry["done"] = True
                        self._log(entry, "heure dite passée pendant un arrêt : pas d'appel")
                        self._save()
                        continue
                    self._fire(entry, f"at:{trigger['at']}", {})
                    entry["done"] = True
                    self._save()
            for job in list(self.jobs):
                if job["state"] in (PLANNED, NO_ANSWER, FAILED) and now >= job["next_at"]:
                    self._advance(job)
            self.jobs = [j for j in self.jobs if j["state"] not in JOB_FINAL or now - j["created"] < 7 * 86400]

    # --- Mode Flex (présentation) -------------------------------------------------------------------------

    def trigger_flex(self, rule_id: str = "flex") -> str:
        """« Flex » : présentation aux enceintes si vous êtes là, sinon appel après le délai de la règle (annulé si
        vous rentrez entre-temps). Rend « home », « waiting », « already », « disabled » ou « unavailable »."""
        with self._lock:
            entry = next((r for r in self.rules if r["id"] == rule_id), None)
            if entry is None or self.flex_text is None:
                return "unavailable"
            if not entry["enabled"]:
                return "disabled"
            now = self._time()
            for job in self.jobs:
                if job.get("action") != "flex":
                    continue
                if job["state"] not in JOB_FINAL or (job["state"] == PRESENTED and now - job["history"][-1][1] <
                                                     FLEX_REPEAT_GUARD and job.get("channel") == "home"):
                    return "already"  # jamais deux présentations à la fois
            job = {"id": secrets.token_hex(4), "rule": entry["id"], "key": f"flex:{int(now)}", "state": PLANNED,
                   "reason": "", "attempts": 0, "next_at": now, "created": now, "call": "",
                   "history": [[PLANNED, now, "demandé"]], "max_attempts": 1, "interval": 0, "channel": "auto",
                   "resolve": [], "action": "flex", "waited": False}
            self.jobs.append(job)
            self._log(entry, "présentation demandée")
            self._save()
            self._advance_flex(job, entry)
            return "home" if job["state"] == PRESENTED else "waiting" if job["state"] == PLANNED else "call"

    def set_flex_enabled(self, enabled: bool, rule_id: str = "flex") -> bool:
        with self._lock:
            entry = next((r for r in self.rules if r["id"] == rule_id), None)
            if entry is None:
                raise RuleError("Le mode Flex n'est pas disponible.")
            entry["enabled"] = enabled
            if not enabled:
                self._cancel_jobs(entry["id"], CANCELLED, "mode Flex désactivé")
            self._save()
            return enabled

    def _home(self) -> bool:
        """À la maison (ou présence inconnue) : la présentation passe par les enceintes."""
        state, _ = self._state()
        return state is None or state in ("HOME", "POSSIBLE_DEPARTURE")

    def _advance_flex(self, job: dict, entry: dict) -> None:
        now = self._time()
        if now - job["created"] > FLEX_MAX_AGE:
            self._finish(job, GAVE_UP, "trop tard (ORION était arrêtée)")
            return
        if job["state"] in (NO_ANSWER, FAILED):
            self._finish(job, GAVE_UP, "pas de réponse")
            self._send("Je souhaitais me présenter, monsieur. Dites « Flex » quand vous voulez.")
            return
        if self._home() and (self.speaker_ready is None or self.speaker_ready()):
            self._present_home(job, entry)
            return
        if self._home():
            self._log(entry, "enceintes indisponibles : présentation par téléphone")
        elif not job.get("waited"):
            job["waited"] = True
            job["next_at"] = now + entry["rule"].get("delay_seconds", 60)  # une minute pour rentrer
            self._move(job, PLANNED, "absent : appel dans un instant, sauf retour")
            return
        try:
            call = self._calls.request(" ".join(self.flex_text(True)), "routine")
        except CallRefused as exc:
            self._finish(job, GAVE_UP, f"appel impossible : {exc}")
            self._log(entry, f"présentation non faite : {exc}")
            return
        job["attempts"], job["call"] = 1, call.id
        self._move(job, TRYING, "présentation par téléphone")
        if call.final:
            self._outcome(job, FAILED, call.end_reason)

    def _present_home(self, job: dict, entry: dict) -> None:
        if self.flex_present_home is None:
            self._finish(job, GAVE_UP, "aucune enceinte")
            return
        self.flex_present_home(self.flex_text(False))
        job["channel"] = "home"
        self._finish(job, PRESENTED, "présentation aux enceintes")
        self._log(entry, "présentation faite aux enceintes")

    def _advance(self, job: dict) -> None:
        """Prochaine étape d'un appel en attente : vérifié de nouveau, puis tenté (ou message de secours)."""
        entry = next((r for r in self.rules if r["id"] == job["rule"]), None)
        now = self._time()
        if entry is None or not entry["enabled"]:
            self._finish(job, CANCELLED, "règle supprimée ou désactivée")
            return
        if job.get("action") == "flex":
            self._advance_flex(job, entry)
            return
        ok, why = self.check(entry["rule"], {"important": True, "priority": "important"})
        if not ok:
            self._finish(job, RESOLVED, why)
            self._log(entry, f"appel annulé : {why}")
            return
        if now - job["created"] > MAX_JOB_AGE:
            self._finish(job, GAVE_UP, "trop tard")
            self._fallback(job, entry)
            return
        if job["attempts"] >= job["max_attempts"]:
            self._finish(job, GAVE_UP, f"{job['attempts']} tentative(s) sans réponse")
            self._log(entry, f"pas joint après {job['attempts']} tentative(s)")
            self._fallback(job, entry)
            return
        if job["channel"] == "message":
            self._send(job["reason"])
            self._finish(job, SENT, "message")
            self._log(entry, "message envoyé")
            return
        try:
            call = self._calls.request(job["reason"], "routine")
        except CallRefused as exc:
            job["next_at"] = now + LIMIT_RETRY  # limites anti-harcèlement : plus tard, sans compter d'essai
            job["history"].append([job["state"], now, f"reporté : {exc}"])
            self._log(entry, f"appel reporté : {exc}")
            self._save()
            return
        job["attempts"] += 1
        job["call"] = call.id
        self._move(job, TRYING, f"tentative {job['attempts']}")
        if call.final:  # sonnerie impossible (passerelle arrêtée) : échec technique tout de suite
            self._outcome(job, FAILED, call.end_reason)

    # --- Issue des appels ----------------------------------------------------------------------------------

    def on_call(self, event: Event) -> None:
        call_id = str(event.payload.get("call", ""))
        with self._lock:
            job = next((j for j in self.jobs if j["call"] == call_id and j["state"] in (TRYING, ANSWERED)), None)
            if job is None:
                return
            if job.get("action") == "flex":  # présentation : faite si vous avez décroché, jamais rejouée sinon
                if event.type == "call.connected":
                    self._move(job, ANSWERED, "décroché")
                elif event.type == "call.ended" and job["state"] == ANSWERED:
                    job["channel"] = "phone"
                    self._finish(job, PRESENTED, "présentation par téléphone")
                else:
                    self._outcome(job, NO_ANSWER if event.type == "call.expired" else FAILED,
                                  str(event.payload.get("end_reason", "")))
                    self._advance(job)
                return
            if event.type == "call.connected":
                self._move(job, ANSWERED, "décroché")
            elif event.type == "call.ended" and job["state"] == ANSWERED:
                entry = next((r for r in self.rules if r["id"] == job["rule"]), None)
                self._finish(job, UNACKNOWLEDGED, "raccroché sans « c'est noté »")
                self._send(f"Pour mémoire : {job['reason']} Répondez « c'est noté » quand c'est fait.")
                if entry is not None:
                    self._log(entry, "décroché sans accusé : résumé envoyé par message")
            elif event.type == "call.refused":
                self._finish(job, REFUSED, "refusé sur le téléphone")
                self._send(f"Je vous ai appelé : {job['reason']}")
            elif event.type in ("call.expired", "call.failed", "call.ended"):
                self._outcome(job, NO_ANSWER if event.type == "call.expired" else FAILED,
                              str(event.payload.get("end_reason", "")))

    def _outcome(self, job: dict, state: str, why: str) -> None:
        job["next_at"] = self._time() + job["interval"]
        self._move(job, state, why or state)

    def awaiting_ack(self, call_id: str) -> dict | None:
        with self._lock:
            return next((j for j in self.jobs if j["call"] == call_id and j["state"] == ANSWERED
                         and j.get("action") != "flex"), None)

    def acknowledge(self, call_id: str | None = None) -> bool:
        """« C'est noté » pendant l'appel (``call_id``), ou par message après un résumé envoyé (le plus récent)."""
        with self._lock:
            if call_id:
                job = self.awaiting_ack(call_id)
            else:
                recent = [j for j in self.jobs if j["state"] in (UNACKNOWLEDGED, REFUSED, GAVE_UP)
                          and self._time() - j["history"][-1][1] < 4 * 3600 and not j.get("acked")]
                job = recent[-1] if recent else None
            if job is None:
                return False
            job["acked"] = True
            self._finish(job, ACKNOWLEDGED, "« c'est noté »")
            entry = next((r for r in self.rules if r["id"] == job["rule"]), None)
            if entry is not None:
                self._log(entry, "appel acquitté")
                self._save()
            return True

    def on_arrival(self, event: Event) -> None:
        """Vous êtes rentré : les appels encore en attente des règles « résolues au retour » n'ont plus lieu."""
        with self._lock:
            for job in self.jobs:
                if job.get("action") == "flex" and job["state"] == PLANNED:
                    self._advance(job)  # rentré pendant la minute d'attente : enceintes, pas d'appel
                elif job["state"] in (PLANNED, NO_ANSWER, FAILED) and "arrival" in job.get("resolve", []):
                    self._finish(job, RESOLVED, "vous êtes rentré")

    def _cancel_jobs(self, rule_id: str, state: str, why: str) -> None:
        for job in self.jobs:
            if job["rule"] == rule_id and job["state"] not in JOB_FINAL:
                self._finish(job, state, why)

    def _fallback(self, job: dict, entry: dict) -> None:
        """Pas joint : message de secours (une fois), jamais de nouvelle série d'appels."""
        if job["channel"] == "call" and job["attempts"]:
            self._send(f"Je suis désolée, monsieur, je n'ai pas réussi à vous joindre. {job['reason']}")
            self._log(entry, "message de secours envoyé")

    def _send(self, text: str) -> None:
        try:
            self._calls.reply(text)
        except Exception as exc:  # noqa: BLE001 - passerelle absente : noté, ORION continue
            log.warning("Message de règle d'appel non envoyé (%s)", type(exc).__name__)

    def _move(self, job: dict, state: str, why: str) -> None:
        job["state"] = state
        job["history"] = (job["history"] + [[state, self._time(), why[:120]]])[-20:]
        log.info("Appel de règle %s : %s (%s)", job["id"], state, why[:80])
        self._publish(job)
        self._save()

    def _finish(self, job: dict, state: str, why: str) -> None:
        self._move(job, state, why)

    def _publish(self, job: dict) -> None:
        if self._bus is not None:
            self._bus.publish(Event("call_rule.job", "call_rules", {"job": job["id"], "rule": job["rule"],
                                                                    "state": job["state"]}))

    # --- Fil -------------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="regles-appel", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stopping.set()

    def _run(self) -> None:
        while not self._stopping.wait(5.0):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - le fil des règles ne doit pas mourir
                log.exception("Règles d'appel : échéances non vérifiées")


def _event_key(event: Event) -> str:
    """Identité du fait déclencheur (rappel, mail, capteur...) : deux publications du même fait ont la même clé."""
    p = event.payload
    for field in ("reminder_id", "message_id", "thread_message", "call"):
        if p.get(field):
            return f"{event.type}:{p[field]}"
    return f"{event.type}:{p.get('sensor', p.get('source', ''))}:{int(event.timestamp // 60)}"


TU_VERBS = {"es": "suis", "as": "ai", "vas": "vais", "peux": "peux", "dois": "dois", "veux": "veux"}


def spoken_say(text: str) -> str:
    """Vos mots redits par ORION : « me dire que tu fais un test » -> « vous dire que je fais un test »."""
    from jarvis.personality import second_person

    text = second_person(text)
    text = re.sub(r"\btu (\w+)", lambda m: "j'ai" if m.group(1) == "as" else f"je {TU_VERBS.get(m.group(1), m.group(1))}",
                  text)
    return re.sub(r"\b(?:te\s+|t['’])(?=\w)", "me ", text)


def _reason(rule: dict, payload: dict) -> str:
    """Ce qu'ORION dit en décrochant : le fait (texte du Core), jamais un texte libre venu d'un mail."""
    trigger = rule["trigger"]
    if rule.get("say"):  # vos mots, dits au décroché (« pour me dire que tu fais un test »)
        said = spoken_say(rule["say"]).rstrip(" .")
        return f"Vous m'aviez demandé de vous appeler{' ' if said.startswith(('pour', 'au sujet')) else ' : '}{said}."
    if trigger["type"] == "at":
        return "Vous m'aviez demandé de vous appeler à cette heure-ci."
    event = trigger["event"]
    if event == "reminder_due":
        message = str(payload.get("message", ""))[:160]
        return f"Votre rappel arrive à échéance : {message}." if message else "Un de vos rappels arrive à échéance."
    if event in ("mail_reply", "important_mail"):
        sender = str(payload.get("sender", ""))[:40]
        return f"Vous avez reçu une réponse de {sender}." if event == "mail_reply" and sender else \
            f"Un mail important est arrivé{f' de {sender}' if sender else ''}."
    if event == "system_down":
        return f"Un composant ne répond plus : {str(payload.get('source', ''))[:30]}."
    return EVENTS[event][2].capitalize() + "."
