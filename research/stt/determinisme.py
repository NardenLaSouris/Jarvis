import sys, json, wave, numpy as np
sys.path.insert(0, ".")
from faster_whisper import WhisperModel
from jarvis.config import load_config
from jarvis.factory import vocabulary_hint
from bench import norm, wer
cfg = load_config("config.toml"); hint = vocabulary_hint(cfg, "ORION")
rows = [r for r in json.load(open("/home/user/stt-bench/audio/references.json")) if r["variant"] in ("brouhaha_8dB", "hesitation")][:10]
m = WhisperModel("small", device="cpu", compute_type="int8", download_root=str(cfg.stt.download_root), local_files_only=True)
def run(r, vad, temp):
    with wave.open("/home/user/stt-bench/audio/" + r["file"]) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    kw = {} if temp is None else {"temperature": temp}
    seg, _ = m.transcribe(a, language="fr", beam_size=1, condition_on_previous_text=False, vad_filter=vad, hotwords=hint, **kw)
    return " ".join(s.text.strip() for s in seg if s.no_speech_prob < 0.6)
for vad in (False, True):
    for temp in (None, 0.0):
        tot = []
        for rep in range(3):
            e = n = 0
            for r in rows:
                x, y = wer(norm(r["text"]), norm(run(r, vad, temp))); e += x; n += y
            tot.append(round(e / n, 3))
        label = "defaut (repli 0..1)" if temp is None else "0"
        print(f"vad={vad} temperature={label}: WER sur 3 passes {tot}", flush=True)
