from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.audio.voicefx import ProcessedTTS, VoiceEffect, VoiceEffectSettings, with_effect  # noqa: E402
from jarvis.config import load_config  # noqa: E402


def voice_like(rate: int = 22050, seconds: float = 1.5) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)
    signal = sum(np.sin(2 * np.pi * 210 * k * t) / k for k in range(1, 12)) * envelope
    return (signal / np.abs(signal).max() * 20000).astype(np.int16)


class FakeTTS:
    sample_rate = 22050
    voice_name = "fausse"

    def __init__(self):
        self.audio = voice_like()

    def synthesize(self, text):
        return self.audio, self.sample_rate

    def warm_up(self):
        return "chaud"


class StreamingTTS(FakeTTS):
    def stream(self, text):
        yield self.audio[:8000]
        yield self.audio[8000:]


def test_process_keeps_length_type_and_peak():
    audio = voice_like()
    out = VoiceEffect(VoiceEffectSettings(enabled=True)).process(audio, 22050)
    assert out.dtype == np.int16 and len(out) == len(audio)
    assert np.abs(out).max() <= np.abs(audio).max() + 1
    assert not np.array_equal(out, audio)
    assert np.corrcoef(out.astype(float), audio.astype(float))[0, 1] > 0.8


def test_process_is_deterministic_and_handles_silence():
    effect = VoiceEffect(VoiceEffectSettings(enabled=True))
    audio = voice_like(44100, 0.7)
    assert np.array_equal(effect.process(audio, 44100), effect.process(audio, 44100))
    assert effect.process(np.zeros(0, np.int16), 22050).size == 0
    assert not effect.process(np.zeros(500, np.int16), 22050).any()


def test_neutral_settings_change_nothing_audible():
    neutral = VoiceEffectSettings(enabled=True, low_cut_hz=0.0, presence_db=0.0, air_db=0.0, shimmer=0.0,
                                  compression_ratio=1.0, peak_db=0.0)
    audio = voice_like()
    out = VoiceEffect(neutral).process(audio, 22050)
    assert np.abs(out.astype(int) - audio.astype(int)).max() <= 2


def test_disabled_effect_returns_engine_unchanged():
    tts = FakeTTS()
    assert with_effect(tts, {}) is tts
    assert with_effect(tts, {"enabled": False, "shimmer": 0.3}) is tts


def test_unknown_setting_is_refused():
    with pytest.raises(ValueError, match="inconnus"):
        with_effect(FakeTTS(), {"enabled": True, "reverb": 0.5})


def test_wrapper_processes_and_delegates():
    tts = with_effect(FakeTTS(), {"enabled": True})
    assert isinstance(tts, ProcessedTTS)
    audio, rate = tts.synthesize("Bonjour")
    assert rate == 22050 and len(audio) == len(FakeTTS().audio)
    assert tts.voice_name == "fausse" and tts.sample_rate == 22050 and tts.warm_up() == "chaud"
    assert not hasattr(tts, "stream")
    assert tts.last_effect_s >= 0.0


def test_stream_is_processed_only_when_engine_streams():
    tts = with_effect(StreamingTTS(), {"enabled": True})
    chunks = list(tts.stream("Bonjour"))
    assert [len(c) for c in chunks] == [8000, len(FakeTTS().audio) - 8000]


def test_processing_error_falls_back_to_original_voice(monkeypatch):
    tts = with_effect(FakeTTS(), {"enabled": True})

    def broken(audio, rate):
        raise RuntimeError("panne")

    monkeypatch.setattr(tts._effect, "process", broken)
    audio, _ = tts.synthesize("Bonjour")
    assert np.array_equal(audio, FakeTTS().audio)


def test_config_exposes_effect():
    cfg = load_config(ROOT / "config.toml", local=False)
    settings = VoiceEffectSettings.from_dict(cfg.tts.effect)
    assert settings.enabled and 0.0 <= settings.shimmer < 0.5
