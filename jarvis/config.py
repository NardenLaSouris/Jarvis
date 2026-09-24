"""Chargement de la configuration centrale (config.toml)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AssistantConfig:
    name: str = "Jarvis"
    language: str = "fr"
    acknowledgements: tuple[str, ...] = ("Oui, monsieur ?",)
    conversation_timeout: float = 8.0
    max_history_turns: int = 6


@dataclass(frozen=True)
class WakeWordConfig:
    phrase: str = "Jarvis"
    model: Path = Path("models/openwakeword/hey_jarvis_v0.1.onnx")
    melspectrogram_model: Path = Path("models/openwakeword/melspectrogram.onnx")
    embedding_model: Path = Path("models/openwakeword/embedding_model.onnx")
    threshold: float = 0.5


@dataclass(frozen=True)
class STTConfig:
    model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = 1
    download_root: Path = Path("models/whisper")


@dataclass(frozen=True)
class TTSConfig:
    engine: str = "piper"
    fallback_to_piper: bool = True
    elevenlabs: dict = field(default_factory=dict)
    voice: Path = Path("models/piper/fr_FR-tom-medium.onnx")
    speaker: str = ""
    length_scale: float = 1.0
    volume: float = 1.0
    pronunciations: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMConfig:
    backend: str = "ollama"
    host: str = "http://127.0.0.1:11434"
    model: str = "mistral:latest"
    temperature: float = 0.7
    max_tokens: int = 200
    keep_alive: str = "30m"
    timeout: float = 120.0


@dataclass(frozen=True)
class AudioConfig:
    sample_rate: int = 16000
    frame_samples: int = 1280
    input_device: str = ""
    output_device: str = ""
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
    for name, f in known.items():
        value = getattr(instance, name)
        if isinstance(value, Path) and not value.is_absolute():
            object.__setattr__(instance, name, (base_dir / value).resolve())
    return instance


def secret(name: str, env_file: Path) -> str:
    if os.environ.get(name):
        return os.environ[name]
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.strip() == name:
                return value.strip().strip('"').strip("'")
    return ""


def load_config(path: str | Path = "config.toml") -> Config:
    path = Path(path).resolve()
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    base = path.parent
    sections = {
        "assistant": AssistantConfig,
        "wake_word": WakeWordConfig,
        "stt": STTConfig,
        "tts": TTSConfig,
        "llm": LLMConfig,
        "audio": AudioConfig,
    }
    unknown = set(raw) - set(sections)
    if unknown:
        raise ValueError(f"Sections inconnues dans {path.name} : {sorted(unknown)}")
    return Config(**{key: _build(cls, raw.get(key, {}), base) for key, cls in sections.items()})
