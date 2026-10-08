"""Annonce des rendez-vous : « Monsieur, dentiste dans 15 minutes. »

Une fois par événement (identifiant et heure de début), ``minutes`` avant qu'il commence ; jamais les événements
d'une journée entière ; rien quand la maison est vide (présence). Le titre vient du calendrier (donnée de
l'utilisateur, ou d'un .ics partagé) : il est nettoyé comme un mail (sans lien ni mot de réveil).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Callable

from jarvis.mail.watch import spoken

log = logging.getLogger(__name__)


class EventAnnouncer:
    def __init__(self, calendar, announce: Callable[[str, str], None], minutes: float = 15.0,
                 present: Callable[[], bool | None] | None = None, title: str = "monsieur",
                 clock: Callable[[], datetime] = datetime.now):
        self._calendar = calendar
        self._announce = announce
        self.minutes = minutes
        self._present = present or (lambda: None)
        self._title = title
        self._clock = clock
        self._done: set[tuple[str, str]] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def check(self) -> list[str]:
        """Annonce les événements qui commencent dans ``minutes`` (ou moins) ; rend les phrases dites."""
        now = self._clock()
        try:
            events = self._calendar.events(now, now + timedelta(minutes=self.minutes))
        except Exception as exc:  # calendrier .ics injoignable : on réessaiera à la minute suivante
            log.warning("Rendez-vous : calendrier illisible (%s)", exc)
            return []
        said = []
        for e in events:
            key = (e.id, e.start.isoformat())
            if e.all_day or e.start <= now or key in self._done:
                continue
            self._done.add(key)
            if self._present() is False:
                continue
            left = max(1, round((e.start - now).total_seconds() / 60))
            what = spoken(e.title, 60) or "votre rendez-vous"
            where = f", {spoken(e.location, 40)}" if e.location else ""
            text = f"{self._title.capitalize()}, {what}{where} dans {left} minute{'s' if left > 1 else ''}."
            self._announce("Rendez-vous", text)
            said.append(text)
        if len(self._done) > 500:  # anciens événements oubliés (déjà passés)
            self._done = {k for k in self._done if k[1] >= (now - timedelta(days=1)).isoformat()}
        return said

    def start(self) -> None:
        if self.minutes <= 0 or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="rendez-vous", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(60):
            try:
                self.check()
            except Exception:  # ne s'arrête jamais sur une erreur imprévue
                log.exception("Rendez-vous : erreur d'annonce")
