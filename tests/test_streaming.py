"""Tests du pipeline en flux LLM -> phrases -> TTS -> audio (sans modèle, sans réseau).

Lancement : python -m pytest -q tests/test_streaming.py
"""

from __future__ import annotations

import io
import json
import random
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import jarvis.llm.ollama as ollama  # noqa: E402
from jarvis.agent import Agent, AgentSettings  # noqa: E402
from jarvis.audio.endpointing import EndpointerSettings, UtteranceRecorder  # noqa: E402
from jarvis.audio.files import ArraySource  # noqa: E402
from jarvis.capabilities import CapabilityRegistry  # noqa: E402
from jarvis.interfaces import Message  # noqa: E402
from jarvis.personality import load_personality  # noqa: E402
from jarvis.router import IntentRouter  # noqa: E402
from jarvis.streaming import SentenceBuffer, SpeechPipeline, sentences_from_llm, split_sentences  # noqa: E402

ROUTER = IntentRouter(load_personality(ROOT / "personality.toml"), CapabilityRegistry(), random.Random(0),
                      lambda: datetime(2026, 9, 25, 10, 0))


def tokens(text: str) -> list[str]:
    """Découpe comme un LLM : petits morceaux de mots."""
    return [text[i : i + 3] for i in range(0, len(text), 3)]


def test_sentence_buffer_never_emits_word_fragments():
    buffer = SentenceBuffer()
    out = []
    for piece in tokens("Bonjour, monsieur. Je vais vous expliquer comment fonctionne ce système. Ensuite nous"):
        out += buffer.feed(piece)
    assert out == ["Bonjour, monsieur.", "Je vais vous expliquer comment fonctionne ce système."]
    assert buffer.flush() == "Ensuite nous"


def test_sentence_buffer_handles_short_units_numbers_and_abbreviations():
    assert split_sentences("Oui. Il fait 2.5 degrés dehors. M. Dupont arrive ! Et vous ?") == \
        ["Oui. Il fait 2.5 degrés dehors.", "M. Dupont arrive !", "Et vous ?"]
    assert split_sentences("Voici trois conseils : 1. dormir tôt. 2. éviter les écrans.") == \
        ["Voici trois conseils : 1. dormir tôt.", "2. éviter les écrans."]
    assert split_sentences("Paris.") == ["Paris."]


def test_sentences_from_llm_filters_and_measures():
    marks = {}
    fragments = iter(tokens("Bien sûr, monsieur. La capitale de la France est Paris. N'hésitez pas à me demander."))
    out = list(sentences_from_llm(fragments, ROUTER.reply_filter("capitale ?"), marks))
    assert out == ["La capitale de la France est Paris."]
    assert 0 <= marks["llm_first_token"] <= marks["llm_first_sentence"] <= marks["llm_total"]


def test_false_action_stops_generation_and_closes_the_stream():
    closed = []

    def fragments():
        try:
            for piece in tokens("Très bien. La lumière du salon est maintenant éteinte. Autre chose ? Encore."):
                yield piece
        finally:
            closed.append(True)

    out = list(sentences_from_llm(fragments(), ROUTER.reply_filter("éteins la lumière"), {}))
    assert len(out) == 1 and "pas encore disponible" in out[0]
    assert closed == [True]


def test_truncated_tail_is_dropped_only_when_the_llm_hit_the_length_limit():
    text = "Évitez les écrans le soir. Couchez-vous à heure fixe. Essayez un régime"
    cut = list(sentences_from_llm(iter(tokens(text)), ROUTER.reply_filter(user_text="Explique-moi."), {}, done_reason=lambda: "length"))
    kept = list(sentences_from_llm(iter(tokens(text)), ROUTER.reply_filter(user_text="Explique-moi."), {}, done_reason=lambda: "stop"))
    assert cut[-1] == "Couchez-vous à heure fixe." and kept[-1] == "Essayez un régime"


class SlowTTS:
    def __init__(self, delay=0.01):
        self.delay = delay
        self.calls = []

    def synthesize(self, text):
        self.calls.append(text)
        time.sleep(self.delay)
        return np.full(160, len(self.calls), np.int16), 16000


class TimedSink:
    """Simule la lecture (durée réelle) et enregistre les intervalles pour détecter toute superposition."""

    def __init__(self, seconds=0.02):
        self.seconds = seconds
        self.played = []
        self.intervals = []
        self._busy = threading.Lock()

    def play(self, audio, rate):
        assert self._busy.acquire(blocking=False), "deux lectures simultanées"
        start = time.perf_counter()
        time.sleep(self.seconds)
        self.intervals.append((start, time.perf_counter()))
        self.played.append(int(audio[0]))
        self._busy.release()


def slow_sentences(n, delay):
    for i in range(1, n + 1):
        time.sleep(delay)
        yield f"Phrase numéro {i} de la réponse."


