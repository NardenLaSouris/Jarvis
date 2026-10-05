"""Assemblage des composants à partir de la configuration.

C'est le seul endroit qui connaît les implémentations concrètes : ajouter un
nouveau moteur (autre STT, autre LLM...) se fait ici, sans toucher à l'agent.
"""

from __future__ import annotations

import logging
import re
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

        from jarvis.llm.failover import tcp_reachable

        fallback = OllamaLLM(c.fallback_host, c.fallback_model or c.model, c.temperature, c.max_tokens, c.keep_alive,
                             c.fallback_timeout)
        return FailoverLLM(llm, fallback, c.host, retry_after=c.retry_after,
                           reachable=lambda url: tcp_reachable(url, c.connect_timeout), slow_after=c.slow_after,
                           probe_interval=c.probe_interval, first_token_timeout=c.first_token_timeout)
    raise ValueError(f"Backend LLM inconnu : {cfg.llm.backend}")


def build_wake_verifier(cfg: Config):
    """Seconde vérification du wake word ([wake_word] verify), ou None (désactivée, ou Whisper indisponible)."""
    if not cfg.wake_word.verify:
        return None
    from jarvis.wakeword.verify import WakeVerifier

    try:
        return WakeVerifier(cfg.wake_word.verify_model, cfg.stt.download_root, cfg.assistant.language)
    except Exception as exc:  # JARVIS fonctionne sans : la détection seule, comme avant
        log.warning("Vérification du wake word indisponible (%s) : détection seule", exc)
        return None


def build_wake_captures(cfg: Config):
    if cfg.wake_word.captures_keep <= 0:
        return None
    from jarvis.wakeword.captures import WakeCaptures

    return WakeCaptures(cfg.wake_word.captures, cfg.wake_word.captures_keep)


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
    if cfg.tools.enabled and cfg.spotify.enabled and Path(cfg.spotify.catalog_path).exists():
        from jarvis.spotify import SpotifyCatalog

        artists = SpotifyCatalog(cfg.spotify.catalog_path).artists(25)
        if artists:  # vos artistes, bien orthographiés dans la transcription (« Rammstein », « O-Zone »)
            phrases.append("Mets " + ", ".join(artists) + ".")
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
    from jarvis.scheduling.manager import JsonScheduleStore

    store = JsonScheduleStore(cfg.timers.path) if cfg.timers.persist else None
    manager = TimerManager(events, max_seconds=cfg.timers.max_hours * 3600, max_active=cfg.timers.max_active,
                           store=store, max_reminder_seconds=cfg.timers.reminder_max_hours * 3600)
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

    rooms = load_rooms(cfg.lights.rooms, lambda key: secret(f"LIGHT_KEY_{key.upper()}", ENV_FILE), cfg.lights.groups)
    return (rooms, TuyaDriver(cfg.lights.timeout)) if rooms else (None, None)


def light_tools_for(rooms, driver, scenes: dict | None = None, home=None) -> list:
    from jarvis.tools.lights import light_tools, load_scenes

    return light_tools(rooms, driver, load_scenes(scenes), home) if rooms is not None else []


def build_memory(cfg: Config):
    """Mémoire explicite ([memory]) : faits que l'utilisateur demande de retenir ; None si désactivée."""
    if not (cfg.tools.enabled and cfg.memory.enabled):
        return None
    from jarvis.memory import MemoryStore

    return MemoryStore(cfg.memory.path, cfg.memory.max_facts)


