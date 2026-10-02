"""Grandes villes françaises : repli quand une ville entendue n'existe pas (« Allion » mal transcrit pour « Lyon »)."""

from __future__ import annotations

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
