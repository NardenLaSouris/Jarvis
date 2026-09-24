"""Tests des modes de diagnostic ``--tts-test`` et ``--tts-voices-test``.

Utilise la vraie voix Piper configurée ; la sortie audio est remplacée par un
enregistreur (aucun son joué). Lancement : python tests/test_tts_test.py
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.__main__ import TEST_PHRASES, tts_test, tts_voices_test  # noqa: E402
from jarvis.factory import build_tts  # noqa: E402
from jarvis.audio.files import RecordingSink  # noqa: E402
from jarvis.config import load_config  # noqa: E402

_CONFIG = load_config(ROOT / "config.toml")
CFG = replace(_CONFIG, tts=replace(_CONFIG.tts, engine="piper"))


def test_synthesizes_and_plays_once():
    sink = RecordingSink()
    assert tts_test(CFG, "Bonjour monsieur, je suis Jarvis.", sink=sink) == 0
    assert len(sink.played) == 1
    audio, rate = sink.played[0]
    assert rate > 0 and audio.dtype.name == "int16"
    assert 0.5 < len(audio) / rate < 10


def test_empty_text_plays_nothing():
    sink = RecordingSink()
    assert tts_test(CFG, "   ", sink=sink) == 2
    assert sink.played == []


def test_options_are_listed_in_help():
    out = subprocess.run([sys.executable, "-m", "jarvis", "--help"], cwd=ROOT,
                         capture_output=True, text=True, encoding="utf-8").stdout
    for option in ("--tts-test", "--tts-voice", "--tts-voices-test", "--no-pronunciations"):
        assert option in out


def test_voices_test_plays_every_phrase_for_every_voice():
    voices = ["fr_FR-tom-medium", "fr_FR-upmc-medium:pierre"]
    sink = RecordingSink()
    assert tts_voices_test(CFG, voices, sink=sink) == 0
    assert len(sink.played) == len(voices) * len(TEST_PHRASES)


def test_missing_voice_is_reported_without_playing():
    sink = RecordingSink()
    assert tts_voices_test(CFG, ["fr_FR-inexistante-medium"], sink=sink) == 3
    assert tts_test(CFG, "Bonjour", sink=sink, voice="fr_FR-inexistante-medium") == 3
    assert sink.played == []


def test_single_voice_option_uses_that_voice():
    sink = RecordingSink()
    assert tts_test(CFG, "Bonjour monsieur", sink=sink, voice="fr_FR-gilles-low") == 0
    assert sink.played[0][1] == 16000


def test_multi_speaker_voice_selects_named_speaker():
    assert build_tts(CFG, "fr_FR-upmc-medium:pierre")._config.speaker_id == 1
    assert build_tts(CFG, "fr_FR-upmc-medium:jessica")._config.speaker_id == 0


def test_pronunciation_lexicon_keeps_final_s_and_punctuation():
    phrase = "Jarvis. Bonjour monsieur. Jarvis, je vous écoute."
    assert build_tts(CFG, pronunciations=False).phonemes(phrase).startswith("ʒaʁvˈi.")
    with_lexicon = build_tts(CFG).phonemes(phrase)
    assert with_lexicon.startswith("ʒaʁvˈis.") and "ʒaʁvˈis," in with_lexicon
    assert build_tts(CFG).prepare("Je parle de jarvisse") == "Je parle de jarvisse"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
