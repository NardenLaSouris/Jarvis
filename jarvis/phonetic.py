"""Comparaison phonétique de titres et de noms, quelle que soit leur langue.

Whisper transcrit en français : un titre allemand, roumain ou anglais arrive écrit comme il se prononce en
français (« Dou ast » pour « Du hast », « Dragosta dine tei » pour « Dragostea din tei », « Ich vil » pour « Ich
will »). Chaque mot est ramené à une clé sonore simplifiée (sch/sh/ch -> ch, w -> v, ou -> u, lettres muettes et
doubles retirées...) et deux mots sont proches si leurs clés le sont. ``coverage`` dit quelle part des mots demandés
se retrouve dans un candidat.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from functools import lru_cache

from jarvis.personality import normalize

STOP = {"de", "d", "du", "des", "la", "le", "les", "l", "the", "a", "an", "of", "un", "une", "et", "feat", "ft", "remastered",
        "remaster", "version", "edit", "radio", "live"}
RULES = (
    (r"tsch|tch|sch|sh|sz|cz", "ch"), (r"ph", "f"), (r"th", "t"), (r"ck|qu|q", "k"), (r"c(?=[eiy])", "s"), (r"c", "k"),
    (r"w", "v"), (r"eau|au", "o"), (r"oo|ou", "u"), (r"ea", "a"), (r"ei|ey|ay|ai", "e"), (r"ie", "i"), (r"y", "i"),
    (r"z", "s"), (r"x", "ks"), (r"gn", "ni"), (r"h", ""), (r"(.)\1+", r"\1"),
)


@lru_cache(maxsize=4096)
def key(word: str) -> str:
    """Clé sonore d'un mot normalisé (sans accents, minuscules)."""
    out = word
    for pattern, replacement in RULES:
        out = re.sub(pattern, replacement, out)
    if len(out) > 3:
        out = re.sub(r"[estdx]$", "", out)  # finales souvent muettes en français
    return out or word


def words(text: str, drop_stop: bool = True) -> list[str]:
    return [w for w in normalize(text).split() if not drop_stop or w not in STOP]


def close(a: str, b: str) -> bool:
    if a == b:
        return True
    ka, kb = key(a), key(b)
    if ka == kb:
        return True
    if min(len(ka), len(kb)) <= 2:
        return False
    threshold = 0.66 if max(len(ka), len(kb)) <= 4 else 0.75
    return SequenceMatcher(None, ka, kb).ratio() >= threshold


def coverage(query: str, candidate: str) -> float:
    """Part des mots de ``query`` qui ont un équivalent sonore dans ``candidate`` (1.0 si ``query`` est vide)."""
    wanted, found = words(query), words(candidate, drop_stop=False)  # « Du hast » : « du » compte côté titre
    if not wanted:
        return 1.0
    return sum(1 for w in wanted if any(close(w, f) for f in found)) / len(wanted)
