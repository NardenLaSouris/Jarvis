"""Assemblage des composants à partir de la configuration.

C'est le seul endroit qui connaît les implémentations concrètes : ajouter un
nouveau moteur (autre STT, autre LLM...) se fait ici, sans toucher à l'agent.
"""

from __future__ import annotations

import logging
from pathlib import Path

from jarvis.agent import Agent, AgentSettings, EventHandler
from jarvis.audio.endpointing import EndpointerSettings, UtteranceRecorder
from jarvis.capabilities import CapabilityRegistry
from jarvis.config import Config
from jarvis.interfaces import AudioSink, AudioSource, LanguageModel, TextToSpeech, WakeWordDetector

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


ELEVENLABS_PREFIX = "elevenlabs:"


def build_tts(cfg: Config, voice: str | None = None, pronunciations: bool = True) -> TextToSpeech:
    from jarvis.tts.piper import PiperTTS, parse_voice_spec

    eleven_voice = voice[len(ELEVENLABS_PREFIX):] if voice and voice.startswith(ELEVENLABS_PREFIX) else None
    piper_voice = None if eleven_voice else voice
    path, speaker = (cfg.tts.voice, cfg.tts.speaker) if piper_voice is None else parse_voice_spec(piper_voice, cfg.tts.voice.parent)
    lexicon = cfg.tts.pronunciations if pronunciations else {}
    piper = PiperTTS(path, cfg.tts.length_scale, cfg.tts.volume, speaker, lexicon)
    if eleven_voice:
        return _with_fallback(cfg, _build_elevenlabs(cfg, eleven_voice), piper)
    if piper_voice is not None or cfg.tts.engine == "piper":
        return piper
    if cfg.tts.engine == "elevenlabs":
        return _with_fallback(cfg, _build_elevenlabs(cfg), piper)
    raise ValueError(f"Moteur TTS inconnu : {cfg.tts.engine}")


def _build_elevenlabs(cfg: Config, voice_id: str | None = None) -> TextToSpeech | Exception:
    from jarvis.config import secret
    from jarvis.tts.elevenlabs import ElevenLabsTTS, TTSError

    e = cfg.tts.elevenlabs
    try:
        return ElevenLabsTTS(
            voice_id=voice_id or e["voice_id"],
            api_key=secret(e.get("api_key_env", "ELEVENLABS_API_KEY"), ENV_FILE),
            model=e.get("model", "eleven_multilingual_v2"),
            output_format=e.get("output_format", "pcm_22050"),
            voice_settings=e.get("voice_settings"),
            timeout=float(e.get("timeout", 30.0)),
        )
    except TTSError as exc:
        return exc


def _with_fallback(cfg: Config, primary: TextToSpeech | Exception, piper: TextToSpeech) -> TextToSpeech:
    from jarvis.tts.fallback import FallbackTTS

    if isinstance(primary, Exception):
        if not cfg.tts.fallback_to_piper:
            raise primary
        log.warning("ElevenLabs indisponible (%s) : voix locale Piper utilisée", primary)
        return piper
    log.info("Voix : %s", primary.voice_name)
    return FallbackTTS(primary, piper) if cfg.tts.fallback_to_piper else primary


def build_sink(cfg: Config) -> AudioSink:
    from jarvis.audio.devices import SpeakerSink

    return SpeakerSink(cfg.audio.output_device)


def build_agent(
    cfg: Config,
    source: AudioSource | None = None,
    sink: AudioSink | None = None,
    on_event: EventHandler | None = None,
) -> Agent:
    from jarvis.stt.faster_whisper import FasterWhisperSTT

    if source is None:
        from jarvis.audio.devices import MicrophoneSource

        source = MicrophoneSource(cfg.audio.sample_rate, cfg.audio.frame_samples, cfg.audio.input_device)
    sink = sink or build_sink(cfg)

    log.info("Chargement du wake word (%s)…", cfg.wake_word.model.name)
    ww = cfg.wake_word
    wake_word = build_wake_word(cfg)

    log.info("Chargement du STT (whisper %s)…", cfg.stt.model)
    stt = FasterWhisperSTT(
        cfg.stt.model, cfg.assistant.language, cfg.stt.device, cfg.stt.compute_type,
        cfg.stt.beam_size, cfg.stt.download_root,
    )

    log.info("Chargement de la voix…")
    tts = build_tts(cfg)

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
