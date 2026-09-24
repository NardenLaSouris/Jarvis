"""Étape 1 : synthèse et vérification des clips positifs et négatifs (voix Piper).

Les voix Piper sont instables sur des textes très courts : elles peuvent produire
une parole sans rapport avec le texte demandé. Chaque clip est donc :
  - synthétisé soit seul (« Euh, Jarvisse »), soit à la fin d'une phrase longue,
    puis découpé exactement sur le mot grâce à l'alignement des phonèmes ;
  - transcrit par Whisper, clip par clip : un positif n'est gardé que si la
    transcription se termine par le wake word (``verify_pattern``) ; un mot piège
    négatif est écarté si l'on y entend le wake word.
Chaque locuteur est affecté soit à l'entraînement, soit au test : aucun locuteur
de test n'est entendu pendant l'entraînement.
Sortie : data/wakeword/<name>/clips/{train,test}/{pos,neg}/*.wav (16 kHz) + manifest.json.
"""

from __future__ import annotations

import json
import random
import re
import zlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from jarvis.audio.files import write_wav
from jarvis.audio.resample import resample
from wakeword_training.spec import ROOT, VOICES_DIR, Spec

SR = 16000
MAX_ATTEMPTS = 3            # candidats synthétisés au plus par positif demandé
TASK_SIZE = 25              # clips par tâche (les voix à un locuteur sont réparties sur plusieurs processus)
WHISPER_THREADS = 2
_voices: dict[str, object] = {}
_whisper = None


def trim_silence(audio: np.ndarray, sample_rate: int, floor_db: float = -45.0) -> np.ndarray:
    """Retire le silence en début et fin (la fin du clip doit coïncider avec la fin du mot)."""
    hop = sample_rate // 100
    n = len(audio) // hop
    if n == 0:
        return audio
    energy = np.sqrt(np.mean(audio[: n * hop].astype(np.float32).reshape(n, hop) ** 2, axis=1)) + 1e-6
    active = np.nonzero(20 * np.log10(energy / energy.max()) > floor_db)[0]
    if active.size == 0:
        return audio
    return audio[active[0] * hop : (active[-1] + 1) * hop]


def _voice(name: str):
    """Voix Piper avec alignement des phonèmes et un seul thread (un processus par cœur)."""
    import onnx
    import onnxruntime as ort
    from piper import PiperVoice
    from piper.patch_voice_with_alignment import add_alignment_output

    if name not in _voices:
        path = VOICES_DIR / f"{name}.onnx"
        voice = PiperVoice.load(path)
        model = onnx.load(str(path))
        try:
            add_alignment_output(model)
        except ValueError:
            pass  # voix sans alignement : seul le mode « mot seul » sera possible
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        voice.session = ort.InferenceSession(model.SerializeToString(), options, providers=["CPUExecutionProvider"])
        voice.has_alignment = len(voice.session.get_outputs()) > 1
        _voices[name] = voice
    return _voices[name]


def transcribe(audio: np.ndarray) -> str:
    """Transcription d'un clip seul (les transcriptions groupées se sont révélées peu fiables
    sur des clips très courts : Whisper y perd la synchronisation)."""
    global _whisper
    pad = np.zeros(SR // 4, dtype=np.float32)
    samples = np.concatenate([pad, audio.astype(np.float32) / 32768.0, pad])
    if _whisper is None:
        _whisper = _load_whisper(samples)
    segments, _ = _whisper.transcribe(samples, language="fr", beam_size=1, condition_on_previous_text=False)
    return " ".join(s.text for s in segments).strip()


def _load_whisper(probe: np.ndarray):
    """GPU si disponible (~10x plus rapide ; nécessite cuBLAS 12 dans le PATH), sinon CPU."""
    import ctranslate2
    from faster_whisper import WhisperModel

    common = dict(download_root=str(ROOT / "models" / "whisper"), local_files_only=True)
    if ctranslate2.get_cuda_device_count() > 0:
        try:
            model = WhisperModel("small", device="cuda", compute_type="int8_float16", **common)
            list(model.transcribe(probe, language="fr", beam_size=1)[0])
            return model
        except (RuntimeError, ValueError) as exc:
            print(f"  GPU indisponible pour Whisper ({exc}) : vérification sur CPU", flush=True)
    return WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=WHISPER_THREADS, **common)


