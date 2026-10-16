from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "neutts" / "bench"
VOICE = ROOT / "models" / "neutts" / "voices" / "bernard"
TEXT = "Bonjour monsieur. Je suis JARVIS, votre assistant personnel. Comment puis-je vous être utile ?"
CONFIGS = {
    "q4": ("neuphonic/neutts-nano-french-q4-gguf", "neuphonic/neucodec"),
    "q8": ("neuphonic/neutts-nano-french-q8-gguf", "neuphonic/neucodec"),
    "q4-onnx": ("neuphonic/neutts-nano-french-q4-gguf", "neuphonic/neucodec-onnx-decoder"),
    "q8-onnx": ("neuphonic/neutts-nano-french-q8-gguf", "neuphonic/neucodec-onnx-decoder"),
}
SAMPLE_RATE = 24000


def load_env_token() -> None:
    env = ROOT / ".env"
    if "HF_TOKEN" in os.environ or not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == "HF_TOKEN" and value.strip():
            os.environ["HF_TOKEN"] = value.strip().strip('"').strip("'")


class Monitor:
    def __init__(self):
        import psutil

        self.process = psutil.Process()
        self.peak_rss = 0
        self.cpu_samples: list[float] = []
        self._stop = threading.Event()
        self._thread = None

    def rss_mb(self) -> float:
        return self.process.memory_info().rss / 2**20

    def start(self) -> None:
        self._stop.clear()
        self.cpu_samples = []
        self.process.cpu_percent(None)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(0.1):
            self.peak_rss = max(self.peak_rss, self.process.memory_info().rss)
            self.cpu_samples.append(self.process.cpu_percent(None))


def playback_gaps(ready_times: list[float], durations: list[float]) -> float:
    start = ready_times[0]
    audio_before = 0.0
    gaps = 0.0
    for ready, duration in zip(ready_times, durations):
        expected = start + audio_before + gaps
        if ready > expected:
            gaps += ready - expected
        audio_before += duration
    return gaps


def run_config(name: str) -> dict:
    import numpy as np
    import soundfile as sf
    import torch

    torch.set_num_threads(os.cpu_count() or 1)
    monitor = Monitor()
    base_rss = monitor.rss_mb()
    from neutts import NeuTTS

    backbone, codec = CONFIGS[name]
    monitor.start()
    started = time.perf_counter()
    tts = NeuTTS(backbone_repo=backbone, backbone_device="cpu", codec_repo=codec, codec_device="cpu")
    load_time = time.perf_counter() - started
    monitor.stop()
    load_rss = monitor.rss_mb()

    codes_path = VOICE.with_suffix(".codes.npy")
    ref_text = VOICE.with_suffix(".txt").read_text(encoding="utf-8").strip()
    if codes_path.exists():
        ref_codes = np.load(codes_path)
        encode_time = 0.0
    else:
        started = time.perf_counter()
        ref_codes = np.asarray(tts.encode_reference(VOICE.with_suffix(".wav")))
        encode_time = time.perf_counter() - started
        np.save(codes_path, ref_codes)

    tts.infer("Bonjour.", ref_codes, ref_text)

    monitor.start()
    started = time.perf_counter()
    wav = np.asarray(tts.infer(TEXT, ref_codes, ref_text), dtype=np.float32)
    full_time = time.perf_counter() - started
    monitor.stop()
    full_cpu = sum(monitor.cpu_samples) / max(len(monitor.cpu_samples), 1)
    sf.write(OUT / f"neutts_{name}.wav", wav, SAMPLE_RATE)

    monitor.start()
    started = time.perf_counter()
    ready, durations, chunks = [], [], []
    for chunk in tts.infer_stream(TEXT, ref_codes, ref_text):
        chunk = np.asarray(chunk, dtype=np.float32).reshape(-1)
        ready.append(time.perf_counter() - started)
        durations.append(len(chunk) / SAMPLE_RATE)
        chunks.append(chunk)
    stream_time = time.perf_counter() - started
    monitor.stop()
    stream_cpu = sum(monitor.cpu_samples) / max(len(monitor.cpu_samples), 1)
    streamed = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
    sf.write(OUT / f"neutts_{name}_stream.wav", streamed, SAMPLE_RATE)

    audio = len(wav) / SAMPLE_RATE
    stream_audio = len(streamed) / SAMPLE_RATE
    return {
        "config": name,
        "backbone": backbone,
        "codec": codec,
        "load_s": round(load_time, 2),
        "encode_reference_s": round(encode_time, 2),
        "rss_base_mb": round(base_rss),
        "rss_loaded_mb": round(load_rss),
        "rss_peak_mb": round(monitor.peak_rss / 2**20),
        "full_total_s": round(full_time, 2),
        "full_audio_s": round(audio, 2),
        "full_rtf": round(full_time / audio, 3) if audio else None,
        "full_cpu_percent": round(full_cpu),
        "stream_first_chunk_s": round(ready[0], 2) if ready else None,
        "stream_total_s": round(stream_time, 2),
        "stream_audio_s": round(stream_audio, 2),
        "stream_rtf": round(stream_time / stream_audio, 3) if stream_audio else None,
        "stream_chunks": len(chunks),
        "stream_gaps_s": round(playback_gaps(ready, durations), 2) if ready else None,
        "stream_cpu_percent": round(stream_cpu),
        "cpu_count": os.cpu_count(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", choices=list(CONFIGS))
    parser.add_argument("--only", nargs="*", default=list(CONFIGS))
    args = parser.parse_args()
    load_env_token()
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
    OUT.mkdir(parents=True, exist_ok=True)
    if args.config:
        result = run_config(args.config)
        (OUT / f"result_{args.config}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
        return 0
    results = []
    for name in args.only:
        code = subprocess.call([sys.executable, __file__, "--config", name])
        path = OUT / f"result_{name}.json"
        if code == 0 and path.exists():
            results.append(json.loads(path.read_text(encoding="utf-8")))
        else:
            print(f"{name} : échec (code {code})")
    rows = [
        ("Chargement du modèle (s)", "load_s"),
        ("RAM après chargement (Mo)", "rss_loaded_mb"),
        ("RAM pic (Mo)", "rss_peak_mb"),
        ("Complet : génération (s)", "full_total_s"),
        ("Complet : durée audio (s)", "full_audio_s"),
        ("Complet : real-time factor", "full_rtf"),
        ("Complet : CPU moyen (%)", "full_cpu_percent"),
        ("Flux : premier chunk (s)", "stream_first_chunk_s"),
        ("Flux : génération (s)", "stream_total_s"),
        ("Flux : real-time factor", "stream_rtf"),
        ("Flux : chunks", "stream_chunks"),
        ("Flux : coupures de lecture (s)", "stream_gaps_s"),
        ("Flux : CPU moyen (%)", "stream_cpu_percent"),
    ]
    print("\n" + " " * 34 + "".join(f"{r['config']:>12}" for r in results))
    for label, key in rows:
        print(f"{label:34}" + "".join(f"{str(r[key]):>12}" for r in results))
    print(f"\nFichiers WAV : {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
