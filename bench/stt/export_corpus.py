import sys, wave
from pathlib import Path
sys.path.insert(0, "/home/user/orion-dev/bench/stt"); sys.path.insert(0, "/home/user/orion-dev")
import run
from jarvis.config import load_config
from jarvis.factory import vocabulary_hint
out = Path("/tmp/stt_corpus"); out.mkdir(exist_ok=True)
lines = []
for name, audio, text in run.synthetic(Path("/opt/jarvis/models/piper")):
    with wave.open(str(out / f"{name}.wav"), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(audio.tobytes())
    lines.append(f"{name}.wav\t{text}")
(out / "textes.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
(out / "indice.txt").write_text(vocabulary_hint(load_config("/opt/jarvis/config.toml")), encoding="utf-8")
print(len(lines))