def _render(voice, speaker: int, text: str, target: str | None, context_s: float, rng: random.Random) -> np.ndarray | None:
    """Synthétise ``text`` ; si ``target`` est donné, ne garde que ce mot final (+ contexte)."""
    from piper import SynthesisConfig

    config = SynthesisConfig(
        speaker_id=speaker if voice.config.num_speakers > 1 else None,
        length_scale=rng.uniform(0.85, 1.25),
        noise_scale=rng.uniform(0.25, 0.5),
        noise_w_scale=rng.uniform(0.3, 0.6),
    )
    chunks = list(voice.synthesize(text, config, include_alignments=target is not None))
    if not chunks:
        return None
    rate = voice.config.sample_rate
    if target is None:
        audio = np.concatenate([c.audio_int16_array for c in chunks])
    else:
        chunk = chunks[-1]
        if not chunk.phoneme_alignments:
            return None
        wanted = [p for p in voice.phonemize(target)[0] if p not in " .,!?"]
        phones = [a.phoneme for a in chunk.phoneme_alignments]
        ends = np.cumsum([a.num_samples for a in chunk.phoneme_alignments])
        for i in range(len(phones) - len(wanted), -1, -1):
            if phones[i : i + len(wanted)] == wanted:
                start = (ends[i - 1] if i else 0) - int(context_s * rate)
                audio = chunk.audio_int16_array[max(0, start) : ends[i + len(wanted) - 1]]
                break
        else:
            return None
    audio = trim_silence(resample(audio, rate, SR), SR)
    return audio if SR // 5 <= len(audio) <= 6 * SR else None


def _generate_task(task: tuple) -> list[dict]:
    """Une tâche = un lot de positifs OU de négatifs pour un locuteur (reprise possible)."""
    voice_name, speaker, part, kind, split, count, spec_raw, out_dir = task
    tag = f"{voice_name}_{speaker}_{kind}{part}"
    done = Path(out_dir) / "tasks" / f"{tag}.json"
    if done.exists():  # reprise : tâche déjà traitée
        return json.loads(done.read_text(encoding="utf-8"))

    positives, negatives = spec_raw["positives"], spec_raw["negatives"]
    pattern = re.compile(positives["verify_pattern"])
    final_word = re.compile(positives["verify_pattern"] + r"\w*\W*$")
    voice = _voice(voice_name)
    rng = random.Random(tag)
    rows = []

    def save(audio, label, text, transcript, hard=False):
        path = Path(out_dir) / split / label / f"{tag}_{len(rows):03d}.wav"
        write_wav(path, audio, SR)
        rows.append({"path": str(path), "split": split, "label": label, "text": text, "transcript": transcript,
                     "hard": hard, "voice": voice_name, "speaker": speaker})

    if kind == "pos":
        accepted = 0
        for _ in range(count * MAX_ATTEMPTS):
            if accepted >= count:
                break
            word = rng.choice(positives["texts"])
            if voice.has_alignment and rng.random() < 0.5:
                text = rng.choice(positives["carriers"]).format(w=word.rstrip(" .!?"))
                audio = _render(voice, speaker, text, word.rstrip(" .!?"), rng.choice([0.0, 0.0, 0.5]), rng)
            else:
                text = rng.choice(positives["prefixes"]) + word
                audio = _render(voice, speaker, text, None, 0.0, rng)
            if audio is not None:
                transcript = transcribe(audio)
                if final_word.search(transcript.lower()):
                    accepted += 1
                    save(audio, "pos", text, transcript)
    else:
        for _ in range(count):
            if rng.random() < negatives["hard_fraction"]:  # mot piège, vérifié par Whisper
                word = rng.choice(negatives["hard"])
                if voice.has_alignment:
                    text = rng.choice(positives["carriers"]).format(w=word)
                    audio = _render(voice, speaker, text, word, rng.choice([0.0, 0.5]), rng)
                else:
                    text, audio = word, _render(voice, speaker, word, None, 0.0, rng)
                if audio is not None:
                    transcript = transcribe(audio)
                    if not pattern.search(transcript.lower()):  # on y entend le wake word : écarté
                        save(audio, "neg", text, transcript, hard=True)
            else:  # phrase ordinaire : même mal prononcée, elle reste un négatif valable
                text = rng.choice(negatives["phrases"])
                audio = _render(voice, speaker, text, None, 0.0, rng)
                if audio is not None:
                    save(audio, "neg", text, "")

    done.parent.mkdir(parents=True, exist_ok=True)
    done.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return rows


