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
from jarvis.personality import load_personality  # noqa: E402
from jarvis.prompts import build_system_prompt  # noqa: E402
from jarvis.router import IntentRouter  # noqa: E402

SR, FRAME = 16000, 1280


def tone(seconds: float, amplitude: int = 3000) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return (amplitude * np.sin(2 * np.pi * 220 * t)).astype(np.int16)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SR), dtype=np.int16)


def test_config_loads_and_resolves_paths():
    cfg = load_config(ROOT / "config.toml", local=False)
    assert cfg.assistant.personality.exists()
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
    prompt = build_system_prompt(load_personality(ROOT / "personality.toml"), CapabilityRegistry())
    assert "JARVIS" in prompt and "PAS ENCORE disponible" in prompt and "domotique" in prompt


class FakeWake:
    """Se déclenche sur les blocs dont l'amplitude dépasse 20000."""

    def process(self, frame):
        return 1.0 if np.abs(frame).max() > 20000 else 0.0

    def reset(self):
        pass


class FakeSTT:
    def transcribe(self, audio, sample_rate):
        return "raconte-moi une histoire"


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
                  FakeSTT(), llm, FakeTTS(), IntentRouter(load_personality(ROOT / "personality.toml"), CapabilityRegistry()),
                  lambda k, t: events.append(k))
    agent.run()
    assert events.count("wake") == 1
    assert events.count("user") == 2
    assert len(sink.played) == 3                  # accusé + 2 réponses
    assert len(llm.calls[1]) == 4                 # système + historique de la conversation


def test_latency_summary_lists_every_stage():
    from jarvis.agent import format_latency

    latency = {"fin de parole": 0.9, "stt": 0.3, "llm_first_token": 0.4, "llm_first_sentence": 0.7,
               "tts_first": 0.2, "audio_first": 0.9, "llm_total": 2.1, "tts_total": 3.2, "total_response": 9.0,
               "début lecture": 102.1, "llm_détail": {"chargement": 0.0, "prompt": 0.1, "génération": 2.0, "jetons": 180},
               "tts_voix": "fr_FR-tom-medium", "phrases": 6, "durée audio": 8.0}
    summary = format_latency(latency, speech_ended_at=100.0)
    for line in ("STT: 0.30 s", "LLM first token: 0.40 s", "LLM first sentence: 0.70 s", "TTS first sentence: 0.20 s",
                 "Audio first chunk: 0.90 s", "LLM total: 2.10 s", "TTS total: 3.20 s", "Total response: 9.00 s",
                 "TOTAL: 2.10 s", "6 phrase(s)"):
        assert line in summary, line


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)


def test_choose_audio_saves_devices_in_config_without_touching_the_rest():
    import tempfile

    from jarvis.__main__ import choose_audio

    lines = ['[audio]', 'input_device = ""       # commentaire', 'output_device = "Ancien, MME"', '']
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "config.toml"
        config.write_text(chr(10).join(lines), encoding="utf-8")
        devices = {"input": [("Chat Mic (GoXLR), MME", "Chat Mic (GoXLR)")],
                   "output": [("System (GoXLR), MME", "System (GoXLR)"), ("Casque, MME", "Casque")]}
        answers = iter(["1", "2"])
        assert choose_audio(config, ask=lambda _: next(answers), devices=devices) == 0
        assert config.read_text(encoding="utf-8").split(chr(10)) == [
            '[audio]', 'input_device = "Chat Mic (GoXLR), MME"       # commentaire', 'output_device = "Casque, MME"', '']
        answers = iter(["0", ""])
        choose_audio(config, ask=lambda _: next(answers), devices=devices)
        saved = config.read_text(encoding="utf-8")
        assert 'input_device = ""' in saved and 'output_device = "Casque, MME"' in saved


def test_stt_auto_device_picks_gpu_only_when_available():
    import jarvis.stt.faster_whisper as fw

    original = fw.cuda_device_count
    try:
        fw.cuda_device_count = lambda: 0
        assert fw.detect_device() == ("cpu", "int8")
        fw.cuda_device_count = lambda: 1
        assert fw.detect_device() == ("cuda", "float16")
    finally:
        fw.cuda_device_count = original


def test_local_configuration_overrides_settings_per_machine(tmp_path):
    from jarvis.config import load_config

    (tmp_path / "config.toml").write_text('[llm]\nmodel = "mistral:latest"\ntemperature = 0.3\n[face]\nport = 8765\n',
                                          encoding="utf-8")
    assert load_config(tmp_path / "config.toml").llm.model == "mistral:latest"
    (tmp_path / "config.local.toml").write_text('[llm]\nmodel = "qwen2.5:3b"\n[audio]\nremote = "http://pc:8765"\n',
                                                encoding="utf-8")
    cfg = load_config(tmp_path / "config.toml")
    assert (cfg.llm.model, cfg.llm.temperature, cfg.face.port, cfg.audio.remote) == (
        "qwen2.5:3b", 0.3, 8765, "http://pc:8765")
    (tmp_path / "config.local.toml").write_text('[llm]\nmodl = "x"\n', encoding="utf-8")
    import pytest

    with pytest.raises(ValueError, match="modl"):
        load_config(tmp_path / "config.toml")


def test_toml_saved_with_a_bom_is_read(tmp_path):
    from jarvis.config import load_config

    (tmp_path / "config.toml").write_text('[llm]\nmodel = "qwen2.5:3b"\n', encoding="utf-8-sig")
    (tmp_path / "config.local.toml").write_text('[face]\nhost = "0.0.0.0"\n', encoding="utf-8-sig")
    cfg = load_config(tmp_path / "config.toml")
    assert (cfg.llm.model, cfg.face.host) == ("qwen2.5:3b", "0.0.0.0")
