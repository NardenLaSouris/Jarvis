"""Échéance d'un mail, décidée par le Core à partir d'une citation exacte du message (proposée par le LLM).

- La citation doit figurer dans le mail (objet ou texte) : une échéance n'est jamais inventée.
- Elle doit dire un délai (« avant », « au plus tard », « d'ici », « sous », « date limite »...) : une date
  seulement mentionnée (« notre réunion du 12 octobre ») n'est pas une échéance.
- Les délais relatifs (« sous 48 heures », « avant vendredi prochain ») partent de la date de réception du mail.
- « Au plus vite », « dès que possible » : urgence, jamais une date.
- Un jour sans heure vaut ce jour à DAY_DEADLINE (18 h, fin de journée de bureau), signalé comme tel.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from orion.personality import normalize
from orion.scheduling.when import parse_when

DAY_DEADLINE = (18, 0)
MARKERS = re.compile(r"\b(?:avant|au plus tard|d ici|sous|dans un delai|delai|date limite|echeance|jusqu au|"
                     r"jusqu a|limite|expire|expiration|au-dela|faute de quoi|passe ce delai|dernier jour|"
                     r"doit etre (?:complete|envoye|transmis|regle|paye|renvoye|retourne)|a regler le|"
                     r"a payer le|a renvoyer le|a retourner le|pour le)\b")
VAGUE = re.compile(r"\b(?:au plus vite|des que possible|rapidement|dans les meilleurs delais|asap|sans tarder|"
                   r"au plus tot|en urgence|urgent)\b")


@dataclass(frozen=True)
class Deadline:
    at: datetime
    quote: str
    explicit: bool  # date ou délai écrits dans le mail (sinon estimation : à confirmer)
    precise: bool  # heure écrite (sinon fin de journée de bureau)


def _found(quote: str, text: str) -> bool:
    words = normalize(quote).split()
    return len(words) >= 2 and " ".join(words) in normalize(text)


def deadline(quote: str, message_text: str, received: datetime, explicit: bool = True) -> Deadline | None:
    """Échéance tirée de ``quote`` si elle est fiable et vérifiable ; None sinon (pas de date devinée)."""
    quote = " ".join(str(quote or "").split())[:300]
    if not quote or not _found(quote, message_text):
        return None
    norm = f" {normalize(quote)} "
    if not MARKERS.search(norm):
        return None  # date seulement mentionnée
    when = parse_when(quote, received)
    if when is None:
        return None
    at = when.at
    if not when.precise:
        at = at.replace(hour=DAY_DEADLINE[0], minute=DAY_DEADLINE[1], second=0, microsecond=0)
    return Deadline(at, quote, explicit, when.precise)


SENTENCES = re.compile(r"(?<=[.!?;\n])\s+")


def scan(message_text: str, received: datetime) -> Deadline | None:
    """Sans citation exploitable du LLM : la première phrase du mail qui dit un délai lisible (déterministe)."""
    for sentence in SENTENCES.split(message_text):
        sentence = " ".join(sentence.split())[:300]
        if MARKERS.search(f" {normalize(sentence)} "):
            found = deadline(sentence, message_text, received)
            if found is not None:
                return found
    return None


def vague(text: str) -> bool:
    """« Au plus vite » et consorts : urgent, sans date."""
    return bool(VAGUE.search(f" {normalize(text)} "))
