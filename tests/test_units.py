"""Tests rapides, sans modèle ni matériel audio.

Lancement : python -m pytest tests/test_units.py   (ou python tests/test_units.py)
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.agent import Agent, AgentSettings, clean_for_speech  # noqa: E402
from jarvis.audio.endpointing import EndpointerSettings, UtteranceRecorder  # noqa: E402
from jarvis.audio.files import ArraySource, RecordingSink  # noqa: E402
from jarvis.capabilities import CapabilityRegistry  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.prompts import build_system_prompt  # noqa: E402

SR, FRAME = 16000, 1280


def tone(seconds: float, amplitude: int = 3000) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return (amplitude * np.sin(2 * np.pi * 220 * t)).astype(np.int16)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SR), dtype=np.int16)


def test_config_loads_and_resolves_paths():
    cfg = load_config(ROOT / "config.toml")
    assert cfg.assistant.name == "Jarvis"
    assert cfg.wake_word.phrase == "Jarvis"
    assert cfg.wake_word.model.is_absolute()


def test_endpointing_captures_one_utterance():
    source = ArraySource(np.concatenate([silence(1), tone(1.5), silence(2)]), SR, FRAME)
    audio = UtteranceRecorder(source, EndpointerSettings(end_of_speech_silence=0.8)).record(start_timeout=5)
    assert audio is not None
    assert 1.5 <= len(audio) / SR <= 2.8


def test_endpointing_times_out_on_silence():
    source = ArraySource(silence(5), SR, FRAME)
    assert UtteranceRecorder(source, EndpointerSettings()).record(start_timeout=2) is None


def test_clean_for_speech_strips_markdown():
    assert clean_for_speech("**Bien sûr**, monsieur :\n- un\n- deux") == "Bien sûr, monsieur : un deux"


def test_prompt_mentions_unavailable_features():
    prompt = build_system_prompt("Jarvis", CapabilityRegistry())
    assert "Jarvis" in prompt and "pas encore disponible" in prompt and "domotique" in prompt


class FakeWake:
    """Se déclenche sur les blocs dont l'amplitude dépasse 20000."""

    def process(self, frame):
        return 1.0 if np.abs(frame).max() > 20000 else 0.0

    def reset(self):
        pass


class FakeSTT:
    def transcribe(self, audio, sample_rate):
        return "bonjour"


class FakeTTS:
    def synthesize(self, text):
        return np.zeros(10, dtype=np.int16), 22050


class FakeLLM:
    def __init__(self):
        self.calls = []

    def chat(self, messages):
        self.calls.append(messages)
        return "Bonjour, monsieur."

    def warm_up(self):
        pass


def test_agent_conversation_flow_with_follow_up_and_sleep():
    audio = np.concatenate([
        silence(1), tone(0.2, 30000), silence(0.5),   # wake word
        tone(1.0), silence(1.5),                       # 1re phrase
        tone(1.0), silence(4),                         # relance sans wake word, puis veille
        tone(1.0), silence(1),                         # parole en veille : ignorée
    ])
    source = ArraySource(audio, SR, FRAME)
    sink, llm, events = RecordingSink(), FakeLLM(), []
    settings = AgentSettings("Jarvis", "Jarvis", 0.5, ("Oui, monsieur ?",), 3.0, 2.0, 6)
    agent = Agent(settings, source, sink, FakeWake(), UtteranceRecorder(source, EndpointerSettings()),
                  FakeSTT(), llm, FakeTTS(), CapabilityRegistry(), lambda k, t: events.append(k))
    agent.run()
    assert events.count("wake") == 1
    assert events.count("user") == 2
    assert len(sink.played) == 3                  # accusé + 2 réponses
    assert len(llm.calls[1]) == 4                 # système + historique de la conversation


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
