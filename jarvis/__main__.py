"""Point d'entrée : ``python -m jarvis``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from jarvis.config import load_config

ROOT = Path(__file__).resolve().parent.parent
VOICE_CANDIDATES = ["fr_FR-tom-medium", "fr_FR-gilles-low", "fr_FR-upmc-medium:pierre"]
TEST_PHRASES = [
    "Bonjour monsieur. Je suis Jarvis. Je vous écoute.",
    "Jarvis, je suis à votre service. Que puis-je faire pour vous ?",
    "Je peux répondre à vos questions, vous assister et vous informer.",
    "Jarvis. Bonjour monsieur. Je vais vous assister.",
]


def main() -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="Assistant vocal JARVIS")
    parser.add_argument("--config", default=str(ROOT / "config.toml"), help="fichier de configuration")
    parser.add_argument("--list-devices", action="store_true", help="liste les périphériques audio et quitte")
    parser.add_argument("--wake-test", action="store_true",
                        help="affiche en direct le score du wake word pour régler le seuil")
    parser.add_argument("--tts-test", metavar="TEXTE",
                        help="synthétise TEXTE avec la voix de JARVIS, le joue sur la sortie audio configurée et quitte")
    parser.add_argument("--tts-voice", metavar="VOIX",
                        help="avec --tts-test : voix à utiliser à la place de celle de config.toml "
                             "(ex. fr_FR-gilles-low, fr_FR-upmc-medium:pierre, elevenlabs:<voice_id>)")
    parser.add_argument("--tts-voices-test", metavar="VOIX", nargs="*",
                        help="joue les mêmes phrases de test avec chaque voix, l'une après l'autre "
                             f"(par défaut : {', '.join(VOICE_CANDIDATES)})")
    parser.add_argument("--no-pronunciations", action="store_true",
                        help="avec --tts-test / --tts-voices-test : ignore [tts.pronunciations] (prononciation brute d'espeak)")
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
    if args.tts_test is not None:
        return tts_test(cfg, args.tts_test, voice=args.tts_voice, pronunciations=not args.no_pronunciations)
    if args.tts_voices_test is not None:
        return tts_voices_test(cfg, args.tts_voices_test or VOICE_CANDIDATES, pronunciations=not args.no_pronunciations)

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


def _speak(tts, sink, text: str, log: logging.Logger) -> None:
    import time

    started = time.perf_counter()
    audio, rate = tts.synthesize(text)
    if audio.size == 0:
        raise RuntimeError("Génération Piper : aucun audio produit.")
    log.info("Génération réussie (%s) en %.2f s : %d échantillons, %.2f s d'audio, %d Hz, %s mono",
             getattr(tts, "last_voice", "") or tts.voice_name, time.perf_counter() - started,
             audio.size, audio.size / rate, rate, audio.dtype)
    log.info("Lecture AudioSink commencée (sortie : %s)", getattr(sink, "device_name", type(sink).__name__))
    started = time.perf_counter()
    sink.play(audio, rate)
    log.info("Lecture terminée en %.2f s", time.perf_counter() - started)


def _missing_voices(cfg, voices: list[str]) -> list[str]:
    from jarvis.tts.piper import parse_voice_spec

    return [v for v in voices
            if not v.startswith("elevenlabs:") and not parse_voice_spec(v, cfg.tts.voice.parent)[0].exists()]


def _report_missing(missing: list[str], log: logging.Logger) -> None:
    names = sorted({v.partition(":")[0] for v in missing})
    log.error("Voix absentes de models/piper : %s", ", ".join(names))
    log.error("Téléchargement : python scripts/download_models.py %s", " ".join(f"--voice {n}" for n in names))


def tts_test(cfg, text: str, sink=None, voice: str | None = None, pronunciations: bool = True) -> int:
    from jarvis.factory import build_sink, build_tts

    log = logging.getLogger("jarvis.tts_test")
    if not text.strip():
        log.error("Texte vide : rien à synthétiser.")
        return 2
    if voice and _missing_voices(cfg, [voice]):
        _report_missing([voice], log)
        return 3
    tts = build_tts(cfg, voice, pronunciations)
    log.info("Voix : %s", tts.voice_name)
    if hasattr(tts, "phonemes"):
        log.info("Phonèmes : %s", tts.phonemes(text))
    _speak(tts, sink or build_sink(cfg), text, log)
    return 0


def tts_voices_test(cfg, voices: list[str], sink=None, pronunciations: bool = True) -> int:
    from jarvis.factory import build_sink, build_tts

    log = logging.getLogger("jarvis.tts_voices_test")
    missing = _missing_voices(cfg, voices)
    if missing:
        _report_missing(missing, log)
    available = [v for v in voices if v not in missing]
    if not available:
        return 3
    sink = sink or build_sink(cfg)
    lexicon = "avec" if pronunciations and cfg.tts.pronunciations else "sans"
    log.info("%d voix à comparer, %d phrases chacune, %s lexique de prononciation",
             len(available), len(TEST_PHRASES), lexicon)
    for i, voice in enumerate(available, 1):
        tts = build_tts(cfg, voice, pronunciations)
        log.info("══════ Voix %d/%d : %s ══════", i, len(available), tts.voice_name)
        for k, phrase in enumerate(TEST_PHRASES, 1):
            log.info("[%s] Phrase %d/%d : « %s »", tts.voice_name, k, len(TEST_PHRASES), phrase)
            if hasattr(tts, "phonemes"):
                log.info("[%s] Phonèmes : %s", tts.voice_name, tts.phonemes(phrase))
            _speak(tts, sink, phrase, log)
    log.info("Comparaison terminée : %s", ", ".join(available))
    return 0 if not missing else 3


if __name__ == "__main__":
    sys.exit(main())
