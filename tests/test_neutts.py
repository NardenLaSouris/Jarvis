from __future__ import annotations

import sys
import tempfile
import types
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import jarvis.tts.neutts as engine_module  # noqa: E402
from jarvis.__main__ import tts_test  # noqa: E402
from jarvis.audio.files import RecordingSink  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.streaming import SpeechPipeline  # noqa: E402


class FakeNeuTTS:
    instances: list = []

    def __init__(self, backbone_repo, backbone_device, codec_repo, codec_device):
        self.args = (backbone_repo, backbone_device, codec_repo, codec_device)
        FakeNeuTTS.instances.append(self)

    def infer(self, text, ref_codes, ref_text):
        return np.full(2400, 0.5, np.float32)

    def infer_stream(self, text, ref_codes, ref_text):
        for value in (0.25, -0.25, 2.0):
            yield np.full(1200, value, np.float32)


ORIGINAL_NEUTTS = sys.modules.get("neutts")


def install_fake_neutts():
    module = types.ModuleType("neutts")
    module.NeuTTS = FakeNeuTTS
    sys.modules["neutts"] = module


def teardown_module():
    if ORIGINAL_NEUTTS is None:
        sys.modules.pop("neutts", None)
    else:
        sys.modules["neutts"] = ORIGINAL_NEUTTS


def make_voice(folder: Path, with_codes: bool = True) -> Path:
    voice = folder / "voix.wav"
    voice.write_bytes(b"")
    voice.with_suffix(".txt").write_text("Texte de référence.", encoding="utf-8")
    if with_codes:
        np.save(voice.with_suffix(".codes.npy"), np.arange(10))
    return voice


def test_engine_runs_on_cpu_with_configured_models_and_cached_reference():
    install_fake_neutts()
    with tempfile.TemporaryDirectory() as tmp:
        engine = engine_module.NeuTTSEngine("repo/q8-gguf", "repo/onnx", make_voice(Path(tmp)))
    assert FakeNeuTTS.instances[-1].args == ("repo/q8-gguf", "cpu", "repo/onnx", "cpu")
    assert engine.sample_rate == 24000 and engine.voice_name == "neutts:voix"


def test_synthesize_returns_int16_audio():
    install_fake_neutts()
    with tempfile.TemporaryDirectory() as tmp:
        engine = engine_module.NeuTTSEngine("b", "c", make_voice(Path(tmp)))
    audio, rate = engine.synthesize("Bonjour.")
    assert audio.dtype == np.int16 and rate == 24000 and len(audio) == 2400 and audio[0] == 16383


def test_stream_yields_int16_chunks_and_clips():
    install_fake_neutts()
    with tempfile.TemporaryDirectory() as tmp:
        engine = engine_module.NeuTTSEngine("b", "c", make_voice(Path(tmp)))
    chunks = list(engine.stream("Bonjour."))
    assert [c.dtype for c in chunks] == [np.int16] * 3
    assert [int(c[0]) for c in chunks] == [8191, -8191, 32767]


def test_reference_is_encoded_once_then_cached():
    calls = []
    original = engine_module.encode_reference
    engine_module.encode_reference = lambda voice: calls.append(voice) or np.arange(5)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            voice = make_voice(Path(tmp), with_codes=False)
            codes, text = engine_module.load_reference(voice)
            again, _ = engine_module.load_reference(voice)
    finally:
        engine_module.encode_reference = original
    assert len(calls) == 1 and list(codes) == list(again) == [0, 1, 2, 3, 4] and text == "Texte de référence."


def test_missing_reference_is_reported():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            engine_module.load_reference(Path(tmp) / "absente.wav")
        except FileNotFoundError as exc:
            assert "absente" in str(exc)
        else:
            raise AssertionError("une voix absente doit être signalée")


def test_pipeline_plays_streamed_chunks_in_order_and_counts_sentences_once():
    install_fake_neutts()
    with tempfile.TemporaryDirectory() as tmp:
        engine = engine_module.NeuTTSEngine("b", "c", make_voice(Path(tmp)))
    sink = RecordingSink()
    stats = SpeechPipeline(engine, sink, stream_audio=True).speak(["Première phrase complète.", "Seconde phrase ici."])
    assert stats.sentences == ["Première phrase complète.", "Seconde phrase ici."]
    assert [int(a[0]) for a, _ in sink.played] == [8191, -8191, 32767] * 2
    assert abs(stats.audio_seconds - 6 * 1200 / 24000) < 1e-9


def test_pipeline_without_streaming_plays_whole_sentences():
    install_fake_neutts()
    with tempfile.TemporaryDirectory() as tmp:
        engine = engine_module.NeuTTSEngine("b", "c", make_voice(Path(tmp)))
    sink = RecordingSink()
    SpeechPipeline(engine, sink, stream_audio=False).speak(["Une phrase complète."])
    assert len(sink.played) == 1 and len(sink.played[0][0]) == 2400


def test_tts_test_command_plays_on_the_sink():
    install_fake_neutts()
    base = load_config(ROOT / "config.toml")
    tmp = tempfile.TemporaryDirectory()
    voice = make_voice(Path(tmp.name))
    cfg = replace(base, tts=replace(base.tts, engine="neutts", stream_audio=True, neutts={"voice": str(voice)}))
    sink = RecordingSink()
    assert tts_test(cfg, "Bonjour monsieur.", sink=sink) == 0
    assert len(sink.played) == (3 if cfg.tts.stream_audio else 1)
    assert tts_test(cfg, "   ", sink=sink) == 2
    tmp.cleanup()


def test_pipeline_merges_short_sentences_with_the_next_one():
    class Echo:
        sample_rate = 24000
        spoken: list = []

        def synthesize(self, text):
            Echo.spoken.append(text)
            return np.zeros(240, np.int16), 24000

    stats = SpeechPipeline(Echo(), RecordingSink(), merge_under=40).speak(
        ["Bonjour monsieur.", "Je suis JARVIS, votre assistant personnel.", "Ça va ?"])
    assert Echo.spoken == ["Bonjour monsieur. Je suis JARVIS, votre assistant personnel.", "Ça va ?"]
    assert len(stats.sentences) == 2


def test_pronunciations_respell_whole_words_before_synthesis():
    assert engine_module.respell("Je suis JARVIS, Jarvisien.", {"Jarvis": "Jarvisse"}) == "Je suis Jarvisse, Jarvisien."


def test_repeated_sentences_are_synthesized_once():
    install_fake_neutts()
    with tempfile.TemporaryDirectory() as tmp:
        engine = engine_module.NeuTTSEngine("b", "c", make_voice(Path(tmp)))
    calls = []
    infer = engine._tts.infer
    engine._tts.infer = lambda *args: calls.append(args[0]) or infer(*args)
    engine.synthesize("Oui, monsieur ?")
    engine.synthesize("Oui, monsieur ?")
    engine.synthesize("Nous sommes samedi.")
    assert calls == ["Oui, monsieur ?", "Nous sommes samedi."]


def test_default_engine_is_piper_with_jarvis_pronounced_with_s():
    from jarvis.factory import build_tts

    tts = build_tts(load_config(ROOT / "config.toml"))
    assert type(tts).__name__ == "PiperTTS"
    assert "ʒaʁvˈis" in tts.phonemes("Je suis Jarvis.")
