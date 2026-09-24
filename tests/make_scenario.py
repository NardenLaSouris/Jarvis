"""Régénère ``tests/fixtures/scenario.wav`` (utilisé par test_pipeline_e2e.py).

Le wake word est synthétisé avec une voix anglaise, car le modèle openWakeWord
« hey_jarvis » est entraîné sur la prononciation anglaise. Le même extrait est
réutilisé pour les deux réveils afin que le test porte sur le pipeline et non
sur la variabilité aléatoire de la synthèse.

Usage : python tests/make_scenario.py chemin/vers/en_US-lessac-medium.onnx
(voix : https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/lessac/medium)
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


def main(english_voice: str) -> None:
    en = PiperVoice.load(english_voice)
    fr = PiperVoice.load(load_config(ROOT / "config.toml").tts.voice)

    def say(voice: PiperVoice, text: str) -> np.ndarray:
        audio = np.concatenate([c.audio_int16_array for c in voice.synthesize(text)])
        return resample(audio, voice.config.sample_rate, SR)

    def silence(seconds: float) -> np.ndarray:
        return np.zeros(int(seconds * SR), dtype=np.int16)

    wake = say(en, "Jarvis")
    parts = [
        silence(1.5), wake, silence(1.0),
        say(fr, "Quelle est la capitale de l'Australie ?"), silence(2.0),
        say(fr, "Et combien d'habitants compte cette ville ?"), silence(10),
        wake, silence(1.0),
        say(fr, "Peux-tu ouvrir Firefox et envoyer un mail à Paul ?"), silence(10),
        say(fr, "Bonjour, ceci ne te concerne pas."), silence(2),
    ]
    audio = np.concatenate(parts).astype(np.float32)
    audio += np.random.default_rng(0).normal(0, 40, len(audio))  # léger bruit de fond
    out = ROOT / "tests" / "fixtures" / "scenario.wav"
    write_wav(out, np.clip(audio, -32768, 32767).astype(np.int16), SR)
    print(f"{out} ({len(audio) / SR:.1f} s)")


if __name__ == "__main__":
    main(sys.argv[1])