def build_network_tool(cfg: Config, rooms=None):
    """Outil network_status : worker LLM, agents des PC, ampoules, Internet (connexions TCP courtes)."""
    from jarvis.tools.network import network_tool, url_target

    targets = [("le worker LLM", *url_target(cfg.llm.host))]
    for key, spec in (cfg.tools.devices or {}).items():
        if spec.get("url"):
            targets.append((str(spec.get("name", key)), *url_target(str(spec["url"]))))
    if rooms is not None:
        targets += [(f"la lumière {room.name}".replace("la lumière la ", "la lumière de la ")
                     .replace("la lumière l'", "la lumière de l'"), room.ip, 6668) for room in rooms.rooms()]
    if cfg.web.enabled:
        targets.append(("la recherche Web", *url_target(cfg.web.base_url)))
    targets.append(("Internet", "1.1.1.1", 443))
    return network_tool(targets)


def build_spotify(cfg: Config):
    """Client Spotify si l'identifiant (SPOTIFY_CLIENT_ID dans .env) et le jeton (--spotify-login) existent."""
    if not (cfg.tools.enabled and cfg.spotify.enabled):
        return None
    from jarvis.spotify import SpotifyClient

    client = SpotifyClient(secret("SPOTIFY_CLIENT_ID", ENV_FILE), cfg.spotify.token_path, device=cfg.spotify.device)
    if cfg.spotify.device and re.fullmatch(r"[\w@.-]+", cfg.spotify.player_service or "") \
            and not cfg.spotify.player_service.startswith("-"):
        import subprocess

        service = cfg.spotify.player_service
        # Commande fixe (aucun texte venu d'une demande) : relance du lecteur de JARVIS, service de l'utilisateur.
        client.revive = lambda: subprocess.run(["systemctl", "--user", "restart", service], check=True, timeout=20,
                                               capture_output=True)
    if not client.configured:
        log.info("Spotify non relié (SPOTIFY_CLIENT_ID et python -m jarvis --spotify-login) : touches multimédia seules")
        return None
    from jarvis.spotify import SpotifyCatalog

    client.catalog = SpotifyCatalog(cfg.spotify.catalog_path)
    if client.catalog.stale:
        def refresh() -> None:
            try:
                client.catalog.refresh(client)
            except Exception as exc:  # le catalogue est un plus : la recherche Spotify reste disponible
                log.warning("Catalogue Spotify non mis à jour : %s", exc)

        threading.Thread(target=refresh, name="catalogue-spotify", daemon=True).start()
    return client


def build_music_relay(cfg: Config):
    """Relais de la musique de librespot (tube) vers la sortie de la voix, si [spotify] device et [audio] remote sont
    renseignés ; None sinon."""
    if not (cfg.spotify.device and cfg.audio.remote):
        return None
    from jarvis.audio.music import MusicRelay

    relay = MusicRelay(cfg.spotify.pipe, cfg.audio.remote, secret("JARVIS_AGENT_TOKEN", ENV_FILE), cfg.spotify.duck_percent)
    try:
        relay.start()
    except OSError as exc:
        log.warning("Musique Spotify : relais impossible (%s)", exc)
        return None
    return relay


def build_calendar(cfg: Config):
    """Calendrier ([calendar]) : local (data/calendar.json) et, si renseigné, un fichier ou une adresse iCal en lecture
    seule ; None si désactivé."""
    if not (cfg.tools.enabled and cfg.calendar.enabled):
        return None
    from jarvis.agenda import Calendar, IcsCalendar, LocalCalendar

    providers = [LocalCalendar(cfg.calendar.path)]
    ics = cfg.calendar.ics or secret("JARVIS_CALENDAR_ICS", ENV_FILE)
    if ics:
        providers.append(IcsCalendar(ics))
    return Calendar(providers, day_start=cfg.calendar.day_start, day_end=cfg.calendar.day_end)


