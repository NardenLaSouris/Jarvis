"""Couleur du visage de JARVIS, choisie à la voix (« mets ton visage en vert ») ou dans JARVIS Control.

« auto » : bleu le jour, noir et blanc la nuit ([face] night_start / night_end). Une couleur choisie s'applique
jour et nuit, jusqu'au retour à « auto » ; le rouge d'erreur passe toujours devant. Le choix est enregistré
(``data/face_theme.json``) et lu par le serveur du visage : tous les écrans prennent la même couleur.
La page construit la palette à partir de la teinte (0-360) envoyée par le serveur ; « arcenciel » la fait tourner.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, timedelta
from pathlib import Path

from jarvis.personality import normalize

log = logging.getLogger(__name__)

# Teinte de chaque couleur (cercle chromatique, en degrés).
COLORS = {
    "rouge": 0, "corail": 12, "orange": 28, "or": 44, "jaune": 55, "citron": 68, "vert": 130, "emeraude": 155,
    "turquoise": 172, "cyan": 188, "azur": 205, "indigo": 240, "violet": 272, "magenta": 300, "rose": 325,
    "framboise": 342,
}
# Thèmes de fête, pris d'eux-mêmes en « auto » aux dates dites (ou choisis à la voix pour les essayer) :
# couleurs qui alternent (teintes, « day », « night » ou « rainbow »), message à l'écran, effet animé.
SEASONS = {
    "nouvelan": {"label": "Nouvel an", "hues": [44, "night", 44, 205], "greeting": "Bonne année !",
                 "effect": "sparkle"},
    "saintvalentin": {"label": "Saint-Valentin", "hues": [342, 325], "greeting": "Joyeuse Saint-Valentin",
                      "effect": "hearts"},
    "saintpatrick": {"label": "Saint-Patrick", "hues": [130, 155], "greeting": "Joyeuse Saint-Patrick",
                     "effect": "sparkle"},
    "paques": {"label": "Pâques", "hues": [325, 55, 130, 188], "greeting": "Joyeuses Pâques", "effect": "confetti"},
    "fetemusique": {"label": "Fête de la musique", "hues": "rainbow", "greeting": "Bonne fête de la musique",
                    "effect": "confetti"},
    "quatorzejuillet": {"label": "14 Juillet", "hues": [225, "night", 0], "greeting": "Bonne fête nationale",
                        "effect": "sparkle"},
    "halloween": {"label": "Halloween", "hues": [28, 272], "greeting": "Joyeux Halloween", "effect": "embers"},
    "noel": {"label": "Noël", "hues": [0, 130], "greeting": "Joyeux Noël", "effect": "snow"},
    "anniversaire": {"label": "anniversaire", "hues": "rainbow", "greeting": "Joyeux anniversaire !",
                     "effect": "confetti"},
}
# day : le bleu d'origine (réglé à la main) ; night : noir et blanc ; arcenciel : toutes les couleurs à tour de rôle.
THEMES = ("auto", "day", "night", "arcenciel", *COLORS, *SEASONS)
LABELS = {"auto": "automatique (bleu le jour, noir et blanc la nuit, fêtes)", "day": "bleu",
          "night": "noir et blanc", "arcenciel": "arc-en-ciel", "emeraude": "émeraude",
          **{k: f"thème {v['label']}" if k != "anniversaire" else "thème anniversaire" for k, v in SEASONS.items()}}
SAID = {
    "automatique": "auto", "normal": "auto", "normale": "auto", "habituel": "auto", "habituelle": "auto",
    "par defaut": "auto", "d origine": "auto", "bleu": "day", "noir et blanc": "night", "blanc": "night",
    "gris": "night", "nuit": "night", "arc en ciel": "arcenciel", "toutes les couleurs": "arcenciel",
    "multicolore": "arcenciel", "dore": "or", "doree": "or", "pourpre": "violet", "mauve": "violet",
    "fuchsia": "magenta", "rose bonbon": "rose", "bleu ciel": "azur", "bleu clair": "azur", "bleu fonce": "indigo",
    "vert d eau": "turquoise", "jaune citron": "citron", "rouge vif": "rouge",
    "nouvel an": "nouvelan", "jour de l an": "nouvelan", "reveillon": "nouvelan", "saint valentin": "saintvalentin",
    "saint patrick": "saintpatrick", "paques": "paques", "fete de la musique": "fetemusique",
    "14 juillet": "quatorzejuillet", "quatorze juillet": "quatorzejuillet", "fete nationale": "quatorzejuillet",
    "halloween": "halloween", "noel": "noel", "anniversaire": "anniversaire",
}


def easter(year: int) -> date:
    """Dimanche de Pâques (calendrier grégorien, algorithme de Meeus)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741 - notation de l'algorithme
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def parse_birthday(value: str) -> tuple[int, int] | None:
    """« 07-14 » (mois-jour) -> (7, 14) ; vide -> None."""
    if not value:
        return None
    match = re.fullmatch(r"(\d{1,2})-(\d{1,2})", value.strip())
    if not match:
        raise ValueError(f"[face] birthday : MM-JJ attendu (par exemple 07-14), pas {value!r}")
    month, day = int(match.group(1)), int(match.group(2))
    date(2024, month, day)  # mois ou jour impossible -> ValueError
    return month, day


