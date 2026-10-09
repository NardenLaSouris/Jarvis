"""Banc de bout en bout du mot de réveil, silencieux (fichiers seulement, rien n'est joué ni enregistré).

Comme ORION en service : détecteur openWakeWord image par image (80 ms), seuil et nombre d'images consécutives
(« patience »), puis seconde vérification Whisper « tiny » sur les 2 dernières secondes. Voix de test jamais vues à
l'entraînement, dans des conditions plus dures que l'évaluation de l'entraînement :

  - positifs : calme, bruit, bruit fort (0-5 dB), distance, loin et bruité, voix très variées (hauteur, débit) ;
  - faux réveils : mots pièges un par un, homophones et quasi-homophones synthétisés (« nous aurions »,
    « Marion »...), long flux de phrases ordinaires bruitées (fausses alertes par heure).

Une grille de réglages est évaluée d'un coup ; le rapport (JSON) donne, pour chacun, la détection par condition et
les faux réveils, et recommande le réglage qui détecte le mieux sans dépasser les plafonds de faux réveils.

    python -m wakeword_training e2e --spec wakeword_training/specs/orion_fr.toml --workers 12
"""

from __future__ import annotations

import json
import random
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from jarvis.audio.files import read_wav
from jarvis.wakeword.openwakeword import FRAME_SAMPLES
from wakeword_training.augment import SR, NoiseBank
from wakeword_training.evaluate import REFRACTORY_FRAMES, _detector, _stream_scores
from wakeword_training.features import make_scene
from wakeword_training.spec import DATA, MODELS_DIR, Spec

THRESHOLDS = (0.3, 0.4, 0.5, 0.55, 0.6, 0.65, 0.7, 0.8, 0.9)
PATIENCES = (1, 2, 3)
GRID = [(t, p) for t in THRESHOLDS for p in PATIENCES]
WINDOW = int(2.0 * SR)  # audio relu par la vérification (WAKE_WINDOW d'ORION)
CONDITIONS = {
    "calme": {"clean_probability": 1.0, "far_probability": 0.0, "snr_db": [20.0, 30.0], "pitch_factor": [1.0, 1.0]},
    "bruit": {"clean_probability": 0.0, "far_probability": 0.0, "snr_db": [5.0, 15.0], "pitch_factor": [1.0, 1.0]},
    "bruit_fort": {"clean_probability": 0.0, "far_probability": 0.0, "snr_db": [0.0, 5.0], "pitch_factor": [1.0, 1.0]},
    "distance": {"clean_probability": 0.0, "far_probability": 1.0, "snr_db": [15.0, 25.0], "pitch_factor": [1.0, 1.0]},
    "loin_bruit": {"clean_probability": 0.0, "far_probability": 1.0, "snr_db": [5.0, 10.0], "pitch_factor": [1.0, 1.0]},
    "voix_variees": {"clean_probability": 0.4, "far_probability": 0.3, "snr_db": [10.0, 30.0],
                     "pitch_factor": [0.8, 1.25]},
}
NEGATIVE_CONDITIONS = ("calme", "bruit")
# Phrases piégeuses dites par les voix de test (synthèse) : homophones exacts et mots proches en fin de phrase.
TRAPS = [
    "Nous aurions dû partir plus tôt.", "Aurions-nous le temps ?", "Nous aurons fini demain.",
    "Marion arrive ce soir.", "Regarde l'horizon.", "Le rayon frais est au fond.", "Un avion passe.",
    "Il fait un temps d'orage.", "Ça vient d'Orient.", "C'est une région magnifique.", "J'ai une opinion.",
    "Oriane est là.", "On y va, Brian.", "Un million de fois.", "Bonjour Dorian.", "Ce camion est garé.",
]


def first_trigger(scores: np.ndarray, threshold: float, patience: int, start: int = 0) -> int | None:
    """Image où le réveil serait retenu (seuil franchi ``patience`` images d'affilée), à partir de ``start``."""
    run = 0
    for i in range(start, len(scores)):
        run = run + 1 if scores[i] >= threshold else 0
        if run >= patience:
            return i
    return None


def all_triggers(scores: np.ndarray, threshold: float, patience: int) -> list[int]:
    out, i = [], 0
    while (k := first_trigger(scores, threshold, patience, i)) is not None:
        out.append(k)
        i = k + REFRACTORY_FRAMES
    return out


