"""Grandes villes françaises : repli quand une ville entendue n'existe pas (« Allion » mal transcrit pour « Lyon »),
et ville dont parle la conversation (« je pars à Lyon » : la météo sans ville dite sera celle de Lyon)."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from jarvis.personality import normalize
from jarvis.stt.correction import sound_key

FRENCH_CITIES = (
    "Paris", "Marseille", "Lyon", "Toulouse", "Nice", "Nantes", "Montpellier", "Strasbourg", "Bordeaux", "Lille",
    "Rennes", "Reims", "Toulon", "Saint-Étienne", "Le Havre", "Grenoble", "Dijon", "Angers", "Nîmes", "Villeurbanne",
    "Clermont-Ferrand", "Le Mans", "Aix-en-Provence", "Brest", "Tours", "Amiens", "Limoges", "Annecy", "Perpignan",
    "Metz", "Besançon", "Orléans", "Rouen", "Mulhouse", "Caen", "Nancy", "Avignon", "Poitiers", "La Rochelle", "Pau",
    "Saint-Nazaire", "Vannes", "Lorient", "Quimper", "Cholet", "La Roche-sur-Yon", "Laval", "Saint-Malo",
)


PLACE = r"(?:a|de|d|sur|vers|pour|depuis|pres de|autour de)"


def _mention(city: str) -> re.Pattern:
    name = normalize(city)
    if name.startswith("le "):
        return re.compile(rf"\b(?:au|du|{PLACE} le) {re.escape(name[3:])}\b")
    return re.compile(rf"\b{PLACE} {re.escape(name)}\b")


MENTIONS = tuple((city, _mention(city)) for city in FRENCH_CITIES)


def mentioned_city(text: str) -> str | None:
    """Dernière grande ville citée comme un lieu (« à Lyon », « de Brest », « au Havre ») ; jamais un mot
    courant (« les paris sportifs », « c'est nice »)."""
    norm = normalize(text)
    found = [(match.start(), city) for city, pattern in MENTIONS for match in pattern.finditer(norm)]
    return max(found)[1] if found else None


def closest_city(heard: str, extra: tuple[str, ...] = (), threshold: float = 0.8, margin: float = 0.05) -> str | None:
    """Grande ville qui se prononce presque comme ``heard`` ; None si aucune ne ressort nettement."""
    key = sound_key(normalize(heard).split())
    if len(key) < 3:
        return None
    scores = sorted(((SequenceMatcher(None, key, sound_key(normalize(city).split())).ratio(), city)
                     for city in dict.fromkeys((*extra, *FRENCH_CITIES))), reverse=True)
    best = scores[0]
    if best[0] < threshold or (len(scores) > 1 and best[0] - scores[1][0] < margin):
        return None
    return best[1]
