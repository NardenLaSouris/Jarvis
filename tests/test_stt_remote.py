"""Transcription distante (jarvis.stt.server, jarvis.stt.remote) : vrai serveur HTTP local, Whisper simulé."""

from __future__ import annotations

import base64
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.stt.remote import RemoteSTT  # noqa: E402
from jarvis.stt.server import handler  # noqa: E402

TOKEN = "jeton-de-test-0123456789"


class FakeTranscriber:
    name = "faux"

    def __init__(self):
        self.requests = []

    def transcribe(self, request: dict) -> dict:
        self.requests.append(request)
        samples = len(base64.b64decode(request["audio"])) // 2
        return {"text": f"gpu {samples}", "confidence": -0.2, "seconds": 0.01}


class FakeLocal:
    hotwords = "Orion"
    last_confidence = -0.5

    def __init__(self):
        self.calls = []

    def transcribe(self, audio, rate):
        self.calls.append(self.hotwords)
        return "local"


@pytest.fixture
def server():
    fake = FakeTranscriber()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler(fake, TOKEN))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", fake
    httpd.shutdown()


AUDIO = np.zeros(16000, dtype=np.int16)


def test_speech_is_transcribed_on_the_gpu_with_the_vocabulary_hint(server):
    url, fake = server
    local = FakeLocal()
    stt = RemoteSTT(url, TOKEN, local, "fr")
    stt.hotwords = "Orion, Rammstein"  # mis à jour par l'apprentissage du vocabulaire
    assert stt.transcribe(AUDIO, 16000) == "gpu 16000" and stt.last_source == "gpu"
    assert stt.last_confidence == -0.2 and local.calls == []
    assert fake.requests[0]["hotwords"] == "Orion, Rammstein" and fake.requests[0]["language"] == "fr"


def test_gpu_absent_falls_back_to_local_whisper_then_comes_back(server):
    url, _ = server
    now, up = [0.0], [False]
    local = FakeLocal()
    stt = RemoteSTT(url, TOKEN, local, "fr", reachable=lambda u: up[0], clock=lambda: now[0], retry_after=30)
    stt.hotwords = "Orion, Rammstein"
    assert stt.transcribe(AUDIO, 16000) == "local" and stt.last_source == "local"
    assert local.calls == ["Orion, Rammstein"]  # le secours reçoit aussi le vocabulaire
    up[0] = True
    assert stt.transcribe(AUDIO, 16000) == "local"  # pas de nouvel essai avant retry_after
    now[0] = 31
    assert stt.transcribe(AUDIO, 16000) == "gpu 16000"


def test_a_wrong_token_is_refused_and_local_whisper_answers(server):
    url, fake = server
    stt = RemoteSTT(url, "mauvais-jeton-0123456789", FakeLocal(), "fr")
    assert stt.transcribe(AUDIO, 16000) == "local" and fake.requests == []
