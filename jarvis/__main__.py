"""Point d'entrée : ``python -m jarvis``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from jarvis.config import load_config

ROOT = Path(__file__).resolve().parent.parent
STT_TEST_SAMPLE = ROOT / "tests" / "fixtures" / "scenario.wav"
STT_TEST_SECONDS = 9.0


def main() -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="Assistant vocal JARVIS")
    parser.add_argument("--config", default=str(ROOT / "config.toml"), help="fichier de configuration")
    parser.add_argument("--list-devices", action="store_true", help="liste les périphériques audio et quitte")
    parser.add_argument("--choose-audio", action="store_true",
                        help="choisit le micro et la sortie de JARVIS (sans changer ceux de Windows) et l'enregistre")
    parser.add_argument("--wake-test", action="store_true",
                        help="affiche en direct le score du wake word pour régler le seuil")
    parser.add_argument("--tts-test", metavar="TEXTE",
                        help="synthétise TEXTE avec la voix de JARVIS, le joue sur la sortie audio configurée et quitte")
    parser.add_argument("--stt-test", metavar="WAV", nargs="?", const="",
                        help="diagnostic STT : GPU/CUDA, device et type de calcul utilisés, temps de chargement "
                             "et de transcription (WAV facultatif, sinon extrait de tests/fixtures/scenario.wav)")
    parser.add_argument("--activity", metavar="N", nargs="?", type=int, const=20,
                        help="affiche les N dernières entrées du journal d'activité (20 par défaut) et quitte")
    parser.add_argument("--web-test", metavar="QUESTION",
                        help="recherche Web sans micro ni voix : route, sources, temps et réponse de JARVIS "
                             "(-v affiche aussi les données transmises au LLM)")
    parser.add_argument("--no-face", action="store_true", help="lance JARVIS sans le visage graphique")
    parser.add_argument("--face-demo", action="store_true",
                        help="affiche le visage et fait défiler ses états (parole avec la vraie voix), sans micro")
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

    if args.choose_audio:
        return choose_audio(Path(args.config))

    cfg = load_config(args.config)

    if args.wake_test:
        return wake_test(cfg)
    if args.stt_test is not None:
        return stt_test(cfg, Path(args.stt_test) if args.stt_test else None)
    if args.tts_test is not None:
        return tts_test(cfg, args.tts_test)
    if args.web_test is not None:
        return web_test(cfg, args.web_test)
    if args.activity is not None:
        return show_activity(cfg, args.activity)
    if args.face_demo:
        return face_demo(cfg)

    from jarvis.factory import build_agent

    source = sink = None
    if args.input_wav:
        from jarvis.audio.files import ArraySource, RecordingSink, read_wav

        audio, rate = read_wav(args.input_wav)
        source = ArraySource(audio, cfg.audio.sample_rate, cfg.audio.frame_samples, source_rate=rate)
        sink = RecordingSink(args.output_dir)

    from jarvis.factory import build_events

    events = build_events(cfg)
    on_event = None
    visual = None if args.no_face else start_face(cfg)
    if visual is not None:
        from jarvis.face import FaceBridge, MeteredSink
        from jarvis.factory import build_sink

        sink = MeteredSink(sink or build_sink(cfg), visual)
        bridge = FaceBridge(visual)
        bridge.attach(events)
        on_event = bridge.on_event
    agent = build_agent(cfg, source, sink, on_event, events=events)
    try:
        agent.run()
    except KeyboardInterrupt:
        logging.info("Arrêt demandé.")
    return 0


def choose_audio(config_path: Path, ask=input, devices=None) -> int:
    """Propose les micros et sorties audio, puis enregistre le choix dans [audio] de config.toml."""
    import re

    if devices is None:
        from jarvis.audio.devices import list_devices

        devices = {"input": list_devices("input"), "output": list_devices("output")}
    text = config_path.read_text(encoding="utf-8")
    for kind, key, label in (("input", "input_device", "Micro"), ("output", "output_device", "Sortie audio")):
        current = re.search(rf'^{key}\s*=\s*"([^"]*)"', text, re.MULTILINE)
        print(f"\n{label} de JARVIS (actuel : {current.group(1) if current and current.group(1) else 'celui de Windows'})")
        print("  0. Celui de Windows (suit le périphérique par défaut)")
        for i, (_, name) in enumerate(devices[kind], 1):
            print(f"  {i}. {name}")
        answer = ask("Numéro (Entrée = ne rien changer) : ").strip()
        if not answer:
            continue
        if not answer.isdigit() or int(answer) > len(devices[kind]):
            print("Choix invalide, inchangé.")
            continue
        value = "" if answer == "0" else devices[kind][int(answer) - 1][0]
        text = re.sub(rf'^({key}\s*=\s*)"[^"]*"', lambda m: f'{m.group(1)}"{value}"', text, count=1, flags=re.MULTILINE)
        print(f"-> {value or 'celui de Windows'}")
    config_path.write_text(text, encoding="utf-8")
    print(f"\nEnregistré dans {config_path.name}. Relancez JARVIS pour l'utiliser.")
    return 0


def show_activity(cfg, limit: int) -> int:
    from jarvis.activity import ActivityLog, JsonlActivityStore

    entries = ActivityLog(JsonlActivityStore(cfg.activity.path)).recent(max(1, limit))
    if not entries:
        print(f"Journal d'activité vide ({cfg.activity.path}).")
    for entry in entries:
        print(entry.line())
    return 0


def start_face(cfg, force: bool = False):
    """Démarre le visage graphique ; None s'il est désactivé ou indisponible (JARVIS continue en vocal)."""
    if not (cfg.face.enabled or force):
        return None
    try:
        from jarvis.face import FaceServer, VisualState

        visual = VisualState()
        url = FaceServer(visual, cfg.face.host, cfg.face.port).start()
    except Exception:
        logging.getLogger(__name__).warning("Visage indisponible, JARVIS continue en vocal.", exc_info=True)
        return None
    if url is None:
        return None
    logging.getLogger(__name__).info("Visage : %s", url)
    if cfg.face.open_browser:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass
    return visual


