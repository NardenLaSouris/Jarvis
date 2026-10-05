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
from pathlib import Path

from jarvis.personality import normalize

log = logging.getLogger(__name__)

# Teinte de chaque couleur (cercle chromatique, en degrés).
COLORS = {
    "rouge": 0, "corail": 12, "orange": 28, "or": 44, "jaune": 55, "citron": 68, "vert": 130, "emeraude": 155,
    "turquoise": 172, "cyan": 188, "azur": 205, "indigo": 240, "violet": 272, "magenta": 300, "rose": 325,
    "framboise": 342,
}
# day : le bleu d'origine (réglé à la main) ; night : noir et blanc ; arcenciel : toutes les couleurs à tour de rôle.
THEMES = ("auto", "day", "night", "arcenciel", *COLORS)
LABELS = {"auto": "automatique (bleu le jour, noir et blanc la nuit)", "day": "bleu", "night": "noir et blanc",
          "arcenciel": "arc-en-ciel", "emeraude": "émeraude"}
SAID = {
    "automatique": "auto", "normal": "auto", "normale": "auto", "habituel": "auto", "habituelle": "auto",
    "par defaut": "auto", "d origine": "auto", "bleu": "day", "noir et blanc": "night", "blanc": "night",
    "gris": "night", "nuit": "night", "arc en ciel": "arcenciel", "toutes les couleurs": "arcenciel",
    "multicolore": "arcenciel", "dore": "or", "doree": "or", "pourpre": "violet", "mauve": "violet",
    "fuchsia": "magenta", "rose bonbon": "rose", "bleu ciel": "azur", "bleu clair": "azur", "bleu fonce": "indigo",
    "vert d eau": "turquoise", "jaune citron": "citron", "rouge vif": "rouge",
}


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
