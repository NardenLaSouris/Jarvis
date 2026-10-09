"""Chargement de la configuration centrale (config.toml)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AssistantConfig:
    language: str = "fr"
    personality: Path = Path("personality.toml")
    conversation_timeout: float = 8.0
    max_history_turns: int = 6
    barge_in: bool = True


@dataclass(frozen=True)
class WakeWordConfig:
    phrase: str = "Jarvis"
    model: Path = Path("models/openwakeword/hey_jarvis_v0.1.onnx")
    melspectrogram_model: Path = Path("models/openwakeword/melspectrogram.onnx")
    embedding_model: Path = Path("models/openwakeword/embedding_model.onnx")
    threshold: float = 0.5
    patience: int = 1
    # Seconde vérification par un petit Whisper sur le Core (jarvis/wakeword/verify.py) : « Jarvis » doit être
    # entendu dans l'audio du déclenchement.
    verify: bool = False
    verify_model: str = "tiny"
    # Réveil franc (score ≥ sure_score pendant sure_patience images, déclenchement compris ou juste après) : accepté
    # sans la vérification, qui reconnaît mal une voix lointaine ou noyée. 0 : toujours vérifier.
    sure_score: float = 0.0
    sure_patience: int = 3
    # Audio et issue de chaque réveil (jarvis/wakeword/captures.py) ; captures_keep = 0 : aucune capture.
    captures: Path = Path("data/wakeword/captures")
    captures_keep: int = 300


@dataclass(frozen=True)
class STTConfig:
    model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = 1
    download_root: Path = Path("models/whisper")
    fallback_device: str = "cpu"
    fallback_compute_type: str = "int8"
    vocabulary_hint: bool = True
    min_confidence: float = -1.0
    # Détection de la parole (Silero, fournie avec faster-whisper) avant la transcription. False : comportement
    # d'origine.
    vad_filter: bool = False


@dataclass(frozen=True)
class TTSConfig:
    engine: str = "piper"
    voice: Path = Path("models/piper/fr_FR-tom-medium.onnx")
    speaker: str = ""
    length_scale: float = 1.0
    volume: float = 1.0
    pronunciations: dict = field(default_factory=dict)
    stream_audio: bool = False
    merge_under: int = 0
    neutts: dict = field(default_factory=dict)
    effect: dict = field(default_factory=dict)


@dataclass(frozen=True)
class LLMConfig:
    backend: str = "ollama"
    host: str = "http://127.0.0.1:11434"
    model: str = "mistral:latest"
    temperature: float = 0.7
    max_tokens: int = 200
    keep_alive: str = "30m"
    timeout: float = 120.0
    fallback_host: str = ""
    fallback_model: str = ""
    # false : modèle non éprouvé (abliterated...) ; avec [tools.policy] engine = "both", ses propositions sont
    # limitées aux outils d'information, de maison, de médias et de minuteurs.
    trusted: bool = True
    # Modèles à réflexion (Qwen3...) : "false" pour répondre sans réfléchir (sinon la réflexion consomme les
    # max_tokens et la réponse reste vide) ; "" : réglage du modèle.
    think: str = ""
    # Garde-fou de ce que dit le modèle (jarvis/llm/guard.py) : "auto" (actif si trusted = false), "on" ou "off".
    guard: str = "auto"
    # Contexte du modèle principal (jetons) : le prompt du planificateur en fait environ 7 000 ; à 4 096 (défaut
    # d'Ollama), il était tronqué sans avertissement (outils et règles perdus) et jamais gardé en cache. 0 : défaut.
    num_ctx: int = 8192
    # Worker distant (fallback_host renseigné) : connexion de vérification, délai du secours, sonde, lenteur.
    connect_timeout: float = 1.0
    fallback_timeout: float = 30.0
    probe_interval: float = 10.0
    slow_after: float = 8.0
    retry_after: float = 30.0
    # Premier fragment de réponse attendu au plus tant de secondes (worker gelé : secours sans attendre ``timeout``).
    first_token_timeout: float = 10.0


@dataclass(frozen=True)
class WebConfig:
    enabled: bool = False
    provider: str = "searxng"
    base_url: str = "http://127.0.0.1:8080"
    language: str = "fr"
    max_results: int = 5
    timeout: float = 10.0
    fetch_pages: int = 1
    max_page_bytes: int = 1_000_000


@dataclass(frozen=True)
class ToolsConfig:
    enabled: bool = False
    timeout: float = 10.0
    fast_path: bool = True
    get_time: dict = field(default_factory=dict)
    get_date: dict = field(default_factory=dict)
    system_info: dict = field(default_factory=dict)
    open_url: dict = field(default_factory=dict)
    open_application: dict = field(default_factory=dict)
    close_application: dict = field(default_factory=dict)
    list_running_applications: dict = field(default_factory=dict)
    set_volume: dict = field(default_factory=dict)
    mute_volume: dict = field(default_factory=dict)
    unmute_volume: dict = field(default_factory=dict)
    lock_pc: dict = field(default_factory=dict)
    media_play_pause: dict = field(default_factory=dict)
    media_next: dict = field(default_factory=dict)
    media_previous: dict = field(default_factory=dict)
    find_files: dict = field(default_factory=dict)
    read_text_file: dict = field(default_factory=dict)
    create_text_file: dict = field(default_factory=dict)
    copy_file: dict = field(default_factory=dict)
    move_file: dict = field(default_factory=dict)
    delete_file: dict = field(default_factory=dict)
    files: dict = field(default_factory=dict)
    applications: dict = field(default_factory=dict)
    devices: dict = field(default_factory=dict)
    # Autorisation : engine = "builtin" (PermissionManager seul), "shadow" (Cedar journalisé) ou "both" (double verrou).
    policy: dict = field(default_factory=dict)

    def settings(self) -> dict[str, dict]:
        return {f.name: getattr(self, f.name) for f in fields(self)
                if isinstance(getattr(self, f.name), dict) and f.name != "policy"}


@dataclass(frozen=True)
class RoutinesConfig:
    enabled: bool = True
    path: Path = Path("data/routines.json")


@dataclass(frozen=True)
class AlarmsConfig:
    enabled: bool = True
    sound: Path = Path("data/alarm.mp3")
    max_minutes: float = 5.0
    volume: int = 0
    briefing: bool = True


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool = True
    path: Path = Path("data/memory.json")
    max_facts: int = 200
    prompt_facts: int = 20


@dataclass(frozen=True)
class CalendarConfig:
    enabled: bool = True
    path: Path = Path("data/calendar.json")
    ics: str = ""
    day_start: str = "08:00"
    day_end: str = "20:00"
    announce_minutes: float = 15.0  # rendez-vous annoncé ce nombre de minutes avant (0 : jamais)


@dataclass(frozen=True)
class SpotifyConfig:
    enabled: bool = True
    token_path: Path = Path("data/spotify_token.json")
    # Lecture par ORION lui-même (librespot sur le Core, appareil Spotify Connect « device »), relayée vers la sortie
    # de la voix ([audio] remote) ; vide = la musique joue sur un appareil Spotify existant (PC, téléphone).
    device: str = ""
    # Service utilisateur systemd du lecteur (librespot), relancé si l'appareil « device » a disparu de Spotify
    # (session perdue après une longue inactivité) ; vide = jamais relancé.
    player_service: str = ""
    pipe: Path = Path("data/music.fifo")
    catalog_path: Path = Path("data/spotify_catalog.json")
    duck_percent: int = 15


@dataclass(frozen=True)
class PresenceConfig:
    """Présence à la maison (jarvis/presence) : capteurs, fenêtres de corrélation, seuil, accueil au retour."""
    enabled: bool = False
    departure_window: float = 180.0
    arrival_window: float = 300.0
    threshold: int = 90
    absence_minutes: float = 20.0  # téléphone parti sans porte : départ après ce délai sans mouvement (0 : jamais)
    door_left_open_minutes: float = 10.0  # porte d'entrée ouverte plus longtemps : noté au journal (0 : jamais)
    max_event_age: float = 120.0
    welcome_routine: bool = True
    absence_summary: bool = True
    state_path: Path = Path("data/presence.json")
    journal_path: Path = Path("data/house_journal.jsonl")
    journal_max_kb: int = 1000
    sensors: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MailConfig:
    """Boîte mail (jarvis/mail) en IMAP/SMTP ; mot de passe (d'application) dans .env : MAIL_PASSWORD."""
    enabled: bool = False
    user: str = ""
    imap_host: str = ""
    imap_port: int = 993
    smtp_host: str = ""  # vide : imap_host avec « smtp. » au lieu de « imap. »
    smtp_port: int = 465
    folder: str = "INBOX"
    archive_folder: str = "Archive"
    trash_folder: str = "Trash"
    timeout: float = 20.0
    max_fetch: int = 100
    important_senders: tuple = ()
    noise_senders: tuple = ()
    contacts: dict = field(default_factory=dict)  # nom dit -> adresse (seuls destinataires possibles par leur nom)
    # Annonce des nouveaux mails (jarvis/mail/watch.py) : toutes les watch_minutes (0 : jamais), « important »,
    # « personnel » (importants et mails de vraies personnes) ou « aucun » ; rien n'est dit pendant les heures calmes.
    watch_minutes: float = 5.0
    announce: str = "important"
    quiet_start: str = "22:30"
    quiet_end: str = "08:00"
    seen_path: Path = Path("data/mail_seen.json")


@dataclass(frozen=True)
class ApiConfig:
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8766
    allowed_ips: tuple = ()


@dataclass(frozen=True)
class LightsConfig:
    enabled: bool = False
    timeout: float = 3.0
    rooms: dict = field(default_factory=dict)
    groups: dict = field(default_factory=dict)
    scenes: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TimersConfig:
    enabled: bool = False
    max_hours: float = 24.0
    reminder_max_hours: float = 72.0
    max_active: int = 20
    persist: bool = True
    path: Path = Path("data/schedule.json")


@dataclass(frozen=True)
class WeatherConfig:
    enabled: bool = False
    provider: str = "open-meteo"
    default_location: str = "Paris"
    latitude: float | None = None
    longitude: float | None = None
    country: str = "FR"
    temperature_unit: str = "celsius"
    timeout: float = 8.0
    current_cache_minutes: float = 5.0
    forecast_cache_minutes: float = 15.0


@dataclass(frozen=True)
class NotificationsConfig:
    voice_enabled: bool = True


@dataclass(frozen=True)
class ActivityConfig:
    enabled: bool = False
    path: Path = Path("data/activity.jsonl")
    max_kb: int = 1024


@dataclass(frozen=True)
class FaceConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = True
    night_start: str = "22:00"
    night_end: str = "07:00"
    # Couleur choisie à la voix ou dans ORION Control (jarvis/face/themes.py).
    theme_path: Path = Path("data/face_theme.json")
    # Thèmes de fête en « auto » (Noël, Halloween, Pâques...) ; birthday : « MM-JJ » pour le thème anniversaire.
    seasons: bool = True
    birthday: str = ""
    # Premier démarrage d'ORION (« AAAA-MM-JJ ») : son anniversaire chaque année.
    jarvis_birthday: str = ""


@dataclass(frozen=True)
class AudioConfig:
    sample_rate: int = 16000
    frame_samples: int = 1280
    input_device: str = ""
    output_device: str = ""
    remote: str = ""
    listen_timeout: float = 6.0
    end_of_speech_silence: float = 0.9
    max_utterance: float = 15.0
    min_rms: float = 300.0
    speech_to_noise_ratio: float = 3.0


@dataclass(frozen=True)
class Config:
    assistant: AssistantConfig
    wake_word: WakeWordConfig
    stt: STTConfig
    tts: TTSConfig
    llm: LLMConfig
    audio: AudioConfig
    web: WebConfig = WebConfig()
    face: FaceConfig = FaceConfig()
    tools: ToolsConfig = ToolsConfig()
    activity: ActivityConfig = ActivityConfig()
    timers: TimersConfig = TimersConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    weather: WeatherConfig = WeatherConfig()
    lights: LightsConfig = LightsConfig()
    routines: RoutinesConfig = RoutinesConfig()
    alarms: AlarmsConfig = AlarmsConfig()
    api: ApiConfig = ApiConfig()
    memory: MemoryConfig = MemoryConfig()
    calendar: CalendarConfig = CalendarConfig()
    spotify: SpotifyConfig = SpotifyConfig()
    presence: PresenceConfig = PresenceConfig()
    mail: MailConfig = MailConfig()
    users: dict = field(default_factory=dict)
    terminals: dict = field(default_factory=dict)


# Durées, délais et fréquences : strictement positifs (« timeout = -1 », « sample_rate = -5 » étaient acceptés).
POSITIVE = ("timeout", "interval", "sample_rate", "_after", "silence", "max_utterance", "max_minutes",
            "cache_minutes")


def _value(section: str, name: str, default: Any, value: Any, base_dir: Path) -> Any:
    """Valeur d'un réglage convertie au type de sa valeur par défaut ; ValueError nommant la clé sinon."""
    where = f"[{section}] {name}"
    if isinstance(default, Path):
        if not isinstance(value, str):
            raise ValueError(f"{where} : chemin (texte) attendu, pas {value!r}")
        path = Path(value)
        return path if path.is_absolute() else (base_dir / path).resolve()
    if isinstance(default, tuple):
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{where} : liste attendue, pas {value!r}")
        return tuple(value)
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{where} : true ou false attendu, pas {value!r}")
        return value
    if isinstance(default, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or (isinstance(default, int) and not isinstance(default, float) and isinstance(value, float)):
            raise ValueError(f"{where} : nombre attendu, pas {value!r}")
        value = float(value) if isinstance(default, float) else value
        if any(word in name for word in POSITIVE) and value <= 0:
            raise ValueError(f"{where} : doit être positif, pas {value!r}")
        return value
    if isinstance(default, str) and not isinstance(value, str):
        raise ValueError(f"{where} : texte attendu, pas {value!r}")
    return value


def _build(cls: type, data: dict[str, Any], base_dir: Path):
    """Instancie une section en convertissant et vérifiant les types (Path relatifs, tuples, nombres)."""
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"Clés inconnues dans [{cls.__name__}] : {sorted(unknown)}")
    section = cls.__name__.removesuffix("Config").lower()
    kwargs = {}
    for name, value in data.items():
        default = known[name].default
        kwargs[name] = _value(section, name, default, value, base_dir)
    instance = cls(**kwargs)
    # Les chemins non surchargés sont aussi résolus par rapport au fichier.
    for name in known:
        value = getattr(instance, name)
        if isinstance(value, Path) and not value.is_absolute():
            object.__setattr__(instance, name, (base_dir / value).resolve())
    return instance


def read_toml(path: Path) -> dict:
    """Fichier TOML, qu'il ait été enregistré avec ou sans BOM (certains éditeurs Windows en ajoutent un)."""
    return tomllib.loads(Path(path).read_text(encoding="utf-8-sig"))


def secret(name: str, env_file: Path) -> str:
    if os.environ.get(name):
        return os.environ[name]
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.strip() == name:
                return value.strip().strip('"').strip("'")
    return ""


def read_with_local(path: Path, local: bool = True) -> dict:
    """Lit ``path``, puis (si ``local``) ``<nom>.local.toml`` à côté s'il existe : réglages propres à la
    machine, non versionnés, qui remplacent les réglages du même nom section par section."""
    raw = read_toml(path)
    overrides = path.with_name(f"{path.stem}.local.toml")
    if local and overrides.exists():
        for section, values in read_toml(overrides).items():
            if not isinstance(values, dict):
                raise ValueError(f"{overrides.name} : [{section}] doit être une section")
            raw.setdefault(section, {}).update(values)
    return raw


def load_config(path: str | Path = "config.toml", local: bool = True) -> Config:
    path = Path(path).resolve()
    raw = read_with_local(path, local)
    base = path.parent
    sections = {
        "assistant": AssistantConfig,
        "wake_word": WakeWordConfig,
        "stt": STTConfig,
        "tts": TTSConfig,
        "llm": LLMConfig,
        "audio": AudioConfig,
        "web": WebConfig,
        "face": FaceConfig,
        "tools": ToolsConfig,
        "activity": ActivityConfig,
        "timers": TimersConfig,
        "notifications": NotificationsConfig,
        "weather": WeatherConfig,
        "lights": LightsConfig,
        "routines": RoutinesConfig,
        "alarms": AlarmsConfig,
        "api": ApiConfig,
        "memory": MemoryConfig,
        "calendar": CalendarConfig,
        "spotify": SpotifyConfig,
        "presence": PresenceConfig,
        "mail": MailConfig,
    }
    tables = {key: raw.pop(key, {}) for key in ("users", "terminals")}
    unknown = set(raw) - set(sections)
    if unknown:
        raise ValueError(f"Sections inconnues dans {path.name} : {sorted(unknown)}")
    return Config(**{key: _build(cls, raw.get(key, {}), base) for key, cls in sections.items()}, **tables)