def test_pipeline_keeps_order_without_loss_duplication_or_overlap():
    tts, sink = SlowTTS(), TimedSink()
    stats = SpeechPipeline(tts, sink).speak(slow_sentences(8, 0.005))
    expected = [f"Phrase numéro {i} de la réponse." for i in range(1, 9)]
    assert stats.sentences == expected
    assert tts.calls == expected
    assert sink.played == list(range(1, 9))
    assert all(a[1] <= b[0] for a, b in zip(sink.intervals, sink.intervals[1:]))


def test_speech_starts_before_generation_ends():
    tts, sink = SlowTTS(0.01), TimedSink(0.01)
    stats = SpeechPipeline(tts, sink).speak(slow_sentences(6, 0.05))
    assert stats.first_audio < stats.generation_done
    assert stats.since_start(stats.first_audio) < 0.2


def test_short_reply_is_immediate():
    stats = SpeechPipeline(SlowTTS(0.0), TimedSink(0.0)).speak(["Paris."])
    assert stats.sentences == ["Paris."]
    assert stats.since_start(stats.first_audio) < 0.05


def test_cancel_stops_generation_and_playback():
    tts, sink = SlowTTS(0.0), TimedSink(0.02)
    pipeline = SpeechPipeline(tts, sink)
    produced = []

    def sentences():
        for i in range(1, 50):
            produced.append(i)
            if i == 3:
                pipeline.cancel()
            yield f"Phrase numéro {i} de la réponse."
            time.sleep(0.005)

    stats = pipeline.speak(sentences())
    assert len(produced) <= 4 and len(stats.sentences) <= 3


def test_tts_error_is_raised_and_nothing_hangs():
    class BrokenTTS:
        def synthesize(self, text):
            raise RuntimeError("voix indisponible")

    try:
        SpeechPipeline(BrokenTTS(), TimedSink()).speak(slow_sentences(5, 0.0))
    except RuntimeError as exc:
        assert "voix indisponible" in str(exc)
    else:
        raise AssertionError("l'erreur du TTS doit remonter")


def test_ollama_stream_yields_fragments_and_records_stats():
    lines = [json.dumps({"message": {"content": c}, "done": False}).encode() + b"\n" for c in ("Bon", "jour.", " Paris")]
    lines.append(json.dumps({"message": {"content": ""}, "done": True, "done_reason": "stop", "eval_count": 3,
                             "load_duration": 0, "prompt_eval_duration": 1e8, "eval_duration": 2e8}).encode() + b"\n")

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    original = ollama.urllib.request.urlopen
    seen = {}

    def fake_urlopen(request, timeout):
        seen["body"] = json.loads(request.data)
        return Response(b"".join(lines))

    ollama.urllib.request.urlopen = fake_urlopen
    try:
        llm = ollama.OllamaLLM("http://x", "modele")
        assert list(llm.stream([Message("user", "salut")])) == ["Bon", "jour.", " Paris"]
    finally:
        ollama.urllib.request.urlopen = original
    assert seen["body"]["stream"] is True
    assert llm.last_done_reason == "stop" and llm.last_stats["jetons"] == 3


def test_agent_streams_llm_reply_sentence_by_sentence():
    answer = ("Bonjour, monsieur. Le soleil est une étoile de type naine jaune. Il se trouve à environ "
              "150 millions de kilomètres de la Terre. Sa lumière met huit minutes à nous parvenir.")

    class StreamingLLM:
        last_done_reason = "stop"

        def stream(self, messages):
            for piece in tokens(answer):
                time.sleep(0.002)
                yield piece

    class Stt:
        def transcribe(self, audio, rate):
            return "Parle-moi du soleil en détail"

    class Wake:
        def process(self, frame):
            return 1.0 if np.abs(frame).max() > 20000 else 0.0

        def reset(self):
            pass

    sr = 16000
    audio = np.concatenate([np.zeros(sr), np.full(3200, 30000, np.int16), np.zeros(sr // 2),
                            (3000 * np.sin(np.arange(sr) / sr * 1400)).astype(np.int16), np.zeros(4 * sr)])
    source, sink, tts, events = ArraySource(audio, sr, 1280), TimedSink(0.0), SlowTTS(0.0), []
    settings = AgentSettings("JARVIS", "Jarvis", 0.5, ("Oui, monsieur ?",), 3.0, 2.0, 6)
    agent = Agent(settings, source, sink, Wake(), UtteranceRecorder(source, EndpointerSettings()),
                  Stt(), StreamingLLM(), tts, ROUTER, lambda kind, text: events.append((kind, text)))
    agent.run()
    spoken = tts.calls[1:]
    assert spoken == ["Le soleil est une étoile de type naine jaune.",
                      "Il se trouve à environ 150 millions de kilomètres de la Terre.",
                      "Sa lumière met huit minutes à nous parvenir."]
    timings = [text for kind, text in events if kind == "timing"]
    for label in ("LLM first token", "LLM first sentence", "TTS first sentence", "Audio first chunk", "LLM total",
                  "TTS total"):
        assert any(t.startswith(label) for t in timings), label
