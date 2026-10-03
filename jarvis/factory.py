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
from jarvis.config import Config, secret
from jarvis.events import EventBus
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
        from jarvis.tts.neutts import NeuTTSEngine

        n = t.neutts
        return NeuTTSEngine(n.get("backbone", "neuphonic/neutts-nano-french-q8-gguf"),
                            n.get("codec", "neuphonic/neucodec-onnx-decoder"),
                            ENV_FILE.parent / n.get("voice", "models/neutts/voices/bernard.wav"),
                            hf_token=secret("HF_TOKEN", ENV_FILE), pronunciations=n.get("pronunciations"))
    raise ValueError(f"Moteur TTS inconnu : {t.engine}")


def allowed_apps(cfg: Config) -> dict:
    """Applications que les outils peuvent ouvrir ou fermer ({clé: Application}), vide sans ces outils."""
    from jarvis.tools.applications import load_applications

    t = cfg.tools
    if not t.enabled or not (t.open_application.get("enabled", True) or t.close_application.get("enabled", True)):
        return {}
    return load_applications(t.applications)


def vocabulary_hint(cfg: Config, personality_name: str = "JARVIS") -> str:
    """Phrases données à Whisper pour orienter la transcription vers le vocabulaire de JARVIS : son nom, les
    applications autorisées et les commandes activées (minuteurs, rappels, recherche, météo et ville par défaut)."""
    labels = [a.label for a in allowed_apps(cfg).values()]
    verbs = ("ouvre", "ferme", "lance", "quitte")
    commands = ", ".join(f"{verbs[i % len(verbs)]} {label}" for i, label in enumerate(labels))
    hint = f"{personality_name.capitalize()}, {commands}." if commands else f"{personality_name.capitalize()}."
    phrases = []
    if cfg.tools.enabled and cfg.timers.enabled:
        phrases += ["Mets un minuteur.", "Rappelle-moi."]
    if cfg.web.enabled:
        phrases.append("Recherche-moi.")
    phrases.append("Raconte-moi.")
    if cfg.tools.enabled and cfg.weather.enabled:
        phrases.append(f"Quel temps fera-t-il à {cfg.weather.default_location} ?")
    return " ".join([hint, *phrases])


def build_stt(cfg: Config) -> SpeechToText:
    from jarvis.stt.faster_whisper import FasterWhisperSTT

    c = cfg.stt
    return FasterWhisperSTT(
        c.model, cfg.assistant.language, c.device, c.compute_type, c.beam_size, c.download_root,
        c.fallback_device, c.fallback_compute_type, hotwords=vocabulary_hint(cfg) if c.vocabulary_hint else "",
    )


def build_corrector(cfg: Config, personality):
    """Correcteur des commandes mal transcrites, pour les applications autorisées ; None sans outils."""
    from jarvis.stt.correction import CommandCorrector

    apps = allowed_apps(cfg)
    if not apps:
        return None
    verbs = {"ouvre": ("ouvre", "ouvrir", "lance", "lancer", "demarre", "demarrer", "ouvre moi", "lance moi"),
             "ferme": ("ferme", "fermer", "quitte", "quitter", "ferme moi")}
    objects = {alias: key for key, app in apps.items() for alias in (key, *app.app.aliases)}
    return CommandCorrector(verbs, objects, ignored=(personality.assistant_name, personality.user_title),
                            rewrite=personality.canonical)


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


def build_events(cfg: Config) -> EventBus:
    """Bus d'événements de JARVIS, avec le journal d'activité s'il est activé."""
    bus = EventBus()
    if cfg.activity.enabled:
        from jarvis.activity import ActivityLog, JsonlActivityStore

        ActivityLog(JsonlActivityStore(cfg.activity.path, cfg.activity.max_kb * 1024)).attach(bus)
    return bus


def build_timers(cfg: Config, events: EventBus | None):
    """Gestionnaire de minuteurs et rappels démarré, ou None s'ils sont désactivés."""
    if not cfg.timers.enabled:
        return None
    from jarvis.scheduling import TimerManager

    manager = TimerManager(events, max_seconds=cfg.timers.max_hours * 3600, max_active=cfg.timers.max_active)
    manager.start()
    return manager