def build_routines(cfg: Config, tools, notifications, events: EventBus, sink=None, timers=None, calendar=None):
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
        events_today = engine.today(routines=False)  # programme de la journée : sans les routines
        if calendar is not None:
            try:
                events_today = [f"rendez-vous {line}" for line in calendar.today_lines()] + events_today
            except Exception as exc:
                log.warning("Calendrier indisponible pour l'annonce de la journée : %s", exc)
        if timers is not None:
            now = timers.now()
            events_today += [f"rappel « {r.message} » à {spoken_clock(r.expires_at.hour, r.expires_at.minute)}"
                             for r in timers.reminders() if r.expires_at.date() == now.date()]
        return events_today

    alarm = AlarmPlayer(sink, cfg.alarms.sound, cfg.alarms.max_minutes, before_ringing) \
        if sink is not None and cfg.alarms.enabled else None
    owner = next(u.id for u in build_profiles(cfg).users() if u.role == "owner")
    run_as_owner = lambda data: tools.submit(data, user=owner)  # noqa: E731
    announcer = Announcer(run_as_owner, today_events)
    engine = RoutineEngine(JsonRoutineStore(cfg.routines.path), tools.registry, run_as_owner, say, events,
                           alarm=alarm, announce=announcer.text)
    if alarm is not None:
        from jarvis.tools.alarms import alarm_tools

        for tool in alarm_tools(engine, cfg.alarms.briefing):
            tools.registry.register(tool)
    from jarvis.tools.routines import routine_tools

    for tool in routine_tools(engine):
        tools.registry.register(tool)
    engine.start()
    return engine


def build_api(cfg: Config, tools, routines, *, web=None, devices=None, rooms=None, driver=None, timers=None,
              worker=None, home=None, memory=None, profiles=None):
    """API d'administration démarrée ([api], pour JARVIS Control), ou None si désactivée."""
    if not cfg.api.enabled:
        return None
    from jarvis.activity import JsonlActivityStore
    from jarvis.api import CoreApi, CoreStatus
    from jarvis.face.themes import FaceThemeStore

    activity = JsonlActivityStore(cfg.activity.path) if cfg.activity.enabled else None
    status = CoreStatus(llm_url=cfg.llm.host, llm_model=cfg.llm.model, fallback_model=cfg.llm.fallback_model,
                        web=web, devices=devices, rooms=rooms, driver=driver, timers=timers, routines=routines,
                        activity=activity, worker=worker, home=home, profiles=profiles)
    api = CoreApi(cfg.api.host, cfg.api.port, frozenset(cfg.api.allowed_ips), secret("JARVIS_AGENT_TOKEN", ENV_FILE),
                  tools=tools, routines=routines, status=status, activity=activity, memory=memory,
                  face_themes=FaceThemeStore(cfg.face.theme_path) if cfg.face.enabled else None)
    try:
        api.start()
    except OSError as exc:
        log.warning("API d'administration indisponible (%s:%s) : %s", cfg.api.host, cfg.api.port, exc)
        return None
    return api


def build_tools(cfg: Config, personality, events: EventBus | None = None, timers=None, weather=None, devices=None,
                lights=(), extra=()):
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
    tools = [*tools, *lights, *extra]
    for tool in tools:
        registry.register(tool)
    confirmations = ConfirmationManager(personality.confirm_yes, personality.confirm_no,
                                        ignored=(personality.assistant_name, personality.user_title))
    profiles = build_profiles(cfg)
    owner = next(u.id for u in profiles.users() if u.role == "owner")
    return ToolCore(registry, PermissionManager(profiles=profiles), confirmations, timeout=cfg.tools.timeout,
                    user=profiles.context().user_id or owner, events=events)


