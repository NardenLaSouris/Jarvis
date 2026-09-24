"""Étape 4 : évaluation objective et choix du seuil.

Tout passe par le détecteur utilisé en direct (``OpenWakeWordDetector``), bloc
de 80 ms par bloc de 80 ms. Données utilisées, jamais vues à l'entraînement :
  - clips positifs des locuteurs de test, dans 3 conditions (calme, bruit, distance) ;
  - mots pièges (« J'arrive », « Service »...) des locuteurs de test, un par un ;
  - long flux de phrases ordinaires (locuteurs de test) avec bruit de fond ;
  - 30 % réservés des embeddings externes openWakeWord (~3 h : parole, musique, bruits) ;
  - la moitié des enregistrements réels, s'il y en a (python -m wakeword_training record).

Le seuil retenu est le plus bas qui respecte les deux plafonds de la spécification
(fausses alertes par heure, et part des mots pièges qui déclenchent) : c'est celui
qui détecte le plus sans les dépasser.
"""

from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from jarvis.audio.files import read_wav
from jarvis.audio.resample import resample
from jarvis.wakeword.openwakeword import FRAME_SAMPLES, OpenWakeWordDetector
from wakeword_training.augment import SR, NoiseBank
from wakeword_training.features import make_scene, prepare_real_positive
from wakeword_training.spec import DATA, MODELS_DIR, Spec

THRESHOLDS = np.round(np.arange(0.05, 1.0, 0.01), 2)
REFRACTORY_FRAMES = 25      # ~2 s : après un réveil, JARVIS écoute la demande
CONDITIONS = {
    "calme": {"clean_probability": 1.0, "far_probability": 0.0, "snr_db": [20.0, 30.0]},
    "bruit": {"clean_probability": 0.0, "far_probability": 0.0, "snr_db": [5.0, 15.0]},
    "distance": {"clean_probability": 0.0, "far_probability": 1.0, "snr_db": [15.0, 25.0]},
}


def _detector(model: Path) -> OpenWakeWordDetector:
    return OpenWakeWordDetector(model, MODELS_DIR / "melspectrogram.onnx", MODELS_DIR / "embedding_model.onnx")


def _stream_scores(detector: OpenWakeWordDetector, audio: np.ndarray) -> np.ndarray:
    n = len(audio) // FRAME_SAMPLES
    return np.array([detector.process(f) for f in audio[: n * FRAME_SAMPLES].reshape(n, FRAME_SAMPLES)])


def count_activations(scores: np.ndarray, threshold: float) -> int:
    """Nombre de réveils, avec une période réfractaire comme dans l'agent."""
    count, k = 0, 0
    above = np.nonzero(scores >= threshold)[0]
    for idx in above:
        if idx >= k:
            count += 1
            k = idx + REFRACTORY_FRAMES
    return count