def season_of(day: date, birthday: tuple[int, int] | None = None) -> str | None:
    """Fête du jour (prise d'elle-même par le visage en « auto »), ou None. L'anniversaire passe avant tout."""
    md = (day.month, day.day)
    if birthday is not None and md == birthday:
        return "anniversaire"
    if md in ((12, 31), (1, 1)):
        return "nouvelan"
    if (12, 20) <= md <= (12, 26):
        return "noel"
    if (10, 29) <= md <= (10, 31):
        return "halloween"
    sunday = easter(day.year)
    if sunday - timedelta(days=1) <= day <= sunday + timedelta(days=1):
        return "paques"
    return {(2, 14): "saintvalentin", (3, 17): "saintpatrick", (6, 21): "fetemusique",
            (7, 14): "quatorzejuillet"}.get(md)


def label(theme: str) -> str:
    return LABELS.get(theme, theme)


def said_theme(text: str) -> str | None:
    """Couleur dite dans la phrase (la plus longue d'abord : « bleu ciel » avant « bleu »), ou None."""
    padded = f" {normalize(text)} "
    names = {**{c: c for c in COLORS}, **SAID}
    for name in sorted(names, key=len, reverse=True):
        if f" {name} " in padded:
            return names[name]
    return None


class FaceThemeStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._cache: tuple[float, str] | None = None

    def get(self) -> str:
        """Thème choisi ; « auto » si rien n'est enregistré ou si le fichier est illisible."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return "auto"
        if self._cache is not None and self._cache[0] == mtime:
            return self._cache[1]
        try:
            theme = json.loads(self.path.read_text(encoding="utf-8")).get("theme", "auto")
        except (OSError, ValueError, AttributeError) as exc:
            log.warning("Couleur du visage illisible (%s) : %s", self.path, exc)
            theme = "auto"
        theme = theme if theme in THEMES else "auto"
        self._cache = (mtime, theme)
        return theme

    def set(self, theme: str) -> str:
        if theme not in THEMES:
            raise ValueError(f"Thème inconnu : {theme!r}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"theme": theme}), encoding="utf-8")
        os.replace(tmp, self.path)
        self._cache = None
        return theme


def face_theme_tool(store: FaceThemeStore):
    """Outil SAFE ``set_face_theme`` : la couleur doit avoir été dite (le LLM n'en choisit pas une au hasard)."""
    from jarvis.tools.base import Param, Risk, Tool

    def run(theme: str) -> dict:
        return {"theme": store.set(theme), "label": label(theme)}

    def say(r: dict) -> str:
        if r["theme"] == "auto":
            return "Je reprends mes couleurs habituelles."
        return f"Je passe en {r['label']}."

    return Tool("set_face_theme",
                "Change la couleur du visage de JARVIS (auto : bleu le jour, noir et blanc la nuit).",
                {"theme": Param(str, "couleur dite (auto pour revenir aux couleurs habituelles)", choices=THEMES,
                                evidence=lambda value, text: said_theme(text) == value)},
                {"theme": "couleur", "label": "nom dit"}, Risk.SAFE, run, say=say)
