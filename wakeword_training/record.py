"""Enregistrement d'échantillons réels avec le micro configuré pour JARVIS.

Trois séries, rangées dans data/wakeword/<name>/real/ :
  positive/ : le wake word, à différentes distances et intonations ;
  negative/ : des phrases pièges (proches du wake word) et des phrases ordinaires ;
  ambient/  : l'ambiance de la pièce (silence, TV basse, cuisine...).
La moitié sert à l'entraînement, l'autre moitié à l'évaluation.
"""

from __future__ import annotations

import random
import time
from pathlib import Path

import numpy as np

from jarvis.audio.devices import MicrophoneSource
from jarvis.audio.files import write_wav
from jarvis.config import load_config
from wakeword_training.spec import ROOT, Spec

DISTANCES = ["près du micro (~50 cm)", "à distance normale (~1,5 m)", "loin, depuis l'autre bout de la pièce (~3 m)"]


def _record(mic: MicrophoneSource, seconds: float) -> np.ndarray:
    mic.flush()
    n = int(seconds * mic.sample_rate / mic.frame_samples)
    return np.concatenate([mic.read() for _ in range(n)])


def _countdown(mic: MicrophoneSource, seconds: float) -> np.ndarray:
    print("  ● enregistrement…", end="", flush=True)
    audio = _record(mic, seconds)
    print(f" ok (niveau max {int(np.abs(audio.astype(np.int32)).max())})")
    return audio


def run(spec: Spec, kind: str, count: int) -> None:
    cfg = load_config(ROOT / "config.toml")
    mic = MicrophoneSource(cfg.audio.sample_rate, cfg.audio.frame_samples, cfg.audio.input_device)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = spec.real_dir / kind
    phrase = spec.phrase
    try:
        if kind == "positive":
            print(f"Dites « {phrase} » naturellement, comme pour appeler JARVIS, après avoir appuyé sur Entrée.\n"
                  "Variez l'intonation et la vitesse ; déplacez-vous quand c'est indiqué.")
            for i in range(count):
                where = DISTANCES[min(i * len(DISTANCES) // count, len(DISTANCES) - 1)]
                input(f"[{i + 1}/{count}] {where} — Entrée puis « {phrase} » ")
                write_wav(out / f"{stamp}_{i:03d}.wav", _countdown(mic, 2.5), mic.sample_rate)
        elif kind == "negative":
            texts = spec["negatives"]["hard"] + spec["negatives"]["phrases"]
            picks = random.Random(stamp).sample(texts, min(count, len(texts)))
            print("Lisez chaque phrase naturellement après avoir appuyé sur Entrée.")
            for i, text in enumerate(picks):
                input(f"[{i + 1}/{len(picks)}] « {text} » — Entrée ")
                write_wav(out / f"{stamp}_{i:03d}.wav", _countdown(mic, 3.5), mic.sample_rate)
        elif kind == "ambient":
            input(f"Ambiance : laissez la pièce vivre normalement (TV, conversation, cuisine) "
                  f"sans dire « {phrase} ». Entrée pour enregistrer {count} s ")
            write_wav(out / f"{stamp}.wav", _countdown(mic, count), mic.sample_rate)
        else:
            raise SystemExit(f"Type inconnu : {kind}")
    finally:
        mic.close()
    print(f"Enregistrements dans {Path(out).relative_to(ROOT)}")
