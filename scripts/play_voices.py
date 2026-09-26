from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.audio.devices import SpeakerSink
from jarvis.audio.files import read_wav
from jarvis.config import load_config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", default=[str(ROOT / "data" / "neutts" / "bench")])
    args = parser.parse_args()
    files = []
    for path in map(Path, args.paths):
        files += sorted(path.glob("*.wav")) if path.is_dir() else [path]
    if not files:
        print("Aucun fichier WAV trouvé.")
        return 1
    sink = SpeakerSink(load_config(ROOT / "config.toml").audio.output_device)
    print(f"Sortie audio : {sink.device_name}")
    for i, path in enumerate(files, 1):
        audio, rate = read_wav(path)
        print(f"[{i}/{len(files)}] {path.name} ({len(audio) / rate:.1f} s)")
        sink.play(audio, rate)
    return 0


if __name__ == "__main__":
    sys.exit(main())
