"""Copie des WAV de référence pour tests/test_wakeword.py.

Les clips proviennent uniquement des locuteurs de TEST de l'entraînement
(jamais vus par le modèle) : quelques « Jarvis » et des phrases pièges.
Usage (après `python -m wakeword_training generate`) : python tests/make_wakeword_fixtures.py
"""

from __future__ import annotations

import json
import random
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wakeword_training.spec import load_spec  # noqa: E402

N_PER_LABEL = 16


def main() -> None:
    spec = load_spec(ROOT / "wakeword_training" / "specs" / "jarvis_fr.toml")
    rows = json.loads((spec.clips_dir / "manifest.json").read_text(encoding="utf-8"))
    rng = random.Random(0)
    out = ROOT / "tests" / "fixtures" / "wakeword"
    for label, sub in (("pos", "positive"), ("neg", "negative")):
        candidates = [r for r in rows if r["split"] == "test" and r["label"] == label and spec.uses_clip(r)]
        if label == "neg":  # d'abord les phrases citées dans la mission et les mots proches
            wanted = {"Bonjour", "J'arrive", "J'ai besoin de quelque chose", "J'avais prévu de partir",
                      "Ça marche", "Merci", "Bonsoir", "Gervais", "Travis", "Service", "J'avais", "Jardin"}
            candidates = [r for r in candidates if r["text"].rsplit(", ", 1)[-1].strip(" .!?") in wanted] or candidates
        rng.shuffle(candidates)
        by_speaker = {}
        for r in candidates:  # un clip par (locuteur, mot) : variété de voix et de mots
            by_speaker.setdefault((r["voice"], r["speaker"], r["text"].rsplit(", ", 1)[-1]), r)
        picks = list(by_speaker.values())[:N_PER_LABEL]
        target = out / sub
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True)
        for r in picks:
            name = f"{r['voice']}_{r['speaker']}_{r['text']}".replace(" ", "_")
            name = "".join(c for c in name if c.isalnum() or c in "_-")
            shutil.copy(r["path"], target / f"{name}.wav")
        print(f"{sub} : {len(picks)} fichiers")


if __name__ == "__main__":
    main()
