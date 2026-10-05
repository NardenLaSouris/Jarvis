"""Correction des commandes mal transcrites (« ou vos teams » -> « ouvre steam »).

Comparaison phonétique approximative entre la phrase entendue et les commandes connues
(verbe d'action + application). Prudente par construction :
- seules les phrases courtes sont examinées ;
- avec un verbe d'action reconnu tel quel, seul le nom de l'application est corrigé, s'il est très proche ;
- sans verbe reconnu, toute la phrase doit ressembler nettement à une commande, et nettement plus
  qu'à toute autre ;
sinon la phrase est laissée intacte.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Callable, Iterable, Mapping

from jarvis.personality import normalize

SOUND_RULES = (
    (r"eau|au", "o"), (r"ph", "f"), (r"qu", "k"), (r"chr", "kr"), (r"ck", "k"), (r"c(?=[aoulr])", "k"),
    (r"ee|ea", "i"), (r"y", "i"), (r"w", "v"), (r"x", "ks"), (r"h", ""),
)
ARTICLES = {"le", "la", "les", "l", "un", "une", "moi"}
POLITE = ("s il te plait", "s il vous plait", "stp", "svp", "merci")
# Verbes d'autres commandes, bien entendus : jamais pris pour un « ouvre » mal transcrit (« Joue Spotify » devenait
# « ouvre spotify » et ouvrait l'application au lieu de reprendre la musique).
OTHER_VERBS = {"joue", "jouer", "rejoue", "mets", "met", "mettre", "remets", "relance", "reprends", "reprend", "coupe",
               "monte", "baisse", "passe", "allume", "eteins", "rallume", "cherche", "trouve", "lis", "supprime",
               "copie", "deplace", "cree", "verrouille", "pause", "arrete", "stoppe", "ecoute", "change"}


def sound(word: str) -> str:
    """Clé sonore d'un mot : graphies équivalentes rapprochées, finales muettes et lettres doublées retirées."""
    for pattern, replacement in SOUND_RULES:
        word = re.sub(pattern, replacement, word)
    if len(word) > 2:
        word = re.sub(r"(es|s|e|x)$", "", word)
    word = re.sub(r"[cg]$", "k", word)
    return re.sub(r"(.)\1+", r"\1", word)


def sound_key(words: Iterable[str]) -> str:
    return "".join(sound(w) for w in words)


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


class CommandCorrector:
    """``verbs`` : action -> formes du verbe (« ouvre » : ouvre, lance...) ; ``objects`` : nom dit -> nom canonique.

    ``rewrite`` normalise la phrase avant comparaison (par défaut ``normalize`` ; en service, la
    normalisation de la personnalité, qui corrige aussi « Jervis » en « jarvis »).
    """

    def __init__(self, verbs: Mapping[str, Iterable[str]], objects: Mapping[str, str], ignored: Iterable[str] = (),
                 phrase_threshold: float = 0.78, object_threshold: float = 0.88, margin: float = 0.06,
                 max_words: int = 5, rewrite: Callable[[str], str] = normalize):
        self._verbs = {normalize(form): action for action, forms in verbs.items() for form in forms}
        self._objects = {normalize(name): canonical for name, canonical in objects.items()}
        self._ignored = {normalize(w) for w in ignored}
        self._phrase_threshold = phrase_threshold
        self._object_threshold = object_threshold
        self._margin = margin
        self._max_words = max_words
        self._rewrite = rewrite
        self._object_keys = [(sound_key(name.split()), canonical) for name, canonical in self._objects.items()]
        self._phrases = [
            (sound_key(f"{form} {name}".split()), action, canonical, f"{form} {name}")
            for form, action in self._verbs.items() if " " not in form
            for name, canonical in self._objects.items()
        ]

    def correct(self, text: str) -> str:
        words = self._core_words(text)
        if not words or len(words) > self._max_words:
            return text
        with_verb = self._with_known_verb(words)
        if with_verb is not None:
            return with_verb if with_verb != " ".join(words) else text
        if words[0] in OTHER_VERBS:
            return text
        return self._whole_phrase(words) or text

    def _core_words(self, text: str) -> list[str]:
        norm = f" {self._rewrite(text)} "
        for phrase in POLITE:
            norm = norm.replace(f" {phrase} ", " ")
        return [w for w in norm.split() if w not in self._ignored]

    def _with_known_verb(self, words: list[str]) -> str | None:
        for size in (2, 1):
            verb = " ".join(words[:size])
            if verb in self._verbs and len(words) > size:
                rest = [w for w in words[size:] if w not in ARTICLES]
                if not rest:
                    return None
                if " ".join(rest) in self._objects:
                    return " ".join(words)
                key = sound_key(rest)
                scored = sorted(((similarity(key, k), c) for k, c in self._object_keys), reverse=True)
                best = scored[0] if scored else (0.0, "")
                if best[0] >= self._object_threshold:
                    return f"{verb} {best[1]}"
                return " ".join(words)
        return None

    def _whole_phrase(self, words: list[str]) -> str | None:
        key = sound_key(words)
        best: dict[tuple[str, str], tuple[float, str]] = {}
        for phrase_key, action, canonical, phrase in self._phrases:
            score = similarity(key, phrase_key)
            if score > best.get((action, canonical), (0.0, ""))[0]:
                best[(action, canonical)] = (score, phrase)
        ranked = sorted(best.values(), reverse=True)
        if not ranked or ranked[0][0] < self._phrase_threshold:
            return None
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < self._margin:
            return None
        return ranked[0][1]
