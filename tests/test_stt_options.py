"""Options du STT (faster-whisper) : VAD et indice de vocabulaire transmis tels quels, sans modèle chargé."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytest.importorskip("faster_whisper")

from jarvis.config import load_config  # noqa: E402
from jarvis.stt.faster_whisper import FasterWhisperSTT  # noqa: E402


class FakeModel:
    def __init__(self, segments):
        self.segments, self.calls = segments, []

    def transcribe(self, samples, **options):
        self.calls.append(options)
        return iter(self.segments), None


def stt(vad: bool, segments=()):
    engine = FasterWhisperSTT.__new__(FasterWhisperSTT)
    engine._model = FakeModel(list(segments))
    engine._language, engine._beam_size, engine.hotwords, engine.vad_filter = "fr", 1, "Orion, ouvre Discord.", vad
    engine.last_confidence = None
    return engine


def segment(text, no_speech=0.1, logprob=-0.2):
    return SimpleNamespace(text=text, no_speech_prob=no_speech, avg_logprob=logprob)


@pytest.mark.parametrize("vad", [True, False])
def test_vad_option_reaches_whisper(vad):
    engine = stt(vad, [segment(" Bonjour.")])
    assert engine.transcribe(np.zeros(16000, np.int16), 16000) == "Bonjour."
    options = engine._model.calls[0]
    assert options["vad_filter"] is vad and options["language"] == "fr" and options["hotwords"] == "Orion, ouvre Discord."


def test_segments_without_speech_are_dropped():
    engine = stt(True, [segment(" Sous-titres réalisés par la communauté.", no_speech=0.9), segment(" Oui.")])
    assert engine.transcribe(np.zeros(16000, np.int16), 16000) == "Oui."


def test_wrong_sample_rate_is_refused():
    with pytest.raises(ValueError):
        stt(True).transcribe(np.zeros(8000, np.int16), 8000)


def test_vad_is_on_in_the_shipped_config_and_off_by_default():
    assert load_config(ROOT / "config.toml", local=False).stt.vad_filter is True
    from jarvis.config import STTConfig

    assert STTConfig().vad_filter is False
