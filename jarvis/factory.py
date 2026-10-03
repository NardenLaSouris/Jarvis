"""Assemblage des composants à partir de la configuration.

C'est le seul endroit qui connaît les implémentations concrètes : ajouter un
nouveau moteur (autre STT, autre LLM...) se fait ici, sans toucher à l'agent.
"""

from __future__ import annotations

import logging
import threading
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
        llm = OllamaLLM(c.host, c.model, c.temperature, c.max_tokens, c.keep_alive, c.timeout)
        if not c.fallback_host:
            return llm
        from jarvis.llm.failover import FailoverLLM

        fallback = OllamaLLM(c.fallback_host, c.fallback_model or c.model, c.temperature, c.max_tokens, c.keep_alive,
                             c.timeout)
        return FailoverLLM(llm, fallback, c.host)
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
    phrases += ["Raconte-moi.", "Arrête. Tais-toi."]
    if cfg.tools.enabled and cfg.weather.enabled:
        phrases.append(f"Quel temps fera-t-il à {cfg.weather.default_location} ?")
    if cfg.tools.enabled and cfg.lights.enabled:
        phrases += ["Allume la lumière.", "Luminosité 30 %."]
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


def build_devices(cfg: Config):
    """Appareils du réseau ([tools.devices]), ou None : le Core agit alors sur sa propre machine."""
    from jarvis.tools.devices import load_devices

    return load_devices(cfg.tools.devices) if cfg.tools.enabled else None


def build_lights(cfg: Config):
    """Pièces et pilote des lumières ([lights]), clés locales dans .env (LIGHT_KEY_<PIÈCE>) ; (None, None) si
    désactivées."""
    if not (cfg.tools.enabled and cfg.lights.enabled):
        return None, None
    from jarvis.tools.lights import TuyaDriver, load_rooms

    rooms = load_rooms(cfg.lights.rooms, lambda key: secret(f"LIGHT_KEY_{key.upper()}", ENV_FILE))
    return (rooms, TuyaDriver(cfg.lights.timeout)) if rooms else (None, None)


def light_tools_for(rooms, driver) -> list:
    from jarvis.tools.lights import light_tools

    return light_tools(rooms, driver) if rooms is not None else []


def build_routines(cfg: Config, tools, notifications, events: EventBus, sink=None, timers=None):
    """Moteur des routines démarré ([routines]), avec sonnerie des réveils et annonces ; outils de réveil ajoutés au
    registre ([alarms]). None sans outils."""
    if tools is None or not cfg.routines.enabled:
        return None
    from jarvis.notifications import Notification
    from jarvis.routines import JsonRoutineStore, RoutineEngine
    from jarvis.routines.alarm import AlarmPlayer
    from jarvis.routines.announce import Announcer
    from jarvis.scheduling.clock import spoken_clock

    def say(title: str, text: str) -> None:
        notifications.notify(Notification(title[:80], text, "routines"))

    def before_ringing() -> None:
        if cfg.alarms.volume:
            for tool, parameters in (("unmute_volume", {}), ("set_volume", {"volume": cfg.alarms.volume})):
                if tools.registry.exists(tool):
                    tools.submit({"type": "tool_call", "tool": tool, "parameters": parameters})

    def today_events() -> list[str]:
        events_today = engine.today()
        if timers is not None:
            now = timers.now()
            events_today += [f"rappel « {r.message} » à {spoken_clock(r.expires_at.hour, r.expires_at.minute)}"
                             for r in timers.reminders() if r.expires_at.date() == now.date()]
        return events_today

    alarm = AlarmPlayer(sink, cfg.alarms.sound, cfg.alarms.max_minutes, before_ringing) \
        if sink is not None and cfg.alarms.enabled else None
    announcer = Announcer(tools.submit, today_events)
    engine = RoutineEngine(JsonRoutineStore(cfg.routines.path), tools.registry, tools.submit, say, events,
                           alarm=alarm, announce=announcer.text)
    if alarm is not None:
        from jarvis.tools.alarms import alarm_tools

        for tool in alarm_tools(engine, cfg.alarms.briefing):
            tools.registry.register(tool)
    engine.start()
    return engine


def build_api(cfg: Config, tools, routines, *, web=None, devices=None, rooms=None, driver=None, timers=None):
    """API d'administration démarrée ([api], pour JARVIS Control), ou None si désactivée."""
    if not cfg.api.enabled:
        return None
    from jarvis.activity import JsonlActivityStore
    from jarvis.api import CoreApi, CoreStatus

    activity = JsonlActivityStore(cfg.activity.path) if cfg.activity.enabled else None
    status = CoreStatus(llm_url=cfg.llm.host, llm_model=cfg.llm.model, fallback_model=cfg.llm.fallback_model,
                        web=web, devices=devices, rooms=rooms, driver=driver, timers=timers, routines=routines,
                        activity=activity)
    api = CoreApi(cfg.api.host, cfg.api.port, frozenset(cfg.api.allowed_ips), secret("JARVIS_AGENT_TOKEN", ENV_FILE),
                  tools=tools, routines=routines, status=status, activity=activity)
    try:
        api.start()
    except OSError as exc:
        log.warning("API d'administration indisponible (%s:%s) : %s", cfg.api.host, cfg.api.port, exc)
        return None
    return api


