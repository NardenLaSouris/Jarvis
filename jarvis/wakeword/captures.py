"""Audio de chaque réveil, pour régler le wake word sur la vraie maison et le réentraîner.

Chaque détection enregistre ses deux dernières secondes (``<date>_<score>.wav``) et une fiche JSON : score,
images, vérification (transcription), puis l'issue de la conversation :
- ``used`` : une demande a suivi (vrai « Jarvis ») ;
- ``silent`` : rien n'a suivi (faux réveil probable) ;
- ``rejected`` : écarté par la vérification (jarvis.wakeword.verify).

``python -m jarvis --wake-report`` résume ces fiches et ce que donnerait un autre seuil ;
``python -m wakeword_training import-captures`` les verse dans les enregistrements réels de l'entraînement
(faux réveils comme mots pièges, vrais « Jarvis » comme votre voix). Seuls les ``keep`` derniers sont gardés.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

OUTCOMES = ("used", "silent", "rejected")


class WakeCaptures:
    def __init__(self, folder: Path, keep: int = 300):
        self.folder = Path(folder)
        self.keep = keep

    def save(self, audio: np.ndarray, rate: int, meta: dict) -> Path | None:
        """Enregistre l'audio du déclenchement ; rend le chemin du .wav (None si l'écriture échoue)."""
        from jarvis.audio.files import write_wav

        stamp = datetime.now()
        name = f"{stamp:%Y%m%d-%H%M%S}-{stamp.microsecond // 1000:03d}_{meta.get('score', 0):.2f}"
        path, n = self.folder / f"{name}.wav", 1
        while path.exists():  # deux réveils dans la même milliseconde : jamais l'un écrasé par l'autre
            n += 1
            path = self.folder / f"{name}-{n}.wav"
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            write_wav(path, audio.astype(np.int16), rate)
            path.with_suffix(".json").write_text(json.dumps({"time": stamp.isoformat(timespec="seconds"), **meta},
                                                            ensure_ascii=False), encoding="utf-8")
            self._rotate()
        except OSError as exc:
            log.warning("Capture du wake word impossible : %s", exc)
            return None
        return path

    def finish(self, path: Path | None, outcome: str) -> None:
        """Issue de la conversation ouverte par ce réveil."""
        if path is None:
            return
        sidecar = path.with_suffix(".json")
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
            meta["outcome"] = outcome
            sidecar.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        except (OSError, ValueError) as exc:
            log.warning("Capture du wake word %s : issue non notée (%s)", path.name, exc)

    def _rotate(self) -> None:
        wavs = sorted(self.folder.glob("*.wav"))
        for old in wavs[: max(0, len(wavs) - self.keep)]:
            old.unlink(missing_ok=True)
            old.with_suffix(".json").unlink(missing_ok=True)


def load_captures(folder: Path) -> list[dict]:
    out = []
    for sidecar in sorted(Path(folder).glob("*.json")):
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and sidecar.with_suffix(".wav").exists():
            out.append({**meta, "wav": str(sidecar.with_suffix(".wav"))})
    return out


def report(folder: Path) -> str:
    """Bilan des réveils capturés et effet d'un autre seuil (sur le score de chaque déclenchement)."""
    captures = [c for c in load_captures(folder) if c.get("outcome") in OUTCOMES]
    if not captures:
        return f"Aucun réveil capturé dans {folder} pour le moment."
    counts = {o: sum(c["outcome"] == o for c in captures) for o in OUTCOMES}
    lines = [f"{len(captures)} réveils : {counts['used']} suivis d'une demande, {counts['silent']} sans suite, "
             f"{counts['rejected']} écartés par la vérification.", "",
             "seuil   demandes gardées   réveils sans suite évités   écartés par la vérification évités"]
    for threshold in (0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 0.98):
        kept = sum(c["outcome"] == "used" and c.get("score", 0) >= threshold for c in captures)
        silent = sum(c["outcome"] == "silent" and c.get("score", 0) < threshold for c in captures)
        rejected = sum(c["outcome"] == "rejected" and c.get("score", 0) < threshold for c in captures)
        lines.append(f"{threshold:.2f}    {kept:>3} / {counts['used']:<3}          {silent:>3} / {counts['silent']:<3}"
                     f"                   {rejected:>3} / {counts['rejected']}")
    heard = [c.get("heard", "") for c in captures if c["outcome"] == "rejected" and c.get("heard")]
    if heard:
        lines += ["", "Entendu lors des réveils écartés : " + " ; ".join(f"« {h} »" for h in heard[-12:])]
    return "\n".join(lines)
