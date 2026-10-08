"""Annonce des nouveaux mails importants : ORION regarde la boîte à intervalle régulier et le dit.

- Seuls les mails jugés importants (ou personnels, selon [mail] announce) sont retenus, par le tri déterministe de
  jarvis/mail/priority.py : jamais le LLM, le contenu d'un mail ne décide de rien.
- Présent et hors heures calmes : annonce vocale (« Monsieur, nouveau mail important de la banque : « Facture
  d'octobre ». »), puis « lis ce mail » vise ce mail.
- Absent : rien n'est dit ; l'événement « mail.received » entre au journal de la maison et le résumé « Bon retour »
  le cite à l'arrivée.
- Heures calmes : rien n'est dit ; le point du jour (routine du matin) résume ce qui est arrivé.

Au premier lancement, les non lus existants sont considérés comme déjà vus (pas d'avalanche d'annonces). Expéditeur
et objet sont des données non fiables : tronqués, sans lien, et sans le nom de l'assistant (une annonce ne doit
jamais pouvoir réveiller ORION).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, time as clock_time
from pathlib import Path
from typing import Callable

from jarvis.events import Event, EventBus
from jarvis.mail.models import MailError, MailMessage
from jarvis.mail.priority import IMPORTANT, NOISE
from jarvis.persist import load_json_list

log = logging.getLogger(__name__)

MAIL_RECEIVED = "mail.received"
LEVELS = ("important", "personnel", "aucun")
MAX_SEEN = 2000
URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
# Mots qui réveillent l'assistant (et leurs transcriptions approchées) : retirés de tout texte annoncé.
WAKE_WORDS = re.compile(r"\b(?:orion|oryon|aurion|jarvis|jervis|djarvis)\b", re.IGNORECASE)


def spoken(text: str, limit: int) -> str:
    """Texte d'un mail prêt à être dit : sans lien, sans mot de réveil, sans caractère de contrôle, tronqué."""
    text = WAKE_WORDS.sub("", URL.sub("", str(text)))
    text = " ".join(re.sub(r"[\x00-\x1f«»\"]", " ", text).split())
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    """Heures calmes (« 22:30 » à « 08:00 », à cheval sur minuit possible) ; vide : jamais."""
    if not start or not end:
        return False
    a, b = clock_time.fromisoformat(start), clock_time.fromisoformat(end)
    t = now.time()
    return a <= t < b if a <= b else t >= a or t < b


