"""Journal des échanges : ce qui a été dit à ORION et ce qu'elle a répondu, par la voix ou par message Linphone, pour
pouvoir le retrouver (« qu'est-ce que je t'ai dit hier sur le garage ? »).

- base SQLite locale (data/journal.sqlite), index plein texte FTS5 : recherche par mots, accents et majuscules
  ignorés ; rien ne quitte le mini-PC ;
- conservation bornée ([memory] journal_days, 90 jours par défaut), purge au démarrage puis chaque jour ;
- désactivable ([memory] journal = false) ; distinct de la mémoire explicite (jarvis.memory), qui seule nourrit le
  prompt : le journal n'est lu que sur demande, par l'outil search_history, réservé au propriétaire.

Une phrase qui ressemble à un secret (mot de passe, code, clé, jeton) n'est pas enregistrée.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from jarvis.personality import normalize
from jarvis.tools.base import Param, Risk, Tool, ToolError

log = logging.getLogger(__name__)

# Événements de l'agent enregistrés : (canal, auteur).
KINDS = {"user": ("voix", "vous"), "assistant": ("voix", "orion"),
         "message": ("message", "vous"), "message_reply": ("message", "orion")}
SECRET = re.compile(r"\b(mot de passe|mdp|password|passwd|code pin|code secret|cle api|api key|jeton|token|"
                    r"identifiant de connexion)\b")
MAX_TEXT = 2000
# Requête SQL (pas un outil : seul le Core exécute des outils, voir tests/test_policy.py).
sql = sqlite3.Connection.execute
MAX_RESULTS = 5


class Journal:
    def __init__(self, path: Path, days: int = 90, clock: Callable[[], datetime] = datetime.now):
        self.days = days
        self._clock = clock
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        sql(self._db, "CREATE VIRTUAL TABLE IF NOT EXISTS echanges USING fts5("
                         "texte, canal UNINDEXED, auteur UNINDEXED, quand UNINDEXED, "
                         "tokenize = 'unicode61 remove_diacritics 2')")
        self._db.commit()
        self._purged = 0.0
        self.purge()

    def add(self, kind: str, text: str) -> bool:
        """Enregistre un événement de l'agent s'il s'agit d'un échange ; False sinon (ou secret écarté)."""
        if kind not in KINDS or not text or not text.strip():
            return False
        if SECRET.search(normalize(text)):
            log.info("Journal : phrase écartée (ressemble à un secret)")
            return False
        channel, author = KINDS[kind]
        with self._lock:
            sql(self._db, "INSERT INTO echanges (texte, canal, auteur, quand) VALUES (?, ?, ?, ?)",
                             (text.strip()[:MAX_TEXT], channel, author, self._clock().isoformat(timespec="seconds")))
            self._db.commit()
        if time.monotonic() - self._purged > 86400:
            self.purge()
        return True

    def purge(self) -> int:
        limit = (self._clock() - timedelta(days=self.days)).isoformat(timespec="seconds")
        with self._lock:
            count = sql(self._db, "DELETE FROM echanges WHERE quand < ?", (limit,)).rowcount
            self._db.commit()
        self._purged = time.monotonic()
        if count:
            log.info("Journal : %d échange(s) de plus de %d jours effacé(s)", count, self.days)
        return count

    def search(self, query: str, days: int | None = None, limit: int = MAX_RESULTS) -> list[dict]:
        """Échanges les plus récents contenant tous les mots de ``query`` (un mot court ou vide est ignoré)."""
        words = [w for w in re.findall(r"[a-z0-9]+", normalize(query)) if len(w) > 2]
        if not words:
            return []
        match = " ".join(f'"{w}"*' for w in words)
        since = (self._clock() - timedelta(days=days)).isoformat(timespec="seconds") if days else ""
        with self._lock:
            rows = sql(self._db, "SELECT texte, canal, auteur, quand FROM echanges WHERE echanges MATCH ? "
                                    "AND quand >= ? ORDER BY quand DESC LIMIT ?", (match, since, limit)).fetchall()
        return [{"text": t, "channel": c, "author": a, "when": q} for t, c, a, q in rows]

    def count(self) -> int:
        with self._lock:
            return sql(self._db, "SELECT count(*) FROM echanges").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _spoken_day(when: str, now: datetime) -> str:
    moment = datetime.fromisoformat(when)
    days = (now.date() - moment.date()).days
    day = "aujourd'hui" if days == 0 else "hier" if days == 1 else f"le {moment.day}/{moment.month}"
    return f"{day} à {moment.hour} h {moment.minute:02d}"


def journal_tools(journal: Journal) -> list[Tool]:
    def search_history(query: str, days: int = 0) -> dict:
        if days < 0 or days > journal.days:
            raise ToolError("invalid_parameters", f"Je ne garde que les {journal.days} derniers jours.")
        found = journal.search(query, days or None)
        return {"query": query, "results": found}

    def said(r: dict) -> str:
        if not r["results"]:
            return f"Je ne retrouve rien sur « {r['query'][:40]} » dans nos derniers échanges."
        now = journal._clock()
        items = " ; ".join(f"{_spoken_day(x['when'], now)}, {'vous' if x['author'] == 'vous' else 'moi'} "
                           f"({x['channel']}) : « {x['text'][:160]} »" for x in r["results"][:3])
        return f"Voici ce que je retrouve : {items}."

    return [Tool("search_history", "Retrouve dans le journal des échanges (voix et messages) ce qui a été dit sur un "
                 "sujet, quand l'utilisateur demande ce qu'il a dit ou demandé auparavant.",
                 {"query": Param(str, "mots du sujet recherché", max_length=80),
                  "days": Param(int, "nombre de jours en arrière, s'il est précisé", required=False)},
                 {"results": "échanges trouvés (texte, canal, auteur, date)"}, Risk.SAFE, search_history, say=said)]