def build_notifications(cfg: Config, personality, events: EventBus):
    """Gestionnaire de notifications et son canal vocal (None si désactivé), branchés sur les événements."""
    from jarvis.notifications import EventNotifications, NotificationManager, VoiceNotificationChannel

    manager = NotificationManager(events)
    voice = None
    if cfg.notifications.voice_enabled:
        voice = VoiceNotificationChannel(events)
        manager.register(voice)
    EventNotifications(manager, personality).attach(events)
    manager.start()
    return manager, voice


def build_weather(cfg: Config, events: EventBus | None):
    """Service météo, ou None si désactivé."""
    w = cfg.weather
    if not w.enabled:
        return None
    if w.provider != "open-meteo":
        raise ValueError(f"Fournisseur météo inconnu : {w.provider}")
    from jarvis.weather import OpenMeteoProvider, WeatherService

    if (w.latitude is None) != (w.longitude is None):
        raise ValueError("[weather] latitude et longitude vont ensemble")
    coordinates = (float(w.latitude), float(w.longitude)) if w.latitude is not None else None
    provider = OpenMeteoProvider(timeout=w.timeout, country=w.country, temperature_unit=w.temperature_unit)
    return WeatherService(provider, w.default_location, events, current_ttl=w.current_cache_minutes * 60,
                          forecast_ttl=w.forecast_cache_minutes * 60, default_coordinates=coordinates)


def build_tools(cfg: Config, personality, events: EventBus | None = None, timers=None, weather=None):
    """Core des outils (registre, permissions, confirmation), ou None si les outils sont désactivés."""
    if not cfg.tools.enabled:
        return None
    from jarvis.tools import ConfirmationManager, PermissionManager, ToolCore, ToolRegistry, builtin_tools

    registry = ToolRegistry()
    for tool in builtin_tools(cfg.tools.settings(), timers=timers, weather=weather):
        registry.register(tool)
    confirmations = ConfirmationManager(personality.confirm_yes, personality.confirm_no,
                                        ignored=(personality.assistant_name, personality.user_title))
    return ToolCore(registry, PermissionManager(), confirmations, timeout=cfg.tools.timeout, events=events)


def build_source(cfg: Config) -> AudioSource:
    """Micro local, ou celui d'un autre PC via son agent quand [audio] remote est renseigné."""
    a = cfg.audio
    if a.remote:
        from jarvis.audio.network import NetworkSource

        return NetworkSource(a.remote, secret("JARVIS_AGENT_TOKEN", ENV_FILE), a.sample_rate, a.frame_samples)
    from jarvis.audio.devices import MicrophoneSource

    return MicrophoneSource(a.sample_rate, a.frame_samples, a.input_device)


def build_sink(cfg: Config) -> AudioSink:
    if cfg.audio.remote:
        from jarvis.audio.network import NetworkSink

        return NetworkSink(cfg.audio.remote, secret("JARVIS_AGENT_TOKEN", ENV_FILE))
    from jarvis.audio.devices import SpeakerSink

    return SpeakerSink(cfg.audio.output_device)


def build_agent(
    cfg: Config,
    source: AudioSource | None = None,
    sink: AudioSink | None = None,
    on_event: EventHandler | None = None,
    events: EventBus | None = None,
) -> Agent:
    source = source if source is not None else build_source(cfg)
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
    events = events if events is not None else build_events(cfg)
    timers = build_timers(cfg, events) if cfg.tools.enabled else None
    notifications, voice = build_notifications(cfg, personality, events)
    weather = build_weather(cfg, events) if cfg.tools.enabled else None
    tools = build_tools(cfg, personality, events, timers, weather)
    if tools is not None and len(tools.registry):
        from jarvis.tools import ToolsCapability

        capabilities.register(ToolsCapability(tools.registry))
        log.info("Outils : %s", ", ".join(t.name for t in tools.registry.list()))
    tool_names = tuple(t.name for t in tools.registry.list()) if tools is not None else ()
    router = IntentRouter(personality, capabilities, web_enabled=web is not None, tools=tool_names)
    corrector = build_corrector(cfg, personality) if tools is not None else None
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
                 stream_audio=cfg.tts.stream_audio, merge_under=cfg.tts.merge_under, web=web,
                 tools=tools, corrector=corrector, notifications=voice,
                 services=tuple(s for s in (timers, notifications) if s is not None))