def face_demo(cfg) -> int:
    """Fait défiler veille, écoute, réflexion et parole (vraie voix, niveau audio mesuré)."""
    import time

    from jarvis.face import MeteredSink
    from jarvis.factory import build_sink, build_tts

    log = logging.getLogger("jarvis.face_demo")
    visual = start_face(cfg, force=True)
    if visual is None:
        return 1
    tts = build_tts(cfg)
    sink = MeteredSink(build_sink(cfg), visual)
    lines = ["Bonjour monsieur. Tous les systèmes sont opérationnels.",
             "La recherche est terminée. Voici ce que j'ai trouvé.",
             "Je reste à votre écoute."]
    try:
        for turn in range(10_000):
            for state in ("standby", "listening", "thinking"):
                log.info("État : %s", state)
                visual.set_state(state)
                time.sleep(5)
            log.info("État : speaking")
            audio, rate = tts.synthesize(lines[turn % len(lines)])
            sink.play(audio, rate)
            getattr(sink, "drain", lambda: None)()
            time.sleep(0.6)
    except KeyboardInterrupt:
        log.info("Fin de la démonstration.")
    return 0


def web_test(cfg, question: str, llm=None, web=None) -> int:
    """Diagnostic de la recherche Web, au clavier : même traitement qu'en conversation, sans audio."""
    import time

    from jarvis.agent import clean_for_speech, polish_web_sentence
    from jarvis.capabilities import CapabilityRegistry
    from jarvis.factory import build_llm, build_web
    from jarvis.interfaces import Message
    from jarvis.personality import load_personality
    from jarvis.router import IntentRouter
    from jarvis.streaming import sentences_from_llm
    from jarvis.web.research import WebSearchCapability, web_request

    log = logging.getLogger("jarvis.web_test")
    if not question.strip():
        log.error("Question vide.")
        return 2
    web = web or build_web(cfg)
    if web is None:
        log.error("Recherche Web désactivée : vérifiez [web] enabled et provider dans config.toml.")
        return 2
    personality = load_personality(cfg.assistant.personality)
    capabilities = CapabilityRegistry()
    capabilities.register(WebSearchCapability())
    router = IntentRouter(personality, capabilities, web_enabled=True)
    route = router.route(question)
    log.info("Route : %s", route.label)
    if route.label != "web.search":
        log.info("En conversation, cette demande n'irait pas sur le Web ; recherche forcée pour le test.")

    started = time.perf_counter()
    context = web.run(question)
    log.info("Requête : « %s » (%.2f s, %d page(s) lue(s))", context.query, time.perf_counter() - started,
             len(context.pages))
    if context.error:
        log.error("Recherche impossible : %s", context.error)
        print(f"\nJARVIS : {router.phrase('web_unavailable')}")
        return 1
    for source in context.sources:
        date = f" ({source['published_at']})" if source["published_at"] else ""
        print(f"  [{source['position']}] {source['source']}{date} — {source['title']}\n      {source['url']}")
    if not context.found:
        print(f"\nJARVIS : {router.phrase('web_no_results')}")
        return 0

    llm = llm or build_llm(cfg)
    request = web_request(question, context, personality.assistant_name)
    log.debug("Données transmises au LLM :\n%s", request)
    messages = [Message("system", router.system_prompt(searched=True)), Message("user", request)]
    started = time.perf_counter()
    spoken: list[str] = []
    fragments = llm.stream(messages) if hasattr(llm, "stream") else iter([llm.chat(messages)])
    for sentence in sentences_from_llm(fragments, router.reply_filter(user_text=question), {},
                                       done_reason=lambda: getattr(llm, "last_done_reason", "")):
        sentence = polish_web_sentence(clean_for_speech(sentence), question, not spoken, personality.assistant_name)
        if sentence:
            spoken.append(sentence)
    log.info("LLM : %.2f s", time.perf_counter() - started)
    print(f"\nJARVIS : {' '.join(spoken)}")
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


