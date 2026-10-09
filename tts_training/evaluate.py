"""Intelligibilité d'une voix Piper : synthèse de phrases absentes du corpus, puis transcription par Whisper.

  python tts_training/evaluate.py models/piper/fr_FR-orion-medium.onnx [autre.onnx ...] [--out DOSSIER]

Affiche, par voix, la part de mots retrouvés (phrases sans nombres : Whisper les écrit en chiffres) et le temps
de calcul par seconde de voix. Avec --out, les WAV sont conservés
pour l'écoute.
"""

from __future__ import annotations

import argparse
import difflib
import time
import wave
from pathlib import Path

import numpy as np
from piper import PiperVoice

from dataset import WHISPER_MODEL, _whisper_16k, tokens

SENTENCES = [
    "Bonjour monsieur, il fait beau aujourd'hui et la journée s'annonce calme.",
    "J'ai éteint la lumière de la chambre.",
    "Votre minuteur est terminé, monsieur.",
    "Je n'ai pas trouvé de réponse fiable sur Internet.",
    "Souhaitez-vous que je lise vos nouveaux messages ?",
    "La musique est en pause, je reprends quand vous voulez.",
    "Attention, la batterie de l'ordinateur portable est presque vide.",
    "Je suis ORION. Que puis-je faire pour vous ce soir ?",
]


def main() -> None:
    import whisper

    parser = argparse.ArgumentParser(prog="tts_training/evaluate.py")
    parser.add_argument("voices", nargs="+", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    model = whisper.load_model(WHISPER_MODEL)
    for path in args.voices:
        voice = PiperVoice.load(path)
        scores, compute, spoken = [], 0.0, 0.0
        for i, text in enumerate(SENTENCES):
            started = time.perf_counter()
            audio = np.concatenate([c.audio_int16_array for c in voice.synthesize(text)])
            compute += time.perf_counter() - started
            sr = voice.config.sample_rate
            spoken += len(audio) / sr
            if args.out:
                args.out.mkdir(parents=True, exist_ok=True)
                with wave.open(str(args.out / f"{path.stem}_{i}.wav"), "wb") as w:
                    w.setnchannels(1), w.setsampwidth(2), w.setframerate(sr)
                    w.writeframes(audio.tobytes())
            heard = model.transcribe(_whisper_16k(audio.astype(np.float32) / 32768, sr), language="fr")["text"]
            score = difflib.SequenceMatcher(None, tokens(text), tokens(heard)).ratio()
            scores.append(score)
            print(f"  {score:.2f}  {heard.strip()}")
        print(f"{path.name} : intelligibilité {np.mean(scores):.3f}, "
              f"{compute / spoken:.3f} s de calcul par seconde de voix\n")


if __name__ == "__main__":
    main()
