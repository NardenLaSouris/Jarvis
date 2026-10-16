"""Prépare le jeu de données Piper à partir des enregistrements longs de la voix d'ORION.

  python tts_training/dataset.py build RAW_DIR OUT_DIR [--corpus tts_training/corpus_orion_fr.md]
  python tts_training/dataset.py check OUT_DIR [--all]

Chaque section « ## Titre » du corpus correspond au WAV « N Titre.wav » de RAW_DIR (N = rang de la section).
Whisper (horodatage par mot) sert uniquement à placer les coupes : le texte retenu est celui du corpus,
aligné sur les mots entendus. Sortie : OUT_DIR/wavs/*.wav (22,05 kHz mono) et OUT_DIR/metadata.csv
(« fichier|texte », format d'entraînement Piper).

`check` re-transcrit chaque segment et signale ceux qui s'écartent du texte (coupe trop courte ou trop longue).
Les nombres sont ignorés dans la comparaison : Whisper les écrit en chiffres (« 7h15 »).

Dépendances (environnement d'entraînement, voir train.sh) : openai-whisper, librosa, soundfile.
"""

from __future__ import annotations

import argparse
import difflib
import re
import unicodedata
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
import whisper

SR = 22050
WHISPER_SR = 16000
WHISPER_MODEL = "turbo"
PAD = 0.12          # marge autour de chaque phrase (s)
MIN_MATCH = 0.5     # part minimale des mots d'une phrase retrouvés par Whisper
CHECK_RATIO = 0.85  # en dessous, check signale le segment
CORPUS = Path(__file__).parent / "corpus_orion_fr.md"


def sections(corpus: Path) -> list[list[str]]:
    out: list[list[str]] = []
    for line in corpus.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("## "):
            out.append([])
        elif line and not line.startswith("#") and out:
            out[-1].append(line)
    return out


def tokens(text: str, digits: bool = True) -> list[str]:
    text = unicodedata.normalize("NFKD", text.lower().replace("’", "'"))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.findall(r"[a-z0-9]+" if digits else r"[a-z]+", text)


def _whisper_16k(y: np.ndarray, sr: int) -> np.ndarray:
    # tableau plutôt que chemin : Whisper n'a alors pas besoin de ffmpeg
    return librosa.resample(y, orig_sr=sr, target_sr=WHISPER_SR).astype(np.float32)


def _sentence_spans(model, y: np.ndarray, lines: list[str]) -> tuple[list, str]:
    """(début, fin, bord_début_ouvert, bord_fin_ouvert) par phrase, ou None si la phrase n'est pas retrouvée."""
    result = model.transcribe(_whisper_16k(y, SR), language="fr", word_timestamps=True,
                              initial_prompt=" ".join(lines[:3]), condition_on_previous_text=False)
    # un mot Whisper peut donner plusieurs jetons (« j'allume »)
    hyp, times = [], []
    for seg in result["segments"]:
        for w in seg["words"]:
            for t in tokens(w["word"]):
                hyp.append(t)
                times.append((w["start"], w["end"]))
    ref, owner = [], []
    for k, line in enumerate(lines):
        for t in tokens(line):
            ref.append(t)
            owner.append(k)
    mapping = {}
    for a, b, n in difflib.SequenceMatcher(None, ref, hyp, autojunk=False).get_matching_blocks():
        for i in range(n):
            mapping[a + i] = b + i
    spans = []
    for k in range(len(lines)):
        mine = [i for i, o in enumerate(owner) if o == k]
        found = [mapping[i] for i in mine if i in mapping]
        if not found or len(found) / len(mine) < MIN_MATCH:
            spans.append(None)
            continue
        # premier / dernier mot non retrouvé (nombre écrit en chiffres…) : bord ouvert, étendu jusqu'à la
        # phrase voisine puis recadré sur la parole
        spans.append((times[min(found)][0], times[max(found)][1], mine[0] not in mapping, mine[-1] not in mapping))
    return spans, result["text"]


