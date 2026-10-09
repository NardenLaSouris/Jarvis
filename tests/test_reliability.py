"""Reprise du Core : une erreur imprévue pendant une conversation ne fait pas tomber ORION.

Aucun son, aucun modèle : source audio en mémoire, faux STT, faux LLM.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.agent import MAX_CRASHES, Agent, AgentSettings  # noqa: E402
from jarvis.audio.endpointing import EndpointerSettings, UtteranceRecorder  # noqa: E402
from jarvis.audio.files import ArraySource, RecordingSink  # noqa: E402
from jarvis.capabilities import CapabilityRegistry  # noqa: E402
from jarvis.personality import load_personality  # noqa: E402
from jarvis.router import IntentRouter  # noqa: E402

RATE, FRAME = 16000, 1280


def conversations(count: int) -> np.ndarray:
    silence = np.zeros(RATE, np.int16)
    speech = (5000 * np.sin(np.arange(RATE) / RATE * 2 * np.pi * 300)).astype(np.int16)
    parts = [silence]
    for _ in range(count):
        parts += [np.full(FRAME, 30000, np.int16), np.zeros(FRAME * 4, np.int16), speech, np.zeros(RATE * 4, np.int16)]
    return np.concatenate(parts)


def run(count: int, failures: int):
    heard, spoken = [], []

    class Wake:
        def process(self, f):
            return 1.0 if f.max() > 20000 else 0.0

        def reset(self):
            pass

    class Stt:
        def transcribe(self, clip, rate):
            heard.append(len(heard))
            if len(heard) <= failures:
                raise RuntimeError("panne imprévue du STT")
            return "Quelle heure est-il ?"

    class Llm:
        def chat(self, messages):
            return "Réponse."

    class Tts:
        def synthesize(self, text):
            spoken.append(text)
            return np.zeros(10, np.int16), RATE

    source = ArraySource(conversations(count), RATE, FRAME)
    router = IntentRouter(load_personality(ROOT / "personality.toml"), CapabilityRegistry())
    settings = AgentSettings("ORION", "Orion", 0.5, ("Oui ?",), 2.0, 1.0, 4)
    Agent(settings, source, RecordingSink(), Wake(), UtteranceRecorder(source, EndpointerSettings()), Stt(), Llm(),
          Tts(), router, lambda kind, text: None).run()
    return heard, spoken


def test_a_crashing_conversation_does_not_stop_orion(caplog):
    with caplog.at_level(logging.ERROR):
        heard, spoken = run(count=3, failures=1)
    assert len(heard) == 3  # les deux conversations suivantes ont eu lieu
    assert "Erreur imprévue pendant la conversation" in caplog.text


def test_repeated_crashes_hand_over_to_systemd():
    with pytest.raises(RuntimeError):
        run(count=MAX_CRASHES + 2, failures=MAX_CRASHES + 2)


def test_a_reset_connection_is_logged_quietly(caplog):
    from jarvis.face.server import ExclusiveServer

    server = ExclusiveServer.__new__(ExclusiveServer)
    try:
        raise ConnectionResetError(104, "Connection reset by peer")
    except ConnectionResetError:
        with caplog.at_level(logging.DEBUG):
            server.handle_error(None, ("192.168.1.73", 50000))
    assert "Traceback" not in caplog.text
