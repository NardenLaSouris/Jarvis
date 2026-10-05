"""Réveils capturés en service (jarvis/wakeword/captures.py) -> enregistrements réels de l'entraînement.

- réveil sans suite ou écarté par la vérification : mot piège réel (``real/negative``), le plus utile pour
  apprendre les voix de jeu, de Discord ou de télévision de la maison ;
- réveil suivi d'une demande et confirmé (« Jarvis » entendu) : votre voix (``real/positive``), recadrée sur le mot
  à l'entraînement comme vos enregistrements.

Les fichiers déjà importés ne sont pas recopiés. Le réveil retenu sans vérification (second appel) est ignoré :
son issue ne dit pas si le premier pic était juste.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from jarvis.wakeword.captures import load_captures
from wakeword_training.spec import Spec


def run(spec: Spec, source: Path) -> dict[str, int]:
    counts = {"positive": 0, "negative": 0, "skipped": 0}
    for capture in load_captures(source):
        outcome = capture.get("outcome")
        if outcome in ("silent", "rejected"):
            kind = "negative"
        elif outcome == "used" and capture.get("verified") is True:
            kind = "positive"
        else:
            counts["skipped"] += 1
            continue
        wav = Path(capture["wav"])
        target = spec.real_dir / kind / f"capture_{wav.name}"
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(wav, target)
        counts[kind] += 1
    print(f"Importés : {counts['positive']} « Jarvis » de votre voix, {counts['negative']} faux réveils "
          f"(mots pièges), {counts['skipped']} ignorés -> {spec.real_dir}")
    return counts