def build(raw_dir: Path, out_dir: Path, corpus: Path) -> None:
    wav_dir = out_dir / "wavs"
    wav_dir.mkdir(parents=True, exist_ok=True)
    model = whisper.load_model(WHISPER_MODEL)
    rows, skipped = [], []
    all_sections = sections(corpus)
    for idx, lines in enumerate(all_sections, 1):
        src = next(raw_dir.glob(f"{idx} *.wav"), None)
        if src is None:
            skipped.append(f"section {idx} : aucun fichier « {idx} *.wav »")
            continue
        y, _ = librosa.load(src, sr=SR, mono=True)
        spans, heard = _sentence_spans(model, y, lines)
        (out_dir / f"whisper_{idx}.txt").write_text(heard.strip() + "\n", encoding="utf-8")
        for k, (line, span) in enumerate(zip(lines, spans)):
            if span is None:
                skipped.append(f"{idx}.{k + 1} non retrouvée : {line}")
                continue
            start, end, open_start, open_end = span
            # ne pas empiéter sur les phrases voisines
            prev_end = next((s[1] for s in reversed(spans[:k]) if s), 0.0)
            next_start = next((s[0] for s in spans[k + 1:] if s), len(y) / SR)
            a = prev_end + 0.05 if open_start else max(start - PAD, (prev_end + start) / 2)
            b = next_start - 0.05 if open_end else min(end + PAD, (end + next_start) / 2)
            seg = y[int(a * SR): int(b * SR)]
            if open_start or open_end:
                _, (i0, i1) = librosa.effects.trim(seg, top_db=35)
                pad = int(PAD * SR)
                seg = seg[max(0, i0 - pad): i1 + pad]
            seg = seg / max(1e-6, float(np.abs(seg).max())) * 0.9
            name = f"orion_{idx}_{k + 1:02d}.wav"
            sf.write(wav_dir / name, seg, SR, subtype="PCM_16")
            rows.append(f"{name}|{line}")
            print(f"{name}\t{len(seg) / SR:5.2f} s\t{line}")
    (out_dir / "metadata.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"\n{len(rows)} segments sur {sum(map(len, all_sections))} phrases")
    for s in skipped:
        print("ÉCARTÉ", s)


def check(out_dir: Path, show_all: bool) -> None:
    model = whisper.load_model(WHISPER_MODEL)
    flagged = 0
    for row in (out_dir / "metadata.csv").read_text(encoding="utf-8").splitlines():
        name, text = row.split("|", 1)
        y, sr = librosa.load(out_dir / "wavs" / name, sr=None)
        heard = model.transcribe(_whisper_16k(y, sr), language="fr")["text"].strip()
        ratio = difflib.SequenceMatcher(None, tokens(text, False), tokens(heard, False)).ratio()
        if ratio < CHECK_RATIO:
            flagged += 1
        if ratio < CHECK_RATIO or show_all:
            print(f"{'!!' if ratio < CHECK_RATIO else '  '} {ratio:.2f} {name}\n   attendu : {text}\n   entendu : {heard}")
    print(f"{flagged} segment(s) à réécouter")


def main() -> None:
    parser = argparse.ArgumentParser(prog="tts_training/dataset.py")
    sub = parser.add_subparsers(dest="step", required=True)
    b = sub.add_parser("build")
    b.add_argument("raw_dir", type=Path)
    b.add_argument("out_dir", type=Path)
    b.add_argument("--corpus", type=Path, default=CORPUS)
    c = sub.add_parser("check")
    c.add_argument("out_dir", type=Path)
    c.add_argument("--all", action="store_true", help="affiche aussi les segments conformes")
    args = parser.parse_args()
    if args.step == "build":
        build(args.raw_dir, args.out_dir, args.corpus)
    else:
        check(args.out_dir, args.all)


if __name__ == "__main__":
    main()