def stt_test(cfg, wav: Path | None = None, runs: int = 3) -> int:
    import subprocess
    import time

    import ctranslate2
    import numpy as np

    from jarvis.audio.resample import resample
    from jarvis.audio.files import read_wav
    from jarvis.factory import build_stt
    from jarvis.stt.faster_whisper import cuda_library_dirs, cuda_device_count

    log = logging.getLogger("jarvis.stt_test")
    log.info("ctranslate2 %s — GPU CUDA détectés : %d", ctranslate2.__version__, cuda_device_count())
    if cuda_device_count():
        log.info("Types de calcul CUDA supportés : %s", ", ".join(sorted(ctranslate2.get_supported_compute_types("cuda"))))
    try:
        gpu = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version,memory.used,memory.total",
                              "--format=csv,noheader"], capture_output=True, text=True, timeout=10).stdout.strip()
        log.info("GPU NVIDIA : %s", gpu or "aucun")
    except (OSError, subprocess.SubprocessError):
        log.info("GPU NVIDIA : nvidia-smi introuvable")
    libs = cuda_library_dirs()
    log.info("Bibliothèques CUDA (pip) : %s", ", ".join(str(d) for d in libs) or "aucune (pip install -r requirements-gpu.txt)")
    log.info("Configuration demandée : device=%s, compute_type=%s (repli : %s/%s)",
             cfg.stt.device, cfg.stt.compute_type, cfg.stt.fallback_device, cfg.stt.fallback_compute_type)

    stt = build_stt(cfg)
    audio, rate = read_wav(wav or STT_TEST_SAMPLE)
    if not wav:
        audio = audio[: int(STT_TEST_SECONDS * rate)]
    source = str(wav or STT_TEST_SAMPLE)
    audio = resample(audio, rate, 16000).astype(np.int16)
    log.info("Audio : %s (%.2f s)", source, len(audio) / 16000)
    times = []
    for i in range(1, runs + 1):
        started = time.perf_counter()
        text = stt.transcribe(audio, 16000)
        times.append(time.perf_counter() - started)
        log.info("Transcription %d/%d : %.2f s — « %s »", i, runs, times[-1], text)
    log.info("STT device: %s | compute type: %s | model: %s | chargement %.2f s | transcription médiane %.2f s",
             stt.device.upper(), stt.compute_type, stt.model_name, stt.load_time, sorted(times)[len(times) // 2])
    return 0


def tts_test(cfg, text: str, sink=None) -> int:
    import time

    import numpy as np

    from jarvis.factory import build_sink, build_tts

    log = logging.getLogger("jarvis.tts_test")
    if not text.strip():
        log.error("Texte vide : rien à synthétiser.")
        return 2
    started = time.perf_counter()
    tts = build_tts(cfg)
    loaded = time.perf_counter()
    warm_up = getattr(tts, "warm_up", None)
    if warm_up:
        warm_up()
    log.info("Voix : %s (chargement %.2f s, préchauffage %.2f s)", tts.voice_name, loaded - started,
             time.perf_counter() - loaded)
    sink = sink or build_sink(cfg)
    log.info("Sortie audio : %s", getattr(sink, "device_name", type(sink).__name__))
    started = time.perf_counter()
    streaming = cfg.tts.stream_audio and hasattr(tts, "stream")
    chunks = tts.stream(text) if streaming else iter([tts.synthesize(text)[0]])
    first, samples = None, 0
    for chunk in chunks:
        if first is None:
            first = time.perf_counter() - started
            log.info("Premier morceau audio après %.2f s", first)
        sink.play(np.asarray(chunk, dtype=np.int16), tts.sample_rate)
        samples += len(chunk)
    drain = getattr(sink, "drain", None)
    if drain:
        drain()
    if not samples:
        log.error("Aucun audio produit.")
        return 1
    total = time.perf_counter() - started
    audio = samples / tts.sample_rate
    log.info("Terminé : %.2f s d'audio en %.2f s (lecture comprise), %s", audio, total,
             "flux" if streaming else "phrase entière")
    return 0


if __name__ == "__main__":
    sys.exit(main())
