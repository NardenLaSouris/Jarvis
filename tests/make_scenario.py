"""Régénère ``tests/fixtures/scenario.wav`` (utilisé par test_pipeline_e2e.py).

Tout est synthétisé avec la voix TTS de JARVIS (fr_FR-tom-medium), qui fait
partie des voix exclues de l'entraînement du wake word : le test n'utilise donc
pas une voix « apprise ». Le wake word est écrit « Jarvisse » pour qu'espeak
prononce le « s » final, comme on le dit naturellement en français. Le même
extrait est réutilisé pour les deux réveils afin que le test porte sur le
pipeline et non sur la variabilité aléatoire de la synthèse.

Usage : python tests/make_scenario.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from piper import PiperVoice

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.audio.files import write_wav  # noqa: E402
from jarvis.audio.resample import resample  # noqa: E402
from jarvis.config import load_config  # noqa: E402

SR = 16000
WAKE_TEXT = "Jarvisse !"


def main() -> None:
    fr = PiperVoice.load(load_config(ROOT / "config.toml").tts.voice)

    def say(text: str) -> np.ndarray:
        audio = np.concatenate([c.audio_int16_array for c in fr.synthesize(text)])
        return resample(audio, fr.config.sample_rate, SR)

    def silence(seconds: float) -> np.ndarray:
        return np.zeros(int(seconds * SR), dtype=np.int16)

    wake = say(WAKE_TEXT)
    parts = [
        silence(2.5), wake, silence(1.0),
        say("Quelle est la capitale de l'Australie ?"), silence(2.0),
        say("Et combien d'habitants compte cette ville ?"), silence(10),
        wake, silence(1.0),
        say("Peux-tu ouvrir Firefox et envoyer un mail à Paul ?"), silence(10),
        say("Bonjour, j'arrive, ceci ne te concerne pas."), silence(2),
    ]
    audio = np.concatenate(parts).astype(np.float32)
    audio += np.random.default_rng(0).normal(0, 40, len(audio))  # léger bruit de fond
    out = ROOT / "tests" / "fixtures" / "scenario.wav"
    write_wav(out, np.clip(audio, -32768, 32767).astype(np.int16), SR)
    print(f"{out} ({len(audio) / SR:.1f} s)")


if __name__ == "__main__":
    main()
