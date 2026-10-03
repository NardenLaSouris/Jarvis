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
    applications: dict = field(default_factory=dict)
    devices: dict = field(default_factory=dict)

    def settings(self) -> dict[str, dict]:
        return {f.name: getattr(self, f.name) for f in fields(self) if isinstance(getattr(self, f.name), dict)}


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


@dataclass(frozen=True)
class TimersConfig:
    enabled: bool = False
    max_hours: float = 24.0
    max_active: int = 20


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


def _build(cls: type, data: dict[str, Any], base_dir: Path):
    """Instancie une section en convertissant les types (Path relatifs, tuples)."""
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"Clés inconnues dans [{cls.__name__}] : {sorted(unknown)}")
    kwargs = {}
    for name, value in data.items():
        default = known[name].default
        if isinstance(default, Path):
            path = Path(value)
            value = path if path.is_absolute() else (base_dir / path).resolve()
        elif isinstance(default, tuple):
            value = tuple(value)
        elif isinstance(default, float):
            value = float(value)
        kwargs[name] = value
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
    }
    unknown = set(raw) - set(sections)
    if unknown:
        raise ValueError(f"Sections inconnues dans {path.name} : {sorted(unknown)}")
    return Config(**{key: _build(cls, raw.get(key, {}), base) for key, cls in sections.items()})
