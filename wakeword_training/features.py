"""Étape 2 : scènes audio augmentées -> fenêtres de caractéristiques pour l'entraînement.

Chaque clip est placé dans une « scène » : du bruit de fond, puis le clip, qui se
termine à au moins 2 s du début. Les embeddings sont calculés par le même code
que la détection en direct (``FeatureExtractor``). Une fenêtre = les 16 derniers
embeddings, soit ce que voit le classifieur à un instant donné.

  - positif : fenêtres se terminant dans les 0 à 240 ms après la fin du mot ;
  - négatif : toutes les fenêtres des clips négatifs, et des scènes de bruit seul.
Les fenêtres où le mot n'est que partiellement présent ne sont pas utilisées.
"""

from __future__ import annotations

import json
import zlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from jarvis.audio.files import read_wav
from jarvis.wakeword.openwakeword import FRAME_SAMPLES, FeatureExtractor
from wakeword_training.augment import SR, NoiseBank, far_field, pitch_shift
from wakeword_training.generate import trim_silence
from wakeword_training.spec import DATA, MODELS_DIR, Spec

WINDOW = 16                 # embeddings par fenêtre (entrée du classifieur)
CLEAN_CONTEXT = 10          # embeddings initiaux pollués par l'initialisation des tampons
MIN_END_S = 2.1             # fin du mot au plus tôt (contexte propre pour toute la fenêtre)
POSITIVE_OFFSETS = (0, 1, 2, 3)   # blocs de 80 ms après la fin du mot
VAL_FRACTION = 0.1
NOISE_ONLY_PROBABILITY = 0.25   # scènes de bruit seul (sans parole) ajoutées aux négatifs


def make_scene(clip: np.ndarray, rng: np.random.Generator, noise: NoiseBank, aug: dict) -> tuple[np.ndarray, int, int]:
    """Retourne (audio int16, début du clip, fin du clip) en échantillons."""
    speech = pitch_shift(clip.astype(np.float32), rng.uniform(*aug["pitch_factor"]))
    far = rng.random() < aug["far_probability"]
    if far:
        speech = far_field(speech, rng)
    level = np.exp(rng.uniform(np.log(300), np.log(4000))) * (rng.uniform(0.2, 0.6) if far else 1.0)
    speech *= level / (np.sqrt(np.mean(speech**2)) + 1e-9)

    end = int(max(MIN_END_S + rng.uniform(0, 0.3), len(speech) / SR + 0.3) * SR)
    total = end + int(0.4 * SR)
    start = end - len(speech)

    scene = rng.normal(0, rng.uniform(3, 30), total)  # souffle du micro
    if rng.random() >= aug["clean_probability"]:
        snr = rng.uniform(*aug["snr_db"])
        scene += noise.sample(total) * level / 10 ** (snr / 20)
    scene[start:end] += speech
    return np.clip(scene, -32768, 32767).astype(np.int16), start, end


def windows(embeddings: np.ndarray, ends: list[int]) -> np.ndarray:
    ends = [k for k in ends if CLEAN_CONTEXT + WINDOW - 1 <= k < len(embeddings)]
    if not ends:
        return np.zeros((0, WINDOW, embeddings.shape[1]), np.float16)
    return np.stack([embeddings[k - WINDOW + 1 : k + 1] for k in ends]).astype(np.float16)


