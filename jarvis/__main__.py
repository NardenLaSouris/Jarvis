"""Point d'entrée : ``python -m jarvis``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from jarvis.config import load_config

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="Assistant vocal JARVIS")
    parser.add_argument("--config", default=str(ROOT / "config.toml"), help="fichier de configuration")
    parser.add_argument("--list-devices", action="store_true", help="liste les périphériques audio et quitte")
    parser.add_argument("--wake-test", action="store_true",
                        help="affiche en direct le score du wake word pour régler le seuil")
    parser.add_argument("--input-wav", type=Path, help="simule le micro avec un fichier WAV (diagnostic)")
    parser.add_argument("--output-dir", type=Path, help="avec --input-wav : enregistre les réponses en WAV")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("faster_whisper", "httpx", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.list_devices:
        import sounddevice as sd

        print(sd.query_devices())
        return 0

    cfg = load_config(args.config)

    if args.wake_test:
        return wake_test(cfg)

    from jarvis.factory import build_agent

    source = sink = None
    if args.input_wav:
        from jarvis.audio.files import ArraySource, RecordingSink, read_wav

        audio, rate = read_wav(args.input_wav)
        source = ArraySource(audio, cfg.audio.sample_rate, cfg.audio.frame_samples, source_rate=rate)
        sink = RecordingSink(args.output_dir)

    agent = build_agent(cfg, source, sink)
    try:
        agent.run()
    except KeyboardInterrupt:
        logging.info("Arrêt demandé.")
    return 0


def wake_test(cfg) -> int:
    """Calibration : prononcez le wake word et observez les scores obtenus."""
    from jarvis.audio.devices import MicrophoneSource
    from jarvis.audio.endpointing import rms
    from jarvis.factory import build_wake_word

    ww = cfg.wake_word
    detector = build_wake_word(cfg)
    mic = MicrophoneSource(cfg.audio.sample_rate, cfg.audio.frame_samples, cfg.audio.input_device)
    print(f"Wake word: {ww.phrase}\nModèle: {ww.model.name}\nSeuil: {ww.threshold}\n"
          f"Prononcez « {ww.phrase} » plusieurs fois. Ctrl+C pour quitter.\n")
    peak, detections = 0.0, 0
    try:
        while True:
            frame = mic.read()
            score = detector.process(frame)
            peak = max(peak, score)
            if score >= ww.threshold:
                detections += 1
                print(f"\rScore: {score:.2f}  Detected: YES  (#{detections})" + " " * 40)
                detector.reset()
                continue
            bar = "#" * int(score * 30)
            print(f"\rScore: {score:.2f} |{bar:<30}| Detected: no   max {peak:.2f}  niveau micro {rms(frame):5.0f}",
                  end="", flush=True)
    except KeyboardInterrupt:
        print(f"\n\nDétections : {detections} — score maximal observé : {peak:.2f}")
    finally:
        mic.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
