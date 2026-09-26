"""Assemblage des composants à partir de la configuration.

C'est le seul endroit qui connaît les implémentations concrètes : ajouter un
nouveau moteur (autre STT, autre LLM...) se fait ici, sans toucher à l'agent.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from jarvis.agent import Agent, AgentSettings, EventHandler
from jarvis.audio.endpointing import EndpointerSettings, UtteranceRecorder
from jarvis.capabilities import CapabilityRegistry
from jarvis.personality import load_personality
from jarvis.router import IntentRouter
from jarvis.config import Config
from jarvis.interfaces import (
    AudioSink, AudioSource, LanguageModel, SpeechToText, TextToSpeech, WakeWordDetector,
)

log = logging.getLogger(__name__)

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def build_llm(cfg: Config) -> LanguageModel:
    if cfg.llm.backend == "ollama":
        from jarvis.llm.ollama import OllamaLLM

        c = cfg.llm
        return OllamaLLM(c.host, c.model, c.temperature, c.max_tokens, c.keep_alive, c.timeout)
    raise ValueError(f"Backend LLM inconnu : {cfg.llm.backend}")


def build_wake_word(cfg: Config) -> WakeWordDetector:
    from jarvis.wakeword.openwakeword import OpenWakeWordDetector

    ww = cfg.wake_word
    return OpenWakeWordDetector(ww.model, ww.melspectrogram_model, ww.embedding_model)


def build_tts(cfg: Config) -> TextToSpeech:
    t = cfg.tts
    if t.engine == "piper":
        from jarvis.tts.piper import PiperTTS

        return PiperTTS(t.voice, t.length_scale, t.volume, t.speaker, t.pronunciations)
    if t.engine == "neutts":
        from jarvis.config import secret
        from jarvis.tts.neutts import NeuTTSEngine

        n = t.neutts
        return NeuTTSEngine(n.get("backbone", "neuphonic/neutts-nano-french-q8-gguf"),
                            n.get("codec", "neuphonic/neucodec-onnx-decoder"),
                            ENV_FILE.parent / n.get("voice", "models/neutts/voices/bernard.wav"),
                            hf_token=secret("HF_TOKEN", ENV_FILE), pronunciations=n.get("pronunciations"))
    raise ValueError(f"Moteur TTS inconnu : {t.engine}")


def build_stt(cfg: Config) -> SpeechToText:
    from jarvis.stt.faster_whisper import FasterWhisperSTT

    c = cfg.stt
    return FasterWhisperSTT(
        c.model, cfg.assistant.language, c.device, c.compute_type, c.beam_size, c.download_root,
        c.fallback_device, c.fallback_compute_type,
    )


def build_web(cfg: Config):
    """Recherche Web configurée, ou None si elle est désactivée ou sans moteur."""
    w = cfg.web
    if not w.enabled:
        return None
    if not w.provider:
        log.warning("Recherche Web activée mais aucun moteur configuré ([web] provider) : désactivée.")
        return None
    if w.provider != "searxng":
        raise ValueError(f"Moteur de recherche inconnu : {w.provider}")
    from jarvis.web.research import WebResearch
    from jarvis.web.searxng import SearXNGProvider

    provider = SearXNGProvider(w.base_url, w.language, w.max_results, w.timeout, max_page_bytes=w.max_page_bytes)
    return WebResearch(provider, w.max_results, w.fetch_pages)


def build_sink(cfg: Config) -> AudioSink:
    from jarvis.audio.devices import SpeakerSink

    return SpeakerSink(cfg.audio.output_device)


def build_agent(
    cfg: Config,
    source: AudioSource | None = None,
    sink: AudioSink | None = None,
    on_event: EventHandler | None = None,
) -> Agent:
    if source is None:
        from jarvis.audio.devices import MicrophoneSource

        source = MicrophoneSource(cfg.audio.sample_rate, cfg.audio.frame_samples, cfg.audio.input_device)
    sink = sink or build_sink(cfg)
    from jarvis.hardware import machine_summary

    log.info("Machine : %s", machine_summary())

    log.info("Chargement du wake word (%s)…", cfg.wake_word.model.name)
    ww = cfg.wake_word
    wake_word = build_wake_word(cfg)

    log.info("Chargement du STT (whisper %s)…", cfg.stt.model)
    stt = build_stt(cfg)

    log.info("Chargement de la voix…")
    started = time.perf_counter()
    tts = build_tts(cfg)
    loaded = time.perf_counter()
    warm_up = getattr(tts, "warm_up", None)
    if warm_up:
        warm_up()
    log.info("TTS %s : chargement %.2f s, préchauffage %.2f s", getattr(tts, "voice_name", ""),
             loaded - started, time.perf_counter() - loaded)

    log.info("Chargement du LLM (%s)…", cfg.llm.model)
    llm = build_llm(cfg)
    try:
        llm.warm_up()
    except Exception as exc:  # JARVIS démarre quand même et le signalera à l'usage.
        log.warning("Préchargement du LLM impossible : %s", exc)

    personality = load_personality(cfg.assistant.personality)
    capabilities = CapabilityRegistry()
    web = build_web(cfg)
    if web is not None:
        from jarvis.web.research import WebSearchCapability

        capabilities.register(WebSearchCapability())
        available = getattr(web.provider, "available", lambda: True)()
        log.info("Recherche Web : %s (%s)", cfg.web.base_url, "joignable" if available else "INJOIGNABLE pour le moment")
    router = IntentRouter(personality, capabilities, web_enabled=web is not None)
    log.info("Personnalité : %s, %d intentions prédéfinies", personality.assistant_name, len(personality.intents))

    a = cfg.audio
    recorder = UtteranceRecorder(
        source, EndpointerSettings(a.end_of_speech_silence, a.max_utterance, a.min_rms, a.speech_to_noise_ratio)
    )
    settings = AgentSettings(
        assistant_name=personality.assistant_name,
        wake_phrase=ww.phrase,
        wake_threshold=ww.threshold,
        acknowledgements=router.wake_phrases(),
        listen_timeout=a.listen_timeout,
        conversation_timeout=cfg.assistant.conversation_timeout,
        max_history_turns=cfg.assistant.max_history_turns,
    )
    return Agent(settings, source, sink, wake_word, recorder, stt, llm, tts, router, on_event,
                 stream_audio=cfg.tts.stream_audio, merge_under=cfg.tts.merge_under, web=web)
