"""Banc STT sur le corpus synthétique (research/stt/corpus.py) : WER, latence, texte inventé sans parole, coupures.

    python research/stt/bench.py AUDIO SORTIE.json [--configs a,b] [--subset]
À lancer dans une copie du dépôt sur le mini-PC (processeur, comme le service), ORION au repos.

Le WER compare des textes normalisés (minuscules, sans accents ni ponctuation, nombres écrits en chiffres des deux
côtés) : ce qui est mesuré, c'est l'écart de mots, pas l'orthographe des nombres. Corpus synthétique : utile pour
comparer des réglages entre eux, pas pour annoncer une précision en conditions réelles.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import unicodedata
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, ".")

from jarvis.scheduling.durations import _number, tokens  # noqa: E402

NO_SPEECH = 0.6
CONFIGS = {
    "actuel": {"model": "small", "beam_size": 1, "hotwords": True},
    "sans_indice": {"model": "small", "beam_size": 1, "hotwords": False},
    "sans_horodatage": {"model": "small", "beam_size": 1, "hotwords": True, "without_timestamps": True},
    "beam5": {"model": "small", "beam_size": 5, "hotwords": True},
    "vad": {"model": "small", "beam_size": 1, "hotwords": True, "vad_filter": True},
    "base": {"model": "base", "beam_size": 1, "hotwords": True},
    "fils4": {"model": "small", "beam_size": 1, "hotwords": True, "cpu_threads": 4},
}


def norm(text: str) -> list[str]:
    text = unicodedata.normalize("NFD", text.lower().replace("’", "'"))
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"(\d)\s*h\s*(\d)", r"\1 h \2", text)
    words = tokens(re.sub(r"[^a-z0-9%]+", " ", text).replace("%", " pour cent "))
    out, i = [], 0
    while i < len(words):
        number = _number(words, i) if words[i] not in ("un", "une") or (i + 1 < len(words) and words[i + 1] in
                                                                       ("minute", "minutes", "heure", "heures")) else None
        if number is not None:
            out.append(str(number[0]))
            i += number[1]
        else:
            out.append(words[i])
            i += 1
    text = " ".join(w for w in out if w not in ("euh", "heu"))
    text = re.sub(r"\bheures?\b", "h", text)
    text = re.sub(r"\bh et demie\b", "h 30", text)
    text = re.sub(r"\bh et quart\b", "h 15", text)
    return text.split()


def wer(ref: list[str], hyp: list[str]) -> tuple[int, int]:
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
    return d[len(hyp)], len(ref)


def read(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def recorded_share(audio: np.ndarray) -> float:
    """Part de l'énergie de parole gardée par l'enregistreur d'ORION (début et fin de phrase, pauses d'hésitation)."""
    from jarvis.audio.endpointing import EndpointerSettings, UtteranceRecorder
    from jarvis.audio.files import ArraySource

    scene = np.concatenate([np.zeros(16000, np.int16), audio, np.zeros(32000, np.int16)])
    source = ArraySource(scene, 16000, 1280)
    clip = UtteranceRecorder(source, EndpointerSettings()).record(start_timeout=5.0)
    if clip is None:
        return 0.0
    total = float(np.sum(audio.astype(np.float64) ** 2)) or 1.0
    return min(1.0, float(np.sum(clip.astype(np.float64) ** 2)) / total)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--configs", default=",".join(CONFIGS))
    parser.add_argument("--subset", action="store_true", help="deux voix seulement pour les variantes")
    args = parser.parse_args()
    from faster_whisper import WhisperModel

    from jarvis.config import load_config
    from jarvis.factory import vocabulary_hint

    cfg = load_config("config.toml")
    hint = vocabulary_hint(cfg, "ORION")
    rows = json.loads((args.audio / "references.json").read_text(encoding="utf-8"))
    report = {"hint": hint, "configs": {}}
    shares = [(r, recorded_share(read(args.audio / r["file"]))) for r in rows if r["text"]]
    report["segmentation"] = {
        "moyenne_part_gardee": round(statistics.mean(s for _, s in shares), 3),
        "coupees_sous_95": [r["file"] for r, s in shares if s < 0.95],
        "par_variante": {v: round(statistics.mean(s for r, s in shares if r["variant"] == v), 3)
                         for v in sorted({r["variant"] for r, _ in shares})}}
    print("segmentation :", json.dumps(report["segmentation"]["par_variante"], ensure_ascii=False),
          len(report["segmentation"]["coupees_sous_95"]), "coupée(s)", flush=True)
    for name in args.configs.split(","):
        c = CONFIGS[name]
        model = WhisperModel(c["model"], device="cpu", compute_type="int8", download_root=str(cfg.stt.download_root),
                             cpu_threads=c.get("cpu_threads", 0), local_files_only=True)
        list(model.transcribe(np.zeros(16000, np.float32), language="fr", beam_size=1)[0])
        subset = [r for r in rows if not args.subset or name == "actuel" or r["voice"] in ("fr_FR-siwis-medium:", "fr_FR-upmc-medium:pierre", "siwis", "")]
        results = []
        for r in subset:
            audio = read(args.audio / r["file"]).astype(np.float32) / 32768.0
            started = time.perf_counter()
            segments, _ = model.transcribe(audio, language="fr", beam_size=c["beam_size"], condition_on_previous_text=False,
                                           vad_filter=c.get("vad_filter", False), hotwords=hint if c["hotwords"] else None,
                                           without_timestamps=c.get("without_timestamps", False))
            kept = [s for s in segments if s.no_speech_prob < NO_SPEECH]
            text = " ".join(s.text.strip() for s in kept).strip()
            seconds = time.perf_counter() - started
            errors, words = wer(norm(r["text"]), norm(text)) if r["text"] else (0, 0)
            results.append({**r, "hyp": text, "s": round(seconds, 3), "audio_s": round(len(audio) / 16000, 2),
                            "errors": errors, "words": words, "invented": bool(not r["text"] and text)})
        speech = [x for x in results if x["text"]]
        by = {}
        for key in ("category", "variant", "speed"):
            for value in sorted({x[key] for x in speech}):
                part = [x for x in speech if x[key] == value]
                by[f"{key}:{value}"] = round(sum(x["errors"] for x in part) / max(1, sum(x["words"] for x in part)), 3)
        summary = {"fichiers": len(results), "wer": round(sum(x["errors"] for x in speech) / max(1, sum(x["words"] for x in speech)), 3),
                   "latence_mediane_s": round(statistics.median(x["s"] for x in speech), 2),
                   "latence_p90_s": round(sorted(x["s"] for x in speech)[int(0.9 * len(speech))], 2),
                   "secondes_par_seconde_audio": round(sum(x["s"] for x in speech) / sum(x["audio_s"] for x in speech), 3),
                   "texte_invente_sans_parole": f"{sum(x['invented'] for x in results)}/{sum(1 for x in results if not x['text'])}",
                   "par": by}
        report["configs"][name] = {"summary": summary, "results": results}
        print(name, json.dumps({k: v for k, v in summary.items() if k != "par"}, ensure_ascii=False), flush=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