class MailWatcher:
    def __init__(self, box, events: EventBus | None, announce: Callable[[str, str, dict], None], state_path: Path,
                 interval: float = 300.0, level: str = "important", quiet_start: str = "22:30",
                 quiet_end: str = "08:00", present: Callable[[], bool | None] | None = None,
                 title: str = "monsieur", clock: Callable[[], datetime] = datetime.now):
        if level not in LEVELS:
            raise ValueError(f"[mail] announce : {level!r} (attendu : {', '.join(LEVELS)})")
        self.box = box
        self._events = events
        self._announce = announce
        self._path = Path(state_path)
        self.interval = interval
        self.level = level
        self._quiet = (quiet_start, quiet_end)
        self._present = present or (lambda: None)
        self._title = title
        self._clock = clock
        self._lock = threading.Lock()
        self._seen: list[str] | None = None  # None : premier passage, les non lus existants deviennent « vus »
        self.pending: list[dict] = []        # retenus mais pas encore dits (absence, nuit) : pour le point du jour
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if self._path.exists():
            self._seen = [str(x) for x in load_json_list(self._path, "mails déjà vus")][-MAX_SEEN:]

    # --- Tri ---------------------------------------------------------------------------------------

    def wanted(self, message: MailMessage) -> bool:
        if self.level == "aucun":
            return False
        priority = self.box.sorter.priority(message)
        return priority == IMPORTANT or (self.level == "personnel" and priority != NOISE)

    # --- Un passage ----------------------------------------------------------------------------------

    def check(self) -> list[MailMessage]:
        """Regarde les non lus ; rend les mails nouvellement retenus (annoncés ou mis de côté)."""
        with self._lock:
            try:
                _, unread = self.box.provider.unread(50)
            except MailError as exc:
                log.warning("Mails : surveillance impossible (%s)", exc.message)
                return []
            ids = [m.id for m in unread]
            if self._seen is None:  # premier passage : rien n'est annoncé
                self._seen = ids
                self._save()
                log.info("Mails : surveillance démarrée (%d non lus déjà connus)", len(ids))
                return []
            fresh = [m for m in unread if m.id not in self._seen]
            if not fresh:
                return []
            self._seen = (self._seen + [m.id for m in fresh])[-MAX_SEEN:]
            self._save()
            kept = [m for m in sorted(fresh, key=lambda m: int(m.id) if m.id.isdigit() else 0) if self.wanted(m)]
        for m in kept:
            if self._events is not None:
                self._events.publish(Event(MAIL_RECEIVED, "mail", {
                    "sender": spoken(m.who, 40), "subject": spoken(m.subject, 80),
                    "priority": self.box.sorter.priority(m)}))
        if kept:
            self._say_or_keep(kept)
        return kept

    def _say_or_keep(self, kept: list[MailMessage]) -> None:
        quiet = in_quiet_hours(self._clock(), *self._quiet)
        away = self._present() is False
        if quiet or away:
            self.pending += [{"sender": spoken(m.who, 40), "subject": spoken(m.subject, 80)} for m in kept]
            log.info("Mails : %d retenu(s), annonce différée (%s)", len(kept), "nuit" if quiet else "absence")
            return
        self.box.remember(kept)  # « lis ce mail », « lis le deuxième » visent les mails annoncés
        # Après l'annonce, ORION écoute : « oui », « lis-le » lisent le mail (le premier s'il y en a plusieurs).
        offer = {"tool": "read_mail", "parameters": {} if len(kept) == 1 else {"position": 1}}
        self._announce("Nouveau mail", self.sentence(kept), offer)

    def sentence(self, kept: list[MailMessage]) -> str:
        kind = "important" if self.level == "important" else ""
        if len(kept) == 1:
            m = kept[0]
            subject = spoken(m.subject, 80)
            return (f"{self._title.capitalize()}, nouveau mail {kind} de {spoken(m.who, 40)}".replace("  ", " ")
                    + (f" : « {subject} »." if subject else "."))
        senders = ", ".join(dict.fromkeys(spoken(m.who, 30) for m in kept[:4]))
        more = "" if len(kept) <= 4 else f" et {len(kept) - 4} autres"
        kinds = f"{kind}s" if kind else ""
        return f"{self._title.capitalize()}, {len(kept)} nouveaux mails {kinds} : {senders}{more}.".replace("  ", " ")

    def summary(self) -> str:
        """Pour le point du jour : les mails retenus pendant la nuit ou l'absence, puis la liste est vidée."""
        with self._lock:
            pending, self.pending = self.pending, []
        if not pending:
            return ""
        if len(pending) == 1:
            p = pending[0]
            return f"Un mail important est arrivé, de {p['sender']}" + (f" : « {p['subject']} »." if p["subject"] else ".")
        senders = ", ".join(dict.fromkeys(p["sender"] for p in pending[:4]))
        return f"{len(pending)} mails importants sont arrivés : {senders}."

    def on_arrival(self, event: Event) -> None:
        """Retour à la maison : le résumé « Bon retour » (journal) les a cités ; rien à redire au point du jour."""
        with self._lock:
            self.pending = []

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._seen), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            log.warning("Mails : état de la surveillance non enregistré (%s)", exc)

    # --- Fil -----------------------------------------------------------------------------------------

    def start(self) -> None:
        if self.interval <= 0 or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="mails", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        self._stop.wait(20)  # laisse ORION démarrer
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.check()
            except Exception:  # la surveillance ne doit jamais s'arrêter sur une erreur imprévue
                log.exception("Mails : erreur de surveillance")
            self._stop.wait(max(30.0, self.interval - (time.monotonic() - started)))