def _process_chunk(task: tuple) -> dict[str, list[np.ndarray]]:
    items, aug, babble_paths, noise_dirs, seed = task
    rng = np.random.default_rng(seed)
    extractor = FeatureExtractor(MODELS_DIR / "melspectrogram.onnx", MODELS_DIR / "embedding_model.onnx")
    noise = NoiseBank([read_wav(Path(p))[0] for p in babble_paths], noise_dirs, rng)
    out: dict[str, list[np.ndarray]] = {"pos": [], "neg": [], "hard": []}
    for path, label, copies in items:
        clip, _ = read_wav(Path(path))
        for _ in range(copies):
            audio, start, end = make_scene(clip, rng, noise, aug)
            emb = extractor.embed_clip(audio)
            if label == "pos":
                last = int(np.ceil(end / FRAME_SAMPLES)) - 1
                out["pos"].append(windows(emb, [last + o for o in POSITIVE_OFFSETS]))
            else:
                out[label].append(windows(emb, list(range(len(emb)))))
        if label == "neg" and rng.random() < NOISE_ONLY_PROBABILITY:
            audio, _, _ = make_scene(np.zeros(SR, np.int16), rng, noise, {**aug, "clean_probability": 0.0})
            out["neg"].append(windows(extractor.embed_clip(audio), list(range(len(audio) // FRAME_SAMPLES))))
    return out


def _real_clips(spec: Spec, split_index: int) -> list[tuple[str, str]]:
    """Enregistrements réels : un sur deux pour l'entraînement, l'autre pour l'évaluation."""
    out = []
    for label, sub in (("pos", "positive"), ("neg", "negative")):
        for i, p in enumerate(sorted((spec.real_dir / sub).glob("*.wav"))):
            if i % 2 == split_index:
                out.append((str(p), label))
    return out


def prepare_real_positive(path: Path, out: Path) -> Path:
    """Recadre un enregistrement réel sur le mot (même convention que les clips synthétiques)."""
    from jarvis.audio.files import write_wav

    audio, rate = read_wav(path)
    write_wav(out, trim_silence(audio, rate, floor_db=-30.0), rate)
    return out


def run(spec: Spec, workers: int) -> None:
    rows = json.loads((spec.clips_dir / "manifest.json").read_text(encoding="utf-8"))
    aug = spec["augmentation"]
    items = {"train": [], "val": []}
    for r in rows:
        if r["split"] != "train" or not spec.uses_clip(r):
            continue
        # Validation (choix de l'époque) tirée par clip : avec peu de voix, un tirage par
        # locuteur retirerait une voix entière de l'entraînement. L'évaluation finale,
        # elle, porte sur des locuteurs jamais vus.
        split = "val" if zlib.crc32(r["path"].encode()) % 1000 < VAL_FRACTION * 1000 else "train"
        if r["label"] == "pos":
            label, copies = "pos", aug["copies_per_positive"]
        elif r.get("hard"):
            label, copies = "hard", aug["copies_per_hard_negative"]
        else:
            label, copies = "neg", aug["copies_per_negative"]
        items[split].append((r["path"], label, copies))

    real = _real_clips(spec, split_index=0)
    if real:
        prepared = spec.workdir / "real_prepared"
        for path, label in real:
            p = Path(path)
            if label == "pos":
                p = prepare_real_positive(p, prepared / p.name)
            # Votre voix réelle : fortement représentée ; vos négatifs comptent comme mots pièges.
            items["train"].append((str(p), "pos" if label == "pos" else "hard", aug["copies_per_positive"] * 3))
        print(f"{len(real)} enregistrements réels ajoutés à l'entraînement")

    babble = [r["path"] for r in rows if r["split"] == "train" and r["label"] == "neg"][:300]
    noise_dirs = [DATA / "noise", spec.real_dir / "ambient"]
    spec.features_dir.mkdir(parents=True, exist_ok=True)

    for split, split_items in items.items():
        rng = np.random.default_rng(0)
        rng.shuffle(split_items)
        chunks = [split_items[i::workers * 4] for i in range(workers * 4)]
        tasks = [(c, aug, babble, noise_dirs, zlib.crc32(f"{split}-{i}".encode())) for i, c in enumerate(chunks) if c]
        collected: dict[str, list[np.ndarray]] = {"pos": [], "neg": [], "hard": []}
        with ProcessPoolExecutor(workers) as pool:
            for i, result in enumerate(pool.map(_process_chunk, tasks), 1):
                for key, arrays in result.items():
                    collected[key] += arrays
                print(f"  {split} : {i}/{len(tasks)} lots", end="\r")
        counts = []
        for key, arrays in collected.items():
            arr = np.concatenate(arrays) if arrays else np.zeros((0, WINDOW, 96), np.float16)
            np.save(spec.features_dir / f"{split}_{key}.npy", arr)
            counts.append(f"{len(arr)} {key}")
        print(f"  {split} : fenêtres " + ", ".join(counts) + " " * 20)
