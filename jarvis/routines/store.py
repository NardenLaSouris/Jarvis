"""Routines enregistrées dans un fichier JSON, réécrit d'un bloc (fichier temporaire puis remplacement)."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class JsonRoutineStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("Routines illisibles (%s) : %s", self.path, exc)
            raise
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def save(self, routines: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(routines, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


class MemoryRoutineStore:
    def __init__(self, routines: list[dict] | None = None):
        self.routines = list(routines or [])

    def load(self) -> list[dict]:
        return [dict(r) for r in self.routines]

    def save(self, routines: list[dict]) -> None:
        self.routines = [dict(r) for r in routines]
