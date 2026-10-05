"""Lecture prudente des fichiers JSON de JARVIS (routines, mémoire, échéances, calendrier local).

Un fichier illisible (contenu abîmé, coupure pendant une écriture) ou d'une forme inattendue n'empêche jamais
JARVIS de démarrer, et n'est jamais écrasé par l'enregistrement suivant : il est mis de côté sous
« <nom>.illisible-<date> » (à réparer ou supprimer à la main) et JARVIS repart d'une liste vide.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


def set_aside(path: Path, what: str, reason: object) -> Path | None:
    """Renomme le fichier illisible (jamais supprimé) ; rend le nouveau chemin, ou None si c'est impossible."""
    stamp = f"{path.name}.illisible-{datetime.now():%Y%m%d-%H%M%S}"
    target, n = path.with_name(stamp), 1
    while target.exists():  # jamais une mise de côté précédente écrasée
        n += 1
        target = path.with_name(f"{stamp}-{n}")
    try:
        os.replace(path, target)
    except OSError as exc:
        log.error("%s illisibles (%s) : %s ; mise de côté impossible (%s)", what, path, reason, exc)
        return None
    log.error("%s illisibles (%s) : %s ; fichier mis de côté : %s, JARVIS repart à vide", what, path, reason,
              target.name)
    return target


def load_json_list(path: Path, what: str) -> list:
    """Liste enregistrée dans ``path`` ; [] si le fichier n'existe pas ou a été mis de côté (illisible)."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        if isinstance(exc, UnicodeDecodeError):
            set_aside(path, what, exc)
        else:
            log.error("%s illisibles (%s) : %s", what, path, exc)
        return []
    try:
        data = json.loads(text)
    except ValueError as exc:
        set_aside(path, what, exc)
        return []
    if not isinstance(data, list):
        set_aside(path, what, f"une liste était attendue, pas {type(data).__name__}")
        return []
    return data