_verifier = None


def _verify(audio: np.ndarray, frame: int, phrase: str) -> tuple[bool, str]:
    global _verifier
    if _verifier is None:
        from jarvis.wakeword.verify import WakeVerifier

        _verifier = WakeVerifier("tiny", MODELS_DIR.parent / "whisper", "fr", phrase)
    end = (frame + 1) * FRAME_SAMPLES
    return _verifier.check(audio[max(0, end - WINDOW) : end], SR)


def _noise(babble: list[str], rng: np.random.Generator) -> NoiseBank:
    return NoiseBank([read_wav(Path(p))[0] for p in babble], [DATA / "noise"], rng)


def _scene_task(task: tuple) -> list[dict]:
    """Un lot de scènes (positives ou pièges) : pour chaque réglage, déclenché ? confirmé par Whisper ?"""
    model, phrase, items, condition, babble, seed, kind = task
    rng = np.random.default_rng(seed)
    detector, noise = _detector(Path(model)), _noise(babble, rng)
    out = []
    for item in items:
        clip = read_wav(Path(item))[0] if isinstance(item, str) else np.asarray(item, dtype=np.int16)
        audio, start, end = make_scene(clip, rng, noise, CONDITIONS[condition])
        detector.reset()
        scores = _stream_scores(detector, audio)
        lo = max(0, start // FRAME_SAMPLES - 3) if kind == "pos" else 0
        cache: dict[int, tuple[bool, str]] = {}
        result = {"peak": float(scores[lo:].max()) if len(scores) > lo else 0.0, "grid": {}}
        for t, p in GRID:
            k = first_trigger(scores, t, p, lo)
            if k is not None and kind == "pos" and k * FRAME_SAMPLES > end + SR:  # trop tard : pas ce mot
                k = None
            if k is None:
                result["grid"][f"{t}/{p}"] = (False, False)
                continue
            if k not in cache:
                cache[k] = _verify(audio, k, phrase)
            result["grid"][f"{t}/{p}"] = (True, cache[k][0])
        out.append(result)
    return out


def _stream_task(task: tuple) -> dict:
    """Long flux de phrases ordinaires bruitées : réveils par réglage, avant et après vérification."""
    model, phrase, paths, babble, seed = task
    rng = np.random.default_rng(seed)
    detector, noise = _detector(Path(model)), _noise(babble, rng)
    aug = {"clean_probability": 0.3, "far_probability": 0.3, "snr_db": [5.0, 30.0], "pitch_factor": [1.0, 1.0]}
    audio = np.concatenate([make_scene(read_wav(Path(p))[0], rng, noise, aug)[0] for p in paths])
    detector.reset()
    scores = _stream_scores(detector, audio)
    cache: dict[int, bool] = {}
    counts = {}
    for t, p in GRID:
        ks = all_triggers(scores, t, p)
        for k in ks:
            if k not in cache:
                cache[k] = _verify(audio, k, phrase)[0]
        counts[f"{t}/{p}"] = (len(ks), sum(cache[k] for k in ks))
    return {"seconds": len(audio) / SR, "counts": counts}


def _trap_clips(spec: Spec, voices: list[tuple[str, int]], per_voice: int = 3) -> list[np.ndarray]:
    from wakeword_training.generate import _render, _voice

    rng, clips = random.Random(7), []
    for name, speaker in voices:
        voice = _voice(name)
        for text in TRAPS:
            for _ in range(per_voice):
                audio = _render(voice, speaker, text, None, 0.0, rng)
                if audio is not None:
                    clips.append(audio)
    return clips


def run(spec: Spec, workers: int, model: Path | None = None, out: Path | None = None) -> dict:
    model = model or MODELS_DIR / f"{spec.name}.onnx"
    rows = json.loads((spec.clips_dir / "manifest.json").read_text(encoding="utf-8"))
    test = [r for r in rows if r["split"] == "test"]
    pos = [r["path"] for r in test if r["label"] == "pos"]
    hard = [r["path"] for r in test if r["label"] == "neg" and r.get("hard")]
    ordinary = [r["path"] for r in test if r["label"] == "neg" and not r.get("hard")]
    babble = random.Random(1).sample([r["path"] for r in rows if r["label"] == "neg" and r["split"] == "train"
                                      and not r.get("hard")], 200)
    test_voices = [(v.split(":")[0], int(v.split(":")[1]) if ":" in v else 0) for v in spec["voices"]["test_voices"]]
    traps = _trap_clips(spec, test_voices)
    print(f"Banc de bout en bout de {model.name} : {len(pos)} « {spec.phrase} » x {len(CONDITIONS)} conditions, "
          f"{len(hard)} mots pièges, {len(traps)} phrases piégeuses, {len(ordinary)} phrases ordinaires", flush=True)

    def chunks(items, n):
        return [items[i::n] for i in range(n) if items[i::n]]

    jobs, labels = [], []
    for condition in CONDITIONS:
        for i, part in enumerate(chunks(pos, workers)):
            jobs.append((str(model), spec.phrase, part, condition, babble, 100 + i, "pos"))
            labels.append(("pos", condition))
    for condition in NEGATIVE_CONDITIONS:
        for i, part in enumerate(chunks(hard, workers)):
            jobs.append((str(model), spec.phrase, part, condition, babble, 200 + i, "hard"))
            labels.append(("hard", condition))
        for i, part in enumerate(chunks([t.astype(np.int16) for t in traps], workers)):
            jobs.append((str(model), spec.phrase, part, condition, babble, 300 + i, "trap"))
            labels.append(("trap", condition))
    streams = [(str(model), spec.phrase, part, babble, 400 + i) for i, part in enumerate(chunks(ordinary, workers))]

    results: dict[tuple[str, str], list[dict]] = {}
    with ProcessPoolExecutor(workers) as pool:
        for label, res in zip(labels, pool.map(_scene_task, jobs)):
            results.setdefault(label, []).extend(res)
        stream_res = list(pool.map(_stream_task, streams))

    hours = sum(s["seconds"] for s in stream_res) / 3600
    report = {"model": model.name, "phrase": spec.phrase, "hours_ordinary": round(hours, 2), "grid": {}}
    for t, p in GRID:
        key = f"{t}/{p}"
        entry = {"detection": {}, "detection_model_only": {}}
        for (kind, condition), res in results.items():
            n = len(res) or 1
            detected = sum(r["grid"][key][0] for r in res) / n
            final = sum(r["grid"][key][0] and r["grid"][key][1] for r in res) / n
            if kind == "pos":
                entry["detection"][condition] = round(final, 3)
                entry["detection_model_only"][condition] = round(detected, 3)
            else:
                entry.setdefault(f"false_wakes_{kind}", {})[condition] = round(final, 3)
                entry.setdefault(f"false_triggers_{kind}_model_only", {})[condition] = round(detected, 3)
        raw = sum(s["counts"][key][0] for s in stream_res)
        kept = sum(s["counts"][key][1] for s in stream_res)
        entry["false_wakes_per_hour"] = round(kept / hours, 2) if hours else None
        entry["false_triggers_per_hour_model_only"] = round(raw / hours, 2) if hours else None
        report["grid"][key] = entry

    # Recommandation : la meilleure détection dans le pire cas, sous les plafonds de faux réveils.
    def ok(e):
        traps_max = max([*e.get("false_wakes_trap", {}).values(), 0])
        hard_max = max([*e.get("false_wakes_hard", {}).values(), 0])
        return (e["false_wakes_per_hour"] or 0) <= 0.5 and hard_max <= 0.03 and traps_max <= 0.05

    scored = sorted(((min(e["detection"].values()), sum(e["detection"].values()), k)
                     for k, e in report["grid"].items() if ok(e)), reverse=True)
    report["recommended"] = scored[0][2] if scored else None
    out = out or spec.workdir / "e2e.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n{'réglage':9} " + " ".join(f"{c[:11]:>11}" for c in CONDITIONS) + "   pièges  phrases  FA/h")
    for key, e in report["grid"].items():
        mark = " <- recommandé" if key == report["recommended"] else ""
        print(f"{key:9} " + " ".join(f"{e['detection'][c]:11.0%}" for c in CONDITIONS)
              + f"  {max([*e.get('false_wakes_hard', {}).values(), 0]):6.0%}"
              + f"  {max([*e.get('false_wakes_trap', {}).values(), 0]):7.0%}"
              + f"  {e['false_wakes_per_hour']:5.2f}{mark}")
    print(f"\nRapport : {out}")
    return report