def _positive_task(task: tuple) -> list[float]:
    model, paths, condition, babble, seed = task
    rng = np.random.default_rng(seed)
    detector = _detector(Path(model))
    noise = NoiseBank([read_wav(Path(p))[0] for p in babble], [DATA / "noise"], rng)
    aug = {**CONDITIONS[condition], "pitch_factor": [1.0, 1.0]}
    best = []
    for path in paths:
        clip, _ = read_wav(Path(path))
        audio, start, _ = make_scene(clip, rng, noise, aug)
        detector.reset()
        scores = _stream_scores(detector, audio)
        best.append(float(scores[start // FRAME_SAMPLES :].max()))
    return best


def _negative_stream_task(task: tuple) -> tuple[np.ndarray, float]:
    model, paths, babble, seed = task
    rng = np.random.default_rng(seed)
    detector = _detector(Path(model))
    noise = NoiseBank([read_wav(Path(p))[0] for p in babble], [DATA / "noise"], rng)
    aug = {"clean_probability": 0.3, "far_probability": 0.3, "snr_db": [5.0, 30.0], "pitch_factor": [1.0, 1.0]}
    scenes = [make_scene(read_wav(Path(p))[0], rng, noise, aug)[0] for p in paths]
    stream = np.concatenate(scenes)
    detector.reset()
    return _stream_scores(detector, stream), len(stream) / SR


def _external_scores(spec: Spec, model: Path) -> tuple[np.ndarray, float]:
    import onnxruntime as ort

    t = spec["training"]
    stream = np.load(spec.resolve(t["external_negatives"]), mmap_mode="r")
    held = np.asarray(stream[int(len(stream) * t["external_negatives_train_fraction"]) :], dtype=np.float32)
    session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
    name = session.get_inputs()[0].name
    n_window = session.get_inputs()[0].shape[1]
    windows = np.lib.stride_tricks.sliding_window_view(held, (n_window, held.shape[1]))[:, 0]
    step = 1 if isinstance(session.get_inputs()[0].shape[0], int) else 4096  # lot fixe (ex. hey_jarvis)
    scores = np.concatenate([session.run(None, {name: np.ascontiguousarray(windows[i : i + step])})[0][:, 0]
                             for i in range(0, len(windows), step)])
    return scores, len(held) * FRAME_SAMPLES / SR


def _real(spec: Spec, model: Path) -> dict:
    """Enregistrements réels réservés à l'évaluation (indices impairs)."""
    detector = _detector(model)
    ambient = sorted((spec.real_dir / "ambient").glob("*.wav"))
    lead = (resample(*read_wav(ambient[0]), SR)[: 2 * SR] if ambient
            else np.random.default_rng(0).normal(0, 10, 2 * SR).astype(np.int16))
    out = {}
    positives = sorted((spec.real_dir / "positive").glob("*.wav"))[1::2]
    if positives:
        best = []
        for p in positives:
            clip = read_wav(prepare_real_positive(p, spec.workdir / "real_prepared" / p.name))[0]
            detector.reset()
            best.append(float(_stream_scores(detector, np.concatenate([lead, clip, lead[: SR // 2]]))[len(lead) // FRAME_SAMPLES :].max()))
        out["positive_best_scores"] = best
    streams = sorted((spec.real_dir / "negative").glob("*.wav"))[1::2] + ambient
    if streams:
        audio = np.concatenate([lead] + [resample(*read_wav(p), SR) for p in streams])
        detector.reset()
        out["negative_scores"] = _stream_scores(detector, audio)
        out["negative_seconds"] = len(audio) / SR
    return out


def run(spec: Spec, workers: int, model: Path | None = None, choose_threshold: bool = True) -> dict:
    model = model or spec.model_path
    rows = json.loads((spec.clips_dir / "manifest.json").read_text(encoding="utf-8"))
    test = [r for r in rows if r["split"] == "test" and spec.uses_clip(r)]
    test_pos = [r["path"] for r in test if r["label"] == "pos"]
    hard_rows = [r for r in test if r["label"] == "neg" and r.get("hard")]
    test_neg = [r["path"] for r in test if r["label"] == "neg" and not r.get("hard")]
    babble = test_neg[::3][:150]
    print(f"Évaluation de {model.name} : {len(test_pos)} clips positifs x {len(CONDITIONS)} conditions, "
          f"{len(hard_rows)} mots pièges, {len(test_neg)} phrases ordinaires (locuteurs jamais vus à l'entraînement)")

    recall_scores: dict[str, np.ndarray] = {}
    with ProcessPoolExecutor(workers) as pool:
        for condition in CONDITIONS:
            chunks = [test_pos[i::workers] for i in range(workers)]
            tasks = [(str(model), c, condition, babble, 1000 + i) for i, c in enumerate(chunks) if c]
            recall_scores[condition] = np.concatenate([np.array(r) for r in pool.map(_positive_task, tasks)])
        hard_chunks = [hard_rows[i::workers] for i in range(workers)]
        hard_scores = np.concatenate([np.array(r) for r in pool.map(
            _positive_task, [(str(model), [h["path"] for h in c], "calme", babble, 3000 + i)
                             for i, c in enumerate(hard_chunks) if c])])
        hard_words = [h["text"].rsplit(", ", 1)[-1].strip(" .!?") for c in hard_chunks for h in c]
        chunks = [test_neg[i::workers] for i in range(workers)]
        neg_results = list(pool.map(_negative_stream_task, [(str(model), c, babble, 2000 + i) for i, c in enumerate(chunks) if c]))
    ext_scores, ext_seconds = _external_scores(spec, model)
    real = _real(spec, model)
    syn_seconds = sum(s for _, s in neg_results)

    table = []
    for thr in THRESHOLDS:
        syn_fa = sum(count_activations(s, thr) for s, _ in neg_results)
        ext_fa = count_activations(ext_scores, thr)
        row = {
            "threshold": float(thr),
            **{f"recall_{c}": float(np.mean(v >= thr)) for c, v in recall_scores.items()},
            "fa_per_hour_speech": syn_fa / syn_seconds * 3600,
            "fa_per_hour_external": ext_fa / ext_seconds * 3600,
            "fa_per_hour": (syn_fa + ext_fa) / (syn_seconds + ext_seconds) * 3600,
            "hard_trigger_rate": float(np.mean(hard_scores >= thr)),
        }
        if "positive_best_scores" in real:
            row["recall_real"] = float(np.mean(np.array(real["positive_best_scores"]) >= thr))
        if "negative_scores" in real:
            row["fa_real"] = count_activations(real["negative_scores"], thr)
        table.append(row)

    limit = spec["evaluation"]["max_false_activations_per_hour"]
    hard_limit = spec["evaluation"]["max_hard_negative_trigger_rate"]
    ok = [r for r in table if r["fa_per_hour"] <= limit and r["hard_trigger_rate"] <= hard_limit]
    chosen = ok[0] if ok else table[-1]

    print(f"\nFlux négatifs : {syn_seconds / 60:.0f} min de phrases + {ext_seconds / 3600:.1f} h d'audio externe"
          + (f" + {real['negative_seconds'] / 60:.1f} min d'enregistrements réels" if "negative_seconds" in real else ""))
    header = ("seuil  calme  bruit  distance" + ("  réel" if "recall_real" in chosen else "")
              + "  | fausses alertes/h (parole, externe) | mots pièges" + ("  réel(nb)" if "fa_real" in chosen else ""))
    print(header)
    for r in table:
        if r is chosen or round(r["threshold"] * 100) % 10 == 0:
            line = (f"{r['threshold']:.2f}   {r['recall_calme']:5.1%} {r['recall_bruit']:6.1%} {r['recall_distance']:8.1%}"
                    + (f" {r['recall_real']:6.1%}" if "recall_real" in r else "")
                    + f"  | {r['fa_per_hour']:5.2f} ({r['fa_per_hour_speech']:.2f}, {r['fa_per_hour_external']:.2f})"
                    + f" | {r['hard_trigger_rate']:6.1%}"
                    + (f"  {r['fa_real']}" if "fa_real" in r else "")
                    + ("   <= retenu" if r is chosen else ""))
            print(line)
    print(f"\nSeuil retenu : {chosen['threshold']:.2f} (plus bas seuil avec <= {limit} fausse alerte/h "
          f"et <= {hard_limit:.0%} de mots pièges déclencheurs"
          + ("" if ok else " — PLAFONDS NON ATTEINTS, voir les limites") + ")")
    per_word: dict[str, list[bool]] = {}
    for word, score in zip(hard_words, hard_scores):
        per_word.setdefault(word, []).append(bool(score >= chosen["threshold"]))
    worst = sorted(((float(np.mean(v)), w, len(v)) for w, v in per_word.items()), reverse=True)[:8]
    print("Mots pièges les plus déclencheurs à ce seuil : "
          + (", ".join(f"{w} {rate:.0%} (sur {n})" for rate, w, n in worst if rate > 0) or "aucun"))

    report = {"model": model.name, "threshold": chosen["threshold"], "max_false_activations_per_hour": limit,
              "max_hard_negative_trigger_rate": hard_limit,
              "hard_negative_triggers_at_threshold": {w: rate for rate, w, _ in worst},
              "chosen": chosen, "table": table,
              "test_set": {"positive_clips": len(test_pos), "negative_clips": len(test_neg),
                           "hard_negative_clips": len(hard_rows),
                           "negative_speech_minutes": syn_seconds / 60, "external_hours": ext_seconds / 3600,
                           "real_positive": len(real.get("positive_best_scores", [])),
                           "real_negative_minutes": real.get("negative_seconds", 0) / 60}}
    if choose_threshold:
        existing = json.loads(spec.report_path.read_text(encoding="utf-8")) if spec.report_path.exists() else {}
        spec.report_path.write_text(json.dumps({**existing, "evaluation": report}, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
