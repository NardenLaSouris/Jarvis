"""Signature synthétique discrète de la voix d'ORION, appliquée en mémoire à la sortie du TTS."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, fields
from typing import Iterator

import numpy as np

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class VoiceEffectSettings:
    enabled: bool = False
    low_cut_hz: float = 90.0
    presence_db: float = 2.0
    presence_hz: float = 3500.0
    air_db: float = 2.5
    air_hz: float = 8000.0
    shimmer: float = 0.16
    shimmer_ms: float = 0.8
    compression_ratio: float = 2.5
    threshold_db: float = -20.0
    peak_db: float = -1.0

    @classmethod
    def from_dict(cls, values: dict) -> "VoiceEffectSettings":
        known = {f.name for f in fields(cls)}
        unknown = set(values) - known
        if unknown:
            raise ValueError(f"[tts.effect] : réglages inconnus {sorted(unknown)}")
        return cls(**values)


def _shelf(freqs: np.ndarray, corner: float, gain_db: float) -> np.ndarray:
    return gain_db / (1.0 + (corner / np.maximum(freqs, 1.0)) ** 2)


def _bell(freqs: np.ndarray, center: float, gain_db: float, octaves: float = 1.2) -> np.ndarray:
    distance = np.log2(np.maximum(freqs, 1.0) / center) / (octaves / 2)
    return gain_db * np.exp(-0.5 * distance ** 2)


class VoiceEffect:
    def __init__(self, settings: VoiceEffectSettings):
        self.settings = settings
        self._responses: dict[tuple[int, int], np.ndarray] = {}

    def _response(self, n: int, rate: int) -> np.ndarray:
        key = (n, rate)
        response = self._responses.get(key)
        if response is None:
            s = self.settings
            freqs = np.fft.rfftfreq(n, 1.0 / rate)
            gain_db = _bell(freqs, s.presence_hz, s.presence_db) + _shelf(freqs, s.air_hz, s.air_db)
            response = 10 ** (gain_db / 20) * (1.0 / (1.0 + (s.low_cut_hz / np.maximum(freqs, 1.0)) ** 4))
            delay = s.shimmer_ms / 1000.0
            response = response * (1.0 + s.shimmer * np.exp(-2j * np.pi * freqs * delay)) / (1.0 + s.shimmer)
            if len(self._responses) > 32:
                self._responses.clear()
            self._responses[key] = response
        return response

    def _compress(self, audio: np.ndarray, rate: int) -> np.ndarray:
        s = self.settings
        if s.compression_ratio <= 1.0:
            return audio
        hop = max(1, rate // 200)
        frames = len(audio) // hop + 1
        padded = np.zeros(frames * hop, np.float32)
        padded[:len(audio)] = audio
        level = 20 * np.log10(np.sqrt((padded.reshape(frames, hop) ** 2).mean(1)) + 1e-6)
        over = np.maximum(level - s.threshold_db, 0.0)
        target = -over * (1.0 - 1.0 / s.compression_ratio)
        attack, release = np.exp(-1.0 / (0.005 * 200)), np.exp(-1.0 / (0.08 * 200))
        gain = np.empty(frames, np.float32)
        current = 0.0
        for i, value in enumerate(target):
            coef = attack if value < current else release
            current = coef * current + (1.0 - coef) * value
            gain[i] = current
        centers = np.arange(frames) * hop + hop / 2
        return audio * 10 ** (np.interp(np.arange(len(audio)), centers, gain) / 20)

    def process(self, audio: np.ndarray, rate: int) -> np.ndarray:
        if audio.size == 0:
            return audio
        x = audio.astype(np.float32) / 32768.0
        peak_in = float(np.abs(x).max())
        n = 1 << int(np.ceil(np.log2(len(x) + int(rate * self.settings.shimmer_ms / 1000) + 1)))
        y = np.fft.irfft(np.fft.rfft(x, n) * self._response(n, rate), n)[:len(x)].astype(np.float32)
        y = self._compress(y, rate)
        peak = float(np.abs(y).max())
        if peak > 0:
            y *= min(10 ** (self.settings.peak_db / 20), max(peak_in, 1e-3)) / peak
        return (np.clip(y, -1.0, 1.0) * 32767).astype(np.int16)


class ProcessedTTS:
    def __init__(self, tts, effect: VoiceEffect):
        self._tts = tts
        self._effect = effect
        self.sample_rate = tts.sample_rate
        self.voice_name = getattr(tts, "voice_name", "")
        self.last_effect_s = 0.0
        if hasattr(tts, "stream"):
            self.stream = self._stream

    def __getattr__(self, name):
        return getattr(self._tts, name)

    def _apply(self, audio: np.ndarray, rate: int) -> np.ndarray:
        started = time.perf_counter()
        try:
            processed = self._effect.process(audio, rate)
        except Exception:
            log.exception("Traitement de la voix en échec : voix d'origine")
            processed = audio
        self.last_effect_s = time.perf_counter() - started
        return processed

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        audio, rate = self._tts.synthesize(text)
        return self._apply(np.asarray(audio, dtype=np.int16), rate), rate

    def _stream(self, text: str) -> Iterator[np.ndarray]:
        for chunk in self._tts.stream(text):
            yield self._apply(np.asarray(chunk, dtype=np.int16), self.sample_rate)


def with_effect(tts, values: dict):
    settings = VoiceEffectSettings.from_dict(values or {})
    return ProcessedTTS(tts, VoiceEffect(settings)) if settings.enabled else tts