def build_tools(cfg: Config, personality, events: EventBus | None = None, timers=None, weather=None, devices=None,
                lights=()):
    """Core des outils (registre, permissions, confirmation), ou None si les outils sont désactivés.

    Avec des appareils, les outils qui agissent sur un PC sont confiés à leurs agents : le Core n'agit jamais sur
    sa propre machine."""
    if not cfg.tools.enabled:
        return None
    from jarvis.tools import ConfirmationManager, PermissionManager, ToolCore, ToolRegistry, builtin_tools

    tools = builtin_tools(cfg.tools.settings(), timers=timers, weather=weather)
    if devices is not None:
        from jarvis.tools.builtin import PC_TOOLS
        from jarvis.tools.devices import AgentClient, remote_tool

        client = AgentClient(secret("JARVIS_AGENT_TOKEN", ENV_FILE), timeout=max(1.0, cfg.tools.timeout - 2))
        tools = [remote_tool(tool, devices, client) if tool.name in PC_TOOLS else tool for tool in tools]
    registry = ToolRegistry()
    tools = [*tools, *lights]
    for tool in tools:
        registry.register(tool)
    confirmations = ConfirmationManager(personality.confirm_yes, personality.confirm_no,
                                        ignored=(personality.assistant_name, personality.user_title))
    return ToolCore(registry, PermissionManager(), confirmations, timeout=cfg.tools.timeout, events=events)


def prime_llm(llm: LanguageModel, router: IntentRouter, registry=None) -> threading.Thread | None:
    """Prépare en arrière-plan les prompts de conversation et de choix d'outil : JARVIS écoute tout de suite,
    et la première vraie demande ne paie plus leur lecture (jusqu'à 2 minutes sur un processeur sans GPU)."""
    prime = getattr(llm, "prime", None)
    if prime is None:
        return None
    from jarvis.interfaces import Message
    from jarvis.tools.planner import planner_prompt

    hello = Message("user", "Bonjour.")
    prompts = [[Message("system", router.system_prompt()), hello]]
    if registry is not None and len(registry):
        prompts.append([Message("system", planner_prompt(registry)), hello])

    def run() -> None:
        started = time.perf_counter()
        try:
            prime(prompts)
        except Exception as exc:
            log.warning("Préparation des prompts du LLM impossible : %s", exc)
            return
        log.info("Prompts du LLM prêts (%.0f s)", time.perf_counter() - started)

    thread = threading.Thread(target=run, name="llm-prime", daemon=True)
    thread.start()
    return thread


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
    devices = build_devices(cfg)
    rooms, driver = build_lights(cfg)
    tools = build_tools(cfg, personality, events, timers, weather, devices, light_tools_for(rooms, driver))
    routines = build_routines(cfg, tools, notifications, events, sink, timers)
    api = build_api(cfg, tools, routines, web=web, devices=devices, rooms=rooms, driver=driver, timers=timers)
    if tools is not None and len(tools.registry):
        from jarvis.tools import ToolsCapability

        capabilities.register(ToolsCapability(tools.registry))
        log.info("Outils : %s", ", ".join(t.name for t in tools.registry.list()))
    tool_names = tuple(t.name for t in tools.registry.list()) if tools is not None else ()
    router = IntentRouter(personality, capabilities, web_enabled=web is not None, tools=tool_names)
    corrector = build_corrector(cfg, personality) if tools is not None else None
    log.info("Personnalité : %s, %d intentions prédéfinies", personality.assistant_name, len(personality.intents))
    prime_llm(llm, router, tools.registry if tools is not None else None)

    a = cfg.audio
    recorder = UtteranceRecorder(
        source, EndpointerSettings(a.end_of_speech_silence, a.max_utterance, a.min_rms, a.speech_to_noise_ratio)
    )
    settings = AgentSettings(
        assistant_name=personality.assistant_name,
        wake_phrase=ww.phrase,
        wake_threshold=ww.threshold,
        wake_patience=ww.patience,
        acknowledgements=router.wake_phrases(),
        listen_timeout=a.listen_timeout,
        conversation_timeout=cfg.assistant.conversation_timeout,
        max_history_turns=cfg.assistant.max_history_turns,
        barge_in=cfg.assistant.barge_in,
        min_confidence=cfg.stt.min_confidence,
    )
    return Agent(settings, source, sink, wake_word, recorder, stt, llm, tts, router, on_event,
                 stream_audio=cfg.tts.stream_audio, merge_under=cfg.tts.merge_under, web=web,
                 tools=tools, corrector=corrector, notifications=voice, alarm=routines.alarm if routines else None,
                 services=tuple(s for s in (api, routines, timers, notifications) if s is not None))
