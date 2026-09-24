"""Entraînement d'un wake word openWakeWord personnalisé.

  python -m wakeword_training download   # voix Piper et négatifs externes
  python -m wakeword_training generate   # synthèse des clips
  python -m wakeword_training features   # augmentation + caractéristiques
  python -m wakeword_training train      # classifieur -> models/openwakeword/<name>.onnx
  python -m wakeword_training evaluate   # mesures et seuil recommandé
  python -m wakeword_training all        # les 5 étapes ci-dessus
  python -m wakeword_training record positive|negative|ambient   # vos enregistrements
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.request
from pathlib import Path

from wakeword_training.spec import DATA, VOICES_DIR, Spec, load_spec

PIPER_VOICES = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
EXTERNAL_NEGATIVES = ("https://huggingface.co/datasets/davidscripka/openwakeword_features"
                      "/resolve/main/validation_set_features.npy")


def _fetch(url: str, dest: Path) -> None:
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)
    tmp.replace(dest)


def download(spec: Spec) -> None:
    for voice in spec["voices"]["files"]:
        locale, name, quality = voice.split("-")
        url = f"{PIPER_VOICES}/{locale.split('_')[0]}/{locale}/{name}/{quality}/{voice}.onnx"
        _fetch(url, VOICES_DIR / f"{voice}.onnx")
        _fetch(url + ".json", VOICES_DIR / f"{voice}.onnx.json")
    _fetch(EXTERNAL_NEGATIVES, spec.resolve(spec["training"]["external_negatives"]))
    print(f"Données prêtes dans {DATA}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="wakeword_training")
    parser.add_argument("step", choices=["download", "generate", "features", "train", "evaluate", "all", "record"])
    parser.add_argument("kind", nargs="?", help="pour record : positive, negative ou ambient")
    parser.add_argument("--spec", default=str(Path(__file__).parent / "specs" / "jarvis_fr.toml"))
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    parser.add_argument("--count", type=int, help="pour record : nombre de prises (ou secondes pour ambient)")
    parser.add_argument("--model", type=Path, help="pour evaluate : autre modèle à mesurer (ex. hey_jarvis)")
    args = parser.parse_args()
    spec = load_spec(args.spec)

    if args.step == "record":
        from wakeword_training import record

        defaults = {"positive": 30, "negative": 30, "ambient": 120}
        record.run(spec, args.kind or "positive", args.count or defaults.get(args.kind or "positive", 30))
        return 0

    steps = ["download", "generate", "features", "train", "evaluate"] if args.step == "all" else [args.step]
    for step in steps:
        print(f"=== {step} ===")
        if step == "download":
            download(spec)
        elif step == "generate":
            from wakeword_training import generate
            generate.run(spec, args.workers)
        elif step == "features":
            from wakeword_training import features
            features.run(spec, args.workers)
        elif step == "train":
            from wakeword_training import train
            train.run(spec)
        elif step == "evaluate":
            from wakeword_training import evaluate
            evaluate.run(spec, args.workers, model=args.model, choose_threshold=args.model is None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
