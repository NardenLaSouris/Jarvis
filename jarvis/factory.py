"""Assemblage des composants à partir de la configuration.

C'est le seul endroit qui connaît les implémentations concrètes : ajouter un
nouveau moteur (autre STT, autre LLM...) se fait ici, sans toucher à l'agent.
"""

from __future__ import annotations

import logging

from jarvis.agent import Agent, AgentSettings, EventHandler
from jarvis.audio.endpointing import EndpointerSettings, UtteranceRecorder
from jarvis.capabilities import CapabilityRegistry
from jarvis.config import Config
from jarvis.interfaces import AudioSink, AudioSource, LanguageModel

log = logging.getLogger(__name__)


def build_llm(cfg: Config) -> LanguageModel:
    if cfg.llm.backend == "ollama":
        from jarvis.llm.ollama import OllamaLLM

        c = cfg.llm
        return OllamaLLM(c.host, c.model, c.temperature, c.max_tokens, c.keep_alive, c.timeout)
    raise ValueError(f"Backend LLM inconnu : {cfg.llm.backend}")


def build_agent(
    cfg: Config,
    source: AudioSource | None = None,
    sink: AudioSink | None = None,
    on_event: EventHandler | None = None,
) -> Agent:
    from jarvis.stt.faster_whisper import FasterWhisperSTT
    from jarvis.tts.piper import PiperTTS
    from jarvis.wakeword.openwakeword import OpenWakeWordDetector

    if source is None or sink is None:
        from jarvis.audio.devices import MicrophoneSource, SpeakerSink

        source = source or MicrophoneSource(cfg.audio.sample_rate, cfg.audio.frame_samples, cfg.audio.input_device)
        sink = sink or SpeakerSink(cfg.audio.output_device)

    log.info("Chargement du wake word…")
    ww = cfg.wake_word
    wake_word = OpenWakeWordDetector(ww.model, ww.melspectrogram_model, ww.embedding_model)

    log.info("Chargement du STT (whisper %s)…", cfg.stt.model)
    stt = FasterWhisperSTT(
        cfg.stt.model, cfg.assistant.language, cfg.stt.device, cfg.stt.compute_type,
        cfg.stt.beam_size, cfg.stt.download_root,
    )

    log.info("Chargement de la voix…")
    tts = PiperTTS(cfg.tts.voice, cfg.tts.length_scale, cfg.tts.volume)

    log.info("Chargement du LLM (%s)…", cfg.llm.model)
    llm = build_llm(cfg)
    try:
        llm.warm_up()
    except Exception as exc:  # JARVIS démarre quand même et le signalera à l'usage.
        log.warning("Préchargement du LLM impossible : %s", exc)

    a = cfg.audio
    recorder = UtteranceRecorder(
        source, EndpointerSettings(a.end_of_speech_silence, a.max_utterance, a.min_rms, a.speech_to_noise_ratio)
    )
    settings = AgentSettings(
        assistant_name=cfg.assistant.name,
        wake_phrase=ww.phrase,
        wake_threshold=ww.threshold,
        acknowledgements=cfg.assistant.acknowledgements,
        listen_timeout=a.listen_timeout,
        conversation_timeout=cfg.assistant.conversation_timeout,
        max_history_turns=cfg.assistant.max_history_turns,
    )
    return Agent(settings, source, sink, wake_word, recorder, stt, llm, tts, CapabilityRegistry(), on_event)