def build_profiles(cfg: Config):
    """Profils ([users]) et terminaux ([terminals]) ; par défaut un propriétaire et le terminal « main »."""
    from jarvis.profiles import load_profiles

    return load_profiles(cfg.users, cfg.terminals, cfg.audio.remote)


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
    from jarvis.home import HomeState

    home = HomeState()
    memory = build_memory(cfg)
    extra = []
    speaker = {"core": None}  # utilisateur en cours (profil du terminal), connu une fois le Core des outils créé
    from jarvis.tools.core import request_user

    # Utilisateur de la demande exécutée (identité portée par le Core), sinon celui de la conversation.
    current_user = lambda: request_user(speaker["core"].user if speaker["core"] is not None else "owner")  # noqa: E731
    if memory is not None:
        from jarvis.memory import memory_tools

        extra += memory_tools(memory, current_user)
    spotify = build_spotify(cfg)
    music = build_music_relay(cfg) if spotify is not None else None
    if spotify is not None:
        from jarvis.spotify import spotify_tools

        extra += spotify_tools(spotify, on_pause=music.stop if music is not None else None)
    if cfg.face.enabled:
        from jarvis.face.themes import FaceThemeStore, face_theme_tool

        extra.append(face_theme_tool(FaceThemeStore(cfg.face.theme_path)))
    calendar = build_calendar(cfg)
    if calendar is not None:
        from jarvis.agenda import calendar_tools

        extra += calendar_tools(calendar)
    if cfg.tools.enabled:
        extra.append(build_network_tool(cfg, rooms))
    tools = build_tools(cfg, personality, events, timers, weather, devices,
                        light_tools_for(rooms, driver, cfg.lights.scenes, home), extra)
    speaker["core"] = tools
    if music is not None:
        from jarvis.audio.music import DuckingSink

        sink = DuckingSink(sink, music)
        forward = on_event

        def on_event(kind: str, text: str, forward=forward) -> None:
            music.on_event(kind)
            if forward is not None:
                forward(kind, text)
    routines = build_routines(cfg, tools, notifications, events, sink, timers, calendar)
    api = build_api(cfg, tools, routines, web=web, devices=devices, rooms=rooms, driver=driver, timers=timers,
                    worker=llm if hasattr(llm, "probe") else None, home=home, memory=memory,
                    profiles=build_profiles(cfg))
    if tools is not None and len(tools.registry):
        from jarvis.tools import ToolsCapability

        capabilities.register(ToolsCapability(tools.registry))
        log.info("Outils : %s", ", ".join(t.name for t in tools.registry.list()))
    tool_names = tuple(t.name for t in tools.registry.list()) if tools is not None else ()
    memory_text = None
    if memory is not None:
        from jarvis.memory import memory_prompt

        def memory_text() -> str:
            """Faits du seul utilisateur en cours, et seulement pour un adulte (jamais lus à un invité)."""
            profile = build_profiles(cfg).user(current_user())
            if profile is None or profile.role not in ("owner", "adult"):
                return ""
            return memory_prompt(memory, cfg.memory.prompt_facts, current_user())
    router = IntentRouter(personality, capabilities, web_enabled=web is not None, tools=tool_names, memory=memory_text)
    if hasattr(llm, "compact_system"):
        llm.compact_system = router.compact_prompt
    corrector = build_corrector(cfg, personality) if tools is not None else None
    log.info("Personnalité : %s, %d intentions prédéfinies", personality.assistant_name, len(personality.intents))
    prime_llm(llm, router, tools.registry if tools is not None else None)
    worker = llm if hasattr(llm, "probe") else None
    from jarvis.events import SYSTEM_ERROR, SYSTEM_RECOVERED, Event

    def report(source: str, ok: bool, message: str) -> None:
        events.publish(Event(SYSTEM_RECOVERED if ok else SYSTEM_ERROR, "system", {"source": source, "message": message}))

    if worker is not None:
        worker.on_state = lambda state, ok: report("llm", ok, f"worker LLM {state}")
        worker.start()
    if hasattr(source, "on_status"):
        source.on_status = lambda ok: report("micro", ok, "micro distant " + ("connecté" if ok else "injoignable"))

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
                 services=tuple(s for s in (api, routines, timers, notifications, worker) if s is not None),
                 fast_path=cfg.tools.fast_path, routines=routines, profiles=build_profiles(cfg),
                 wake_verifier=build_wake_verifier(cfg), wake_captures=build_wake_captures(cfg))
