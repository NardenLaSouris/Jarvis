"""Touches multimédia du PC : lecture/pause, morceau suivant, précédent, arrêt.

Elles pilotent le lecteur actif (application Spotify, navigateur, VLC...) sans compte ni autorisation : c'est
l'équivalent des touches du clavier. Sous Windows, ``keybd_event`` (user32) ; sous Linux, ``playerctl`` s'il est
installé. Avec des appareils configurés, ces outils s'exécutent sur le PC visé par son agent.
La touche lecture/pause bascule : sans retour du lecteur, JARVIS dit « lecture/pause » et non « en pause ».
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Callable

from jarvis.tools.base import EXECUTION_FAILED, Risk, Tool, ToolError

KEYS = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2}
PLAYERCTL = {"play_pause": "play-pause", "next": "next", "previous": "previous", "stop": "stop"}


def press_media_key(action: str) -> None:
    if sys.platform == "win32":
        import ctypes

        user32 = ctypes.windll.user32
        code = KEYS[action]
        user32.keybd_event(code, 0, 0, 0)
        user32.keybd_event(code, 0, 2, 0)  # KEYEVENTF_KEYUP
        return
    if shutil.which("playerctl") is None:
        raise ToolError(EXECUTION_FAILED, "Aucun lecteur multimédia ne peut être piloté sur cette machine.")
    done = subprocess.run(["playerctl", PLAYERCTL[action]], capture_output=True, timeout=5, shell=False)
    if done.returncode != 0:
        raise ToolError(EXECUTION_FAILED, "Aucun lecteur multimédia n'est ouvert.")


def media_tools(press: Callable[[str], None] | None = None) -> list[Tool]:
    def key(action: str):
        def run() -> dict:
            (press or globals()["press_media_key"])(action)  # cherchée à l'appel : remplaçable dans les tests
            return {"action": action}
        return run

    return [
        Tool("media_play_pause", "Met en pause ou reprend la musique ou la vidéo en cours (touche lecture/pause).",
             {}, {"action": "touche"}, Risk.SAFE, key("play_pause"), say=lambda r: "Lecture ou pause, c'est fait."),
        Tool("media_next", "Passe au morceau suivant.", {}, {"action": "touche"}, Risk.SAFE, key("next"),
             say=lambda r: "Morceau suivant."),
        Tool("media_previous", "Revient au morceau précédent.", {}, {"action": "touche"}, Risk.SAFE, key("previous"),
             say=lambda r: "Morceau précédent."),
    ]