def _speaker_count(voice_name: str) -> int:
    config = json.loads((VOICES_DIR / f"{voice_name}.onnx.json").read_text(encoding="utf-8"))
    return int(config.get("num_speakers", 1))


def _is_test(voices: dict, voice_name: str, speaker: int) -> bool:
    if voice_name in voices["test_voices"] or f"{voice_name}:{speaker}" in voices["test_voices"]:
        return True
    return (_speaker_count(voice_name) >= 10
            and zlib.crc32(f"{voice_name}-{speaker}".encode()) % 1000 < voices["test_speaker_fraction"] * 1000)


def run(spec: Spec, workers: int) -> None:
    voices, positives, negatives = spec["voices"], spec["positives"], spec["negatives"]
    missing = [v for v in voices["files"] if not (VOICES_DIR / f"{v}.onnx").exists()]
    if missing:
        raise SystemExit(f"Voix manquantes : {missing}. Lancez d'abord : python -m wakeword_training download")

    tasks = []
    for voice_name in voices["files"]:
        n_speakers = _speaker_count(voice_name)
        scale = max(1, positives["min_clips_per_voice"] // (positives["clips_per_speaker"] * n_speakers))
        for speaker in range(n_speakers):
            split = "test" if _is_test(voices, voice_name, speaker) else "train"
            for kind, section in (("pos", positives), ("neg", negatives)):
                total = section["clips_per_speaker"] * scale
                parts = max(1, total // TASK_SIZE)
                tasks += [(voice_name, speaker, part, kind, split, total // parts, spec.raw, str(spec.clips_dir))
                          for part in range(parts)]

    print(f"Synthèse + vérification Whisper : {len(tasks)} tâches, {workers} processus…", flush=True)
    rows = []
    with ProcessPoolExecutor(workers) as pool:
        for i, result in enumerate(pool.map(_generate_task, tasks), 1):
            rows.extend(result)
            if i % 10 == 0 or i == len(tasks):
                print(f"  {i}/{len(tasks)} tâches", flush=True)
    (spec.clips_dir / "manifest.json").write_text(json.dumps(rows, ensure_ascii=False, indent=0), encoding="utf-8")

    print("Clips retenus (positifs demandés -> validés par Whisper) :")
    for voice_name in voices["files"]:
        wanted = sum(t[5] for t in tasks if t[0] == voice_name and t[3] == "pos")
        mine = [r for r in rows if r["voice"] == voice_name]
        got = {s: sum(1 for r in mine if r["label"] == "pos" and r["split"] == s) for s in ("train", "test")}
        neg = sum(1 for r in mine if r["label"] == "neg")
        print(f"  {voice_name:22} positifs {wanted:5} -> {got['train'] + got['test']:5} "
              f"(entraînement {got['train']}, test {got['test']}) ; négatifs {neg}")
