"""Banc STT sur GPU (Katana) : mêmes phrases, même indice et mêmes paramètres que bench/stt/run.py sur le mini."""
import os, re, statistics, sys, time, unicodedata, wave
from pathlib import Path
import numpy as np

for mod in ("cublas", "cudnn"):  # DLL CUDA des paquets pip nvidia-*
    d = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia" / mod / "bin"
    if d.exists():
        os.add_dll_directory(str(d)); os.environ["PATH"] = str(d) + os.pathsep + os.environ["PATH"]
from faster_whisper import WhisperModel

HERE = Path(__file__).resolve().parent
def norm(t):
    t = unicodedata.normalize("NFD", t.lower()); t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.findall(r"[a-z0-9]+", t)
def wer(r, h):
    r, h = norm(r), norm(h); row = list(range(len(h) + 1))
    for i, a in enumerate(r, 1):
        prev, row[0] = row[0], i
        for j, b in enumerate(h, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (a != b))
    return row[-1], len(r)
corpus = []
for line in (HERE / "textes.txt").read_text(encoding="utf-8").splitlines():
    name, text = line.split("\t", 1)
    with wave.open(str(HERE / name)) as w:
        corpus.append((name, np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768, text))
hint = (HERE / "indice.txt").read_text(encoding="utf-8")
compute = sys.argv[2] if len(sys.argv) > 2 else "int8_float16"
for model_name in sys.argv[1].split(","):
    t0 = time.perf_counter()
    model = WhisperModel(model_name, device="cuda", compute_type=compute, download_root=str(HERE / "models"))
    list(model.transcribe(np.zeros(16000, dtype=np.float32), language="fr", beam_size=1)[0])
    print(f"{model_name} chargé en {time.perf_counter() - t0:.1f} s", flush=True)
    for label, hw in (("avec indice", hint), ("sans indice", None)):
        durations, errors, total, wrong = [], 0, 0, []
        for name, audio, text in corpus:
            s = time.perf_counter()
            segs, _ = model.transcribe(audio, language="fr", beam_size=1, condition_on_previous_text=False,
                                       vad_filter=False, hotwords=hw, temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0))
            heard = " ".join(x.text.strip() for x in segs if x.no_speech_prob < 0.6).strip()
            durations.append(time.perf_counter() - s)
            e, n = wer(text, heard); errors += e; total += n
            if e: wrong.append(f"    {name} : {text} -> {heard}")
        durations.sort()
        print(f"{model_name:16} {label:12} WER {100*errors/total:5.1f} %   médiane {statistics.median(durations):.2f} s, "
              f"p90 {durations[int(.9*(len(durations)-1))]:.2f} s", flush=True)
        if "-v" in sys.argv: print("\n".join(wrong), flush=True)
    del model
