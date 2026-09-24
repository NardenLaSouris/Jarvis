"""Tests du moteur ElevenLabs et du repli sur Piper, sans aucun appel réseau.

Lancement : python tests/test_elevenlabs.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import urllib.error
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import jarvis.tts.elevenlabs as eleven  # noqa: E402
from jarvis import factory  # noqa: E402
from jarvis.config import load_config, secret  # noqa: E402
from jarvis.tts.fallback import FallbackTTS  # noqa: E402

CFG = load_config(ROOT / "config.toml")


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def with_fake_api(handler):
    original = eleven.urllib.request.urlopen
    eleven.urllib.request.urlopen = handler
    return original


def test_request_and_pcm_decoding():
    seen = {}
    pcm = np.array([0, 1000, -1000, 32767], dtype="<i2").tobytes()

    def handler(request, timeout):
        seen.update(url=request.full_url, key=request.get_header("Xi-api-key"), body=json.loads(request.data))
        return FakeResponse(pcm)

    original = with_fake_api(handler)
    try:
        tts = eleven.ElevenLabsTTS("VOIX", "CLE", output_format="pcm_22050", voice_settings={"stability": 0.6})
        audio, rate = tts.synthesize("Bonjour monsieur.")
    finally:
        eleven.urllib.request.urlopen = original
    assert rate == 22050 and audio.dtype == np.int16 and audio.tolist() == [0, 1000, -1000, 32767]
    assert seen["url"].endswith("/VOIX?output_format=pcm_22050") and seen["key"] == "CLE"
    assert seen["body"]["text"] == "Bonjour monsieur." and seen["body"]["voice_settings"] == {"stability": 0.6}


def test_http_error_raises_tts_error():
    def handler(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"detail":"invalid key"}'))

    original = with_fake_api(handler)
    try:
        eleven.ElevenLabsTTS("VOIX", "CLE").synthesize("Bonjour")
    except eleven.TTSError as exc:
        assert "401" in str(exc)
    else:
        raise AssertionError("une erreur HTTP doit lever TTSError")
    finally:
        eleven.urllib.request.urlopen = original


def test_missing_key_is_refused():
    try:
        eleven.ElevenLabsTTS("VOIX", "")
    except eleven.TTSError:
        return
    raise AssertionError("une clé vide doit être refusée")


def test_fallback_uses_local_voice_when_primary_fails():
    class Broken:
        def synthesize(self, text):
            raise eleven.TTSError("réseau coupé")

    class Local:
        def synthesize(self, text):
            return np.ones(10, np.int16), 16000

    audio, rate = FallbackTTS(Broken(), Local()).synthesize("Bonjour")
    assert rate == 16000 and audio.size == 10


def test_factory_falls_back_to_piper_without_key():
    cfg = replace(CFG, tts=replace(CFG.tts, engine="elevenlabs", fallback_to_piper=True))
    original_env_file = factory.ENV_FILE
    saved_key = os.environ.pop("ELEVENLABS_API_KEY", None)
    factory.ENV_FILE = Path(tempfile.gettempdir()) / "jarvis-absent.env"
    try:
        assert type(factory.build_tts(cfg)).__name__ == "PiperTTS"
    finally:
        factory.ENV_FILE = original_env_file
        if saved_key is not None:
            os.environ["ELEVENLABS_API_KEY"] = saved_key


def test_secret_is_read_from_env_file():
    with tempfile.TemporaryDirectory() as tmp:
        env = Path(tmp) / ".env"
        env.write_text('AUTRE=1\nJARVIS_TEST_SECRET="abc123"\n', encoding="utf-8")
        os.environ.pop("JARVIS_TEST_SECRET", None)
        assert secret("JARVIS_TEST_SECRET", env) == "abc123"
        assert secret("ABSENT", env) == ""


def test_config_selects_elevenlabs_voice():
    assert CFG.tts.elevenlabs["voice_id"] == "CwhRBWXzGAHq8TQ4Fs17"
    assert CFG.tts.elevenlabs["output_format"].startswith("pcm_")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
