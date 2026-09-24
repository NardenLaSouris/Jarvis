"""Lecture d'une spécification de wake word (specs/*.toml) et chemins de travail."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "wakeword"
VOICES_DIR = DATA / "voices"
MODELS_DIR = ROOT / "models" / "openwakeword"


@dataclass(frozen=True)
class Spec:
    name: str
    phrase: str
    raw: dict[str, Any]

    def __getitem__(self, section: str) -> dict[str, Any]:
        return self.raw[section]

    @property
    def workdir(self) -> Path:
        return DATA / self.name

    @property
    def clips_dir(self) -> Path:
        return self.workdir / "clips"

    @property
    def features_dir(self) -> Path:
        return self.workdir / "features"

    @property
    def real_dir(self) -> Path:
        """Enregistrements réels (python -m wakeword_training record)."""
        return self.workdir / "real"

    @property
    def model_path(self) -> Path:
        return MODELS_DIR / f"{self.name}.onnx"

    @property
    def report_path(self) -> Path:
        return MODELS_DIR / f"{self.name}.json"

    def uses_clip(self, row: dict) -> bool:
        """Un clip positif n'est utilisé que si son texte se termine par une graphie actuelle
        (permet d'affiner la liste sans régénérer tous les clips)."""
        if row["label"] != "pos":
            return True
        ending = row["text"].rstrip(" .!?")
        return any(ending.endswith(t.rstrip(" .!?")) for t in self["positives"]["texts"])

    def resolve(self, path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else ROOT / p


def load_spec(path: str | Path) -> Spec:
    with Path(path).open("rb") as fh:
        raw = tomllib.load(fh)
    return Spec(name=raw["name"], phrase=raw["phrase"], raw=raw)
