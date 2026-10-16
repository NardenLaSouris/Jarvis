"""Banc de transcription (STT) d'ORION : durée et taux d'erreur par mot (WER) de plusieurs réglages de Whisper.

À lancer sur la machine qui transcrit (le mini-PC), dans une copie de travail, de préférence quand ORION est peu
sollicité (le banc occupe le processeur) :
    nice python bench/stt/run.py --config /opt/orion/config.toml [--audio DOSSIER] [--models small,base]

Deux corpus :
- synthétique (par défaut) : les phrases de phrases.txt dites par trois voix Piper (siwis, tom, upmc), jamais la
  voix d'ORION. Utile pour COMPARER des réglages entre eux, pas pour juger la qualité réelle (voix trop propres) ;
- réel (``--audio``) : vos enregistrements, un .wav 16 kHz mono par phrase et un fichier ``textes.txt`` au format
  « fichier.wav<TAB>texte exact ». Lui seul dit si un réglage convient en vrai (micro, pièce, accent).

Réglages comparés : chaque modèle demandé, avec et sans l'indice de vocabulaire ([stt] vocabulary_hint), avec les
autres paramètres de la configuration (beam_size, repli de température). Rien n'est modifié ni téléchargé.
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orion.audio.resample import resample  # noqa: E402
from orion.config import load_config  # noqa: E402
from orion.factory import vocabulary_hint  # noqa: E402
from orion.personality import normalize  # noqa: E402
from orion.stt.faster_whisper import FasterWhisperSTT  # noqa: E402

HERE = Path(__file__).resolve().parent
VOICES = ("fr_FR-siwis-medium", "fr_FR-tom-medium", "fr_FR-upmc-medium")


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", normalize(text))


def wer(reference: str, hypothesis: str) -> tuple[int, int]:
    """(erreurs, mots de la référence) : distance d'édition mot à mot."""
    ref, hyp = words(reference), words(hypothesis)
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r != h))
    return row[-1], len(ref)


def synthetic(models_dir: Path) -> list[tuple[str, np.ndarray, str]]:
    from orion.tts.piper import PiperTTS

    phrases = [p.strip() for p in (HERE / "phrases.txt").read_text(encoding="utf-8").splitlines()
               if p.strip() and not p.startswith("#")]
    corpus = []
    for name in VOICES:
        path = models_dir / f"{name}.onnx"
        if not path.exists():
            continue
        tts = PiperTTS(path)
        for i, text in enumerate(phrases):
            audio, rate = tts.synthesize(text)
            silence = np.zeros(int(0.3 * 16000), dtype=np.int16)
            corpus.append((f"{name}-{i:02d}", np.concatenate([silence, resample(audio, rate, 16000), silence]), text))
    return corpus


def recorded(folder: Path) -> list[tuple[str, np.ndarray, str]]:
    corpus = []
    for line in (folder / "textes.txt").read_text(encoding="utf-8").splitlines():
        if "\t" not in line:
            continue
        name, text = line.split("\t", 1)
        with wave.open(str(folder / name)) as w:
            if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise SystemExit(f"{name} : attendu 16 kHz, mono, 16 bits")
            corpus.append((name, np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16), text.strip()))
    return corpus


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--audio", type=Path, help="dossier de vos enregistrements (avec textes.txt)")
    parser.add_argument("--models", default="small,base")
    parser.add_argument("--errors", action="store_true", help="afficher chaque transcription fausse")
    args = parser.parse_args()
    cfg = load_config(args.config)
    root = Path(args.config).resolve().parent
    corpus = recorded(args.audio) if args.audio else synthetic(root / "models" / "piper")
    seconds = sum(len(a) for _, a, _ in corpus) / 16000
    print(f"Corpus : {len(corpus)} phrases, {seconds:.0f} s d'audio ({'réel' if args.audio else 'synthétique'})")
    hint = vocabulary_hint(cfg)
    c = cfg.stt
    for model in args.models.split(","):
        stt = FasterWhisperSTT(model, cfg.assistant.language, "cpu", "int8", c.beam_size, root / "models" / "whisper",
                               temperature_fallback=c.temperature_fallback)
        for label, hotwords in (("avec indice", hint), ("sans indice", "")):
            stt.hotwords = hotwords
            durations, errors, total = [], 0, 0
            for name, audio, text in corpus:
                started = time.perf_counter()
                heard = stt.transcribe(audio, 16000)
                durations.append(time.perf_counter() - started)
                e, n = wer(text, heard)
                errors, total = errors + e, total + n
                if args.errors and e:
                    print(f"    {name} : « {text} » -> « {heard} »")
            durations.sort()
            print(f"{model:6} {label:12} WER {100 * errors / max(total, 1):5.1f} %   durée médiane "
                  f"{statistics.median(durations):.2f} s, p90 {durations[int(0.9 * (len(durations) - 1))]:.2f} s")


if __name__ == "__main__":
    main()
